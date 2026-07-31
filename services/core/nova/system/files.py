"""Sandboxed filesystem access.

The assistant can read and write files, but only under roots the operator listed
in ``server.file_roots``. Every path is resolved (following symlinks) before the
containment check, so ``~/notes/../../etc/shadow`` and a symlink pointing out of
the sandbox are both rejected.

With no roots configured the sandbox defaults to the user's home directory,
minus the directories that hold credentials.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.errors import PermissionDenied
from ..runtime.logging import get_logger

log = get_logger(__name__)

#: Never readable, even inside a configured root.
DENIED_NAMES: frozenset[str] = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
        ".docker",
        ".netrc",
        "shadow",
        "gshadow",
        "id_rsa",
        "id_ed25519",
        ".env",
        "credentials",
        ".password-store",
    }
)

#: Files above this size are summarised rather than read whole.
MAX_READ_BYTES = 512 * 1024


@dataclass(slots=True)
class FileEntry:
    path: str
    name: str
    is_dir: bool
    size: int
    modified: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "isDir": self.is_dir,
            "size": self.size,
            "modified": self.modified,
        }


class FileSandbox:
    """Path containment plus the file operations the skills expose."""

    def __init__(self, roots: tuple[str, ...] = ()) -> None:
        self.roots = self._resolve_roots(roots)

    def _resolve_roots(self, roots: tuple[str, ...]) -> tuple[Path, ...]:
        resolved: list[Path] = []
        for raw in roots:
            try:
                path = Path(raw).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if path.is_dir():
                resolved.append(path)
            else:
                log.warning("file_root_missing", path=str(path))
        if not resolved:
            resolved.append(Path.home().resolve())
        return tuple(resolved)

    def resolve(self, raw: str, *, must_exist: bool = True) -> Path:
        """Resolve and containment-check a user-supplied path."""
        if not raw or not raw.strip():
            raise PermissionDenied("no path given")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        try:
            # strict=False so we can create new files inside the sandbox.
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PermissionDenied(f"cannot resolve path: {exc}") from exc

        if not any(_is_within(resolved, root) for root in self.roots):
            allowed = ", ".join(str(r) for r in self.roots)
            raise PermissionDenied(f"'{resolved}' is outside the permitted roots ({allowed})")

        for part in resolved.parts:
            if part.lower() in DENIED_NAMES:
                raise PermissionDenied(f"'{part}' holds credentials and is never accessible")

        if must_exist and not resolved.exists():
            raise PermissionDenied(f"no such file: {resolved}")
        return resolved

    # ------------------------------------------------------------- operations

    async def read(self, raw: str, *, max_bytes: int = MAX_READ_BYTES) -> str:
        path = self.resolve(raw)
        if path.is_dir():
            raise PermissionDenied(f"{path} is a directory")
        return await asyncio.to_thread(self._read_sync, path, max_bytes)

    def _read_sync(self, path: Path, max_bytes: int) -> str:
        size = path.stat().st_size
        with path.open("rb") as fh:
            data = fh.read(max_bytes)
        text = data.decode("utf-8", "replace")
        if size > max_bytes:
            text += f"\n… truncated ({size} bytes total)"
        return text

    async def write(self, raw: str, content: str, *, append: bool = False) -> str:
        path = self.resolve(raw, must_exist=False)
        return await asyncio.to_thread(self._write_sync, path, content, append)

    def _write_sync(self, path: Path, content: str, append: bool) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        if append and path.exists():
            with path.open("a", encoding="utf-8") as fh:
                fh.write(content)
            return f"appended {len(content)} bytes to {path}"
        # Atomic replace: a crash mid-write leaves the original intact.
        tmp = path.with_suffix(path.suffix + ".nova-tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return f"wrote {len(content)} bytes to {path}"

    async def list_dir(self, raw: str, *, pattern: str = "", limit: int = 200) -> list[FileEntry]:
        path = self.resolve(raw)
        if not path.is_dir():
            raise PermissionDenied(f"{path} is not a directory")
        return await asyncio.to_thread(self._list_sync, path, pattern, limit)

    def _list_sync(self, path: Path, pattern: str, limit: int) -> list[FileEntry]:
        entries: list[FileEntry] = []
        with os.scandir(path) as iterator:
            for entry in iterator:
                if entry.name.lower() in DENIED_NAMES:
                    continue
                if pattern and not fnmatch.fnmatch(entry.name, pattern):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append(
                    FileEntry(
                        path=str(Path(entry.path)),
                        name=entry.name,
                        is_dir=entry.is_dir(follow_symlinks=False),
                        size=stat.st_size,
                        modified=stat.st_mtime,
                    )
                )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries[:limit]

    async def search(self, root: str, pattern: str, *, limit: int = 100) -> list[FileEntry]:
        """Glob for filenames beneath ``root``."""
        base = self.resolve(root)
        return await asyncio.to_thread(self._search_sync, base, pattern, limit)

    def _search_sync(self, base: Path, pattern: str, limit: int) -> list[FileEntry]:
        found: list[FileEntry] = []
        for path in base.rglob(pattern):
            if len(found) >= limit:
                break
            if any(part.lower() in DENIED_NAMES for part in path.parts):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append(
                FileEntry(str(path), path.name, path.is_dir(), stat.st_size, stat.st_mtime)
            )
        return found

    async def grep(self, root: str, needle: str, *, glob: str = "*", limit: int = 60) -> list[str]:
        """Find lines containing ``needle`` in text files beneath ``root``."""
        base = self.resolve(root)
        return await asyncio.to_thread(self._grep_sync, base, needle, glob, limit)

    def _grep_sync(self, base: Path, needle: str, glob: str, limit: int) -> list[str]:
        matches: list[str] = []
        lowered = needle.lower()
        for path in base.rglob(glob):
            if len(matches) >= limit:
                break
            if not path.is_file() or any(p.lower() in DENIED_NAMES for p in path.parts):
                continue
            try:
                if path.stat().st_size > 4 * 1024 * 1024:
                    continue
                with path.open("r", encoding="utf-8", errors="ignore") as fh:
                    for number, line in enumerate(fh, 1):
                        if lowered in line.lower():
                            matches.append(f"{path}:{number}: {line.strip()[:200]}")
                            if len(matches) >= limit:
                                break
            except OSError:
                continue
        return matches

    async def stat(self, raw: str) -> dict[str, Any]:
        path = self.resolve(raw)
        info = await asyncio.to_thread(path.stat)
        return {
            "path": str(path),
            "size": info.st_size,
            "modified": info.st_mtime,
            "created": info.st_ctime,
            "isDir": path.is_dir(),
            "mode": oct(info.st_mode & 0o777),
        }

    async def delete(self, raw: str, *, recursive: bool = False) -> str:
        path = self.resolve(raw)
        if path in self.roots:
            raise PermissionDenied("refusing to delete a sandbox root")
        return await asyncio.to_thread(self._delete_sync, path, recursive)

    def _delete_sync(self, path: Path, recursive: bool) -> str:
        if path.is_dir():
            if not recursive:
                raise PermissionDenied(f"{path} is a directory — pass recursive=true")
            shutil.rmtree(path)
            return f"deleted directory {path}"
        path.unlink()
        return f"deleted {path}"

    async def make_dir(self, raw: str) -> str:
        path = self.resolve(raw, must_exist=False)
        await asyncio.to_thread(lambda: path.mkdir(parents=True, exist_ok=True))
        return f"created {path}"

    async def disk_tree(self, raw: str, *, depth: int = 1, top: int = 12) -> list[tuple[str, int]]:
        """Largest entries under a path — 'what's eating my disk'."""
        path = self.resolve(raw)
        return await asyncio.to_thread(self._disk_tree_sync, path, depth, top)

    def _disk_tree_sync(self, path: Path, depth: int, top: int) -> list[tuple[str, int]]:
        sizes: list[tuple[str, int]] = []
        for entry in path.iterdir():
            try:
                if entry.is_symlink():
                    continue
                size = _directory_size(entry) if entry.is_dir() else entry.stat().st_size
            except OSError:
                continue
            sizes.append((str(entry), size))
        sizes.sort(key=lambda kv: kv[1], reverse=True)
        return sizes[:top]


def _directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
