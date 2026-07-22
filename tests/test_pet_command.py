from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from claw.cli.commands import _handle_pet_command, is_command
from claw.pet.catalog import PetCatalog


class FakePetProcess:
    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        self.running = True
        return True

    def stop(self):
        self.stops += 1
        self.running = False
        return True


class PetCommandTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        pet_dir = root / "bundled" / "yuexinmiao"
        pet_dir.mkdir(parents=True)
        Image.new("RGBA", (1536, 1872), (0, 0, 0, 0)).save(
            pet_dir / "spritesheet.webp", "WEBP"
        )
        (pet_dir / "pet.json").write_text(
            json.dumps({
                "id": "yuexinmiao",
                "displayName": "月薪喵",
                "spritesheetPath": "spritesheet.webp",
            }),
            encoding="utf-8",
        )
        self.catalog = PetCatalog(root / "data", root / "bundled")
        self.process = FakePetProcess()
        self.state = SimpleNamespace(
            pet_catalog=self.catalog,
            pet_process=self.process,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_pet_slash_command_is_registered_and_manages_runtime(self):
        self.assertTrue(is_command("/pet"))
        self.assertTrue(is_command("/pet open"))
        pet_list = _handle_pet_command(["list"], self.state)
        self.assertIn("月薪喵", pet_list)
        self.assertNotIn("内置", pet_list)
        self.assertIn("已开启", _handle_pet_command(["open"], self.state))
        self.assertTrue(self.process.running)
        self.assertIn("已关闭", _handle_pet_command(["close"], self.state))
        self.assertFalse(self.catalog.load_settings().enabled)
        self.assertIn(
            "已开启",
            _handle_pet_command(["autostart", "on"], self.state),
        )


if __name__ == "__main__":
    unittest.main()
