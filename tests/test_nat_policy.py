"""Exercise exact deployed NAT policy/report AST without importing the monitor."""

import ast
import os
import typing
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "monitor.py"
TREE = ast.parse(SOURCE.read_text())
HELPER = next(
    n
    for n in TREE.body
    if isinstance(n, ast.FunctionDef) and n.name == "unapproved_nat_gateways"
)
# Locate exact status-report and scheduled-check VPN blocks.
STATUS = next(
    n
    for n in ast.walk(TREE)
    if isinstance(n, ast.Try)
    and "nat_list = nat_gateways(config)" in ast.unparse(n)
    and "Site-to-Site VPN" in ast.unparse(n)
)
CHECK = next(
    n
    for n in ast.walk(TREE)
    if isinstance(n, ast.Try)
    and "unexpected_nat = unapproved_nat_gateways(nat_gateways(config))"
    in ast.unparse(n)
)


class NATPolicyTests(unittest.TestCase):
    def test_policy_and_both_report_paths(self):
        for ids, expected in [
            ([], 0),
            (["approved"], 0),
            (["unexpected"], 1),
            (["approved", "unexpected"], 1),
        ]:
            with (
                self.subTest(ids=ids),
                patch.dict(os.environ, {"APPROVED_NAT_GATEWAY_IDS": "approved"}),
            ):
                ns = {
                    "List": typing.List,
                    "os": os,
                    "config": {},
                    "lines": [],
                    "threshold_alerts": [],
                    "change_alerts": [],
                    "nat_gateways": lambda _: [{"id": i} for i in ids],
                    "drgs": lambda _: [],
                    "ipsec_connections": lambda _: [],
                    "network_load_balancers": lambda _: [],
                    "drg_route_alerts": lambda *a: [],
                    "vpn_tunnel_alerts": lambda *a: [],
                    "update_metrics": lambda *a: None,
                }
                exec(
                    compile(
                        ast.Module(body=[HELPER, STATUS, CHECK], type_ignores=[]),
                        "deployed-policy",
                        "exec",
                    ),
                    ns,
                )
                self.assertEqual(bool(ns["threshold_alerts"]), bool(expected))
                if ids:
                    self.assertTrue(
                        any(f"{expected} not approved" in line for line in ns["lines"])
                    )
                self.assertEqual(
                    any("⚠️ NAT Gateway" in line for line in ns["lines"]), False
                )

    def test_no_allowlist_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            ns = {"List": typing.List, "os": os}
            exec(
                compile(ast.Module(body=[HELPER], type_ignores=[]), "policy", "exec"),
                ns,
            )
            self.assertEqual(
                ns["unapproved_nat_gateways"]([{"id": "approved"}]),
                [{"id": "approved"}],
            )


if __name__ == "__main__":
    unittest.main()
