#!/usr/bin/env python3
import os
import io
import re
import json
import time
import tempfile
import datetime
import threading
import requests
import pytz
import oci

TENANCY_OCID = os.environ["OCI_TENANCY_OCID"]
USER_OCID = os.environ["OCI_USER_OCID"]
FINGERPRINT = os.environ["OCI_FINGERPRINT"]
REGION = os.environ.get("OCI_REGION", "uk-london-1")
API_KEY_PEM = os.environ["OCI_API_KEY"]
COMPARTMENT_OCID = os.environ["OCI_COMPARTMENT_OCID"]
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
INTERVAL_HOURS = float(os.environ.get("CHECK_INTERVAL_HOURS", "6"))
OCI_STATE_BUCKET = os.environ.get("OCI_STATE_BUCKET", "")
OCI_ACCOUNT_LABEL = os.environ.get("OCI_ACCOUNT_LABEL", "")
VAT_RATE = float(os.environ.get("VAT_RATE", "0.20"))

STATE_FILE = "/data/state.json"
BUCKET_STATE_KEY = "oci-monitor/state.json"
BUCKET_REPORTS_KEY = "oci-monitor/reports/{ts}.json"

_lock = threading.Lock()
_state: dict = {}

_tenancy_slug = ""  # used in console URLs
_account_label = ""  # display name in messages (compartment name or OCI_ACCOUNT_LABEL)
_availability_domains: list[str] = []
_os_namespace = ""  # OCI Object Storage tenancy namespace

DEFAULTS = {
    "cost_threshold": float(os.environ.get("COST_THRESHOLD_GBP", "5.0")),
    "max_lb_count": int(os.environ.get("MAX_LB_COUNT", "1")),
    "max_free_public_ips": int(os.environ.get("MAX_FREE_PUBLIC_IPS", "2")),
    "max_object_storage_gb": float(os.environ.get("MAX_OBJECT_STORAGE_GB", "18.0")),
    "max_ampere_instances": int(os.environ.get("MAX_AMPERE_INSTANCES", "2")),
    "max_ampere_ocpus": float(os.environ.get("MAX_AMPERE_OCPUS", "2")),
    "max_ampere_memory_gb": float(os.environ.get("MAX_AMPERE_MEMORY_GB", "12")),
    "max_micro_instances": int(os.environ.get("MAX_MICRO_INSTANCES", "2")),
    "alert_on_change": os.environ.get("ALERT_ON_CHANGE", "true").lower()
    not in ("0", "false", "no"),
    "last_change_alert_signature": None,
    "last_threshold_alert_signature": None,
    "last_spend": None,
    "last_spend_currency": "GBP",
    "last_eom_invoice_month": None,
    "silenced_month": None,
    "last_instance_snapshot": None,
    "auto_cleanup": True,
}

HELP_TEXT = """\
*OCI Monitor commands*
/status — spend, LBs, orphaned resources
/scan — full resource audit
/autocleanup — show auto-cleanup status
/autocleanup on|off — enable or disable auto-cleanup
/threshold <GBP> — set monthly cost alert threshold
/lbmax <n> — set max allowed load balancers
/silence — mute scheduled alerts for this calendar month
/unsilence — re-enable scheduled alerts
/cleanup images — delete all unused custom images
/help [command] — show this message, or detail for a command\
"""

HELP_DETAIL: dict[str, str] = {
    "autocleanup": """\
*Auto-cleanup*
Automatically deletes orphaned resources that accrue cost with no benefit:
  • *Unassigned reserved public IPs* — OCI charges for reserved IPs not attached to an instance
  • *Unattached boot volumes* — left behind after an instance is terminated
  • *Unattached block volumes* — data volumes not mounted to any instance

Runs on every scheduled check (default every 6 hours). Deletions are logged; if `OCI_STATE_BUCKET` is set a JSON report is saved to the bucket.

`/autocleanup` — current status
`/autocleanup on` — enable
`/autocleanup off` — disable (use /scan to review manually)\
""",
    "status": """\
*Status*
Snapshot of current resource usage and spend:
  • Monthly spend (exc. and inc. VAT) vs your threshold
  • Compute — checks all accessible compartments against Always Free limits
  • Load balancers — count, empty LBs, bandwidth tier
  • Unassigned reserved public IPs
  • Orphaned volumes and boot volumes
  • Volume backups
  • Custom images (flags unused ones)
  • Object Storage usage vs limit

Use `/threshold` and `/lbmax` to adjust the alert thresholds.\
""",
    "scan": """\
*Scan*
Full resource audit — same checks as /status but lists every resource individually with details (shapes, sizes, backend counts, etc.).\
""",
    "threshold": """\
*Threshold*
Sets the monthly spend limit in GBP. An alert fires when your VAT-inclusive spend exceeds this amount.

Usage: `/threshold 10.0`\
""",
    "lbmax": """\
*LB max*
Sets the maximum number of active load balancers before alerting. OCI Always Free includes one 10 Mbps load balancer.

Usage: `/lbmax 1`\
""",
    "cleanup": """\
*Cleanup*
`/cleanup images` — permanently deletes all custom images not currently in use by any instance. This is irreversible. Use `/scan` first to review what will be removed.\
""",
    "silence": """\
*Silence / Unsilence*
`/silence` — suppresses all scheduled alert checks for the current calendar month. On-demand commands (/status, /scan) still work.
`/unsilence` — re-enables scheduled alerts immediately.\
""",
}


# ── state (local + OCI bucket) ───────────────────────────────────────────────


def _state_from_bucket(config: dict) -> dict | None:
    if not OCI_STATE_BUCKET or not _os_namespace:
        return None
    try:
        client = oci.object_storage.ObjectStorageClient(config)
        resp = client.get_object(_os_namespace, OCI_STATE_BUCKET, BUCKET_STATE_KEY)
        return json.loads(resp.data.content.decode())
    except Exception:
        return None


def _save_state_to_bucket(config: dict, state: dict) -> None:
    if not OCI_STATE_BUCKET or not _os_namespace:
        return
    try:
        client = oci.object_storage.ObjectStorageClient(config)
        body = json.dumps(state).encode()
        client.put_object(
            _os_namespace, OCI_STATE_BUCKET, BUCKET_STATE_KEY, io.BytesIO(body)
        )
    except Exception:
        pass


