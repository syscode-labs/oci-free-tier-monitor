import importlib
import os
import sys
import types
import unittest


def load_monitor():
    os.environ.update(
        {
            "OCI_TENANCY_OCID": "tenancy",
            "OCI_USER_OCID": "user",
            "OCI_FINGERPRINT": "fingerprint",
            "OCI_API_KEY": "key",  # pragma: allowlist secret
            "OCI_COMPARTMENT_OCID": "compartment",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "chat",
            "VAT_RATE": "0",
        }
    )
    sys.modules["requests"] = types.SimpleNamespace()
    sys.modules["pytz"] = types.SimpleNamespace()
    sys.modules["oci"] = types.SimpleNamespace()
    sys.modules.pop("monitor", None)
    return importlib.import_module("monitor")


DRG = "ocid1.drg.oc1.uk-london-1.aaaa"
IGW = "ocid1.internetgateway.oc1.uk-london-1.bbbb"


class ClassifyDrgRoutesTests(unittest.TestCase):
    def setUp(self):
        self.m = load_monitor()
        self.approved = self.m.VPN_APPROVED_DRG_PREFIXES

    def test_approved_omni_route_is_silent(self):
        rules = [
            {
                "destination": "100.72.134.50/32",
                "network_entity_id": DRG,
                "route_table": "vpn",
            }
        ]
        self.assertEqual(self.m.classify_drg_routes(rules, {DRG}, self.approved), [])

    def test_default_route_to_drg_alerts(self):
        rules = [
            {"destination": "0.0.0.0/0", "network_entity_id": DRG, "route_table": "vpn"}
        ]
        alerts = self.m.classify_drg_routes(rules, {DRG}, self.approved)
        self.assertEqual(len(alerts), 1)
        self.assertIn("0.0.0.0/0", alerts[0])

    def test_overbroad_prefix_to_drg_alerts(self):
        rules = [
            {
                "destination": "10.10.210.0/23",
                "network_entity_id": DRG,
                "route_table": "vpn",
            }
        ]
        alerts = self.m.classify_drg_routes(rules, {DRG}, self.approved)
        self.assertEqual(len(alerts), 1)
        self.assertIn("10.10.210.0/23", alerts[0])

    def test_non_drg_target_ignored(self):
        rules = [
            {
                "destination": "0.0.0.0/0",
                "network_entity_id": IGW,
                "route_table": "public",
            }
        ]
        self.assertEqual(self.m.classify_drg_routes(rules, {DRG}, self.approved), [])

    def test_fires_on_ocid_shape_when_drg_ids_empty(self):
        # DRG listing denied (empty set) — still catches the DRG-shaped OCID.
        rules = [
            {"destination": "0.0.0.0/0", "network_entity_id": DRG, "route_table": "vpn"}
        ]
        self.assertEqual(
            len(self.m.classify_drg_routes(rules, set(), self.approved)), 1
        )


class ClassifyVpnTunnelsTests(unittest.TestCase):
    def setUp(self):
        self.m = load_monitor()

    def test_all_up_is_silent(self):
        tunnels = [
            {"ipsec": "oci-s2s", "tunnel": "tunnel-1", "status": "UP"},
            {"ipsec": "oci-s2s", "tunnel": "tunnel-2", "status": "UP"},
        ]
        self.assertEqual(self.m.classify_vpn_tunnels(tunnels), [])

    def test_down_tunnel_alerts(self):
        tunnels = [
            {"ipsec": "oci-s2s", "tunnel": "tunnel-1", "status": "UP"},
            {"ipsec": "oci-s2s", "tunnel": "tunnel-2", "status": "DOWN"},
        ]
        alerts = self.m.classify_vpn_tunnels(tunnels)
        self.assertEqual(len(alerts), 1)
        self.assertIn("tunnel-2", alerts[0])
        self.assertIn("DOWN", alerts[0])


if __name__ == "__main__":
    unittest.main()
