# The asset center: named, categorised reference stills (角色、场景、风格包、
# 道具…) that outlive any single render. Today a reference frame dies with its
# render directory; an asset is the same image with an identity, so a new task
# picks it from the library instead of re-uploading. Submitting a render still
# copies the bytes into the render's own directory — the render stays
# self-contained and deleting an asset never breaks history.
#
# Storage mirrors the other stores: work/.assets/<asset_id>/{asset.json,media.*}
# (".assets" contains a dot, so it can never collide with a render directory).

from datetime import UTC, datetime
from pathlib import Path
import re
import secrets
import shutil
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from videogen_service.artifacts import write_bytes_atomic, write_json_atomic

_ASSET_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ASSETS_DIRNAME = ".assets"
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class AssetError(RuntimeError):
    pass


class AssetNotFound(AssetError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetRecord(StrictModel):
    schema_version: Literal[1] = 1
    asset_id: str
    name: str = Field(min_length=1, max_length=120)
    # Free-form, after the skill categories: 角色 / 场景 / 风格包 / 道具 / 参考帧…
    category: str = Field(default="", max_length=40)
    filename: str
    created_at: str


class AssetView(StrictModel):
    asset_id: str
    name: str
    category: str
    filename: str
    created_at: str
    media_url: str


class AssetStore:
    def __init__(self, work_dir: Path) -> None:
        self._directory = work_dir / ASSETS_DIRNAME
        self._lock = Lock()

    def create(self, *, name: str, category: str, filename: str, data: bytes) -> AssetRecord:
        suffix = Path(filename).suffix.lower() or ".png"
        if suffix not in _IMAGE_SUFFIXES:
            raise AssetError(f"资产格式不支持: {suffix}")
        with self._lock:
            record = AssetRecord(
                asset_id=_mint_asset_id(),
                name=name,
                category=category,
                filename=f"media{suffix}",
                created_at=_now(),
            )
            directory = self._directory / record.asset_id
            write_bytes_atomic(directory / record.filename, data)
            write_json_atomic(
                directory / "asset.json", record.model_dump(mode="json")
            )
            return record

    def list(self) -> list[AssetRecord]:
        """Newest first, like the other stores."""
        with self._lock:
            records: list[AssetRecord] = []
            for path in self._directory.glob("*/asset.json"):
                record = self._read(path)
                if record is None or record.asset_id != path.parent.name:
                    continue
                if not (path.parent / record.filename).is_file():
                    continue
                records.append(record)
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records

    def get(self, asset_id: str) -> AssetRecord:
        with self._lock:
            return self._load(asset_id)

    def media_path(self, asset_id: str) -> Path:
        with self._lock:
            record = self._load(asset_id)
            path = self._directory / asset_id / record.filename
            if not path.is_file():
                raise AssetNotFound(f"资产 {asset_id} 的图片文件不见了")
            return path

    def delete(self, asset_id: str) -> None:
        with self._lock:
            self._load(asset_id)
            directory = (self._directory / asset_id).resolve()
            root = self._directory.resolve()
            if directory == root or root not in directory.parents:
                raise AssetError(f"拒绝删除资产目录之外的路径: {directory}")
            shutil.rmtree(directory)

    def _load(self, asset_id: str) -> AssetRecord:
        if not _ASSET_ID.fullmatch(asset_id):
            raise AssetNotFound("asset id 无效")
        path = self._directory / asset_id / "asset.json"
        if not path.is_file():
            raise AssetNotFound(f"资产 {asset_id} 不存在")
        record = self._read(path)
        if record is None:
            raise AssetNotFound(f"资产 {asset_id} 的记录读不出来")
        return record

    def _read(self, path: Path) -> AssetRecord | None:
        try:
            return AssetRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def asset_view(record: AssetRecord) -> AssetView:
    return AssetView(
        **record.model_dump(exclude={"schema_version"}),
        media_url=f"/v1/assets/{record.asset_id}/media",
    )


def _mint_asset_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"asset_{stamp}_{secrets.token_hex(3)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
