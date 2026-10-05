import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "peek"))

import state  # noqa: E402


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("HERDR_PLUGIN_STATE_DIR")
        os.environ["HERDR_PLUGIN_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        else:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = self.old
        self.tmp.cleanup()

    def test_older_invocation_never_overwrites_newer(self):
        state.set_current("w1:t1", [{"path": "/new", "line": None}], 0, started=200.0)
        state.set_current("w1:t1", [{"path": "/old", "line": None}], 0, started=100.0)
        data = state.read_json(state.current_file("w1:t1"))
        self.assertEqual(data["items"][0]["path"], "/new")

    def test_tabs_are_separate(self):
        state.set_current("w1:t1", [{"path": "/a", "line": None}], 0, started=1.0)
        state.set_current("w1:t2", [{"path": "/b", "line": None}], 0, started=1.0)
        self.assertNotEqual(state.current_file("w1:t1"), state.current_file("w1:t2"))
        self.assertEqual(state.read_json(state.current_file("w1:t2"))["items"][0]["path"], "/b")

    def test_index_is_clamped(self):
        data = state.set_current("t", [{"path": "/a", "line": None}], 9, started=1.0)
        self.assertEqual(data["index"], 0)

    def test_config_parser(self):
        cfg_dir = tempfile.mkdtemp(dir=self.tmp.name)
        with open(os.path.join(cfg_dir, "config.toml"), "w") as fh:
            fh.write('placement = "popup"  # modal\nvideo_fps = 12\n')
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = cfg_dir
        try:
            cfg = state.load_config()
        finally:
            del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
        self.assertEqual(cfg["placement"], "popup")
        self.assertEqual(cfg["video_fps"], 12)
        self.assertEqual(cfg["direction"], "right")


if __name__ == "__main__":
    unittest.main()
