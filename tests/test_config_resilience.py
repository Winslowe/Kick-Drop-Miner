import json
import os
import tempfile
import unittest

import core.config as config_module


class ConfigResilienceTests(unittest.TestCase):
    def setUp(self):
        self.original_path = config_module.CONFIG_FILE
        self.temp_dir = tempfile.TemporaryDirectory()
        config_module.CONFIG_FILE = os.path.join(
            self.temp_dir.name, "config.json"
        )

    def tearDown(self):
        config_module.CONFIG_FILE = self.original_path
        self.temp_dir.cleanup()

    def test_malformed_json_does_not_break_startup(self):
        with open(config_module.CONFIG_FILE, "w", encoding="utf-8") as config_file:
            config_file.write("{broken")
        config = config_module.Config()
        self.assertEqual(config.items, [])

    def test_save_is_complete_and_leaves_no_temp_file(self):
        config = config_module.Config()
        self.assertTrue(
            config.add(
                "https://kick.com/example",
                120,
                campaign_id="campaign",
            )
        )
        with open(config_module.CONFIG_FILE, "r", encoding="utf-8") as config_file:
            saved = json.load(config_file)
        self.assertEqual(len(saved["items"]), 1)
        self.assertFalse(
            any(name.startswith("config.json.tmp.") for name in os.listdir(self.temp_dir.name))
        )


if __name__ == "__main__":
    unittest.main()
