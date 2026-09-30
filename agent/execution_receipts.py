"""Code-owned receipts from this run's completed tool dispatches only.

The collector is ephemeral: never a transcript parser, plugin-produced result,
log, or persistence source. Formatting emits only narrowly validated pointers.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
from pathlib import Path
import re
import shlex
import threading
import uuid


_CURRENT_COLLECTOR: ContextVar = ContextVar("tool_execution_receipt_collector", default=None)
_CURRENT_CALL_ID: ContextVar = ContextVar("tool_execution_receipt_call_id", default=None)
_WORKSPACE = Path("/Users/zeus/lukuku-os")
_PETASOS = Path("/Users/zeus/petasos")
_RECORD_ID = r"[A-Z]{1,4}-[0-9]{3,6}"
_STATUSES = frozenset({"active", "provisional", "draft", "superseded", "archived", "deprecated", "retired", "rejected"})
_SLACK = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.slack\.com/archives/C[A-Z0-9]+/p[0-9]{16}\Z")
_SLACK_READ_TOOLS = frozenset({
    "mcp__slack__slack_read_thread",
    "mcp__slack__slack_read_channel",
    "mcp__slack__slack_search_public",
    "mcp__slack__slack_search_public_and_private",
    "queryable_search",
})
_SENSITIVE = re.compile(
    r"credential|secret|token|password|private[-_]?key|api[-_]?key|"
    r"(?:^|[/._-])(?:auth|env|pii|roster)(?:[/._-]|$)|(?:^|/)\.env(?:[./_-]|$)|"
    r"(?:^|/)(?:people|employees|customers|sessions|memories|logs)(?:[./_-]|$)|"
    r"(?:^|/)(?:\.git|\.ssh|\.aws)(?:/|$)|"
    r"@[a-z0-9.-]+|\b\d{2,3}[- ]\d{3,4}[- ]\d{4}\b|"
    r"sk[-_]|xox[baprs]-|gh[pousr]_",
    re.IGNORECASE,
)


class ToolExecutionCollector:
    """One run, shared safely by its context-propagated dispatch threads."""

    def __init__(self):
        self._records = []
        self._lock = threading.Lock()
        self._closed = False

    @contextmanager
    def activate(self):
        token = _CURRENT_COLLECTOR.set(self)
        try:
            yield self
        finally:
            # A timed-out worker retaining a copied Context cannot add results
            # after the run ends, nor contaminate the next run's collector.
            with self._lock:
                self._closed = True
            _CURRENT_COLLECTOR.reset(token)

    def record(self, name, arguments, result, tool_call_id=None):
        with self._lock:
            if not self._closed:
                self._records.append({
                    "tool_call_id": tool_call_id or f"execution-{uuid.uuid4().hex}",
                    "name": name,
                    "arguments": deepcopy(arguments),
                    "result": deepcopy(result),
                })

    def snapshot(self):
        with self._lock:
            return deepcopy(self._records)


@contextmanager
def execution_call_scope(tool_call_id=None):
    token = _CURRENT_CALL_ID.set(tool_call_id or f"execution-{uuid.uuid4().hex}")
    try:
        yield
    finally:
        _CURRENT_CALL_ID.reset(token)


def record_tool_execution(name, arguments, result, tool_call_id=None):
    """Called only after a real registry handler returned, before transforms."""
    collector = _CURRENT_COLLECTOR.get()
    if collector is not None:
        collector.record(name, arguments, result, tool_call_id or _CURRENT_CALL_ID.get())


def strip_model_receipt_lines(text):
    """Remove complete receipt lines, never inline mentions or ordinary prose."""
    if not isinstance(text, str):
        return ""
    return "\n".join(
        line for line in text.splitlines()
        if not re.match(
            r"^\s*(?:[-*]\s+)?(?:\*\*|__)?근거(?:\*\*|__)?\s*:",
            line,
        )
    ).strip()


def _result_dict(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("error") or value.get("success") is False or value.get("failed") is True:
        return None
    if value.get("status") in ("error", "failed", "blocked", "pending", "running"):
        return None
    return value


def _safe_relative(path, workspace_root, petasos_root):
    if not isinstance(path, str) or not path or _SENSITIVE.search(path):
        return None
    if any(ord(char) < 32 for char in path) or not re.fullmatch(r"[\w./ -]+", path):
        return None
    # Relative file arguments are resolved against an explicitly bounded root,
    # not this formatter's process cwd (which may be another profile/worktree).
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve()
        for root, label in ((workspace_root, "workspace"), (petasos_root, "petasos")):
            try:
                relative = resolved.relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                continue
            if relative != "." and len(relative) <= 180 and not _SENSITIVE.search(relative):
                return label, relative
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def _unnumbered(content):
    return re.sub(r"(?m)^\d+\|", "", content)


def _frontmatter(content):
    numbered = re.match(r"^(\d+)\|", content)
    if numbered and numbered[1] != "1":
        return {}
    text = _unnumbered(content)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        match = re.fullmatch(r"(id|type|status):\s*([\w-]+)\s*", line)
        if match:
            if match[1] in fields:
                return {}
            fields[match[1]] = match[2]
    return {}


def _brain_pointer(content, expected_id=None):
    fields = _frontmatter(content)
    record_id, status = fields.get("id", ""), fields.get("status", "")
    if re.fullmatch(_RECORD_ID, record_id) and status in _STATUSES:
        if expected_id is None or record_id == expected_id:
            return f"brain {record_id}({status})"
    return None


def _vat_command(command, workspace_root, workdir=None):
    # No shell composition, expansions, redirects, aliases, interpreter
    # wrappers, environment assignments or executable lookalikes.
    if (not isinstance(command, str) or any(ord(char) < 32 for char in command)
            or re.search(r"[;|&<>`$\\]", command)):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words or words.pop(0) != "vat":
        return None
    if words[:1] == ["--workspace"]:
        if len(words) < 2 or Path(words[1]).resolve() != Path(workspace_root).resolve():
            return None
        words = words[2:]
    elif not isinstance(workdir, str) or Path(workdir).resolve() != Path(workspace_root).resolve():
        return None
    if len(words) < 3 or words[0] != "brain" or words[1] not in {"query", "read"}:
        return None
    if words[1] == "read":
        return ("read", words[2]) if len(words) == 3 and re.fullmatch(_RECORD_ID, words[2]) else None
    terms = []
    limit_seen = False
    index = 2
    while index < len(words):
        word = words[index]
        if word == "--limit":
            if (limit_seen or index + 1 >= len(words)
                    or not re.fullmatch(r"[0-9]+", words[index + 1])
                    or not words[index + 1].strip("0")):
                return None
            limit_seen = True
            index += 2
            continue
        if not word or word.startswith("-") or _SENSITIVE.search(word):
            return None
        terms.append(word)
        index += 1
    return ("query", " ".join(terms)) if terms else None


def _vat_pointers(arguments, result, workspace_root):
    parsed = _vat_command(arguments.get("command"), workspace_root, arguments.get("workdir"))
    if parsed is None or type(result.get("exit_code")) is not int or result["exit_code"] != 0:
        return []
    if arguments.get("background") or not isinstance(result.get("output"), str):
        return []
    operation, target = parsed
    output = result["output"]
    if operation == "read":
        pointer = _brain_pointer(output, target)
        return [pointer] if pointer else []
    # Actual vat index format supplied by Head: INFO row, indented record
    # path, optional snippet, and count footer. Never scan arbitrary JSON IDs.
    if not re.search(r"(?m)^\d+ results?\. Open the records themselves; this is an index, not an answer\.\s*$", output):
        return []
    pointers = []
    lines = output.splitlines()
    for index, line in enumerate(lines[:-1]):
        match = re.fullmatch(r"INFO\s+(" + _RECORD_ID + r")\s+.+?\s+([a-z]+)\s*", line)
        if not match or match[2] not in _STATUSES:
            continue
        path = lines[index + 1].strip()
        if (_SENSITIVE.search(path) or not re.fullmatch(r"[\w/-]+\.md", path)
                or not re.match(r"(?:decisions|facts|goals|gaps|definitions|policies)/" + re.escape(match[1]) + r"(?:-|\.)", path)
                or ".." in path):
            continue
        pointers.append(f"brain {match[1]}({match[2]})")
    return pointers


def _file_pointers(arguments, result, workspace_root, petasos_root):
    if (not isinstance(result.get("content"), str) or result.get("is_binary")
            or result.get("is_image") or type(result.get("total_lines")) is not int):
        return []
    # This path is supplied by the actual read handler, not guessed from the
    # formatter's process cwd (which may differ from the tool task cwd).
    bounded = _safe_relative(result.get("resolved_path") or arguments.get("path"), workspace_root, petasos_root)
    if bounded is None:
        return []
    label, relative = bounded
    content = result["content"]
    pointers = []
    if label == "workspace" and relative.startswith("brain/"):
        match = re.match(r"(" + _RECORD_ID + r")(?:-|\.)", Path(relative).name)
        if match:
            pointer = _brain_pointer(content, match[1])
            if pointer:
                pointers.append(pointer)
    if label == "workspace" and relative.startswith("wiki/entities/") and relative.endswith(".md"):
        fields = _frontmatter(content)
        entity_id = fields.get("id", "")
        if (entity_id == Path(relative).stem and fields.get("type") in {"project", "team", "system", "organization", "concept"}
                and re.fullmatch(r"[a-z][a-z0-9-]{1,79}", entity_id)
                and not _SENSITIVE.search(entity_id)):
            pointers.append(f"wiki {entity_id}")
    pointers.append(relative)
    # A file body, even one spelling 'permalink:', is not Slack API metadata.
    # Only allowlisted Slack/Queryable tool results can promote those URLs.
    return pointers


def _rendered_slack_search_pointer(raw):
    """Real Slack MCP search wrapper: only first-result metadata before Text."""
    from urllib.parse import parse_qs, urlsplit, urlunsplit
    outer = _result_dict(raw)
    if outer is None or outer.get("isError") is True or outer.get("ok") is False:
        return []
    inner = _result_dict(outer.get("result"))
    if inner is None or inner.get("isError") is True or inner.get("ok") is False:
        return []
    text = inner.get("results")
    if not isinstance(text, str) or not text.startswith("# Search Results for:"):
        return []
    # Never scan user-authored Text, even if it imitates tool metadata.
    if "\nText:" not in text:
        return []
    metadata = text.split("\nText:", 1)[0]
    if not re.search(r"(?m)^### Result 1 of \d+$", metadata):
        return []
    channel = re.search(r"(?m)^Channel: [^\n]+ \(ID: ([A-Z0-9]+)\)$", metadata)
    stamp = re.search(r"(?m)^Message_ts: (\d{10}\.\d{6})$", metadata)
    link = re.search(r"(?m)^Permalink: \[link\]\(([^\s)]+)\)$", metadata)
    if not (channel and stamp and link):
        return []
    try:
        parts = urlsplit(link[1])
        query = parse_qs(parts.query, strict_parsing=True)
    except ValueError:
        return []
    if parts.fragment or any(key not in {"thread_ts", "cid"} for key in query):
        return []
    if query.get("cid", [channel[1]]) != [channel[1]] or query.get("thread_ts", [stamp[1]]) != [stamp[1]]:
        return []
    canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    expected_path = f"/archives/{channel[1]}/p{stamp[1].replace('.', '')}"
    if parts.path != expected_path or not _SLACK.fullmatch(canonical) or _SENSITIVE.search(canonical):
        return []
    return [canonical]


def _slack_result_pointers(raw):
    """Explicit URL fields from allowlisted read-only structured results only.

    String bodies, stdout, arbitrary JSON embedded in text and model receipts
    are never scanned. All querystrings/fragments are rejected, including safe
    ones, so credentials cannot be disclosed through URL suffixes.
    """
    try:
        result = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    pointers = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if (_result_dict(value) is None or value.get("isError") is True
                    or value.get("ok") is False):
                return
            for key, item in value.items():
                if key in {"permalink", "url", "source_url"}:
                    if isinstance(item, str) and _SLACK.fullmatch(item) and not _SENSITIVE.search(item):
                        pointers.append(item)
                elif key in {"results", "matches", "hits", "messages", "threads", "data",
                             "items", "documents", "sources", "metadata", "structuredContent", "content"}:
                    visit(item)

    visit(result)
    return pointers


def format_receipt_response(text, executions, *, workspace_root=_WORKSPACE, petasos_root=_PETASOS):
    """Shared gateway/CLI finalizer. Missing collector always fails closed."""
    clean = strip_model_receipt_lines(text)
    if not clean or not isinstance(executions, (list, tuple)):
        return clean
    pointers = []
    seen = set()
    for execution in executions:
        if not isinstance(execution, dict) or not execution.get("tool_call_id"):
            continue
        arguments = execution.get("arguments")
        if not isinstance(arguments, dict):
            continue
        name = execution.get("name")
        if name in _SLACK_READ_TOOLS:
            candidates = _slack_result_pointers(execution.get("result"))
            if name in {"mcp__slack__slack_search_public", "mcp__slack__slack_search_public_and_private"}:
                candidates += _rendered_slack_search_pointer(execution.get("result"))
        else:
            result = _result_dict(execution.get("result"))
            if result is None:
                continue
            if name == "terminal":
                candidates = _vat_pointers(arguments, result, workspace_root)
            elif name == "read_file":
                candidates = _file_pointers(arguments, result, workspace_root, petasos_root)
            else:
                continue
        for pointer in candidates:
            if pointer not in seen:
                seen.add(pointer)
                pointers.append(pointer)
    if not pointers:
        return clean
    receipt = "근거: " + ", ".join(pointers[:5])
    if len(pointers) > 5:
        receipt += f" 외 {len(pointers) - 5}"
    # Runtime footers can follow the model's human-check line. Keep that line
    # immediately before the code-owned receipt, without inventing a person.
    lines = clean.splitlines()
    confirmations = [line for line in lines if re.match(r"^\s*확인할 사람:", line)]
    if confirmations:
        body = [line for line in lines if not re.match(r"^\s*확인할 사람:", line)]
        clean = "\n".join(body).rstrip() + "\n" + "\n".join(confirmations)
    return clean + "\n" + receipt
