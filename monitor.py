#!/usr/bin/env python3
import os
import io
import json
import time
import tempfile
import datetime
import threading
import requests
import oci

TENANCY_OCID      = os.environ["OCI_TENANCY_OCID"]
USER_OCID         = os.environ["OCI_USER_OCID"]
FINGERPRINT       = os.environ["OCI_FINGERPRINT"]
REGION            = os.environ.get("OCI_REGION", "uk-london-1")
API_KEY_PEM       = os.environ["OCI_API_KEY"]
COMPARTMENT_OCID  = os.environ["OCI_COMPARTMENT_OCID"]
BOT_TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID           = os.environ["TELEGRAM_CHAT_ID"]
INTERVAL_HOURS    = float(os.environ.get("CHECK_INTERVAL_HOURS", "6"))
OCI_STATE_BUCKET  = os.environ.get("OCI_STATE_BUCKET", "")
OCI_ACCOUNT_LABEL = os.environ.get("OCI_ACCOUNT_LABEL", "")

STATE_FILE         = "/data/state.json"
BUCKET_STATE_KEY   = "oci-monitor/state.json"
BUCKET_REPORTS_KEY = "oci-monitor/reports/{ts}.json"

_lock = threading.Lock()
_state: dict = {}

_tenancy_slug         = ""    # used in console URLs
_account_label        = ""    # display name in messages (compartment name or OCI_ACCOUNT_LABEL)
_availability_domains: list[str] = []
_os_namespace         = ""    # OCI Object Storage tenancy namespace