def _save_report_to_bucket(config: dict, report: dict) -> None:
    if not OCI_STATE_BUCKET or not _os_namespace:
        return
    try:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        key = BUCKET_REPORTS_KEY.format(ts=ts)
        client = oci.object_storage.ObjectStorageClient(config)
        body = json.dumps(report, indent=2).encode()
        client.put_object(_os_namespace, OCI_STATE_BUCKET, key, io.BytesIO(body))
    except Exception:
        pass


def load_state(config: dict | None = None) -> None:
    global _state
    loaded = None
    if config:
        loaded = _state_from_bucket(config)
    if loaded is None:
        try:
            with open(STATE_FILE) as f:
                loaded = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            loaded = {}
    _state = loaded
    for k, v in DEFAULTS.items():
        _state.setdefault(k, v)


def save_state(config: dict | None = None) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(_state, f)
    if config:
        _save_state_to_bucket(config, _state)


def sget(key):
    with _lock:
        return _state[key]


def sset(key, value, config: dict | None = None) -> None:
    with _lock:
        _state[key] = value
    save_state(config)


def is_silenced() -> bool:
    month = sget("silenced_month")
    return month == datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def _in_quiet_hours() -> bool:
    tz = pytz.timezone("Europe/London")
    return datetime.datetime.now(tz).hour < 9


# ── OCI helpers ───────────────────────────────────────────────────────────────


def _short_err(e: Exception) -> str:
    if hasattr(e, "status") and hasattr(e, "code"):  # OCI ServiceError
        return f"{e.status} {e.code}"
    return str(e)[:120]


def build_oci_config(key_file_path: str) -> dict:
    return {
        "user": USER_OCID,
        "key_file": key_file_path,
        "fingerprint": FINGERPRINT,
        "tenancy": TENANCY_OCID,
        "region": REGION,
    }


def fetch_tenancy_info(config: dict) -> tuple[str, str]:
    client = oci.identity.IdentityClient(config)
    t = client.get_tenancy(TENANCY_OCID).data
    compartment = client.get_compartment(COMPARTMENT_OCID).data
    label = OCI_ACCOUNT_LABEL or compartment.name or t.description or t.name
    return t.name, label


def fetch_availability_domains(config: dict) -> list[str]:
    client = oci.identity.IdentityClient(config)
    ads = client.list_availability_domains(compartment_id=TENANCY_OCID).data
    return [ad.name for ad in ads]


def fetch_os_namespace(config: dict) -> str:
    client = oci.object_storage.ObjectStorageClient(config)
    return client.get_namespace().data


def _configured_compartment() -> dict:
    return {
        "id": COMPARTMENT_OCID,
        "name": _account_label or "configured compartment",
    }


def accessible_compartments(config: dict) -> list[dict]:
    if not hasattr(oci, "identity"):
        return [_configured_compartment()]

    client = oci.identity.IdentityClient(config)
    try:
        compartments = oci.pagination.list_call_get_all_results(
            client.list_compartments,
            compartment_id=TENANCY_OCID,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
        ).data
    except Exception:
        return [_configured_compartment()]

    result = []
    seen = set()
    for compartment in compartments:
        if getattr(compartment, "lifecycle_state", "ACTIVE") != "ACTIVE":
            continue
        compartment_id = getattr(compartment, "id", None)
        if not compartment_id or compartment_id in seen:
            continue
        seen.add(compartment_id)
        result.append(
            {
                "id": compartment_id,
                "name": getattr(compartment, "name", None)
                or getattr(compartment, "display_name", None)
                or compartment_id,
            }
        )

    return result or [_configured_compartment()]


