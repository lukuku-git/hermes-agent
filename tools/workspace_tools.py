"""Capability-scoped file tools rooted by a trusted launcher environment."""
from __future__ import annotations

import codecs
import json
import os
import re
import secrets
import stat
from pathlib import Path, PurePath

from tools.registry import registry, tool_error, tool_result

ROOT_ENV = "HERMES_WORKSPACE_ROOT"
MAX_READ = 100_000
MAX_READ_LINES = 2_000
MAX_RESULTS = 200
MAX_OUTPUT = 100_000
MAX_WRITE = 2_000_000
SENSITIVE = re.compile(r"(?:^|[._-])(auth|authorization|credential|credentials|secret|secrets|token|tokens|password|passwd|config|configuration|key|keys)(?:$|[._-])", re.I)


class WorkspaceDenied(Exception):
    pass


def _root() -> Path:
    raw = os.environ.get(ROOT_ENV)
    if not raw or "\0" in raw or not os.path.isabs(raw):
        raise WorkspaceDenied
    try:
        st = os.lstat(raw)
    except OSError:
        raise WorkspaceDenied from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise WorkspaceDenied
    # Require the launcher to provide the canonical spelling. This also rejects
    # symlinks in ancestor components, not only a symlink at the final root.
    if os.path.realpath(raw) != os.path.normpath(raw):
        raise WorkspaceDenied
    return Path(raw)


def _parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkspaceDenied
    p = PurePath(value)
    if p.is_absolute() or value.startswith(("~", "/", "\\")):
        raise WorkspaceDenied
    if not p.parts or any(x in ("", ".", "..") for x in p.parts):
        raise WorkspaceDenied
    for part in p.parts:
        if part.startswith(".") or SENSITIVE.search(part):
            raise WorkspaceDenied
    return tuple(p.parts)


def _resolve(value: object, *, must_exist: bool, directory: bool = False) -> tuple[Path, Path]:
    """The single resolver used by every operation; never follows a link."""
    root = _root()
    current = root
    for index, part in enumerate(_parts(value)):
        current /= part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if must_exist or index != len(_parts(value)) - 1:
                raise WorkspaceDenied from None
            break
        except OSError:
            raise WorkspaceDenied from None
        if stat.S_ISLNK(st.st_mode):
            raise WorkspaceDenied
        if index < len(_parts(value)) - 1 and not stat.S_ISDIR(st.st_mode):
            raise WorkspaceDenied
        # A multiply-linked regular inode may have another name outside root.
        if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
            raise WorkspaceDenied
    if directory:
        try:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                raise WorkspaceDenied
        except OSError:
            raise WorkspaceDenied from None
    return root, current


def _denied() -> str:
    # Deliberately omit the input, host path and underlying exception.
    return tool_error("workspace request denied")


def _open_read(root: Path, path: Path) -> int:
    """Open through root-relative descriptors, without ancestor link races."""
    parts = path.relative_to(root).parts
    dfd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
            os.close(dfd)
            dfd = next_fd
        return os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
    finally:
        os.close(dfd)


