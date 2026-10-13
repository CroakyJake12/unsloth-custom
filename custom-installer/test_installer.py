from pathlib import Path
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import tempfile
import zipfile
import unittest
from unittest.mock import patch
from contextlib import closing

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("custom_installer", ROOT / "installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def fixture(self, root):
        studio, desktop, payload = root / "studio", root / "desktop", root / "payload"
        runtime = studio / "unsloth_studio/Lib/site-packages/studio/backend"
        runtime.mkdir(parents=True)
        desktop.mkdir()
        (payload / "backend").mkdir(parents=True)
        (runtime / "main.py").write_text("original = True\n")
        (desktop / "unsloth-studio.exe").write_bytes(b"old-exe")
        (payload / "unsloth-studio.exe").write_bytes(b"custom-exe")
        (payload / "backend/main.py").write_text("custom = True\n")
        with zipfile.ZipFile(payload / "frontend.zip", "w") as z:
            z.writestr("index.html", "Custom UI")
        manifest = {"version": "test", "desktopSha256": installer.digest(payload / "unsloth-studio.exe"),
            "files": [{"path": "main.py", "before": installer.digest(runtime / "main.py"), "after": installer.digest(payload / "backend/main.py")}]}
        (payload / "manifest.json").write_text(json.dumps(manifest))
        with closing(sqlite3.connect(studio / "studio.db")) as conn:
            conn.execute("CREATE TABLE app_settings(value TEXT)")
            conn.execute("INSERT INTO app_settings VALUES ('keep settings')")
            conn.commit()
        return studio, desktop, payload, runtime

    def test_installs_preserves_settings_and_backs_up_original(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            studio, desktop, payload, runtime = self.fixture(Path(directory))
            result = installer.run_installer(payload, studio, desktop, test=True)
            self.assertEqual((desktop / "unsloth-studio.exe").read_bytes(), b"custom-exe")
            with closing(sqlite3.connect(studio / "studio.db")) as conn:
                self.assertEqual(conn.execute("SELECT value FROM app_settings").fetchone()[0], "keep settings")
            self.assertEqual((Path(result["backup"]) / "unsloth-studio.exe").read_bytes(), b"old-exe")

    def test_rejects_drift_before_replacing_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            studio, desktop, payload, runtime = self.fixture(Path(directory))
            (runtime / "main.py").write_text("user_change = True\n")
            with self.assertRaisesRegex(RuntimeError, "changed since validation"):
                installer.run_installer(payload, studio, desktop, test=True)
            self.assertEqual((desktop / "unsloth-studio.exe").read_bytes(), b"old-exe")

    def test_rolls_back_after_replacement_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            studio, desktop, payload, runtime = self.fixture(Path(directory))
            (payload / "frontend.zip").write_bytes(b"invalid-zip")
            with self.assertRaises(zipfile.BadZipFile):
                installer.run_installer(payload, studio, desktop, test=True)
            self.assertEqual((desktop / "unsloth-studio.exe").read_bytes(), b"old-exe")
            self.assertEqual((runtime / "main.py").read_text(), "original = True\n")


if __name__ == "__main__":
    unittest.main()