def monthly_spend_breakdown(config: dict) -> tuple[list[tuple[str, float]], str]:
    client = oci.usage_api.UsageapiClient(config)
    today = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = today.replace(day=1)
    end = today + datetime.timedelta(days=1)
    details = oci.usage_api.models.RequestSummarizedUsagesDetails(
        tenant_id=TENANCY_OCID,
        time_usage_started=start,
        time_usage_ended=end,
        granularity="MONTHLY",
        query_type="COST",
        group_by=["service"],
    )
    resp = client.request_summarized_usages(details)
    currency = resp.data.items[0].currency if resp.data.items else "GBP"
    rows = []
    for item in resp.data.items:
        amount = float(item.computed_amount or 0)
        if amount > 0:
            rows.append((item.service or "Other", amount * (1 + VAT_RATE)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows, currency


def monthly_spend(config: dict) -> tuple[float, str]:
    client = oci.usage_api.UsageapiClient(config)
    today = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = today.replace(day=1)
    end = today + datetime.timedelta(days=1)
    details = oci.usage_api.models.RequestSummarizedUsagesDetails(
        tenant_id=TENANCY_OCID,
        time_usage_started=start,
        time_usage_ended=end,
        granularity="MONTHLY",
        query_type="COST",
    )
    resp = client.request_summarized_usages(details)
    total = sum(float(i.computed_amount or 0) for i in resp.data.items)
    currency = resp.data.items[0].currency if resp.data.items else "GBP"
    return total, currency


def load_balancers(config: dict) -> list[dict]:
    client = oci.load_balancer.LoadBalancerClient(config)
    lbs = oci.pagination.list_call_get_all_results(
        client.list_load_balancers,
        compartment_id=COMPARTMENT_OCID,
    ).data
    result = []
    for lb in lbs:
        if lb.lifecycle_state in ("DELETED", "TERMINATING"):
            continue
        sd = lb.shape_details or {}
        min_mbps = (
            getattr(sd, "minimum_bandwidth_in_mbps", None)
            if hasattr(sd, "minimum_bandwidth_in_mbps")
            else (sd.get("minimum_bandwidth_in_mbps") if isinstance(sd, dict) else None)
        )
        max_mbps = (
            getattr(sd, "maximum_bandwidth_in_mbps", None)
            if hasattr(sd, "maximum_bandwidth_in_mbps")
            else (sd.get("maximum_bandwidth_in_mbps") if isinstance(sd, dict) else None)
        )
        backends = sum(
            len(bs.backends or []) for bs in (lb.backend_sets or {}).values()
        )
        listeners = len(lb.listeners or {})
        result.append(
            {
                "id": lb.id,
                "name": lb.display_name,
                "shape": lb.shape_name,
                "min_mbps": min_mbps,
                "max_mbps": max_mbps,
                "backends": backends,
                "listeners": listeners,
                "state": lb.lifecycle_state,
            }
        )
    return result


# ── Site-to-Site VPN detections (plan 2026-07-13, Task 8) ─────────────────────
# The OCI→home VPN is Always Free; these checks guard against drift into paid
# resources (NAT GW, NLB) or over-broad routing, not against the VPN itself.

# Prefixes the VPN path is allowed to route to a DRG. Anything broader — or
# 0.0.0.0/0 — is an alert. Omni target /32 + the OCI VPN subnet/secondary CIDR.
VPN_APPROVED_DRG_PREFIXES = ("100.72.134.50/32", "10.44.1.0/24", "10.44.0.0/16")


def drgs(config: dict) -> list[dict]:
    client = oci.core.VirtualNetworkClient(config)
    items = oci.pagination.list_call_get_all_results(
        client.list_drgs, compartment_id=COMPARTMENT_OCID
    ).data
    return [
        {"id": d.id, "name": d.display_name, "state": d.lifecycle_state}
        for d in items
        if d.lifecycle_state not in ("TERMINATED", "TERMINATING")
    ]


def ipsec_connections(config: dict) -> list[dict]:
    # Presence-only. Tunnel health needs extra IAM (inspect ipsec-connections /
    # read virtual-network-family); degrade to presence rather than require it.
    client = oci.core.VirtualNetworkClient(config)
    items = oci.pagination.list_call_get_all_results(
        client.list_ip_sec_connections, compartment_id=COMPARTMENT_OCID
    ).data
    return [
        {"id": c.id, "name": c.display_name, "state": c.lifecycle_state}
        for c in items
        if c.lifecycle_state not in ("TERMINATED", "TERMINATING")
    ]


def nat_gateways(config: dict) -> list[dict]:
    client = oci.core.VirtualNetworkClient(config)
    items = oci.pagination.list_call_get_all_results(
        client.list_nat_gateways, compartment_id=COMPARTMENT_OCID
    ).data
    return [
        {"id": n.id, "name": n.display_name, "state": n.lifecycle_state}
        for n in items
        if n.lifecycle_state not in ("TERMINATED", "TERMINATING")
    ]


def network_load_balancers(config: dict) -> list[dict]:
    client = oci.network_load_balancer.NetworkLoadBalancerClient(config)
    items = oci.pagination.list_call_get_all_results(
        client.list_network_load_balancers, compartment_id=COMPARTMENT_OCID
    ).data
    return [
        {"id": n.id, "name": n.display_name, "state": n.lifecycle_state}
        for n in items
        if n.lifecycle_state not in ("DELETED", "DELETING")
    ]


def drg_route_alerts(config: dict, drg_ids: set[str]) -> list[str]:
    client = oci.core.VirtualNetworkClient(config)
    rts = oci.pagination.list_call_get_all_results(
        client.list_route_tables, compartment_id=COMPARTMENT_OCID
    ).data
    rules = [
        {
            "destination": r.destination,
            "network_entity_id": r.network_entity_id,
            "route_table": rt.display_name,
        }
        for rt in rts
        for r in (rt.route_rules or [])
    ]
    return classify_drg_routes(rules, drg_ids, VPN_APPROVED_DRG_PREFIXES)


def classify_drg_routes(
    rules: list[dict], drg_ids: set[str], approved_prefixes: tuple[str, ...]
) -> list[str]:
    """Pure: flag route rules sending an over-broad / unapproved prefix to a DRG.

    A rule targets a DRG when its network_entity_id is a known DRG OCID or looks
    like one (``.drg.`` segment) — so it still fires if DRG listing was denied.
    """
    alerts = []
    for r in rules:
        target = r.get("network_entity_id") or ""
        dest = r.get("destination") or ""
        if target not in drg_ids and ".drg." not in target:
            continue
        rt = r.get("route_table", "?")
        if dest == "0.0.0.0/0":
            alerts.append(f"🕳️ Route {rt}: 0.0.0.0/0 → DRG (over-broad VPN route)")
        elif dest not in approved_prefixes:
            alerts.append(
                f"🕳️ Route {rt}: {dest} → DRG (not in approved Omni/VPN prefixes)"
            )
    return alerts


def orphaned_public_ips(config: dict) -> list[dict]:
    client = oci.core.VirtualNetworkClient(config)
    ips = oci.pagination.list_call_get_all_results(
        client.list_public_ips,
        scope="REGION",
        compartment_id=COMPARTMENT_OCID,
    ).data
    return [
        {"id": ip.id, "ip": ip.ip_address, "name": ip.display_name}
        for ip in ips
        if ip.lifecycle_state == "AVAILABLE"
    ]


def orphaned_boot_volumes(config: dict) -> list[dict]:
    if not _availability_domains:
        return []
    bv_client = oci.core.BlockstorageClient(config)
    compute_client = oci.core.ComputeClient(config)
    result = []
    for ad in _availability_domains:
        vols = oci.pagination.list_call_get_all_results(
            bv_client.list_boot_volumes,
            availability_domain=ad,
            compartment_id=COMPARTMENT_OCID,
        ).data
        available = [v for v in vols if v.lifecycle_state == "AVAILABLE"]
        if not available:
            continue
        attachments = oci.pagination.list_call_get_all_results(
            compute_client.list_boot_volume_attachments,
            availability_domain=ad,
            compartment_id=COMPARTMENT_OCID,
        ).data
        attached_ids = {
            a.boot_volume_id for a in attachments if a.lifecycle_state == "ATTACHED"
        }
        result += [
            {"id": v.id, "name": v.display_name, "size_gb": v.size_in_gbs}
            for v in available
            if v.id not in attached_ids
        ]
    return result


def orphaned_block_volumes(config: dict) -> list[dict]:
    client = oci.core.BlockstorageClient(config)
    vols = oci.pagination.list_call_get_all_results(
        client.list_volumes,
        compartment_id=COMPARTMENT_OCID,
    ).data
    return [
        {"id": v.id, "name": v.display_name, "size_gb": v.size_in_gbs}
        for v in vols
        if v.lifecycle_state == "AVAILABLE"
    ]


def volume_backups(config: dict) -> list[dict]:
    client = oci.core.BlockstorageClient(config)
    bv = oci.pagination.list_call_get_all_results(
        client.list_boot_volume_backups,
        compartment_id=COMPARTMENT_OCID,
        lifecycle_state="AVAILABLE",
    ).data
    bk = oci.pagination.list_call_get_all_results(
        client.list_volume_backups,
        compartment_id=COMPARTMENT_OCID,
        lifecycle_state="AVAILABLE",
    ).data
    return [
        {
            "id": b.id,
            "name": b.display_name,
            "size_gb": b.unique_size_in_gbs or 0,
            "type": getattr(b, "source_type", None) or "MANUAL",
            "kind": "boot",
            "created": str(b.time_created)[:10],
        }
        for b in bv
    ] + [
        {
            "id": b.id,
            "name": b.display_name,
            "size_gb": b.unique_size_in_gbs or 0,
            "type": getattr(b, "source_type", None) or "MANUAL",
            "kind": "block",
            "created": str(b.time_created)[:10],
        }
        for b in bk
    ]


def custom_images(config: dict) -> list[dict]:
    compute_client = oci.core.ComputeClient(config)
    images = oci.pagination.list_call_get_all_results(
        compute_client.list_images,
        compartment_id=COMPARTMENT_OCID,
    ).data
    active_instances = oci.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id=COMPARTMENT_OCID,
    ).data
    active_image_ids = {
        i.image_id
        for i in active_instances
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING")
    }
    return [
        {
            "id": img.id,
            "name": img.display_name,
            "size_gb": round((img.size_in_mbs or 0) / 1024, 1),
            "in_use": img.id in active_image_ids,
        }
        for img in images
        if img.lifecycle_state == "AVAILABLE" and img.compartment_id is not None
    ]


def object_storage_usage_gb(config: dict) -> float:
    if not _os_namespace:
        return 0.0
    os_client = oci.object_storage.ObjectStorageClient(config)
    buckets = oci.pagination.list_call_get_all_results(
        os_client.list_buckets,
        namespace_name=_os_namespace,
        compartment_id=COMPARTMENT_OCID,
    ).data
    total_bytes = 0
    for bucket in buckets:
        try:
            start = None
            while True:
                kwargs: dict = {"fields": "size"}
                if start:
                    kwargs["start"] = start
                resp = os_client.list_objects(_os_namespace, bucket.name, **kwargs)
                total_bytes += sum(obj.size or 0 for obj in (resp.data.objects or []))
                if resp.data.next_start_with:
                    start = resp.data.next_start_with
                else:
                    break
        except Exception:
            pass
    return total_bytes / 1024**3


def compute_instances(config: dict) -> list[dict]:
    client = oci.core.ComputeClient(config)
    result = []
    for compartment in accessible_compartments(config):
        try:
            instances = oci.pagination.list_call_get_all_results(
                client.list_instances,
                compartment_id=compartment["id"],
            ).data
        except Exception:
            continue
        for instance in instances:
            if instance.lifecycle_state in ("TERMINATED", "TERMINATING"):
                continue
            shape_config = getattr(instance, "shape_config", None)
            result.append(
                {
                    "id": instance.id,
                    "name": instance.display_name,
                    "shape": instance.shape,
                    "ocpus": float(getattr(shape_config, "ocpus", 0) or 0),
                    "memory_gb": float(getattr(shape_config, "memory_in_gbs", 0) or 0),
                    "state": instance.lifecycle_state,
                    "compartment_id": compartment["id"],
                    "compartment_name": compartment["name"],
                }
            )
    return result


def compute_free_tier_breaches(instances: list[dict]) -> list[str]:
    ampere = [i for i in instances if i["shape"] == "VM.Standard.A1.Flex"]
    micro = [i for i in instances if i["shape"] == "VM.Standard.E2.1.Micro"]
    ampere_ocpus = sum(i["ocpus"] for i in ampere)
    ampere_memory_gb = sum(i["memory_gb"] for i in ampere)
    breaches = []
    if len(ampere) > sget("max_ampere_instances"):
        breaches.append(f"A1 instances: {len(ampere)} / {sget('max_ampere_instances')}")
    if ampere_ocpus > sget("max_ampere_ocpus"):
        breaches.append(f"A1 OCPUs: {ampere_ocpus:g} / {sget('max_ampere_ocpus'):g}")
    if ampere_memory_gb > sget("max_ampere_memory_gb"):
        breaches.append(
            f"A1 memory: {ampere_memory_gb:g} / {sget('max_ampere_memory_gb'):g} GB"
        )
    if len(micro) > sget("max_micro_instances"):
        breaches.append(
            f"E2 Micro instances: {len(micro)} / {sget('max_micro_instances')}"
        )
    return breaches


# ── cleanup ───────────────────────────────────────────────────────────────────


def _cleanup_ips(config: dict, ips: list[dict]) -> tuple[list, list]:
    client = oci.core.VirtualNetworkClient(config)
    deleted, errors = [], []
    for ip in ips:
        try:
            client.delete_public_ip(ip["id"])
            deleted.append(ip)
        except Exception as e:
            errors.append({"item": ip, "error": str(e)})
    return deleted, errors


def _cleanup_boot_volumes(config: dict, vols: list[dict]) -> tuple[list, list]:
    client = oci.core.BlockstorageClient(config)
    deleted, errors = [], []
    for v in vols:
        try:
            client.delete_boot_volume(v["id"])
            deleted.append(v)
        except Exception as e:
            errors.append({"item": v, "error": str(e)})
    return deleted, errors


def _cleanup_block_volumes(config: dict, vols: list[dict]) -> tuple[list, list]:
    client = oci.core.BlockstorageClient(config)
    deleted, errors = [], []
    for v in vols:
        try:
            client.delete_volume(v["id"])
            deleted.append(v)
        except Exception as e:
            errors.append({"item": v, "error": str(e)})
    return deleted, errors


def _cleanup_images(config: dict, images: list[dict]) -> tuple[list, list]:
    client = oci.core.ComputeClient(config)
    deleted, errors = [], []
    for img in images:
        try:
            client.delete_image(img["id"])
            deleted.append(img)
        except Exception as e:
            errors.append({"item": img, "error": str(e)})
    return deleted, errors


def run_cleanup(config: dict, ips: list, boot_vols: list, block_vols: list) -> dict:
    report = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "deleted_ips": [],
        "deleted_boot_volumes": [],
        "deleted_block_volumes": [],
        "errors": [],
    }
    if ips:
        d, e = _cleanup_ips(config, ips)
        report["deleted_ips"] = d
        report["errors"] += e
    if boot_vols:
        d, e = _cleanup_boot_volumes(config, boot_vols)
        report["deleted_boot_volumes"] = d
        report["errors"] += e
    if block_vols:
        d, e = _cleanup_block_volumes(config, block_vols)
        report["deleted_block_volumes"] = d
        report["errors"] += e
    return report


