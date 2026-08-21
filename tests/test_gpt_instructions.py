"""The Custom GPT instructions must stay in sync with the default rules and within the editor limits."""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(ROOT, "skills", "yandex-disk-downloads-sort", "assets", "rules.default.json")
INSTRUCTIONS = os.path.join(ROOT, "chatgpt", "instructions.md")


class GptInstructionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(RULES, encoding="utf-8") as handle:
            cls.rules = json.load(handle)
        with open(INSTRUCTIONS, encoding="utf-8") as handle:
            cls.text = handle.read()

    def test_within_editor_limit(self):
        self.assertLessEqual(len(self.text), 8000)

    def test_every_category_folder_and_extension_is_listed(self):
        for cat in self.rules["categories"]:
            folder = cat["folder"]
            self.assertIn(f"{folder['en']} / {folder['ru']}", self.text, cat["id"])
            for ext in cat.get("extensions") or []:
                self.assertRegex(self.text, r"(?<![\w.])" + re.escape(ext) + r"(?![\w])", f"{cat['id']}: {ext}")

    def test_skip_extensions_listed(self):
        for ext in self.rules["skip"]["extensions"]:
            self.assertIn(ext, self.text)

    def test_duplicates_folder_listed(self):
        dup = self.rules["special_folders"]["duplicates"]
        self.assertIn(f"{dup['en']} / {dup['ru']}", self.text)


if __name__ == "__main__":
    unittest.main()
