"""Persistent pet catalog and settings.

Bundled pets are immutable. User-added pets live below ``data/pets`` so a
future WebUI can manage them without changing package files.
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
import zlib
from dataclasses import asdict, dataclass
from functools import lru_cache, wraps
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from PIL import Image
from claw.paths import pet_assets_dir


_PET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_WINDOWS_RESERVED_IDS = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_CELL_SIZE = (192, 208)
_STANDARD_FRAME_COUNTS = (6, 8, 8, 4, 5, 8, 6, 6, 6)
_MAX_PACKAGE_FILES = 8
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_SPRITESHEET_BYTES = 50 * 1024 * 1024
_MAX_PACKAGE_UNCOMPRESSED_BYTES = _MAX_SPRITESHEET_BYTES + _MAX_MANIFEST_BYTES
_MAX_COMPRESSION_RATIO = 500
_SUPPORTED_ZIP_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


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


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class PetCatalog:
    """Thread-safe pet metadata, assets, and preferences store."""

    def __init__(self, data_dir: Path, bundled_dir: Path | None = None):
        self._root = Path(data_dir) / "pet"
        self._user_pets = Path(data_dir) / "pets"
        self._bundled = bundled_dir or pet_assets_dir()
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
                enabled=_optional_bool(raw.get("enabled"), True),
                selected_pet_id=str(raw.get("selectedPetId") or "yuexinmiao"),
                launch_on_gateway_start=_optional_bool(
                    raw.get("launchOnGatewayStart"), True
                ),
                position_x=_optional_int(pos.get("x")),
                position_y=_optional_int(pos.get("y")),
            )
            if self.get_pet(settings.selected_pet_id) is None:
                settings.selected_pet_id = "yuexinmiao"
                self._write_settings(settings)
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

    @_synchronized
    def list_pets(self) -> list[dict[str, Any]]:
        pets: list[dict[str, Any]] = []
        seen: set[str] = set()
        asset_indexes: dict[str, int] = {}
        selected_id = self.load_settings().selected_pet_id
        for source, root, read_only in (
            ("user", self._user_pets, False),
            ("bundled", self._bundled, True),
        ):
            if not root.exists():
                continue
            for manifest_path in sorted(root.glob("*/pet.json")):
                if manifest_path.parent.name.startswith("."):
                    continue
                pet = self._read_pet(manifest_path.parent, source, read_only)
                if pet is None or pet["id"] in seen:
                    continue
                # A previously imported copy of a bundled pet can have a
                # different id while containing the exact same spritesheet.
                # Treat the asset itself as the identity for listing so the
                # settings page does not present duplicate visual pets.
                asset_key = _pet_asset_key(pet)
                existing_index = asset_indexes.get(asset_key)
                if existing_index is not None:
                    # Keep the selected ID visible if a bundled pet and a
                    # user-installed copy share the same spritesheet.
                    if pet["id"] == selected_id and pets[existing_index]["id"] != selected_id:
                        seen.discard(pets[existing_index]["id"])
                        pets[existing_index] = pet
                        seen.add(pet["id"])
                    continue
                pets.append(pet)
                seen.add(pet["id"])
                asset_indexes[asset_key] = len(pets) - 1
        return pets

    @_synchronized
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

    @_synchronized
    def install(
        self,
        *,
        pet_id: str,
        display_name: str,
        description: str,
        spritesheet: BinaryIO,
        filename: str = "spritesheet.webp",
        sprite_version_number: int | None = None,
        validate_frames: bool = False,
    ) -> dict[str, Any]:
        pet_id = pet_id.strip().lower()
        if not _PET_ID_RE.fullmatch(pet_id) or pet_id in _WINDOWS_RESERVED_IDS:
            raise PetCatalogError("宠物 ID 只能包含小写字母、数字、下划线和短横线，且不能使用系统保留名称")
        if (self._bundled / pet_id).exists():
            raise PetCatalogError("不能覆盖内置宠物")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".webp"}:
            raise PetCatalogError("spritesheet 仅支持 PNG 或 WebP")

        pet_dir = self._user_pets / pet_id
        if pet_dir.exists():
            raise PetCatalogError(f"宠物已存在: {pet_id}")
        with tempfile.NamedTemporaryFile(
            prefix=f".{pet_id}-upload-",
            suffix=suffix,
            dir=self._root,
            delete=False,
        ) as upload_tmp:
            tmp_path = Path(upload_tmp.name)
        try:
            total = 0
            with tmp_path.open("wb") as upload_tmp:
                while True:
                    chunk = spritesheet.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_SPRITESHEET_BYTES:
                        raise PetCatalogError("spritesheet 超过 50 MB 限制")
                    upload_tmp.write(chunk)
            try:
                with Image.open(tmp_path) as image:
                    width, height = image.size
                    image_format = image.format
                    has_transparency = (
                        image.mode in {"RGBA", "LA"} or "transparency" in image.info
                    )
                    image.verify()
            except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
                raise PetCatalogError("spritesheet 不是有效的 PNG 或 WebP 图像") from exc
            expected_format = ".png" if image_format == "PNG" else ".webp" if image_format == "WEBP" else ""
            if expected_format != suffix:
                raise PetCatalogError("spritesheet 的实际格式必须与 .png 或 .webp 扩展名一致")
            if not has_transparency:
                raise PetCatalogError("spritesheet 必须包含透明通道")
            if width != _CELL_SIZE[0] * 8 or height not in {
                _CELL_SIZE[1] * 9,
                _CELL_SIZE[1] * 11,
            }:
                raise PetCatalogError("spritesheet 必须是 1536x1872 或 1536x2288")
            inferred_version = 2 if height == _CELL_SIZE[1] * 11 else 1
            version = (
                inferred_version
                if sprite_version_number is None
                else sprite_version_number
            )
            if (
                type(version) is not int
                or version not in (1, 2)
                or (version == 2) != (inferred_version == 2)
            ):
                raise PetCatalogError("spriteVersionNumber 与 spritesheet 尺寸不匹配")
            if validate_frames:
                try:
                    self._validate_atlas_frames(tmp_path, version)
                except PetCatalogError:
                    raise
                except (OSError, ValueError) as exc:
                    raise PetCatalogError("spritesheet 图像数据损坏或无法读取") from exc

            staged_dir = self._user_pets / (
                f".install-{pet_id}-{uuid.uuid4().hex}"
            )
            asset_name = f"spritesheet{suffix}"
            try:
                staged_dir.mkdir(parents=False, exist_ok=False)
                shutil.move(str(tmp_path), str(staged_dir / asset_name))
                manifest = {
                    "id": pet_id,
                    "displayName": display_name.strip() or pet_id,
                    "description": description.strip(),
                    "spritesheetPath": asset_name,
                    "spriteVersionNumber": version,
                }
                (staged_dir / "pet.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                try:
                    os.replace(staged_dir, pet_dir)
                except FileExistsError as exc:
                    raise PetCatalogError(f"宠物已存在: {pet_id}") from exc
                except OSError as exc:
                    raise PetCatalogError(f"宠物安装失败: {exc}") from exc
            finally:
                if staged_dir.exists():
                    shutil.rmtree(staged_dir, ignore_errors=True)
        finally:
            tmp_path.unlink(missing_ok=True)
        pet = self.get_pet(pet_id)
        assert pet is not None
        return pet

    @_synchronized
    def install_package(
        self,
        *,
        package: BinaryIO,
        filename: str = "pet.zip",
    ) -> dict[str, Any]:
        """Validate and install a self-contained pet ZIP package.

        A package contains exactly ``pet.json`` and one referenced
        ``spritesheet.png`` or ``spritesheet.webp``. They may live at the ZIP
        root or inside one top-level directory whose name matches the pet id.
        Archive members are never extracted directly, avoiding path traversal.
        """
        if Path(filename).suffix.lower() != ".zip":
            raise PetCatalogError("宠物包仅支持 ZIP 格式")

        try:
            package.seek(0)
            with zipfile.ZipFile(package) as archive:
                members = self._validate_package_members(archive)
                manifest_members = [
                    member for member in members
                    if PurePosixPath(member.filename).name == "pet.json"
                ]
                if len(manifest_members) != 1:
                    raise PetCatalogError("宠物包内必须且只能包含一个 pet.json")

                manifest_member = manifest_members[0]
                if manifest_member.file_size > _MAX_MANIFEST_BYTES:
                    raise PetCatalogError("pet.json 超过 64 KB 限制")
                try:
                    raw = json.loads(archive.read(manifest_member).decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise PetCatalogError("pet.json 必须使用 UTF-8 编码") from exc
                except json.JSONDecodeError as exc:
                    raise PetCatalogError(f"pet.json 不是有效 JSON: {exc.msg}") from exc

                manifest = self._validate_package_manifest(raw)
                manifest_path = PurePosixPath(manifest_member.filename)
                root_parts = manifest_path.parts[:-1]
                if len(root_parts) > 1:
                    raise PetCatalogError("宠物包最多只能包含一个顶层目录")
                if root_parts and root_parts[0] != manifest["id"]:
                    raise PetCatalogError("宠物包顶层目录名必须与 pet.json 中的 id 一致")
                if any(PurePosixPath(member.filename).parts[:-1] != root_parts for member in members):
                    raise PetCatalogError("pet.json 和 spritesheet 必须位于同一目录")

                asset_name = manifest["spritesheetPath"]
                expected_names = {"pet.json", asset_name}
                actual_names = {PurePosixPath(member.filename).name for member in members}
                if actual_names != expected_names or len(members) != 2:
                    raise PetCatalogError("宠物包只能包含 pet.json 和其引用的 spritesheet 文件")
                asset_member = next(
                    member for member in members
                    if PurePosixPath(member.filename).name == asset_name
                )
                if asset_member.file_size > _MAX_SPRITESHEET_BYTES:
                    raise PetCatalogError("spritesheet 超过 50 MB 限制")

                bad_member = archive.testzip()
                if bad_member is not None:
                    raise PetCatalogError(f"宠物包完整性校验失败: {bad_member}")
                spritesheet = io.BytesIO(archive.read(asset_member))
        except (zipfile.BadZipFile, zlib.error) as exc:
            raise PetCatalogError("文件不是有效的 ZIP 宠物包") from exc
        except (RuntimeError, NotImplementedError) as exc:
            raise PetCatalogError("宠物包使用了不支持的加密或压缩方式") from exc

        return self.install(
            pet_id=manifest["id"],
            display_name=manifest["displayName"],
            description=manifest["description"],
            spritesheet=spritesheet,
            filename=asset_name,
            sprite_version_number=manifest["spriteVersionNumber"],
            validate_frames=True,
        )

    @staticmethod
    def _validate_package_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        members: list[zipfile.ZipInfo] = []
        seen: set[str] = set()
        total_size = 0
        for member in archive.infolist():
            name = member.filename
            if not name or "\x00" in name or "\\" in name:
                raise PetCatalogError(f"宠物包包含非法路径: {name!r}")
            path = PurePosixPath(name.rstrip("/"))
            if (
                path.is_absolute()
                or re.match(r"^[A-Za-z]:", name)
                or any(part in {"", ".", ".."} for part in path.parts)
                or len(name) > 240
                or any(len(part) > 100 for part in path.parts)
            ):
                raise PetCatalogError(f"宠物包包含非法路径: {name}")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PetCatalogError("宠物包不能包含符号链接")
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise PetCatalogError("宠物包不能包含设备文件或其他特殊文件")
            if member.flag_bits & 0x1:
                raise PetCatalogError("宠物包不能加密")
            if member.compress_type not in _SUPPORTED_ZIP_COMPRESSION:
                raise PetCatalogError("宠物包仅支持 Store 或 Deflate 压缩")
            if member.is_dir():
                continue
            canonical_name = name.casefold()
            if canonical_name in seen:
                raise PetCatalogError(f"宠物包包含重复文件: {name}")
            seen.add(canonical_name)
            members.append(member)
            total_size += member.file_size
            if member.file_size and (
                member.compress_size == 0
                or member.file_size / member.compress_size > _MAX_COMPRESSION_RATIO
            ):
                raise PetCatalogError(f"宠物包内文件压缩比异常: {name}")

        if not members:
            raise PetCatalogError("宠物包内没有文件")
        if len(members) > _MAX_PACKAGE_FILES:
            raise PetCatalogError(f"宠物包文件过多，最多允许 {_MAX_PACKAGE_FILES} 个文件")
        if total_size > _MAX_PACKAGE_UNCOMPRESSED_BYTES:
            raise PetCatalogError("宠物包解压后超过大小限制")
        return members

    @staticmethod
    def _validate_package_manifest(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise PetCatalogError("pet.json 顶层必须是 JSON 对象")
        pet_id = raw.get("id")
        if (
            not isinstance(pet_id, str)
            or not _PET_ID_RE.fullmatch(pet_id)
            or pet_id in _WINDOWS_RESERVED_IDS
        ):
            raise PetCatalogError(
                "pet.json 的 id 只能包含小写字母、数字、下划线和短横线，且不能使用系统保留名称"
            )
        display_name = raw.get("displayName")
        if not isinstance(display_name, str) or not display_name.strip():
            raise PetCatalogError("pet.json 的 displayName 不能为空")
        if len(display_name.strip()) > 100:
            raise PetCatalogError("pet.json 的 displayName 不能超过 100 个字符")
        description = raw.get("description", "")
        if not isinstance(description, str) or len(description) > 1000:
            raise PetCatalogError("pet.json 的 description 必须是最多 1000 个字符的字符串")
        version = raw.get("spriteVersionNumber")
        if type(version) is not int or version not in (1, 2):
            raise PetCatalogError("pet.json 的 spriteVersionNumber 必须是 1 或 2")
        asset_name = raw.get("spritesheetPath")
        if asset_name not in {"spritesheet.png", "spritesheet.webp"}:
            raise PetCatalogError("pet.json 的 spritesheetPath 必须是 spritesheet.png 或 spritesheet.webp")
        return {
            "id": pet_id,
            "displayName": display_name.strip(),
            "description": description.strip(),
            "spriteVersionNumber": version,
            "spritesheetPath": asset_name,
        }

    @staticmethod
    def _validate_atlas_frames(path: Path, version: int) -> None:
        with Image.open(path) as source:
            atlas = source.convert("RGBA")
        row_counts = (*_STANDARD_FRAME_COUNTS, *((8, 8) if version == 2 else ()))
        alpha = atlas.getchannel("A")
        for row, used_count in enumerate(row_counts):
            for column in range(8):
                left = column * _CELL_SIZE[0]
                top = row * _CELL_SIZE[1]
                cell_alpha = alpha.crop((left, top, left + _CELL_SIZE[0], top + _CELL_SIZE[1]))
                if column < used_count and cell_alpha.getbbox() is None:
                    raise PetCatalogError(f"spritesheet 的第 {row + 1} 行第 {column + 1} 帧为空")
                if column >= used_count and cell_alpha.getbbox() is not None:
                    raise PetCatalogError(f"spritesheet 的第 {row + 1} 行未使用帧必须完全透明")

    @_synchronized
    def remove(self, pet_id: str) -> None:
        if (self._bundled / pet_id).exists():
            raise PetCatalogError("不能删除内置宠物")
        pet_dir = self._user_pets / pet_id
        if not pet_dir.is_dir():
            raise PetCatalogError(f"宠物不存在: {pet_id}")
        was_selected = self.load_settings().selected_pet_id == pet_id
        backup_dir = self._user_pets / f".delete-{pet_id}-{uuid.uuid4().hex}"
        os.replace(pet_dir, backup_dir)
        try:
            if was_selected:
                self.update_settings(selected_pet_id="yuexinmiao")
        except BaseException:
            os.replace(backup_dir, pet_dir)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)

    def _write_settings(self, settings: PetSettings) -> None:
        tmp = self._settings_path.with_name(
            f".{self._settings_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(settings.to_dict(), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            tmp.replace(self._settings_path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _read_pet(pet_dir: Path, source: str, read_only: bool) -> dict[str, Any] | None:
        try:
            raw = json.loads((pet_dir / "pet.json").read_text(encoding="utf-8"))
            asset = pet_dir / str(raw.get("spritesheetPath") or "spritesheet.webp")
            if not asset.is_file() or not _PET_ID_RE.fullmatch(str(raw.get("id", ""))):
                return None
            width, height = _image_dimensions(asset)
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
        except (
            MemoryError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return None


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _pet_asset_key(pet: dict[str, Any]) -> str:
    """Return a stable identity for a pet's visual asset."""
    try:
        asset = Path(str(pet["spritesheetPath"]))
        stat_result = asset.stat()
        digest = _asset_file_digest(
            str(asset.resolve()),
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )
    except (KeyError, MemoryError, OSError, TypeError, ValueError):
        # _read_pet already validates the path; retain a malformed entry as a
        # distinct item if a caller supplies a hand-built pet mapping.
        digest = str(pet.get("spritesheetPath", ""))
    display_name = str(pet.get("displayName", "")).strip().casefold()
    description = str(pet.get("description", "")).strip()
    return f"{display_name}\0{description}\0{digest}"


