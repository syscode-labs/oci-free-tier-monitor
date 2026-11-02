"""Regression coverage for the previously deployed recovery safeguards."""

import os
import unittest
from unittest import mock

from tests import test_alert_gating

load_monitor = test_alert_gating.load_monitor


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.monitor = load_monitor()

    def test_environment_auth_rejects_missing_credentials_and_unknown_mode(self):
        with mock.patch.object(self.monitor, "API_KEY_PEM", ""):
            with self.assertRaisesRegex(RuntimeError, "OCI_API_KEY"):
                self.monitor.build_oci_config("unused")
        with mock.patch.object(self.monitor, "OCI_AUTH_MODE", "unknown"):
            with self.assertRaisesRegex(RuntimeError, "OCI_AUTH_MODE"):
                self.monitor.build_oci_config()

    def test_config_file_auth_validates_key_and_conflicting_credentials(self):
        config = dict(
            tenancy="tenant",
            user="user",
            fingerprint="fingerprint",
            region="region",
            key_file="/example/key.pem",
        )
        with (
            mock.patch.object(self.monitor, "OCI_AUTH_MODE", "config_file"),
            mock.patch.object(self.monitor, "OCI_PROFILE_CONFIG", config),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(self.monitor.os.path, "isfile", return_value=True),
            mock.patch.object(self.monitor.os, "access", return_value=True),
        ):
            self.assertEqual(self.monitor.build_oci_config(), config)
            with mock.patch.object(self.monitor.os, "access", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "unreadable"):
                    self.monitor.build_oci_config()
            with mock.patch.dict(
                os.environ,
                {"OCI_API_KEY": "synthetic"},  # pragma: allowlist secret
            ):
                with self.assertRaisesRegex(RuntimeError, "conflicts"):
                    self.monitor.build_oci_config()

    def test_private_probe_disabled_success_and_failure(self):
        with mock.patch.object(self.monitor, "OCI_PRIVATE_PROBE_HOST", ""):
            self.assertEqual(self.monitor.private_path_probe_alerts(), [])
        with (
            mock.patch.object(
                self.monitor, "OCI_PRIVATE_PROBE_HOST", "private.example"
            ),
            mock.patch.object(self.monitor.socket, "create_connection") as connect,
        ):
            self.assertEqual(self.monitor.private_path_probe_alerts(), [])
            connect.side_effect = OSError("synthetic failure")
            self.assertIn(
                "private-path probe failed", self.monitor.private_path_probe_alerts()[0]
            )

    def test_discovery_errors_do_not_fall_back_to_partial_inventory(self):
        self.monitor.oci = mock.Mock()
        self.monitor.oci.pagination.list_call_get_all_results.side_effect = ValueError(
            "sensitive detail"
        )
        with self.assertRaisesRegex(RuntimeError, r"discovery failed \(ValueError\)"):
            self.monitor.accessible_compartments({})

    def test_failed_scan_does_not_cleanup_or_advance_success_timestamp(self):
        fixture = test_alert_gating.ScheduledAlertGatingTests()
        fixture.monitor = self.monitor
        self.monitor._state = self.monitor.DEFAULTS.copy()
        self.monitor._state["auto_cleanup"] = True
        with (
            fixture.patch_common_checks(),
            mock.patch.object(self.monitor, "check_oci_connectivity"),
            mock.patch.object(self.monitor, "update_metrics") as metrics,
            mock.patch.multiple(
                self.monitor,
                drgs=mock.Mock(return_value=[]),
                ipsec_connections=mock.Mock(return_value=[]),
                nat_gateways=mock.Mock(return_value=[]),
                network_load_balancers=mock.Mock(return_value=[]),
                drg_route_alerts=mock.Mock(return_value=[]),
                vpn_tunnel_alerts=mock.Mock(return_value=[]),
            ),
        ):
            self.monitor.monthly_spend.side_effect = RuntimeError(
                "synthetic scan failure"
            )
            self.monitor.orphaned_public_ips.return_value = [{"id": "synthetic-ip"}]
            self.monitor.check("unused")
            self.monitor.run_cleanup.assert_not_called()
            self.assertFalse(
                any("last_scan_ts" in call.args[0] for call in metrics.call_args_list)
            )