# ── Telegram ──────────────────────────────────────────────────────────────────


def send_telegram(chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )
    if not resp.json().get("ok"):
        if resp.status_code == 400 and "parse" in resp.text:
            print(f"[tg] markdown error at: {message[300:380]!r}", flush=True)
            resp = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        if not resp.json().get("ok"):
            print(f"[tg] send failed chat={chat_id}: {resp.text[:200]}", flush=True)


_TG_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def send_discord(message: str) -> None:
    text = _TG_LINK.sub(r"\1 <\2>", message)[:2000]
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=10)
    if not resp.ok:
        print(f"[discord] send failed: {resp.text[:200]}", flush=True)


def send_slack(message: str) -> None:
    text = _TG_LINK.sub(r"<\2|\1>", message)
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    if not resp.ok:
        print(f"[slack] send failed: {resp.text[:200]}", flush=True)


def notify(message: str) -> None:
    if BOT_TOKEN and CHAT_ID:
        send_telegram(CHAT_ID, message)
    if DISCORD_WEBHOOK_URL:
        send_discord(message)
    if SLACK_WEBHOOK_URL:
        send_slack(message)


def billing_url() -> str:
    return f"https://cloud.oracle.com/invoices-and-orders/invoices?region={REGION}&tenant={_tenancy_slug}"