DEFAULTS = {
    "cost_threshold":      float(os.environ.get("COST_THRESHOLD_GBP", "5.0")),
    "max_lb_count":        int(os.environ.get("MAX_LB_COUNT", "1")),
    "max_free_public_ips": int(os.environ.get("MAX_FREE_PUBLIC_IPS", "2")),
    "silenced_month":      None,
    "auto_cleanup":        True,
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
/help — show this message\
"""


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
        client.put_object(_os_namespace, OCI_STATE_BUCKET, BUCKET_STATE_KEY, io.BytesIO(body))
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


# ── OCI helpers ───────────────────────────────────────────────────────────────

def build_oci_config(key_file_path: str) -> dict:
    return {
        "user":        USER_OCID,
        "key_file":    key_file_path,
        "fingerprint": FINGERPRINT,
        "tenancy":     TENANCY_OCID,
        "region":      REGION,
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


def monthly_spend(config: dict) -> tuple[float, str]:
    client = oci.usage_api.UsageapiClient(config)
    today = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
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


def lb_count(config: dict) -> int:
    client = oci.load_balancer.LoadBalancerClient(config)
    lbs = oci.pagination.list_call_get_all_results(
        client.list_load_balancers, compartment_id=COMPARTMENT_OCID,
    ).data
    return sum(1 for lb in lbs if lb.lifecycle_state not in ("DELETED", "TERMINATING"))


def orphaned_public_ips(config: dict) -> list[dict]:
    client = oci.core.VirtualNetworkClient(config)
    ips = oci.pagination.list_call_get_all_results(
        client.list_public_ips, scope="REGION", compartment_id=COMPARTMENT_OCID,
    ).data
    return [
        {"id": ip.id, "ip": ip.ip_address, "name": ip.display_name}
        for ip in ips if ip.lifecycle_state == "AVAILABLE"
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
        attached_ids = {a.boot_volume_id for a in attachments if a.lifecycle_state == "ATTACHED"}
        result += [
            {"id": v.id, "name": v.display_name, "size_gb": v.size_in_gbs}
            for v in available if v.id not in attached_ids
        ]
    return result


def orphaned_block_volumes(config: dict) -> list[dict]:
    client = oci.core.BlockstorageClient(config)
    vols = oci.pagination.list_call_get_all_results(
        client.list_volumes, compartment_id=COMPARTMENT_OCID,
    ).data
    return [
        {"id": v.id, "name": v.display_name, "size_gb": v.size_in_gbs}
        for v in vols if v.lifecycle_state == "AVAILABLE"
    ]


def compute_instances(config: dict) -> list[dict]:
    client = oci.core.ComputeClient(config)
    instances = oci.pagination.list_call_get_all_results(
        client.list_instances, compartment_id=COMPARTMENT_OCID,
    ).data
    return [
        {"name": i.display_name, "shape": i.shape, "state": i.lifecycle_state}
        for i in instances if i.lifecycle_state not in ("TERMINATED", "TERMINATING")
    ]


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
    requests.post(
        url,
        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True},
        timeout=10,
    )


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
        over = spend > threshold
        breached = breached or over
        lines.append(f"{'💸' if over else '💷'} Spend: {currency} {spend:.2f} / {threshold:.2f}{'  ⚠️' if over else ''}")
    except Exception as e:
        lines.append(f"⚠️ Spend: error — {e}")

    try:
        count = lb_count(config)
        over = count > max_lb
        breached = breached or over
        lines.append(f"{'🚨' if over else '⚖️'} Load balancers: {count} / {max_lb}{'  ⚠️' if over else ''}")
    except Exception as e:
        lines.append(f"⚠️ LBs: error — {e}")

    try:
        ips = orphaned_public_ips(config)
        over = len(ips) > max_free_ips
        breached = breached or over
        lines.append(f"{'🚨' if over else '🌐'} Unassigned IPs: {len(ips)}{'  ⚠️' if over else ''}")
    except Exception as e:
        lines.append(f"⚠️ Public IPs: error — {e}")

    try:
        orphan_gb = sum(v["size_gb"] for v in orphaned_boot_volumes(config) + orphaned_block_volumes(config))
        over = orphan_gb > 0
        breached = breached or over
        lines.append(f"{'🚨' if over else '💾'} Orphaned volumes: {orphan_gb} GB{'  ⚠️' if over else ''}")
    except Exception as e:
        lines.append(f"⚠️ Volumes: error — {e}")

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
            lines.append(f"  • {i['name']} `{i['shape']}` {i['state']}")
    except Exception as e:
        lines.append(f"⚠️ Compute: {e}")

    try:
        count = lb_count(config)
        lines.append(f"\n*Load balancers*: {count}")
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

    return "\n".join(lines)


def _cleanup_summary(report: dict) -> str:
    parts = []
    if report["deleted_ips"]:
        parts.append(f"{len(report['deleted_ips'])} IP(s) deleted")
    if report["deleted_boot_volumes"] or report["deleted_block_volumes"]:
        gb = sum(v["size_gb"] for v in report["deleted_boot_volumes"] + report["deleted_block_volumes"])
        parts.append(f"{gb} GB volume(s) deleted")
    if report["errors"]:
        parts.append(f"{len(report['errors'])} error(s)")
    return ", ".join(parts) if parts else "nothing to clean"


# ── scheduled check ───────────────────────────────────────────────────────────

def check(key_file_path: str) -> None:
    if is_silenced():
        return

    config = build_oci_config(key_file_path)
    threshold = sget("cost_threshold")
    max_lb = sget("max_lb_count")
    max_free_ips = sget("max_free_public_ips")
    auto = sget("auto_cleanup")
    alerts = []

    try:
        spend, currency = monthly_spend(config)
        if spend > threshold:
            alerts.append(f"💸 Spend: {currency} {spend:.2f} / {threshold:.2f} threshold")
    except Exception as e:
        alerts.append(f"⚠️ Cost check failed: {e}")

    try:
        count = lb_count(config)
        if count > max_lb:
            alerts.append(f"⚖️ Load balancers: {count} active (max {max_lb})")
    except Exception as e:
        alerts.append(f"⚠️ LB check failed: {e}")

    ips, boot_vols, block_vols = [], [], []
    try:
        ips = orphaned_public_ips(config)
        if len(ips) > max_free_ips:
            alerts.append(f"🌐 Unassigned reserved IPs: {len(ips)}")
    except Exception as e:
        alerts.append(f"⚠️ Public IP check failed: {e}")

    try:
        boot_vols = orphaned_boot_volumes(config)
        block_vols = orphaned_block_volumes(config)
        orphan_gb = sum(v["size_gb"] for v in boot_vols + block_vols)
        if orphan_gb > 0:
            alerts.append(f"💾 Orphaned volumes: {orphan_gb} GB unattached")
    except Exception as e:
        alerts.append(f"⚠️ Volume check failed: {e}")

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
        send_telegram(CHAT_ID, body)
    elif cleanup_note:
        name = _account_label or "OCI"
        send_telegram(CHAT_ID, f"🤖 *{name} cleanup*{cleanup_note}")


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
            reply = "🔧 Auto-cleanup disabled. Use /scan to inspect and clean up manually."
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
        reply = f"🔕 Scheduled alerts silenced for {month}. Use /unsilence to re-enable."

    elif cmd == "/unsilence":
        sset("silenced_month", None, config)
        reply = "🔔 Scheduled alerts re-enabled."

    elif cmd == "/help":
        reply = HELP_TEXT

    else:
        return

    send_telegram(chat_id, reply)


# ── Telegram polling ──────────────────────────────────────────────────────────

def poll_commands(key_file_path: str) -> None:
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
                    handle_command(text, chat_id, key_file_path)
        except Exception:
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

        threading.Thread(target=poll_commands, args=(key_file_path,), daemon=True).start()

        while True:
            try:
                check(key_file_path)
            except Exception as e:
                send_telegram(CHAT_ID, f"⚠️ *OCI monitor error*: {e}")
            time.sleep(INTERVAL_HOURS * 3600)
    finally:
        os.unlink(key_file_path)


if __name__ == "__main__":
    main()
