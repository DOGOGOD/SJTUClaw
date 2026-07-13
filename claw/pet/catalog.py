"""Persistent pet catalog and settings.

Bundled pets are immutable. User-added pets live below ``data/pets`` so a
future WebUI can manage them without changing package files.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image


_PET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CELL_SIZE = (192, 208)


@dataclass
class PetSettings:
    enabled: bool = True
    selected_pet_id: str = "yuexinmiao"
    launch_on_gateway_start: bool = True
    position_x: int | None = None
    position_y: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "enabled": data["enabled"],
            "selectedPetId": data["selected_pet_id"],
            "launchOnGatewayStart": data["launch_on_gateway_start"],
            "position": {
                "x": data["position_x"],
                "y": data["position_y"],
            },
        }


class PetCatalogError(ValueError):
    pass


class PetCatalog:
    """Thread-safe pet metadata, assets, and preferences store."""

    def __init__(self, data_dir: Path, bundled_dir: Path | None = None):
        self._root = Path(data_dir) / "pet"
        self._user_pets = Path(data_dir) / "pets"
        self._bundled = bundled_dir or Path(__file__).with_name("assets")
        self._settings_path = self._root / "settings.json"
        self._lock = threading.RLock()
        self._root.mkdir(parents=True, exist_ok=True)
        self._user_pets.mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> PetSettings:
        with self._lock:
            try:
                raw = json.loads(self._settings_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                settings = PetSettings()
                self._write_settings(settings)
                return settings
            pos = raw.get("position") if isinstance(raw.get("position"), dict) else {}
            settings = PetSettings(
                enabled=bool(raw.get("enabled", True)),
                selected_pet_id=str(raw.get("selectedPetId") or "yuexinmiao"),
                launch_on_gateway_start=bool(raw.get("launchOnGatewayStart", True)),
                position_x=_optional_int(pos.get("x")),
                position_y=_optional_int(pos.get("y")),
            )
            if self.get_pet(settings.selected_pet_id) is None:
                settings.selected_pet_id = "yuexinmiao"
            return settings

    def update_settings(
        self,
        *,
        enabled: bool | None = None,
        selected_pet_id: str | None = None,
        launch_on_gateway_start: bool | None = None,
        position_x: int | None = None,
        position_y: int | None = None,
        update_position: bool = False,
    ) -> PetSettings:
        with self._lock:
            settings = self.load_settings()
            if enabled is not None:
                settings.enabled = enabled
            if selected_pet_id is not None:
                if self.get_pet(selected_pet_id) is None:
                    raise PetCatalogError(f"宠物不存在: {selected_pet_id}")
                settings.selected_pet_id = selected_pet_id
            if launch_on_gateway_start is not None:
                settings.launch_on_gateway_start = launch_on_gateway_start
            if update_position:
                settings.position_x = position_x
                settings.position_y = position_y
            self._write_settings(settings)
            return settings

    def list_pets(self) -> list[dict[str, Any]]:
        pets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source, root, read_only in (
            ("user", self._user_pets, False),
            ("bundled", self._bundled, True),
        ):
            if not root.exists():
                continue
            for manifest_path in sorted(root.glob("*/pet.json")):
                pet = self._read_pet(manifest_path.parent, source, read_only)
                if pet is not None and pet["id"] not in seen:
                    pets.append(pet)
                    seen.add(pet["id"])
        return pets

    def get_pet(self, pet_id: str) -> dict[str, Any] | None:
        for source, root, read_only in (
            ("user", self._user_pets, False),
            ("bundled", self._bundled, True),
        ):
            pet_dir = root / pet_id
            pet = self._read_pet(pet_dir, source, read_only)
            if pet is not None:
                return pet
        return None

    def install(
        self,
        *,
        pet_id: str,
        display_name: str,
        description: str,
        spritesheet: BinaryIO,
        filename: str = "spritesheet.webp",
        sprite_version_number: int | None = None,
    ) -> dict[str, Any]:
        pet_id = pet_id.strip().lower()
        if not _PET_ID_RE.fullmatch(pet_id):
            raise PetCatalogError("宠物 ID 只能包含小写字母、数字、下划线和短横线")
        if (self._bundled / pet_id).exists():
            raise PetCatalogError("不能覆盖内置宠物")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".webp"}:
            raise PetCatalogError("spritesheet 仅支持 PNG 或 WebP")

        pet_dir = self._user_pets / pet_id
        tmp_path = self._root / f".{pet_id}-upload{suffix}"
        try:
            with tmp_path.open("wb") as out:
                shutil.copyfileobj(spritesheet, out)
            with Image.open(tmp_path) as image:
                width, height = image.size
                image.verify()
            if width != _CELL_SIZE[0] * 8 or height not in {
                _CELL_SIZE[1] * 9,
                _CELL_SIZE[1] * 11,
            }:
                raise PetCatalogError("spritesheet 必须是 1536x1872 或 1536x2288")
            inferred_version = 2 if height == _CELL_SIZE[1] * 11 else 1
            version = sprite_version_number or inferred_version
            if version not in (1, 2) or (version == 2) != (inferred_version == 2):
                raise PetCatalogError("spriteVersionNumber 与 spritesheet 尺寸不匹配")

            pet_dir.mkdir(parents=True, exist_ok=True)
            asset_name = f"spritesheet{suffix}"
            shutil.move(str(tmp_path), str(pet_dir / asset_name))
            manifest = {
                "id": pet_id,
                "displayName": display_name.strip() or pet_id,
                "description": description.strip(),
                "spritesheetPath": asset_name,
                "spriteVersionNumber": version,
            }
            (pet_dir / "pet.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        finally:
            tmp_path.unlink(missing_ok=True)
        pet = self.get_pet(pet_id)
        assert pet is not None
        return pet

    def remove(self, pet_id: str) -> None:
        if (self._bundled / pet_id).exists():
            raise PetCatalogError("不能删除内置宠物")
        pet_dir = self._user_pets / pet_id
        if not pet_dir.is_dir():
            raise PetCatalogError(f"宠物不存在: {pet_id}")
        shutil.rmtree(pet_dir)
        settings = self.load_settings()
        if settings.selected_pet_id == pet_id:
            self.update_settings(selected_pet_id="yuexinmiao")

    def _write_settings(self, settings: PetSettings) -> None:
        tmp = self._settings_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self._settings_path)

    @staticmethod
    def _read_pet(pet_dir: Path, source: str, read_only: bool) -> dict[str, Any] | None:
        try:
            raw = json.loads((pet_dir / "pet.json").read_text(encoding="utf-8"))
            asset = pet_dir / str(raw.get("spritesheetPath") or "spritesheet.webp")
            if not asset.is_file() or not _PET_ID_RE.fullmatch(str(raw.get("id", ""))):
                return None
            with Image.open(asset) as image:
                width, height = image.size
            if width != 1536 or height not in (1872, 2288):
                return None
            version = int(raw.get("spriteVersionNumber") or (2 if height == 2288 else 1))
            return {
                "id": raw["id"],
                "displayName": raw.get("displayName") or raw["id"],
                "description": raw.get("description") or "",
                "spriteVersionNumber": version,
                "spritesheetPath": str(asset.resolve()),
                "source": source,
                "readOnly": read_only,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