def build_status_message(key_file_path: str) -> str:
    config = build_oci_config(key_file_path)
    threshold = sget("cost_threshold")
    max_lb = sget("max_lb_count")
    max_free_ips = sget("max_free_public_ips")
    auto = sget("auto_cleanup")

    header = f"📊 *{_account_label}*" if _account_label else "📊 *OCI*"
    if _tenancy_slug:
        header += f"\n`{_tenancy_slug}` · [Billing]({billing_url()})"
    lines = [header]
    breached = False

    try:
        spend, currency = monthly_spend(config)
        spend_inc = spend * (1 + VAT_RATE)
        over = spend_inc > threshold
        breached = breached or over
        lines.append(
            f"{'💸' if over else '💷'} Spend: {currency} {spend_inc:.2f} / {threshold:.2f}{'  ⚠️' if over else ''}"
        )
    except Exception as e:
        lines.append(f"⚠️ Spend: error — {_short_err(e)}")

    try:
        compute_breaches = compute_free_tier_breaches(compute_instances(config))
        breached = breached or bool(compute_breaches)
        lines.append(
            f"{'🚨' if compute_breaches else '🖥️'} Compute: "
            + (", ".join(compute_breaches) if compute_breaches else "within free tier")
        )
    except Exception as e:
        lines.append(f"⚠️ Compute: error — {_short_err(e)}")

    try:
        lbs = load_balancers(config)
        count = len(lbs)
        over = count > max_lb
        empty = [lb for lb in lbs if lb["backends"] == 0 and lb["listeners"] == 0]
        paid_bw = [lb for lb in lbs if lb["max_mbps"] and lb["max_mbps"] > 10]
        warn = over or bool(empty) or bool(paid_bw)
        breached = breached or warn
        label = f"{count} / {max_lb}"
        if empty:
            label += f"  ({len(empty)} empty — billing with no traffic)"
        if paid_bw:
            label += f"  ({len(paid_bw)} above 10 Mbps free tier)"
        lines.append(
            f"{'🚨' if warn else '⚖️'} Load balancers: {label}{'  ⚠️' if warn else ''}"
        )
    except Exception as e:
        lines.append(f"⚠️ LBs: error — {_short_err(e)}")

    try:
        ips = orphaned_public_ips(config)
        over = len(ips) > max_free_ips
        breached = breached or over
        lines.append(
            f"{'🚨' if over else '🌐'} Unassigned IPs: {len(ips)}{'  ⚠️' if over else ''}"
        )
    except Exception as e:
        lines.append(f"⚠️ Public IPs: error — {_short_err(e)}")

    try:
        orphan_gb = sum(
            v["size_gb"]
            for v in orphaned_boot_volumes(config) + orphaned_block_volumes(config)
        )
        over = orphan_gb > 0
        breached = breached or over
        lines.append(
            f"{'🚨' if over else '💾'} Orphaned volumes: {orphan_gb} GB{'  ⚠️' if over else ''}"
        )
    except Exception as e:
        lines.append(f"⚠️ Volumes: error — {_short_err(e)}")

    try:
        backups = volume_backups(config)
        backup_gb = sum(b["size_gb"] for b in backups)
        over = len(backups) > 0
        breached = breached or over
        lines.append(
            f"{'🚨' if over else '🗂️'} Backups: {len(backups)} ({backup_gb:.0f} GB){'  ⚠️' if over else ''}"
        )
    except Exception as e:
        lines.append(f"⚠️ Backups: error — {_short_err(e)}")

    try:
        imgs = custom_images(config)
        unused = [i for i in imgs if not i["in_use"]]
        over = bool(unused)
        breached = breached or over
        label = (
            f"{len(imgs)} ({len(unused)} unused  ⚠️)"
            if unused
            else (str(len(imgs)) if imgs else "none")
        )
        lines.append(f"{'🚨' if over else '🖼️'} Custom images: {label}")
    except Exception as e:
        lines.append(f"⚠️ Images: error — {_short_err(e)}")

    try:
        max_os = sget("max_object_storage_gb")
        usage_gb = object_storage_usage_gb(config)
        over = usage_gb > max_os
        breached = breached or over
        lines.append(
            f"{'🚨' if over else '🗄️'} Object Storage: {usage_gb:.1f} / {max_os:.0f} GB{'  ⚠️' if over else ''}"
        )
    except Exception as e:
        lines.append(f"⚠️ Object Storage: error — {_short_err(e)}")

    if is_silenced():
        lines.append("🔕 Alerts silenced this month")
    lines.append(f"{'🤖' if auto else '🔧'} Auto-cleanup: {'on' if auto else 'off'}")
    lines.append("🚨 Issues found" if breached else "✅ All clear")
    return "\n".join(lines)


