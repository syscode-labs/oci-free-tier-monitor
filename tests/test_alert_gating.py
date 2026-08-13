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
        "VAT_RATE": "0",
    }
    os.environ.update(env)
    sys.modules["requests"] = types.SimpleNamespace()
    sys.modules["pytz"] = types.SimpleNamespace()
    sys.modules["oci"] = types.SimpleNamespace()
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
            compute_instances=mock.Mock(return_value=[]),
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
            _in_quiet_hours=mock.Mock(return_value=False),
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

    def test_threshold_breach_alerts_only_once_when_amount_unchanged(self):
        """Threshold alerts must not repeat when the amount (rounded to £1) stays the same."""
        with self.patch_common_checks():
            self.monitor.monthly_spend.return_value = (6.0, "GBP")

            self.monitor.check("/tmp/key.pem")
            # Second check: same spend — must NOT re-alert
            self.monitor.check("/tmp/key.pem")

            self.assertEqual(self.monitor.send_telegram.call_count, 1)
            message = self.monitor.send_telegram.call_args.args[1]
        self.assertIn("Spend: GBP 6.00 / 5.00 threshold", message)

    def test_threshold_breach_re_alerts_when_amount_changes(self):
        """A new alert fires when spend crosses the next £1 boundary."""
        with self.patch_common_checks():
            self.monitor.monthly_spend.return_value = (6.0, "GBP")
            self.monitor.check("/tmp/key.pem")

            # Spend jumps to next integer — signature changes, must re-alert
            self.monitor.monthly_spend.return_value = (7.1, "GBP")
            self.monitor.check("/tmp/key.pem")

            self.assertEqual(self.monitor.send_telegram.call_count, 2)

    def test_ampere_compute_above_new_free_tier_limits_alerts(self):
        with self.patch_common_checks():
            self.monitor.compute_instances.return_value = [
                {
                    "name": "ampere-1",
                    "shape": "VM.Standard.A1.Flex",
                    "ocpus": 2.0,
                    "memory_gb": 12.0,
                    "state": "RUNNING",
                },
                {
                    "name": "ampere-2",
                    "shape": "VM.Standard.A1.Flex",
                    "ocpus": 1.0,
                    "memory_gb": 6.0,
                    "state": "RUNNING",
                },
                {
                    "name": "ampere-3",
                    "shape": "VM.Standard.A1.Flex",
                    "ocpus": 1.0,
                    "memory_gb": 6.0,
                    "state": "RUNNING",
                },
            ]

            self.monitor.check("/tmp/key.pem")

            message = self.monitor.send_telegram.call_args.args[1]
        self.assertIn("A1 instances: 3 / 2", message)
        self.assertIn("A1 OCPUs: 4 / 2", message)
        self.assertIn("A1 memory: 24 / 12 GB", message)

    def test_compute_instances_include_flex_shape_resources(self):
        shape_config = types.SimpleNamespace(ocpus=1.0, memory_in_gbs=6.0)
        instance = types.SimpleNamespace(
            id="instance-1",
            display_name="ampere-1",
            shape="VM.Standard.A1.Flex",
            shape_config=shape_config,
            lifecycle_state="RUNNING",
        )
        compute_client = mock.Mock()
        compute_client.list_instances.return_value = types.SimpleNamespace(
            data=[instance],
            has_next_page=False,
        )
        self.monitor.oci = types.SimpleNamespace(
            core=types.SimpleNamespace(
                ComputeClient=mock.Mock(return_value=compute_client)
            ),
            pagination=types.SimpleNamespace(
                list_call_get_all_results=mock.Mock(
                    return_value=types.SimpleNamespace(data=[instance])
                )
            ),
        )

        instances = self.monitor.compute_instances({})

        self.assertEqual(instances[0]["ocpus"], 1.0)
        self.assertEqual(instances[0]["memory_gb"], 6.0)

    def test_compute_instances_scan_all_accessible_compartments(self):
        compartment_a = types.SimpleNamespace(
            id="compartment-a",
            name="homelab",
            lifecycle_state="ACTIVE",
        )
        compartment_b = types.SimpleNamespace(
            id="compartment-b",
            name="talos",
            lifecycle_state="ACTIVE",
        )
        shape_config = types.SimpleNamespace(ocpus=1.0, memory_in_gbs=6.0)
        instance = types.SimpleNamespace(
            id="instance-1",
            display_name="oci-talos-cp-1",
            shape="VM.Standard.A1.Flex",
            shape_config=shape_config,
            lifecycle_state="RUNNING",
        )
        compute_client = mock.Mock()
        identity_client = mock.Mock()

        def list_instances(*, compartment_id):
            data = [instance] if compartment_id == "compartment-b" else []
            return types.SimpleNamespace(data=data)

        compute_client.list_instances.side_effect = list_instances
        self.monitor.oci = types.SimpleNamespace(
            core=types.SimpleNamespace(
                ComputeClient=mock.Mock(return_value=compute_client)
            ),
            identity=types.SimpleNamespace(
                IdentityClient=mock.Mock(return_value=identity_client)
            ),
            pagination=types.SimpleNamespace(
                list_call_get_all_results=mock.Mock(
                    side_effect=[
                        types.SimpleNamespace(data=[compartment_a, compartment_b]),
                        types.SimpleNamespace(data=[]),
                        types.SimpleNamespace(data=[instance]),
                    ]
                )
            ),
        )

        instances = self.monitor.compute_instances({})

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["name"], "oci-talos-cp-1")
        self.assertEqual(instances[0]["compartment_name"], "talos")
        self.assertEqual(instances[0]["compartment_id"], "compartment-b")

    def test_compute_instances_fails_when_any_compartment_cannot_be_inventoried(self):
        compartment_a = types.SimpleNamespace(
            id="compartment-a",
            name="homelab",
            lifecycle_state="ACTIVE",
        )
        compartment_b = types.SimpleNamespace(
            id="compartment-b",
            name="talos",
            lifecycle_state="ACTIVE",
        )
        shape_config = types.SimpleNamespace(ocpus=1.0, memory_in_gbs=6.0)
        instance = types.SimpleNamespace(
            id="instance-1",
            display_name="oci-talos-cp-1",
            shape="VM.Standard.A1.Flex",
            shape_config=shape_config,
            lifecycle_state="RUNNING",
        )
        compute_client = mock.Mock()
        identity_client = mock.Mock()

        def list_instances(*, compartment_id):
            if compartment_id == "compartment-a":
                raise Exception("NotAuthorizedOrNotFound")
            return types.SimpleNamespace(data=[instance])

        compute_client.list_instances.side_effect = list_instances
        self.monitor.oci = types.SimpleNamespace(
            core=types.SimpleNamespace(
                ComputeClient=mock.Mock(return_value=compute_client)
            ),
            identity=types.SimpleNamespace(
                IdentityClient=mock.Mock(return_value=identity_client)
            ),
            pagination=types.SimpleNamespace(
                list_call_get_all_results=mock.Mock(
                    side_effect=[
                        types.SimpleNamespace(data=[compartment_a, compartment_b]),
                        Exception("NotAuthorizedOrNotFound"),
                        types.SimpleNamespace(data=[instance]),
                    ]
                )
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "instance inventory incomplete"):
            self.monitor.compute_instances({})

    def test_compute_failure_does_not_report_instances_as_removed(self):
        previous_snapshot = {
            "instance-1": {
                "name": "oci-talos-cp-1",
                "shape": "VM.Standard.A1.Flex",
                "state": "RUNNING",
                "compartment": "syscode-homelab",
            }
        }
        self.monitor._state["last_instance_snapshot"] = previous_snapshot

        with self.patch_common_checks():
            self.monitor.compute_instances.side_effect = RuntimeError(
                "instance inventory incomplete (syscode-homelab: No route to host)"
            )

            self.monitor.check("/tmp/key.pem")

            message = self.monitor.send_telegram.call_args.args[1]

        self.assertIn("Compute check failed: instance inventory incomplete", message)
        self.assertNotIn("Instance removed:", message)
        self.assertEqual(
            self.monitor._state["last_instance_snapshot"], previous_snapshot
        )

    def test_network_preflight_retries_once_before_failing(self):
        with mock.patch.object(
            self.monitor.socket,
            "create_connection",
            side_effect=[OSError("No route to host"), OSError("No route to host")],
        ) as connect:
            with self.assertRaisesRegex(RuntimeError, "after 2 attempts"):
                self.monitor.check_oci_connectivity()

        self.assertEqual(connect.call_count, 2)

    def test_network_failure_skips_resource_checks_and_alerts_once(self):
        with (
            self.patch_common_checks(),
            mock.patch.object(
                self.monitor,
                "check_oci_connectivity",
                side_effect=RuntimeError("after 2 attempts: No route to host"),
            ) as connectivity,
        ):
            self.monitor.check("/tmp/key.pem")
            self.monitor.check("/tmp/key.pem")

            self.assertEqual(connectivity.call_count, 2)
            self.monitor.monthly_spend.assert_not_called()
            self.monitor.compute_instances.assert_not_called()
            self.assertEqual(self.monitor.send_telegram.call_count, 1)
            message = self.monitor.send_telegram.call_args.args[1]

        self.assertIn("after 2 attempts: No route to host", message)
        self.assertIn("Resource checks skipped.", message)

    def test_scan_message_reports_compute_compartment(self):
        with mock.patch.multiple(
            self.monitor,
            build_oci_config=mock.Mock(return_value={}),
            compute_instances=mock.Mock(
                return_value=[
                    {
                        "name": "oci-talos-cp-1",
                        "shape": "VM.Standard.A1.Flex",
                        "ocpus": 1.0,
                        "memory_gb": 6.0,
                        "state": "RUNNING",
                        "compartment_name": "talos",
                        "compartment_id": "compartment-b",
                    }
                ]
            ),
            load_balancers=mock.Mock(return_value=[]),
            orphaned_public_ips=mock.Mock(return_value=[]),
            orphaned_boot_volumes=mock.Mock(return_value=[]),
            orphaned_block_volumes=mock.Mock(return_value=[]),
            volume_backups=mock.Mock(return_value=[]),
            custom_images=mock.Mock(return_value=[]),
            object_storage_usage_gb=mock.Mock(return_value=0.0),
        ):
            message = self.monitor.build_scan_message("/tmp/key.pem")

        self.assertIn("*Compute (1)*", message)
        self.assertIn("oci-talos-cp-1 `VM.Standard.A1.Flex` RUNNING — talos", message)


if __name__ == "__main__":
    unittest.main()
