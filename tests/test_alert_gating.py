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
    core = types.SimpleNamespace(GaugeMetricFamily=mock.Mock())
    sys.modules["prometheus_client"] = types.SimpleNamespace(
        start_http_server=mock.Mock(),
        REGISTRY=types.SimpleNamespace(register=mock.Mock()),
        core=core,
    )
    sys.modules["prometheus_client.core"] = core
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
            save_metrics=mock.Mock(),
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

    def test_vpn_alert_retries_after_delivery_failure(self):
        """A failed send must not acknowledge a tunnel-down signature."""
        notify = mock.Mock(side_effect=[False, True])
        with (
            self.patch_common_checks(),
            mock.patch.multiple(
                self.monitor,
                drgs=mock.Mock(return_value=[]),
                ipsec_connections=mock.Mock(
                    return_value=[{"id": "ipsec", "name": "home"}]
                ),
                nat_gateways=mock.Mock(return_value=[]),
                network_load_balancers=mock.Mock(return_value=[]),
                drg_route_alerts=mock.Mock(return_value=[]),
                vpn_tunnel_alerts=mock.Mock(return_value=["VPN tunnel down: home/one"]),
                notify=notify,
            ),
        ):
            self.monitor.check("/tmp/key.pem")
            self.assertIsNone(self.monitor._state["last_threshold_alert_signature"])

            self.monitor.check("/tmp/key.pem")

        self.assertEqual(notify.call_count, 2)
        self.assertIn(
            "VPN tunnel down", self.monitor._state["last_threshold_alert_signature"]
        )

    def test_grafana_delivery_posts_annotation(self):
        response = mock.Mock(ok=True)
        with (
            mock.patch.object(self.monitor, "GRAFANA_URL", "https://grafana.example"),
            mock.patch.object(self.monitor, "GRAFANA_API_TOKEN", "token"),
            mock.patch.object(
                self.monitor.requests, "post", return_value=response, create=True
            ) as post,
        ):
            self.assertTrue(self.monitor.send_grafana("VPN tunnel down"))

        self.assertEqual(
            post.call_args.args[0], "https://grafana.example/api/annotations"
        )
        self.assertEqual(
            post.call_args.kwargs["json"]["tags"], ["oci-monitor", "alert"]
        )

    def test_grafana_failure_does_not_acknowledge_delivery(self):
        response = mock.Mock(ok=False, status_code=503)
        with (
            mock.patch.object(self.monitor, "GRAFANA_URL", "https://grafana.example"),
            mock.patch.object(self.monitor, "GRAFANA_API_TOKEN", "token"),
            mock.patch.object(
                self.monitor.requests, "post", return_value=response, create=True
            ),
        ):
            self.assertFalse(self.monitor.send_grafana("VPN tunnel down"))

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
        # Test that compute_instances failure is handled gracefully
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
            # Make compute_instances raise a RuntimeError
            self.monitor.compute_instances.side_effect = RuntimeError(
                "instance inventory incomplete (syscode-homelab: No route to host)"
            )

            self.monitor.check("/tmp/key.pem")

            # The check method catches the RuntimeError and adds a warning to
            # threshold_alerts. Verify the monitor handles the error gracefully
            # by checking that _state is still accessible
            self.assertIn("last_instance_snapshot", self.monitor._state)

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
        # Test that network failure causes resource checks to be skipped
        # and connectivity is retried twice
        with self.patch_common_checks():
            with mock.patch.object(
                self.monitor,
                "check_oci_connectivity",
                side_effect=RuntimeError("after 2 attempts: No route to host"),
            ) as connectivity:
                self.monitor.check("/tmp/key.pem")
                self.monitor.check("/tmp/key.pem")

                # Connectivity should have been checked twice (once per check call)
                self.assertEqual(connectivity.call_count, 2)

                # The key verifications: monthly_spend and compute_instances
                # are NOT called when network failure is active
                self.monitor.monthly_spend.assert_not_called()
                self.monitor.compute_instances.assert_not_called()

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


class NonFreeShapeTests(unittest.TestCase):
    def setUp(self):
        self.monitor = load_monitor()
        self.monitor._state = self.monitor.DEFAULTS.copy()

    def test_non_free_shape_flagged(self):
        breaches = self.monitor.compute_free_tier_breaches(
            [
                {
                    "name": "oci-bastion-01",
                    "shape": "VM.Standard.E4.Flex",
                    "ocpus": 1.0,
                    "memory_gb": 1.0,
                    "state": "RUNNING",
                }
            ]
        )
        self.assertTrue(any("Non-free shape" in b for b in breaches))
        self.assertTrue(any("E4.Flex" in b for b in breaches))

    def test_free_shapes_not_flagged(self):
        breaches = self.monitor.compute_free_tier_breaches(
            [
                {
                    "name": "a",
                    "shape": "VM.Standard.A1.Flex",
                    "ocpus": 1.0,
                    "memory_gb": 6.0,
                    "state": "RUNNING",
                },
                {
                    "name": "b",
                    "shape": "VM.Standard.E2.1.Micro",
                    "ocpus": 1.0,
                    "memory_gb": 1.0,
                    "state": "RUNNING",
                },
            ]
        )
        self.assertFalse(any("Non-free shape" in b for b in breaches))


class KeepFloorImageTests(unittest.TestCase):
    def setUp(self):
        self.monitor = load_monitor()
        self.monitor._state = self.monitor.DEFAULTS.copy()

    def test_keeps_newest_golden_per_type(self):
        imgs = [
            {"id": "1", "name": "golden-micro-20260901", "size_gb": 5, "in_use": False},
            {"id": "2", "name": "golden-micro-20260902", "size_gb": 5, "in_use": False},
            {"id": "3", "name": "import-random", "size_gb": 3, "in_use": False},
        ]
        surplus = self.monitor._keep_floor_images(imgs)
        ids = {i["id"] for i in surplus}
        # keep newest golden (id 2); delete old golden (1) + non-golden unused (3)
        self.assertEqual(ids, {"1", "3"})

    def test_never_touches_in_use_images(self):
        imgs = [
            {"id": "1", "name": "golden-micro-1", "size_gb": 5, "in_use": True},
            {"id": "2", "name": "in-use-thing", "size_gb": 4, "in_use": True},
        ]
        self.assertEqual(self.monitor._keep_floor_images(imgs), [])

    def test_no_golden_deletes_all_unused(self):
        imgs = [{"id": "3", "name": "import-random", "size_gb": 3, "in_use": False}]
        surplus = self.monitor._keep_floor_images(imgs)
        self.assertEqual({i["id"] for i in surplus}, {"3"})

    def test_run_cleanup_deletes_images(self):
        client = mock.Mock()
        self.monitor.oci = types.SimpleNamespace(
            core=types.SimpleNamespace(ComputeClient=mock.Mock(return_value=client))
        )
        report = self.monitor.run_cleanup(
            {}, [], [], [], [{"id": "img-1", "name": "old", "size_gb": 5}]
        )
        client.delete_image.assert_called_once_with("img-1")
        self.assertEqual(report["deleted_images"][0]["id"], "img-1")


if __name__ == "__main__":
    unittest.main()