def build_scan_message(key_file_path: str) -> str:
    config = build_oci_config(key_file_path)
    lines = [f"🔍 *Resource scan — {_account_label or 'OCI'}*"]

    try:
        instances = compute_instances(config)
        lines.append(f"\n*Compute ({len(instances)})*")
        for i in instances:
            compartment = i.get("compartment_name")
            suffix = f" — {compartment}" if compartment else ""
            lines.append(f"  • {i['name']} `{i['shape']}` {i['state']}{suffix}")
    except Exception as e:
        lines.append(f"⚠️ Compute: {e}")

    try:
        lbs = load_balancers(config)
        if lbs:
            lines.append(f"\n*Load balancers ({len(lbs)})*")
            for lb in lbs:
                bw = (
                    f"{lb['min_mbps']}/{lb['max_mbps']} Mbps"
                    if lb["min_mbps"]
                    else lb["shape"]
                )
                tags = []
                if lb["backends"] == 0 and lb["listeners"] == 0:
                    tags.append("⚠️ empty")
                if lb["max_mbps"] and lb["max_mbps"] > 10:
                    tags.append(f"⚠️ above free tier ({lb['max_mbps']} Mbps)")
                tag_str = "  " + "  ".join(tags) if tags else ""
                lines.append(
                    f"  • {lb['name']} `{bw}` {lb['backends']}be/{lb['listeners']}li{tag_str}"
                )
        else:
            lines.append("\n*Load balancers*: none ✅")
    except Exception as e:
        lines.append(f"⚠️ LBs: {e}")

    try:
        ips = orphaned_public_ips(config)
        if ips:
            lines.append(f"\n*Unassigned reserved IPs ({len(ips)}) ⚠️*")
            for ip in ips:
                lines.append(f"  • {ip['ip']} `{ip['name']}`")
        else:
            lines.append("\n*Unassigned IPs*: none ✅")
    except Exception as e:
        lines.append(f"⚠️ Public IPs: {e}")

    try:
        boot_vols = orphaned_boot_volumes(config)
        block_vols = orphaned_block_volumes(config)
        all_orphans = boot_vols + block_vols
        if all_orphans:
            total_gb = sum(v["size_gb"] for v in all_orphans)
            lines.append(f"\n*Orphaned volumes ({total_gb} GB) ⚠️*")
            for v in all_orphans:
                lines.append(f"  • {v['name']} {v['size_gb']} GB")
        else:
            lines.append("\n*Orphaned volumes*: none ✅")
    except Exception as e:
        lines.append(f"⚠️ Volumes: {e}")

    try:
        backups = volume_backups(config)
        if backups:
            total_gb = sum(b["size_gb"] for b in backups)
            lines.append(f"\n*Volume backups ({len(backups)}, {total_gb:.0f} GB) ⚠️*")
            for b in backups:
                lines.append(
                    f"  • {b['name']} {b['size_gb']} GB [{b['kind']}/{b['type']}] {b['created']}"
                )
        else:
            lines.append("\n*Volume backups*: none ✅")
    except Exception as e:
        lines.append(f"⚠️ Backups: {e}")

    try:
        imgs = custom_images(config)
        if imgs:
            unused = [i for i in imgs if not i["in_use"]]
            label = (
                f"{len(imgs)} total, {len(unused)} unused"
                if unused
                else f"{len(imgs)} total, all in use"
            )
            warn = "  ⚠️" if unused else ""
            lines.append(f"\n*Custom images ({label}){warn}*")
            for img in imgs:
                tag = "  ⚠️ unused" if not img["in_use"] else ""
                lines.append(f"  • {img['name']} {img['size_gb']} GB{tag}")
        else:
            lines.append("\n*Custom images*: none")
    except Exception as e:
        lines.append(f"⚠️ Images: {e}")

    try:
        usage_gb = object_storage_usage_gb(config)
        max_os = sget("max_object_storage_gb")
        over = usage_gb > max_os
        lines.append(
            f"\n*Object Storage*: {usage_gb:.1f} GB / {max_os:.0f} GB limit{'  ⚠️' if over else ' ✅'}"
        )
    except Exception as e:
        lines.append(f"⚠️ Object Storage: {e}")

    try:
        drg_list = drgs(config)
        ipsec_list = ipsec_connections(config)
        nat_list = nat_gateways(config)
        nlb_list = network_load_balancers(config)
        if drg_list or ipsec_list or nat_list or nlb_list:
            lines.append("\n*Site-to-Site VPN*")
            lines.append(f"  • DRGs: {len(drg_list)}   IPSec: {len(ipsec_list)}")
            if nat_list:
                lines.append(f"  • ⚠️ NAT Gateway: {len(nat_list)} (not approved)")
            if nlb_list:
                lines.append(f"  • ⚠️ Network LB: {len(nlb_list)} (not approved)")
        else:
            lines.append("\n*Site-to-Site VPN*: none")
    except Exception as e:
        lines.append(f"⚠️ VPN: {e}")

    return "\n".join(lines)


def _cleanup_summary(report: dict) -> str:
    parts = []
    if report["deleted_ips"]:
        parts.append(f"{len(report['deleted_ips'])} IP(s) deleted")
    if report["deleted_boot_volumes"] or report["deleted_block_volumes"]:
        gb = sum(
            v["size_gb"]
            for v in report["deleted_boot_volumes"] + report["deleted_block_volumes"]
        )
        parts.append(f"{gb} GB volume(s) deleted")
    if report["errors"]:
        parts.append(f"{len(report['errors'])} error(s)")
    return ", ".join(parts) if parts else "nothing to clean"


# ── scheduled check ───────────────────────────────────────────────────────────


def _alert_signature(alerts: list[str]) -> str:
    return json.dumps(alerts, sort_keys=True, separators=(",", ":"))


def _instance_diff(old: dict, new: dict) -> list[str]:
    msgs = []
    for iid in set(new) - set(old):
        i = new[iid]
        msgs.append(
            f"🟢 Instance added: {i['name']} `{i['shape']}` {i['state']} — {i['compartment']}"
        )
    for iid in set(old) - set(new):
        i = old[iid]
        msgs.append(
            f"🔴 Instance removed: {i['name']} `{i['shape']}` — {i['compartment']}"
        )
    for iid in set(old) & set(new):
        o, n = old[iid], new[iid]
        if o["state"] != n["state"]:
            msgs.append(
                f"🔄 {n['name']}: {o['state']} → {n['state']} — {n['compartment']}"
            )
    return msgs


def _is_near_end_of_month(days: int = 2) -> bool:
    today = datetime.date.today()
    next_month = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    last_day = next_month - datetime.timedelta(days=1)
    return (last_day - today).days < days


