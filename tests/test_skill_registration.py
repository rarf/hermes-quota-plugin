"""The bundled quota-check skill ships valid, installation-agnostic content.

The skill is registered read-only as ``quota:quota-check`` via
``api.register_skill`` (see ``__init__.py``). Its description must fit the
skill-index budget (~60 chars, one sentence, trigger first) and its body must
stay generic: no private profile names and no provider-specific assumptions
beyond what any install can observe through the plugin itself.
Offline; no network.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SKILL_PATH = ROOT / "skills" / "quota-check" / "SKILL.md"


class TestQuotaCheckSkill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL_PATH.read_text(encoding="utf-8")

    def test_skill_file_exists(self):
        self.assertTrue(SKILL_PATH.exists(), f"missing {SKILL_PATH}")

    def test_frontmatter_name_matches_folder(self):
        m = re.search(r"^name:\s*(\S+)", self.text, re.M)
        if m is None:
            self.fail("frontmatter is missing 'name'")
        self.assertEqual(m.group(1), "quota-check")

    def test_description_fits_skill_index_budget(self):
        m = re.search(r'^description:\s*"([^"]+)"', self.text, re.M)
        if m is None:
            self.fail("description must be a double-quoted string")
        desc = m.group(1)
        self.assertLessEqual(
            len(desc), 60,
            "description exceeds the skill-index one-line budget",
        )
        self.assertTrue(desc.endswith("."))

    def test_alert_thresholds_present(self):
        for pct in ("50", "20", "10", "5"):
            self.assertIn(f"≤{pct}%", self.text)

    def test_ask_first_protocol_present(self):
        self.assertIn("STOP BEFORE STARTING", self.text)
        self.assertIn("SPLIT the task", self.text)
        self.assertIn("USE A DIFFERENT MODEL", self.text)

    def test_no_degrade_rule_present(self):
        self.assertIn("NEVER reduce quality", self.text)

    def test_content_is_install_agnostic(self):
        forbidden = ["commandcode", "assistente-pessoal", "limen", "mateus"]
        low = self.text.lower()
        for token in forbidden:
            self.assertNotIn(token.lower(), low)

    def test_registration_is_wired(self):
        init = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("register_skill", init)
        self.assertIn('"quota-check"', init)


if __name__ == "__main__":
    unittest.main()
