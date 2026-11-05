"""Guard: AGENTS.md (and its CLAUDE.md symlink) keep the OD-0015 and OD-0016/OD-0017 pointer sections.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import re
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

STANDING_ORDERS_HEADING = "## Owner standing orders — OD-0016 (2026-11-03) and OD-0017 (2026-11-05)"
STANDING_ORDERS_GATES = (
    "Payment",
    "secrets or access",
    "migration",
    "external publish or send",
    "legal commitment",
    "permanent delete",
)
# Phrases that only appear in the canonical rule text; a match means rules were copied here.
COPY_DETECTOR = re.compile(r"IQ ?200|Hawk'?s Eye|Uber[- ]style|smart router|zero tolerance", re.IGNORECASE)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


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

    def test_standing_orders_pointer_present(self) -> None:
        text = (ROOT / "AGENTS.md").read_text()
        self.assertIn(STANDING_ORDERS_HEADING, text)
        section = _section(text, STANDING_ORDERS_HEADING)
        for needle in (
            "OD-0016",
            "OD-0017",
            "config/whos_owner_directives_v1.json",
            "docs/WHOS_AGENT_WORKING_ORDER_V2.md",
            "docs/WHOS_CC_COMMAND_CENTER_RUNBOOK.md",
            "`whos-backend` `main`",
        ):
            self.assertIn(needle, section)
        for gate in STANDING_ORDERS_GATES:
            self.assertIn(gate, section)

    def test_standing_orders_section_follows_od0015(self) -> None:
        text = (ROOT / "AGENTS.md").read_text()
        od15 = text.index("## Working Criteria V2 (OD-0015)")
        nxt = text.index("\n## ", od15 + 1)
        self.assertEqual(text.index(STANDING_ORDERS_HEADING), nxt + 1)

    def test_standing_orders_section_is_short(self) -> None:
        section = _section((ROOT / "AGENTS.md").read_text(), STANDING_ORDERS_HEADING)
        self.assertLessEqual(len(section.rstrip("\n").splitlines()), 12)

    def test_no_copied_rule_text(self) -> None:
        text = (ROOT / "AGENTS.md").read_text()
        self.assertIsNone(COPY_DETECTOR.search(_section(text, STANDING_ORDERS_HEADING)))
        self.assertIsNone(COPY_DETECTOR.search(text))

    def test_claude_md_is_agents_md(self) -> None:
        self.assertEqual((ROOT / "CLAUDE.md").resolve(), (ROOT / "AGENTS.md").resolve())


if __name__ == "__main__":
    unittest.main()