def check(key_file_path: str) -> None:
    if is_silenced() or _in_quiet_hours():
        return

    config = build_oci_config(key_file_path)
    threshold = sget("cost_threshold")
    max_lb = sget("max_lb_count")
    max_free_ips = sget("max_free_public_ips")
    auto = sget("auto_cleanup")
    alert_on_change = sget("alert_on_change")
    alerts = []
    threshold_alerts = []
    change_alerts = []
    spend: float | None = None
    spend_inc: float = 0.0
    currency = "GBP"

    try:
        spend, currency = monthly_spend(config)
        spend_inc = spend * (1 + VAT_RATE)
        if spend_inc > threshold:
            threshold_alerts.append(
                f"💸 Spend: {currency} {spend_inc:.2f} / {threshold:.2f} threshold"
            )
        if round(spend_inc, 4) != sget("last_spend") or currency != sget(
            "last_spend_currency"
        ):
            sset("last_spend", round(spend_inc, 4), config)
            sset("last_spend_currency", currency, config)
    except Exception as e:
        threshold_alerts.append(f"⚠️ Cost check failed: {e}")

    try:
        instances = compute_instances(config)
        threshold_alerts.extend(compute_free_tier_breaches(instances))
        snapshot = {
            i["id"]: {
                "name": i["name"],
                "shape": i["shape"],
                "state": i["state"],
                "compartment": i.get("compartment_name", ""),
            }
            for i in instances
        }
        last_snapshot = sget("last_instance_snapshot")
        if last_snapshot is None:
            sset("last_instance_snapshot", snapshot, config)
        else:
            diff = _instance_diff(last_snapshot, snapshot)
            if diff:
                change_alerts.extend(diff)
                sset("last_instance_snapshot", snapshot, config)
    except Exception as e:
        threshold_alerts.append(f"⚠️ Compute check failed: {e}")

    try:
        lbs = load_balancers(config)
        count = len(lbs)
        empty = [lb for lb in lbs if lb["backends"] == 0 and lb["listeners"] == 0]
        paid_bw = [lb for lb in lbs if lb["max_mbps"] and lb["max_mbps"] > 10]
        if count > max_lb:
            threshold_alerts.append(f"⚖️ Load balancers: {count} active (max {max_lb})")
        if empty:
            change_alerts.append(
                f"⚖️ Empty LBs billing with no traffic: {', '.join(lb['name'] for lb in empty)}"
            )
        if paid_bw:
            threshold_alerts.append(
                "⚖️ LBs above 10 Mbps free tier: "
                + ", ".join(f"{lb['name']} ({lb['max_mbps']} Mbps)" for lb in paid_bw)
            )
    except Exception as e:
        threshold_alerts.append(f"⚠️ LB check failed: {e}")

    ips, boot_vols, block_vols = [], [], []
    try:
        ips = orphaned_public_ips(config)
        if len(ips) > max_free_ips:
            threshold_alerts.append(f"🌐 Unassigned reserved IPs: {len(ips)}")
    except Exception as e:
        threshold_alerts.append(f"⚠️ Public IP check failed: {e}")

    try:
        boot_vols = orphaned_boot_volumes(config)
        block_vols = orphaned_block_volumes(config)
        orphan_gb = sum(v["size_gb"] for v in boot_vols + block_vols)
        if orphan_gb > 0:
            change_alerts.append(f"💾 Orphaned volumes: {orphan_gb} GB unattached")
    except Exception as e:
        threshold_alerts.append(f"⚠️ Volume check failed: {e}")

    try:
        backups = volume_backups(config)
        if backups:
            backup_gb = sum(b["size_gb"] for b in backups)
            change_alerts.append(
                f"🗂️ Volume backups: {len(backups)} ({backup_gb:.0f} GB)"
            )
    except Exception as e:
        threshold_alerts.append(f"⚠️ Backup check failed: {e}")

    try:
        imgs = custom_images(config)
        unused_imgs = [i for i in imgs if not i["in_use"]]
        if unused_imgs:
            unused_gb = sum(i["size_gb"] for i in unused_imgs)
            change_alerts.append(
                f"🖼️ Unused custom images: {len(unused_imgs)} ({unused_gb:.0f} GB)"
            )
    except Exception as e:
        threshold_alerts.append(f"⚠️ Image check failed: {e}")

    try:
        max_os = sget("max_object_storage_gb")
        usage_gb = object_storage_usage_gb(config)
        if usage_gb > max_os:
            threshold_alerts.append(
                f"🗄️ Object Storage: {usage_gb:.1f} GB (threshold {max_os:.0f} GB)"
            )
    except Exception as e:
        threshold_alerts.append(f"⚠️ Object Storage check failed: {e}")

    try:
        drg_list = drgs(config)
        ipsec_list = ipsec_connections(config)
        if nat_gateways(config):
            threshold_alerts.append(
                "🚧 NAT Gateway present (not approved for VPN path)"
            )
        if network_load_balancers(config):
            threshold_alerts.append("⚖️ Network Load Balancer present (not approved)")
        threshold_alerts.extend(drg_route_alerts(config, {d["id"] for d in drg_list}))
        if drg_list or ipsec_list:
            change_alerts.append(
                f"🔐 VPN present: {len(drg_list)} DRG, {len(ipsec_list)} IPSec "
                "(Always Free; outbound transfer advisory, not a hard cap)"
            )
    except Exception as e:
        threshold_alerts.append(f"⚠️ VPN check failed: {e}")

    # Gate change_alerts on state change (existing behaviour)
    change_signature = _alert_signature(change_alerts)
    previous_change_signature = sget("last_change_alert_signature")
    change_alerts_changed = previous_change_signature != change_signature
    if change_alerts_changed:
        sset("last_change_alert_signature", change_signature, config)

    # Gate threshold_alerts on state change OR last 2 days of month (EOM recap)
    # Spend is rounded to the nearest £1 in the signature so penny-level fluctuations
    # don't re-fire the same alert on every check cycle.
    _sig_threshold = [
        a.replace(f"{currency} {spend_inc:.2f}", f"{currency} {int(spend_inc)}")
        if spend is not None and "Spend:" in a
        else a
        for a in threshold_alerts
    ]
    threshold_signature = _alert_signature(_sig_threshold)
    previous_threshold_signature = sget("last_threshold_alert_signature")
    threshold_alerts_changed = previous_threshold_signature != threshold_signature
    near_eom = _is_near_end_of_month()
    if threshold_alerts_changed:
        sset("last_threshold_alert_signature", threshold_signature, config)

    alerts = (threshold_alerts if (threshold_alerts_changed or near_eom) else []) + (
        change_alerts if (not alert_on_change or change_alerts_changed) else []
    )

    cleanup_note = ""
    if auto and (ips or boot_vols or block_vols):
        try:
            report = run_cleanup(config, ips, boot_vols, block_vols)
            _save_report_to_bucket(config, report)
            cleanup_note = f"\n🤖 Auto-cleanup: {_cleanup_summary(report)}"
        except Exception as e:
            cleanup_note = f"\n⚠️ Auto-cleanup failed: {e}"

    if alerts:
        name = _account_label or "OCI"
        body = f"🚨 *{name} alert*\n" + "\n".join(alerts) + cleanup_note
        body += f"\n[View billing]({billing_url()})"
        notify(body)
    elif cleanup_note:
        name = _account_label or "OCI"
        notify(f"🤖 *{name} cleanup*{cleanup_note}")

    # EOM invoice preview — once per month, last 2 days only
    if near_eom and spend is not None and spend_inc > 0:
        current_month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        if sget("last_eom_invoice_month") != current_month:
            try:
                breakdown, _ = monthly_spend_breakdown(config)
                name = _account_label or "OCI"
                lines = [f"🧾 *{name} · Month-end invoice preview*"]
                lines.append(f"💷 Total inc. VAT: {currency} {spend_inc:.2f}")
                if breakdown:
                    lines.append("\n*Breakdown by service:*")
                    for service, amount in breakdown:
                        lines.append(f"  • {service} — {currency} {amount:.2f}")
                lines.append(f"\n[View billing]({billing_url()})")
                notify("\n".join(lines))
                sset("last_eom_invoice_month", current_month, config)
            except Exception as e:
                notify(f"⚠️ EOM invoice preview failed: {e}")


