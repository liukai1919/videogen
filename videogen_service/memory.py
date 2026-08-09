# Memory: preferences the user explicitly asked to keep ("以后旁白都用口语").
# Every entry is added by hand, visible in full and deletable — there is no
# silent learning. Enabled entries ride into every summary prompt as a
# standing-preferences section, so the longer the tool is used the closer its
# defaults sit to the operator's taste. One JSON file, same atomic-write style
# as everything else.

from datetime import UTC, datetime
from pathlib import Path
import secrets
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from videogen_service.artifacts import write_json_atomic

# A prompt hoarding hundreds of preferences would crowd out the transcript, so
# injection takes only the newest entries; the file itself keeps everything.
_MAX_INJECTED = 20


class MemoryError(RuntimeError):
    pass


class MemoryNotFound(MemoryError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryEntry(StrictModel):
    entry_id: str
    text: str = Field(min_length=1, max_length=500)
    created_at: str


class MemoryFile(StrictModel):
    schema_version: Literal[1] = 1
    entries: list[MemoryEntry] = Field(default_factory=list)


class MemoryStore:
    def __init__(self, work_dir: Path) -> None:
        self._path = work_dir / "memory.json"
        self._lock = Lock()

    # Defined before `list` below shadows the builtin in this class body, so
    # the return annotation still means the real list type.
    def texts(self) -> list[str]:
        """What a summary prompt should carry: the newest entries, oldest
        first so long-standing preferences read before recent refinements."""
        with self._lock:
            entries = self._load().entries
        return [entry.text for entry in entries[-_MAX_INJECTED:]]

    def list(self) -> list[MemoryEntry]:
        with self._lock:
            return self._load().entries

    def add(self, text: str) -> MemoryEntry:
        cleaned = " ".join(text.split())
        entry = MemoryEntry(
            entry_id=_mint_entry_id(), text=cleaned, created_at=_now()
        )
        with self._lock:
            stored = self._load()
            self._save(
                stored.model_copy(update={"entries": [*stored.entries, entry]})
            )
        return entry

    def remove(self, entry_id: str) -> None:
        with self._lock:
            stored = self._load()
            remaining = [
                entry for entry in stored.entries if entry.entry_id != entry_id
            ]
            if len(remaining) == len(stored.entries):
                raise MemoryNotFound(f"偏好 {entry_id} 不存在")
            self._save(stored.model_copy(update={"entries": remaining}))

    def _load(self) -> MemoryFile:
        try:
            return MemoryFile.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return MemoryFile()

    def _save(self, stored: MemoryFile) -> None:
        write_json_atomic(self._path, stored.model_dump(mode="json"))


def _mint_entry_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"mem_{stamp}_{secrets.token_hex(3)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
