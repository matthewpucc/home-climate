import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

from house_climate.addon import launch_spec, main


class AddonTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/homeassistant")
        self.environment = {
            "SUPERVISOR_TOKEN": "test-supervisor-token",
            "HA_URL": "https://wrong.example",
            "HA_TOKEN": "old-token",
        }

    def test_shadow_defaults_and_supervisor_proxy(self):
        argv, env = launch_spec({}, self.environment, self.root)
        self.assertEqual(argv[1:], ["-m", "house_climate", "--config",
                                   "/homeassistant/house_climate.toml", "run"])
        self.assertNotIn("--live", argv)
        self.assertEqual(env["HA_URL"], "http://supervisor/core")
        self.assertEqual(env["HA_TOKEN"], "test-supervisor-token")
        self.assertNotIn("SUPERVISOR_TOKEN", env)
        self.assertEqual(self.environment["HA_TOKEN"], "old-token")
        self.assertNotIn("test-supervisor-token", " ".join(argv))

    def test_live_is_explicit(self):
        argv, _ = launch_spec({"live": True, "config_file": "climate/custom.toml"},
                              self.environment, self.root)
        self.assertEqual(argv[-1], "--live")
        self.assertIn("/homeassistant/climate/custom.toml", argv)

    def test_invalid_or_coerced_live_values_fail_closed(self):
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                launch_spec({"live": value}, self.environment, self.root)

    def test_config_cannot_escape_mount(self):
        for value in ("../data/options.json", "/etc/passwd", ".", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                launch_spec({"config_file": value}, self.environment, self.root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "escape").symlink_to("/tmp")
            with self.assertRaises(ValueError):
                launch_spec({"config_file": "escape/config.toml"}, self.environment, root)

    def test_token_required_and_options_must_be_object(self):
        with self.assertRaises(ValueError):
            launch_spec({}, {}, self.root)
        with self.assertRaises(ValueError):
            launch_spec([], self.environment, self.root)

    def test_main_executes_controller_without_child_process(self):
        with patch("builtins.open", mock_open(read_data=json.dumps({"live": False}))), \
             patch.dict(os.environ, self.environment, clear=True), \
             patch("house_climate.addon.os.execve") as execute:
            main()
        executable, argv, env = execute.call_args.args
        self.assertEqual(executable, argv[0])
        self.assertEqual(argv[-1], "run")
        self.assertEqual(env["HA_URL"], "http://supervisor/core")

    def test_bad_options_do_not_launch(self):
        with patch("builtins.open", mock_open(read_data='{"live": "true"}')), \
             patch("house_climate.addon.os.execve") as execute, \
             patch("builtins.print"):
            self.assertEqual(main(), 1)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
