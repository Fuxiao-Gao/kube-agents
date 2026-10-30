"""The exec tier's shared names match the design's implementation contract.

The broker and the shim import these names from `credential_proxy_client`;
docs/designs/credential-broker-exec-tier.md §12 is what the operator, the chart
and the reviewers read. A name changed on one side and not the other is a
broker and a shim that disagree in production, so this checks both directions
it can: every value is in the contract, and the contract's bounds are the set
the shim retries plus the ones it does not.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import credential_proxy_client as client

DESIGN = Path(__file__).resolve().parents[3] / "docs" / "designs" / "credential-broker-exec-tier.md"


def _contract() -> str:
    text = DESIGN.read_text(encoding="utf-8")
    return text.split("## 12. Implementation contract", 1)[1]


def _row(contract: str, item: str) -> str:
    (line,) = [line for line in contract.splitlines() if line.startswith(f"| {item}")]
    return line


class ExecTierContractTest(unittest.TestCase):
    def test_every_shared_name_is_in_the_contract(self):
        contract = _contract()
        for value in (
            client.EXEC_URL_ENV, client.EXEC_REPLICAS_FILE_ENV, client.EXEC_REPLICAS_FILE,
            f"CREDENTIAL_PROXY_ROLE={client.EXEC_ROLE}", f'"{client.BUSY_BOUND_KEY}"',
            client.CLASS_INTERACTIVE, client.CLASS_BACKGROUND, f"`{client.CLASS_LABEL}`",
            client.AUDIT_BROKER_TIER, client.AUDIT_BROKER_POD,
            f"`{client.BROKER_TIER_EXEC}`", f"`{client.BROKER_TIER_STATEFUL}`",
            client.API_RELAY_PREFIX.rstrip("/"),
        ):
            with self.subTest(value=value):
                self.assertIn(value, contract)

    def test_the_retried_bounds_are_the_contract_s(self):
        retried = set(re.findall(r"`([a-z-]+)`", _row(_contract(), "Shim retry")))
        self.assertEqual(set(client.RETRYABLE_BUSY_BOUNDS), retried)
        bounds = set(re.findall(r"`([a-z-]+)`", _row(_contract(), "Busy reply").split("The bounds are", 1)[1]))
        self.assertIn(client.ADMISSION_BOUND_DRAINING, bounds)
        self.assertTrue(client.RETRYABLE_BUSY_BOUNDS < bounds)


if __name__ == "__main__":
    unittest.main()
