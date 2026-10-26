import tempfile
import unittest
from pathlib import Path

import launcher

DEFAULTS = {"disable_hdr_exposure": True, "disable_steam_overlay": True, "enable_zink": False, "force_alsa_audio": False}


class Launcher(unittest.TestCase):
    def test_format(self):
        # As MAKO 4.0 wrote it: the keys added later get mako-launch's defaults.
        self.assertEqual(launcher.parse("version=1\nenable_zink=0\nforce_alsa_audio=0\n"), DEFAULTS)
        self.assertEqual(launcher.parse("# c\n version = 1 \nenable_zink=1\n"), {**DEFAULTS, "enable_zink": True})
        # As mako-ui 4.6 writes it, and back the same.
        text = "version=1\ndisable_hdr_exposure=0\ndisable_steam_overlay=1\nenable_zink=0\nforce_alsa_audio=1\n"
        settings = {"disable_hdr_exposure": False, "disable_steam_overlay": True, "enable_zink": False, "force_alsa_audio": True}
        self.assertEqual(launcher.parse(text), settings)
        self.assertEqual(launcher.dumps(settings), text)
        for bad in ("enable_zink=1\n", "version=2\n", "version=1\nenable_zink=2\n", "version=1\nfoo=1\n",
                    "version=1\nenable_zink=1\nenable_zink=0\n", "version=1\na=b=c\n"):
            with self.subTest(text=bad), self.assertRaises(launcher.Invalid):
                launcher.parse(bad)

    def test_paths_and_files(self):
        self.assertEqual(launcher.path({"MAKO_LAUNCH_CONFIG": "/x.conf"}), Path("/x.conf"))
        self.assertEqual(launcher.path({"XDG_CONFIG_HOME": "/c"}), Path("/c/mako-render/launcher.conf"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "launcher.conf"
            self.assertEqual(launcher.load(p), DEFAULTS)
            launcher.save({**DEFAULTS, "force_alsa_audio": True}, p)
            self.assertEqual(launcher.load(p), {**DEFAULTS, "force_alsa_audio": True})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