# ── command handler ───────────────────────────────────────────────────────────


def handle_command(text: str, chat_id: str, key_file_path: str) -> None:
    config = build_oci_config(key_file_path)
    parts = text.split()
    cmd = parts[0].split("@")[0]

    if cmd in ("/status", "/test"):
        try:
            reply = build_status_message(key_file_path)
        except Exception as e:
            reply = f"⚠️ Error: {e}"

    elif cmd == "/scan":
        try:
            reply = build_scan_message(key_file_path)
        except Exception as e:
            reply = f"⚠️ Error: {e}"

    elif cmd == "/autocleanup":
        if len(parts) < 2:
            auto = sget("auto_cleanup")
            reply = f"🤖 Auto-cleanup is currently *{'on' if auto else 'off'}*."
        elif parts[1] == "on":
            sset("auto_cleanup", True, config)
            reply = "✅ Auto-cleanup enabled. Orphaned IPs and volumes will be deleted automatically."
        elif parts[1] == "off":
            sset("auto_cleanup", False, config)
            reply = (
                "🔧 Auto-cleanup disabled. Use /scan to inspect and clean up manually."
            )
        else:
            reply = "Usage: `/autocleanup on` or `/autocleanup off`"

    elif cmd == "/threshold":
        if len(parts) < 2:
            reply = "Usage: `/threshold <GBP>` — e.g. `/threshold 10.0`"
        else:
            try:
                val = float(parts[1])
                sset("cost_threshold", val, config)
                reply = f"✅ Cost threshold set to GBP {val:.2f}"
            except ValueError:
                reply = "⚠️ Invalid value — use a number, e.g. `/threshold 10.0`"

    elif cmd == "/lbmax":
        if len(parts) < 2:
            reply = "Usage: `/lbmax <n>` — e.g. `/lbmax 2`"
        else:
            try:
                val = int(parts[1])
                sset("max_lb_count", val, config)
                reply = f"✅ Max load balancers set to {val}"
            except ValueError:
                reply = "⚠️ Invalid value — use an integer, e.g. `/lbmax 2`"

    elif cmd == "/silence":
        month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        sset("silenced_month", month, config)
        reply = (
            f"🔕 Scheduled alerts silenced for {month}. Use /unsilence to re-enable."
        )

    elif cmd == "/unsilence":
        sset("silenced_month", None, config)
        reply = "🔔 Scheduled alerts re-enabled."

    elif cmd == "/cleanup":
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "images":
            try:
                imgs = custom_images(config)
                unused = [i for i in imgs if not i["in_use"]]
                if not unused:
                    reply = "✅ No unused custom images found."
                else:
                    deleted, errors = _cleanup_images(config, unused)
                    lines = [f"🗑️ Deleted {len(deleted)} custom image(s):"]
                    for img in deleted:
                        lines.append(f"  • {img['name']} ({img['size_gb']} GB)")
                    if errors:
                        lines.append(f"\n⚠️ {len(errors)} failed:")
                        for err in errors:
                            lines.append(
                                f"  • {err['item']['name']}: {err['error'][:80]}"
                            )
                    reply = "\n".join(lines)
            except Exception as e:
                reply = f"⚠️ Error: {_short_err(e)}"
        else:
            reply = "Usage: `/cleanup images`"

    elif cmd == "/help":
        topic = parts[1].lstrip("/") if len(parts) > 1 else ""
        reply = HELP_DETAIL.get(topic, HELP_TEXT)

    else:
        return

    send_telegram(chat_id, reply)


# ── Telegram polling ──────────────────────────────────────────────────────────


def poll_commands(key_file_path: str) -> None:
    print("[bot] polling started", flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params=params,
                timeout=40,
            )
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/"):
                    print(f"[bot] cmd={text!r} chat={chat_id}", flush=True)
                    try:
                        handle_command(text, chat_id, key_file_path)
                        print(f"[bot] handled {text!r}", flush=True)
                    except Exception as e:
                        print(f"[bot] error handling {text!r}: {e}", flush=True)
        except Exception as e:
            print(f"[bot] poll error: {e}", flush=True)
            time.sleep(5)


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    global _tenancy_slug, _account_label, _availability_domains, _os_namespace

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(API_KEY_PEM)
        key_file_path = f.name

    try:
        config = build_oci_config(key_file_path)

        try:
            _tenancy_slug, _account_label = fetch_tenancy_info(config)
        except Exception:
            pass
        try:
            _availability_domains = fetch_availability_domains(config)
        except Exception:
            pass
        try:
            _os_namespace = fetch_os_namespace(config)
        except Exception:
            pass

        load_state(config)

        if BOT_TOKEN and CHAT_ID:
            threading.Thread(
                target=poll_commands, args=(key_file_path,), daemon=True
            ).start()

        while True:
            try:
                check(key_file_path)
            except Exception as e:
                notify(f"⚠️ *OCI monitor error*: {e}")
            time.sleep(INTERVAL_HOURS * 3600)
    finally:
        os.unlink(key_file_path)


if __name__ == "__main__":
    main()