def workspace_read(args: dict, **_: object) -> str:
    try:
        offset = args.get("offset", 1)
        limit = args.get("limit", MAX_READ_LINES)
        if (not isinstance(offset, int) or isinstance(offset, bool) or offset < 1 or
                not isinstance(limit, int) or isinstance(limit, bool) or
                limit < 1 or limit > MAX_READ_LINES):
            raise WorkspaceDenied

        root, path = _resolve(args.get("path"), must_exist=True)
        fd = _open_read(root, path)
        selected: list[str] = []
        selected_chars = 0
        total_lines = 0
        current = ""
        current_exists = False
        decoder = codecs.getincrementaldecoder("utf-8")()

        def consume(text: str, *, final: bool = False) -> None:
            nonlocal current, current_exists, selected_chars, total_lines
            pieces = text.split("\n")
            for index, piece in enumerate(pieces):
                target = offset <= total_lines + 1 < offset + limit
                current_exists = current_exists or bool(piece)
                if target:
                    current += piece
                    if selected_chars + len(current) > MAX_READ:
                        raise WorkspaceDenied
                if index < len(pieces) - 1:
                    if target:
                        line = current + "\n"
                        selected_chars += len(line)
                        if selected_chars > MAX_READ:
                            raise WorkspaceDenied
                        selected.append(line)
                    current = ""
                    current_exists = False
                    total_lines += 1
            if final and current_exists:
                total_lines += 1
                if offset <= total_lines < offset + limit:
                    selected_chars += len(current)
                    if selected_chars > MAX_READ:
                        raise WorkspaceDenied
                    selected.append(current)
                current = ""
                current_exists = False

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise WorkspaceDenied
            while True:
                data = os.read(fd, 64 * 1024)
                if not data:
                    break
                consume(decoder.decode(data))
            consume(decoder.decode(b"", final=True), final=True)
        finally:
            os.close(fd)

        content = "".join(selected)
        next_offset = offset + len(selected) if offset + len(selected) <= total_lines else None
        payload = tool_result(
            path=str(path.relative_to(root)), content=content,
            total_lines=total_lines, next_offset=next_offset,
        )
        if len(payload) > MAX_OUTPUT:
            return tool_error("workspace output limit exceeded")
        return payload
    except (WorkspaceDenied, OSError, UnicodeError):
        return _denied()


def _verified_parent(path: Path) -> tuple[int, os.stat_result | None]:
    root = _root()
    relative = path.relative_to(root)
    # Walk from an already-open root descriptor. openat(O_NOFOLLOW) on every
    # directory prevents an ancestor swap from redirecting the final open.
    pfd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=pfd)
            os.close(pfd)
            pfd = next_fd
    except Exception:
        os.close(pfd)
        raise WorkspaceDenied from None
    try:
        dst = os.stat(path.name, dir_fd=pfd, follow_symlinks=False)
    except FileNotFoundError:
        dst = None
    if dst and (stat.S_ISLNK(dst.st_mode) or not stat.S_ISREG(dst.st_mode) or dst.st_nlink != 1):
        os.close(pfd)
        raise WorkspaceDenied
    return pfd, dst


def _atomic_write(path: Path, content: str) -> None:
    data = content.encode()
    if len(data) > MAX_WRITE:
        raise WorkspaceDenied
    pfd, expected = _verified_parent(path)
    temporary = None
    try:
        temporary = f".workspace-write-{secrets.token_hex(16)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=pfd)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            now = os.stat(path.name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            now = None
        if (expected is None) != (now is None) or (expected and now and
                (expected.st_dev, expected.st_ino, expected.st_nlink) != (now.st_dev, now.st_ino, now.st_nlink)):
            raise WorkspaceDenied
        if now and (stat.S_ISLNK(now.st_mode) or now.st_nlink != 1):
            raise WorkspaceDenied
        os.replace(temporary, path.name, src_dir_fd=pfd, dst_dir_fd=pfd)
        temporary = None
        os.fsync(pfd)
    finally:
        if temporary:
            try:
                os.unlink(temporary, dir_fd=pfd)
            except OSError:
                pass
        os.close(pfd)


def workspace_write(args: dict, **_: object) -> str:
    try:
        root, path = _resolve(args.get("path"), must_exist=False)
        content = args.get("content")
        if not isinstance(content, str):
            raise WorkspaceDenied
        _atomic_write(path, content)
        return tool_result(success=True, path=str(path.relative_to(root)))
    except (WorkspaceDenied, OSError, UnicodeError):
        return _denied()