@lru_cache(maxsize=256)
def _asset_file_digest(path: str, size: int, mtime_ns: int) -> str:
    """Hash a pet asset incrementally and cache it by filesystem identity."""
    del size, mtime_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _image_dimensions(path: Path) -> tuple[int, int]:
    """Read PNG/WebP canvas dimensions without asking Pillow to decode it.

    Pillow's WebP plugin reads the complete compressed file when opening an
    image. Catalog endpoints only need dimensions, so decoding every pet on
    each poll wastes tens of MiB and can fail when a sandbox VM is active.
    """
    with path.open("rb") as stream:
        header = stream.read(32)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(header) < 24 or header[12:16] != b"IHDR":
            raise ValueError("invalid PNG header")
        return (
            int.from_bytes(header[16:20], "big"),
            int.from_bytes(header[20:24], "big"),
        )
    if (
        len(header) >= 20
        and header[:4] == b"RIFF"
        and header[8:12] == b"WEBP"
    ):
        chunk_type = header[12:16]
        payload = header[20:]
        if chunk_type == b"VP8X" and len(payload) >= 10:
            return (
                1 + int.from_bytes(payload[4:7], "little"),
                1 + int.from_bytes(payload[7:10], "little"),
            )
        if chunk_type == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        if (
            chunk_type == b"VP8 "
            and len(payload) >= 10
            and payload[3:6] == b"\x9d\x01\x2a"
        ):
            return (
                int.from_bytes(payload[6:8], "little") & 0x3FFF,
                int.from_bytes(payload[8:10], "little") & 0x3FFF,
            )
    raise ValueError("unsupported image header")
