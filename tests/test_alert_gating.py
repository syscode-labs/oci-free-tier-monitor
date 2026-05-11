import importlib
import os
import sys
import types
import unittest
from unittest import mock


def load_monitor():
    env = {
        "OCI_TENANCY_OCID": "tenancy",
        "OCI_USER_OCID": "user",
        "OCI_FINGERPRINT": "fingerprint",
        "OCI_API_KEY": "key",
        "OCI_COMPARTMENT_OCID": "compartment",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "chat",
    }
    os.environ.update(env)
    sys.modules.setdefault("requests", types.SimpleNamespace())
    sys.modules.setdefault("oci", types.SimpleNamespace())
    sys.modules.pop("monitor", None)
    return importlib.import_module("monitor")


class ScheduledAlertGatingTests(unittest.TestCase):
    def setUp(self):
        self.monitor = load_monitor()
        self.monitor._state = self.monitor.DEFAULTS.copy()
        self.monitor._account_label = "test-account"
        self.monitor._tenancy_slug = "tenant"

    def patch_common_checks(self):
        return mock.patch.multiple(
            self.monitor,
            monthly_spend=mock.Mock(return_value=(1.0, "GBP")),
            load_balancers=mock.Mock(return_value=[]),
            orphaned_public_ips=mock.Mock(return_value=[]),
            orphaned_boot_volumes=mock.Mock(return_value=[]),
            orphaned_block_volumes=mock.Mock(return_value=[]),
            volume_backups=mock.Mock(return_value=[]),
            custom_images=mock.Mock(return_value=[]),
            object_storage_usage_gb=mock.Mock(return_value=0.0),
            run_cleanup=mock.Mock(),
            _save_report_to_bucket=mock.Mock(),
            save_state=mock.Mock(),
            send_telegram=mock.Mock(),
        )

    def test_repeated_non_threshold_findings_only_alert_when_changed(self):
        with self.patch_common_checks():
            self.monitor.custom_images.return_value = [
                {"name": "old-import", "size_gb": 3, "in_use": False}
            ]

            self.monitor.check("/tmp/key.pem")
            self.monitor.check("/tmp/key.pem")

            self.assertEqual(self.monitor.send_telegram.call_count, 1)
            message = self.monitor.send_telegram.call_args.args[1]
        self.assertIn("Unused custom images: 1", message)

    def test_threshold_breaches_alert_even_when_unchanged(self):
        with self.patch_common_checks():
            self.monitor.monthly_spend.return_value = (6.0, "GBP")

            self.monitor.check("/tmp/key.pem")
            self.monitor.check("/tmp/key.pem")

            self.assertEqual(self.monitor.send_telegram.call_count, 2)
            message = self.monitor.send_telegram.call_args.args[1]
        self.assertIn("Spend: GBP 6.00 / 5.00 threshold", message)


if __name__ == "__main__":
    unittest.main()