def workspace_patch(args: dict, **_: object) -> str:
    try:
        root, path = _resolve(args.get("path"), must_exist=True)
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise WorkspaceDenied
        fd = _open_read(root, path)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise WorkspaceDenied
            chunks = []
            size = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, MAX_WRITE + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_WRITE:
                    raise WorkspaceDenied
        finally:
            os.close(fd)
        content = b"".join(chunks).decode("utf-8")
        if content.count(old) != 1:
            return tool_error("patch target must match exactly once")
        _atomic_write(path, content.replace(old, new, 1))
        return tool_result(success=True, path=str(path.relative_to(root)))
    except (WorkspaceDenied, OSError, UnicodeError, ValueError, KeyError):
        return _denied()


def workspace_search(args: dict, **_: object) -> str:
    try:
        # Root may be traversed but never read/written as a file.
        raw_start = args.get("path", ".")
        if raw_start == ".":
            root = start = _root()
        else:
            root, start = _resolve(raw_start, must_exist=True, directory=True)
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 1000:
            raise WorkspaceDenied
        needle, results, used = pattern.casefold(), [], 0
        for base, dirs, files in os.walk(start, topdown=True, followlinks=False):
            safe_dirs = []
            for name in dirs:
                try:
                    _parts(name)
                    if not stat.S_ISLNK(os.lstat(Path(base, name)).st_mode):
                        safe_dirs.append(name)
                except (WorkspaceDenied, OSError):
                    pass
            dirs[:] = safe_dirs
            for name in files:
                try:
                    _parts(name)
                    rel = str(Path(base, name).relative_to(root))
                    resolved_root, path = _resolve(rel, must_exist=True)
                    fd = _open_read(resolved_root, path)
                    try:
                        st = os.fstat(fd)
                        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                            continue
                        data = os.read(fd, MAX_READ + 1)
                    finally:
                        os.close(fd)
                    if len(data) > MAX_READ:
                        continue
                    for number, line in enumerate(data.decode().splitlines(), 1):
                        if needle in line.casefold():
                            item = {"path": rel, "line": number, "text": line[:1000]}
                            size = len(json.dumps(item, ensure_ascii=False))
                            if len(results) >= MAX_RESULTS or used + size > MAX_OUTPUT:
                                return tool_result(results=results, truncated=True)
                            results.append(item); used += size
                except (WorkspaceDenied, OSError, UnicodeError):
                    continue
        return tool_result(results=results, truncated=False)
    except (WorkspaceDenied, OSError):
        return _denied()


def _available() -> bool:
    try:
        _root()
        return True
    except WorkspaceDenied:
        return False


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}


PATH = {"type": "string", "description": "Path relative to the injected workspace root"}
registry.register(
    name="workspace_read", toolset="workspace",
    schema=_schema(
        "workspace_read", "Read a UTF-8 workspace file with bounded line pagination",
        {
            "path": PATH,
            "offset": {"type": "integer", "minimum": 1, "description": "1-based starting line"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LINES},
        },
        ["path"],
    ),
    handler=workspace_read, check_fn=_available, max_result_size_chars=MAX_OUTPUT,
)
registry.register(
    name="workspace_search", toolset="workspace",
    schema=_schema("workspace_search", "Search workspace files without following links", {"pattern": {"type": "string"}, "path": PATH}, ["pattern"]),
    handler=workspace_search, check_fn=_available, max_result_size_chars=MAX_OUTPUT,
)
registry.register(
    name="workspace_write", toolset="workspace",
    schema=_schema("workspace_write", "Atomically write a UTF-8 workspace file", {"path": PATH, "content": {"type": "string"}}, ["path", "content"]),
    handler=workspace_write, check_fn=_available, max_result_size_chars=MAX_OUTPUT,
)
registry.register(
    name="workspace_patch", toolset="workspace",
    schema=_schema("workspace_patch", "Atomically replace one exact occurrence", {"path": PATH, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, ["path", "old_string", "new_string"]),
    handler=workspace_patch, check_fn=_available, max_result_size_chars=MAX_OUTPUT,
)