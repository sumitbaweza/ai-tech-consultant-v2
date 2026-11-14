"""Files on disk (STACK.md S4): screenshots, uploaded documents, lesson files, reports.

Each business has its own folder per kind, so deleting a business deletes its folders (D14).
Paths stored in the database are relative to STORAGE_DIR, so the folder can move.
"""

import shutil
import uuid
from pathlib import Path
from typing import Literal

from core.config import get_settings

Kind = Literal["images", "documents", "lessons", "reports"]
KINDS: tuple[Kind, ...] = ("images", "documents", "lessons", "reports")


def root() -> Path:
    return get_settings().storage_dir


def folder(business_id: uuid.UUID, kind: Kind) -> Path:
    path = root() / kind / str(business_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in name).strip("._")
    return cleaned[:120] or "file"


def save(business_id: uuid.UUID, kind: Kind, name: str, data: bytes) -> str:
    """Write a file and return its path relative to STORAGE_DIR."""
    path = folder(business_id, kind) / _safe_name(name)
    path.write_bytes(data)
    return path.relative_to(root()).as_posix()


def save_text(business_id: uuid.UUID, kind: Kind, name: str, text: str) -> str:
    return save(business_id, kind, name, text.encode("utf-8"))


def absolute(relative_path: str) -> Path:
    path = (root() / relative_path).resolve()
    if root().resolve() not in path.parents:
        raise ValueError("path outside the storage folder")
    return path


def delete_business(business_id: uuid.UUID, kinds: tuple[Kind, ...] = KINDS) -> int:
    """Remove the business's folders; returns how many folders went."""
    removed = 0
    for kind in kinds:
        path = root() / kind / str(business_id)
        if path.exists():
            shutil.rmtree(path)
            removed += 1
    return removed
