"""Guard: AGENTS.md (and its CLAUDE.md symlink) keep the OD-0015 pointer section.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATES = (
    "Payment",
    "secrets/access",
    "migrations",
    "external publish/send",
    "legal commitment",
    "destructive delete",
)


class WorkingCriteriaPointerTest(unittest.TestCase):
    def test_pointer_section_present(self) -> None:
        text = (ROOT / "AGENTS.md").read_text()
        self.assertIn("## Working Criteria V2 (OD-0015)", text)
        self.assertIn("docs/WHOS_OWNER_WORKING_CRITERIA_V2.md", text)
        self.assertIn("docs/WHOS_AGENT_WORKING_ORDER_V2.md", text)
        self.assertIn("never PASSes its own work", text)
        self.assertIn("wise-hands-company/whos-backend` on `main`", text)
        self.assertNotIn("claude/mission-rules-upgrade-20261102", text)
        for gate in GATES:
            self.assertIn(gate, text)

    def test_claude_md_is_agents_md(self) -> None:
        self.assertEqual((ROOT / "CLAUDE.md").resolve(), (ROOT / "AGENTS.md").resolve())


if __name__ == "__main__":
    unittest.main()
