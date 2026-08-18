import importlib
import json
import os
import sys
import types
import unittest
from unittest import mock


class FakeGaugeMetricFamily:
    def __init__(self, name, documentation, value=None, labels=None):
        self.name = name
        self.documentation = documentation
        self.labels = labels or []
        self.samples = []
        if value is not None:
            self.samples.append(({}, value))

    def add_metric(self, labelvalues, value):
        self.samples.append((dict(zip(self.labels, labelvalues)), value))


def load_monitor(tmp_path=None):
    env = {
        "OCI_TENANCY_OCID": "tenancy",
        "OCI_USER_OCID": "user",
        "OCI_FINGERPRINT": "fingerprint",
        "OCI_API_KEY": "key",  # pragma: allowlist secret
        "OCI_COMPARTMENT_OCID": "compartment",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "chat",
        "VAT_RATE": "0",
    }
    os.environ.update(env)
    sys.modules["requests"] = types.SimpleNamespace()
    sys.modules["pytz"] = types.SimpleNamespace()
    sys.modules["oci"] = types.SimpleNamespace()

    core = types.SimpleNamespace(GaugeMetricFamily=FakeGaugeMetricFamily)
    prom = types.SimpleNamespace(
        start_http_server=mock.Mock(),
        REGISTRY=types.SimpleNamespace(register=mock.Mock()),
        core=core,
        GaugeMetricFamily=FakeGaugeMetricFamily,
    )
    sys.modules["prometheus_client"] = prom
    sys.modules["prometheus_client.core"] = core

    sys.modules.pop("monitor", None)
    m = importlib.import_module("monitor")
    if tmp_path is not None:
        m.METRICS_FILE = str(tmp_path / "metrics.json")
    return m


class MetricsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.monitor = load_monitor()
        self.tmpdir = self.id().replace(".", "_")
        self.metrics_path = f"/tmp/{self.tmpdir}-metrics.json"
        self.addCleanup(
            lambda: os.path.exists(self.metrics_path) and os.remove(self.metrics_path)
        )
        self.monitor.METRICS_FILE = self.metrics_path
        self.monitor._last_metrics = {}

    def test_update_metrics_merges_and_persists(self):
        self.monitor.update_metrics({"lb_count": 1})
        self.monitor.update_metrics({"orphaned_public_ips": 2})

        self.assertEqual(
            self.monitor._last_metrics, {"lb_count": 1, "orphaned_public_ips": 2}
        )
        with open(self.metrics_path) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, {"lb_count": 1, "orphaned_public_ips": 2})

    def test_load_metrics_missing_file_yields_empty_dict(self):
        self.monitor._last_metrics = {"stale": "data"}

        self.monitor.load_metrics()

        self.assertEqual(self.monitor._last_metrics, {})

    def test_load_metrics_reads_persisted_snapshot(self):
        self.monitor.update_metrics({"lb_count": 3})
        self.monitor._last_metrics = {}

        self.monitor.load_metrics()

        self.assertEqual(self.monitor._last_metrics, {"lb_count": 3})


class CheckMetricsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.monitor = load_monitor()
        self.monitor._state = self.monitor.DEFAULTS.copy()
        self.monitor._account_label = "test-account"
        self.monitor._tenancy_slug = "tenant"
        self.metrics_path = f"/tmp/{self.id().replace('.', '_')}-metrics.json"
        self.addCleanup(
            lambda: os.path.exists(self.metrics_path) and os.remove(self.metrics_path)
        )
        self.monitor.METRICS_FILE = self.metrics_path
        self.monitor._last_metrics = {}

    def patch_common_checks(self):
        return mock.patch.multiple(
            self.monitor,
            monthly_spend=mock.Mock(return_value=(1.0, "GBP")),
            compute_instances=mock.Mock(return_value=[]),
            load_balancers=mock.Mock(return_value=[]),
            orphaned_public_ips=mock.Mock(return_value=[]),
            orphaned_boot_volumes=mock.Mock(return_value=[]),
            orphaned_block_volumes=mock.Mock(return_value=[]),
            volume_backups=mock.Mock(return_value=[]),
            custom_images=mock.Mock(return_value=[]),
            object_storage_usage_gb=mock.Mock(return_value=0.0),
            drgs=mock.Mock(return_value=[]),
            ipsec_connections=mock.Mock(return_value=[]),
            nat_gateways=mock.Mock(return_value=[]),
            network_load_balancers=mock.Mock(return_value=[]),
            drg_route_alerts=mock.Mock(return_value=[]),
            vpn_tunnel_alerts=mock.Mock(return_value=[]),
            run_cleanup=mock.Mock(),
            _save_report_to_bucket=mock.Mock(),
            save_state=mock.Mock(),
            send_telegram=mock.Mock(),
            _in_quiet_hours=mock.Mock(return_value=False),
        )

    def test_successful_check_populates_metrics_snapshot(self):
        with self.patch_common_checks():
            self.monitor.compute_instances.return_value = [
                {
                    "id": "i1",
                    "name": "vm1",
                    "shape": "VM.Standard.E2.1.Micro",
                    "state": "RUNNING",
                }
            ]
            self.monitor.load_balancers.return_value = [
                {"name": "lb1", "backends": 0, "listeners": 0, "max_mbps": 10}
            ]
            self.monitor.orphaned_public_ips.return_value = [{"ip": "1.2.3.4"}]

            self.monitor.check("/tmp/key.pem")

        m = self.monitor._last_metrics
        self.assertEqual(m["spend_gbp_inc_vat"], 1.0)
        self.assertEqual(
            m["cost_threshold_gbp"], self.monitor.DEFAULTS["cost_threshold"]
        )
        self.assertEqual(
            m["instances"],
            [{"name": "vm1", "shape": "VM.Standard.E2.1.Micro", "state": "RUNNING"}],
        )
        self.assertEqual(m["lb_count"], 1)
        self.assertEqual(m["lb_empty"], 1)
        self.assertEqual(m["orphaned_public_ips"], 1)
        self.assertEqual(m["network_failure"], 0)
        self.assertIn("last_scan_ts", m)
        # Locks the snapshot/collector key contract together so a rename on
        # one side without the other fails a test instead of silently
        # dropping a metric.
        self.assertEqual(
            set(m),
            {
                "network_failure",
                "cost_threshold_gbp",
                "spend_gbp_inc_vat",
                "instances",
                "lb_count",
                "lb_empty",
                "orphaned_public_ips",
                "orphaned_volumes",
                "orphaned_volume_bytes",
                "volume_backups",
                "volume_backup_bytes",
                "unused_images",
                "unused_image_bytes",
                "object_storage_bytes",
                "drg_count",
                "ipsec_count",
                "last_scan_ts",
            },
        )

    def test_connectivity_failure_only_flips_network_failure(self):
        self.monitor._last_metrics = {
            "lb_count": 5,
            "network_failure": 0,
            "last_scan_ts": 111.0,
        }
        with (
            mock.patch.object(
                self.monitor,
                "check_oci_connectivity",
                side_effect=RuntimeError("after 2 attempts: No route to host"),
            ),
            mock.patch.object(self.monitor, "notify"),
            mock.patch.object(self.monitor, "_in_quiet_hours", return_value=False),
            mock.patch.object(self.monitor, "save_state"),
        ):
            self.monitor.check("/tmp/key.pem")

        m = self.monitor._last_metrics
        self.assertEqual(m["network_failure"], 1)
        self.assertEqual(m["lb_count"], 5)
        self.assertEqual(m["last_scan_ts"], 111.0)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = load_monitor()

    def test_collect_yields_scalar_gauges_from_snapshot(self):
        self.monitor._last_metrics = {
            "spend_gbp_inc_vat": 4.2,
            "lb_count": 2,
            "orphaned_public_ips": 1,
            "network_failure": 0,
            "last_scan_ts": self.monitor.time.time(),
        }
        collector = self.monitor.OCIMetricsCollector()

        families = {f.name: f for f in collector.collect()}

        self.assertEqual(families["oci_monitor_spend_gbp_inc_vat"].samples[0][1], 4.2)
        self.assertEqual(families["oci_monitor_load_balancer_count"].samples[0][1], 2)
        self.assertEqual(families["oci_monitor_orphaned_public_ips"].samples[0][1], 1)
        self.assertEqual(families["oci_monitor_scan_stale"].samples[0][1], 0)

    def _stale_after_seconds(self):
        return (
            2 * self.monitor.INTERVAL_HOURS * 3600 + self.monitor.QUIET_HOURS_END * 3600
        )

    def test_collect_marks_stale_when_last_scan_too_old(self):
        self.monitor._last_metrics = {
            "last_scan_ts": self.monitor.time.time() - self._stale_after_seconds() - 1,
        }
        collector = self.monitor.OCIMetricsCollector()

        families = {f.name: f for f in collector.collect()}

        self.assertEqual(families["oci_monitor_scan_stale"].samples[0][1], 1)

    def test_collect_not_stale_across_a_normal_quiet_hours_gap(self):
        # Healthy monitor: last scan just before quiet hours started, gap
        # covers a full quiet-hours window plus a normal check interval —
        # must NOT read stale (this is the case the threshold exists for).
        self.monitor._last_metrics = {
            "last_scan_ts": self.monitor.time.time() - self._stale_after_seconds() + 1,
        }
        collector = self.monitor.OCIMetricsCollector()

        families = {f.name: f for f in collector.collect()}

        self.assertEqual(families["oci_monitor_scan_stale"].samples[0][1], 0)

    def test_collect_marks_stale_when_never_scanned(self):
        self.monitor._last_metrics = {}
        collector = self.monitor.OCIMetricsCollector()

        families = {f.name: f for f in collector.collect()}

        self.assertEqual(families["oci_monitor_scan_stale"].samples[0][1], 1)

    def test_collect_labels_compute_instances_without_leaking_removed_ones(self):
        self.monitor._last_metrics = {
            "instances": [
                {"name": "vm1", "shape": "VM.Standard.E2.1.Micro", "state": "RUNNING"},
            ]
        }
        collector = self.monitor.OCIMetricsCollector()
        families = {f.name: f for f in collector.collect()}
        labels, value = families["oci_monitor_compute_instance_state"].samples[0]
        self.assertEqual(
            labels,
            {"name": "vm1", "shape": "VM.Standard.E2.1.Micro", "state": "RUNNING"},
        )
        self.assertEqual(value, 1)

        # Instance removed between scrapes — fresh collect() must not keep emitting it.
        self.monitor._last_metrics = {"instances": []}
        families = {f.name: f for f in collector.collect()}
        self.assertEqual(families["oci_monitor_compute_instance_state"].samples, [])


if __name__ == "__main__":
    unittest.main()
