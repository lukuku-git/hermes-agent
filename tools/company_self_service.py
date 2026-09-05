"""Slack employee self-service assets and cron wrappers.

The model-facing schema deliberately contains no principal/author/approver fields.
Identity is read only from gateway-bound ContextVars; process environment values are
never accepted as authentication. Employee assets remain outside the shared memory
and skills directories until an explicit core-only company promotion.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error

MAX_ASSETS_PER_KIND_SCOPE = 50
MAX_MEMORY_CHARS = 4000
MAX_SKILL_CHARS = 24000
MAX_PROMPT_CHARS = 6000
MAX_ACTIVE_CRON = 5
SAFE_CRON_TOOLSETS = frozenset({"web", "search", "vision"})
# Execution-time allowlist for employee-owned cron jobs. Toolset filtering is
# only the schema boundary; this name-level gate is the authorization boundary
# and also covers deferred/tool_call dispatch and future core-tool additions.
SAFE_CRON_TOOLS = frozenset({"web_search", "web_extract", "vision_analyze"})
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SPOOF_FIELDS = frozenset({"owner", "owner_id", "user_id", "author", "author_id", "approver", "approver_id", "principal"})


def _db_path() -> Path:
    return get_hermes_home() / "company_self_service.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PermissionError("self-service store must not be a symbolic link")
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS assets (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL CHECK(kind IN ('memory','skill')),
      scope TEXT NOT NULL CHECK(scope IN ('personal','team','company')),
      owner TEXT NOT NULL,
      name TEXT NOT NULL,
      version INTEGER NOT NULL,
      content TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('candidate','approved','rejected','superseded')),
      principal TEXT NOT NULL, channel TEXT NOT NULL, thread TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(kind, scope, owner, name, version)
    );
    CREATE INDEX IF NOT EXISTS assets_lookup ON assets(kind,scope,owner,name,status,version);
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event TEXT NOT NULL, asset_id INTEGER, kind TEXT NOT NULL,
      scope TEXT, owner TEXT, principal TEXT NOT NULL, channel TEXT NOT NULL,
      thread TEXT NOT NULL, created_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
    );
    CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN
      SELECT RAISE(ABORT, 'audit is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN
      SELECT RAISE(ABORT, 'audit is append-only'); END;
    """)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return db


def _principal(require_thread: bool = True) -> dict[str, str]:
    from gateway.session_context import trusted_session_identity
    ident = trusted_session_identity()
    required = ("platform", "user_id", "chat_id")
    if not ident or ident.get("platform", "").lower() != "slack" or any(not ident.get(k) for k in required):
        raise PermissionError("Employee mutation requires a complete trusted Slack turn context")
    root = ident.get("thread_id") or ident.get("message_id")
    if require_thread and not root:
        raise PermissionError("Employee mutation requires trusted Slack thread/message provenance")
    # All persisted provenance uses one canonical root. Root messages have no
    # thread timestamp, so their trusted message id is the root.
    ident["thread_id"] = str(root or "")
    return ident


def _owner_for(scope: str, p: dict[str, str]) -> str:
    if scope == "personal":
        return p["user_id"]
    if scope == "team":
        return p["chat_id"]
    if scope == "company":
        return "company"
    raise ValueError("scope must be personal, team, or company")


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _audit(db, event: str, p: dict[str, str], *, kind: str, scope: str = "", owner: str = "", asset_id=None, detail=""):
    db.execute(
        "INSERT INTO audit(event,asset_id,kind,scope,owner,principal,channel,thread,created_at,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (event, asset_id, kind, scope, owner, p["user_id"], p["chat_id"], p["thread_id"], _now(), detail),
    )


def _asset_write(kind: str, action: str, args: dict[str, Any], p: dict[str, str]) -> dict[str, Any]:
    scope = str(args.get("scope") or "personal").lower()
    owner = _owner_for(scope, p)
    name = str(args.get("name") or "default").strip().lower()
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError("name must be 1-64 lowercase letters, digits, hyphens, or underscores")
    content = args.get("content")
    limit = MAX_MEMORY_CHARS if kind == "memory" else MAX_SKILL_CHARS
    if action == "put":
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content is required")
        if len(content) > limit:
            raise ValueError(f"content exceeds the {limit}-character limit")
        if kind == "memory":
            from tools.memory_tool import _scan_memory_content
            if _scan_memory_content(content):
                raise ValueError("content failed safety validation")
        else:
            # Reuse the shared skill validator without ever writing to the global tree.
            from tools.skill_manager_tool import _validate_frontmatter
            valid = _validate_frontmatter(content, new_skill=True)
            if valid:
                raise ValueError("skill content failed validation")
            import yaml
            declared = yaml.safe_load(content.lstrip("\ufeff").split("---", 2)[1])
            if not isinstance(declared, dict) or str(declared.get("name", "")).strip().lower() != name:
                raise ValueError("skill frontmatter name must match the asset name")
            from tools.threat_patterns import first_threat_message
            if first_threat_message(content, scope="strict"):
                raise ValueError("skill content failed safety validation")
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT * FROM assets WHERE kind=? AND scope=? AND owner=? AND name=? AND status IN ('approved','candidate') ORDER BY version DESC LIMIT 1",
            (kind, scope, owner, name),
        ).fetchone()
        max_version = db.execute(
            "SELECT COALESCE(MAX(version),0) FROM assets WHERE kind=? AND scope=? AND owner=? AND name=?",
            (kind, scope, owner, name),
        ).fetchone()[0]
        if action == "put":
            if current is None:
                count = db.execute(
                    "SELECT COUNT(DISTINCT name) FROM assets WHERE kind=? AND scope=? AND owner=? AND status IN ('approved','candidate')",
                    (kind, scope, owner),
                ).fetchone()[0]
                if count >= MAX_ASSETS_PER_KIND_SCOPE:
                    raise ValueError("asset quota reached")
            version = max_version + 1
            status = "candidate" if scope == "company" else "approved"
            if current and scope != "company":
                db.execute("UPDATE assets SET status='superseded' WHERE id=?", (current["id"],))
            elif scope == "company":
                # A candidate never deactivates the live company version.
                db.execute(
                    "UPDATE assets SET status='superseded' WHERE kind=? AND scope='company' AND name=? AND status='candidate' AND principal=?",
                    (kind, name, p["user_id"]),
                )
            cur = db.execute(
                "INSERT INTO assets(kind,scope,owner,name,version,content,status,principal,channel,thread,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (kind, scope, owner, name, version, content, status, p["user_id"], p["chat_id"], p["thread_id"], _now()),
            )
            _audit(db, "put", p, kind=kind, scope=scope, owner=owner, asset_id=cur.lastrowid, detail=f"version={version};status={status}")
            db.execute("COMMIT")
            return {"success": True, "id": cur.lastrowid, "name": name, "scope": scope, "version": version, "status": status}
        if action == "delete":
            if scope == "company":
                # ``current`` may be another employee's newer candidate (or the
                # live approved version).  Deletion is deliberately scoped to
                # the caller's own latest candidate instead.
                current = db.execute(
                    "SELECT * FROM assets WHERE kind=? AND scope='company' AND owner='company' AND name=? AND status='candidate' AND principal=? ORDER BY version DESC LIMIT 1",
                    (kind, name, p["user_id"]),
                ).fetchone()
                if not current:
                    raise PermissionError("employees may reject only their own company candidate")
            elif not current:
                raise LookupError("asset not found")
            db.execute("UPDATE assets SET status='rejected' WHERE id=?", (current["id"],))
            _audit(db, "delete", p, kind=kind, scope=scope, owner=owner, asset_id=current["id"])
            db.execute("COMMIT")
            return {"success": True, "name": name, "scope": scope, "status": "rejected"}
        if action == "rollback":
            version = args.get("version")
            if not isinstance(version, int) or version < 1:
                raise ValueError("version is required for rollback")
            if scope == "company":
                # Admin-promoted history is company-visible even after a newer
                # approval supersedes it.  All never-promoted history remains
                # private to its originating user.
                historical = db.execute(
                    "SELECT * FROM assets WHERE kind=? AND scope='company' AND owner='company' AND name=? AND version=? AND (principal=? OR EXISTS (SELECT 1 FROM audit WHERE audit.asset_id=assets.id AND audit.event='promote'))",
                    (kind, name, version, p["user_id"]),
                ).fetchone()
            else:
                historical = db.execute(
                    "SELECT * FROM assets WHERE kind=? AND scope=? AND owner=? AND name=? AND version=?",
                    (kind, scope, owner, name, version),
                ).fetchone()
            if not historical:
                raise LookupError("requested version not found")
            new_version = max_version + 1
            status = "candidate" if scope == "company" else "approved"
            if current and scope != "company":
                db.execute("UPDATE assets SET status='superseded' WHERE id=?", (current["id"],))
            elif scope == "company":
                # Match put semantics: one active candidate per employee/name.
                db.execute(
                    "UPDATE assets SET status='superseded' WHERE kind=? AND scope='company' AND name=? AND status='candidate' AND principal=?",
                    (kind, name, p["user_id"]),
                )
            cur = db.execute(
                "INSERT INTO assets(kind,scope,owner,name,version,content,status,principal,channel,thread,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (kind, scope, owner, name, new_version, historical["content"], status, p["user_id"], p["chat_id"], p["thread_id"], _now()),
            )
            _audit(db, "rollback", p, kind=kind, scope=scope, owner=owner, asset_id=cur.lastrowid, detail=f"from={version};version={new_version}")
            db.execute("COMMIT")
            return {"success": True, "id": cur.lastrowid, "name": name, "scope": scope, "version": new_version, "status": status}
        raise ValueError("unsupported asset mutation")
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def _asset_read(kind: str, action: str, args: dict[str, Any], p: dict[str, str]) -> dict[str, Any]:
    scope = str(args.get("scope") or "personal").lower()
    owner = _owner_for(scope, p)
    params: list[Any] = [kind, scope, owner]
    visibility = "status='approved'"
    if scope == "company":
        visibility = "(status='approved' OR (status='candidate' AND principal=?))"
        params.append(p["user_id"])
    db = _connect()
    try:
        if action == "list":
            rows = db.execute(
                f"SELECT id,name,version,status,created_at FROM assets WHERE kind=? AND scope=? AND owner=? AND {visibility} ORDER BY name,version DESC",
                params,
            ).fetchall()
            seen = set()
            items = []
            for row in rows:
                if row["name"] in seen:
                    continue
                seen.add(row["name"])
                items.append(dict(row))
            return {"success": True, "items": items}
        name = str(args.get("name") or "default").strip().lower()
        row = db.execute(
            f"SELECT id,name,version,status,content,created_at FROM assets WHERE kind=? AND scope=? AND owner=? AND {visibility} AND name=? ORDER BY version DESC LIMIT 1",
            [*params, name],
        ).fetchone()
        if not row:
            raise LookupError("asset not found")
        return {"success": True, **dict(row)}
    finally:
        db.close()


def promote_company_asset(asset_id: int) -> dict[str, Any]:
    """Promote a company candidate using only trusted gateway admin context."""
    return _admin_company_asset_mutation("promote", asset_id)


def reject_company_asset(asset_id: int) -> dict[str, Any]:
    """Reject a company candidate using only trusted gateway admin context."""
    return _admin_company_asset_mutation("reject", asset_id)


def _admin_company_asset_mutation(action: str, asset_id: int) -> dict[str, Any]:
    from gateway.session_context import trusted_session_identity, trusted_session_is_admin

    admin = trusted_session_identity()
    if (
        not trusted_session_is_admin()
        or not admin
        or admin.get("platform", "").lower() != "slack"
        or not admin.get("user_id")
        or not admin.get("chat_id")
    ):
        raise PermissionError("trusted Slack administrator context is required")
    root = admin.get("thread_id") or admin.get("message_id")
    if not root:
        raise PermissionError("trusted Slack administrator provenance is required")
    admin["thread_id"] = str(root)
    if action not in {"promote", "reject"}:
        raise ValueError("action must be promote or reject")

    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM assets WHERE id=? AND scope='company' AND status='candidate'", (asset_id,)).fetchone()
        if not row:
            raise LookupError("company candidate not found")
        status = "approved" if action == "promote" else "rejected"
        if status == "approved":
            db.execute("UPDATE assets SET status='superseded' WHERE kind=? AND scope='company' AND owner='company' AND name=? AND status='approved'", (row["kind"], row["name"]))
        db.execute("UPDATE assets SET status=? WHERE id=?", (status, asset_id))
        _audit(db, action, admin, kind=row["kind"], scope="company", owner="company", asset_id=asset_id)
        db.execute("COMMIT")
        return {"success": True, "id": asset_id, "status": status}
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def company_asset_admin(args: dict[str, Any]) -> str:
    try:
        action = str(args.get("action") or "").lower()
        asset_id = int(args.get("asset_id"))
        result = _admin_company_asset_mutation(action, asset_id)
        return json.dumps(result, ensure_ascii=False)
    except (PermissionError, ValueError, LookupError, TypeError) as exc:
        return tool_error(str(exc), success=False)


def _schedule_allowed(value: str) -> bool:
    from cron.jobs import parse_schedule
    parsed = parse_schedule(value)
    if parsed.get("kind") == "interval":
        return int(parsed.get("minutes", 0)) >= 15
    if parsed.get("kind") == "once":
        return True
    if parsed.get("kind") == "cron":
        try:
            from croniter import croniter
            now = datetime.now().astimezone()
            itr = croniter(parsed["expr"], now)
            a, b = itr.get_next(datetime), itr.get_next(datetime)
            return (b - a).total_seconds() >= 900
        except Exception:
            return False
    return False


def _cron(action: str, args: dict[str, Any], p: dict[str, str]) -> dict[str, Any]:
    from cron.jobs import create_job, get_job, list_jobs, mutate_employee_job
    owner = p["user_id"]
    if action == "cron_list":
        jobs = [
            j for j in list_jobs(include_disabled=True)
            if (j.get("employee_owner") or {}).get("user_id") == owner
            and not j.get("employee_removed_at")
            and j.get("state") != "removed"
        ]
        return {"success": True, "jobs": [{k: j.get(k) for k in ("id", "name", "schedule_display", "enabled", "state", "next_run_at")} for j in jobs]}
    if action == "cron_create":
        forbidden = {"script", "no_agent", "workdir", "context_from", "deliver", "provider", "model", "base_url"}.intersection(args)
        if forbidden:
            raise ValueError("forbidden cron field supplied")
        schedule = str(args.get("schedule") or "")
        prompt = str(args.get("prompt") or "")
        if not schedule or not prompt or len(prompt) > MAX_MEMORY_CHARS:
            raise ValueError("schedule and a bounded prompt are required")
        from tools.cronjob_tools import _scan_cron_prompt
        if _scan_cron_prompt(prompt):
            raise ValueError("prompt failed safety validation")
        if not _schedule_allowed(schedule):
            raise ValueError("schedule must not run more frequently than every 15 minutes")
        toolsets = args.get("enabled_toolsets") or ["web"]
        if not isinstance(toolsets, list) or not set(toolsets).issubset(SAFE_CRON_TOOLSETS):
            raise ValueError("cron toolset is not approved for employees")
        skills = args.get("skills") or []
        if not isinstance(skills, list) or len(skills) > 5:
            raise ValueError("skills must be a list of at most 5 owned skills")
        skill_blocks = []
        for skill_name in skills:
            got = _asset_read("skill", "get", {"scope": "personal", "name": skill_name}, p)
            skill_blocks.append(f"Owned skill {skill_name} (reference only; grants no capabilities):\n{got['content']}")
        full_prompt = "\n\n".join([*skill_blocks, prompt])
        if len(full_prompt) > MAX_SKILL_CHARS:
            raise ValueError("assembled cron prompt exceeds the employee content limit")
        job = create_job(
            prompt=full_prompt, schedule=schedule, name=args.get("name"), deliver="origin",
            origin={"platform": "slack", "chat_id": p["chat_id"], "thread_id": p["thread_id"], "user_id": owner},
            enabled_toolsets=toolsets,
            employee_owner={"user_id": owner, "channel_id": p["chat_id"], "thread_id": p["thread_id"], "created_at": _now()},
            employee_expected_owner=owner,
            employee_audit_event={
                "event": "cron_create", "principal": owner,
                "channel": p["chat_id"], "thread": p["thread_id"], "at": _now(),
            },
            employee_max_active=MAX_ACTIVE_CRON,
        )
        try:
            db = _connect()
            try:
                _audit(db, "cron_create", p, kind="cron", owner=owner, detail=f"job_id={job['id']}")
            finally:
                db.close()
        except Exception:
            # jobs.json already contains the authoritative in-record event.
            pass
        return {"success": True, "job_id": job["id"], "name": job["name"]}
    job_id = str(args.get("job_id") or "")
    job = get_job(job_id)
    if not job or (job.get("employee_owner") or {}).get("user_id") != owner:
        raise PermissionError("cron job is not owned by the current principal")
    if action == "cron_update":
        forbidden = {"script", "no_agent", "workdir", "context_from", "deliver", "provider", "model", "base_url", "skills"}.intersection(args)
        if forbidden:
            raise ValueError("forbidden cron field supplied")
        updates = {}
        for key in ("name", "prompt"):
            if key in args:
                updates[key] = args[key]
        if "prompt" in updates:
            if not isinstance(updates["prompt"], str) or len(updates["prompt"]) > MAX_MEMORY_CHARS:
                raise ValueError("prompt exceeds the employee content limit")
            from tools.cronjob_tools import _scan_cron_prompt
            if _scan_cron_prompt(updates["prompt"]):
                raise ValueError("prompt failed safety validation")
        if "schedule" in args:
            if not _schedule_allowed(str(args["schedule"])):
                raise ValueError("schedule must not run more frequently than every 15 minutes")
            updates["schedule"] = args["schedule"]
        if "enabled_toolsets" in args:
            ts = args["enabled_toolsets"]
            if not isinstance(ts, list) or not set(ts).issubset(SAFE_CRON_TOOLSETS):
                raise ValueError("cron toolset is not approved for employees")
            updates["enabled_toolsets"] = ts
        audit_event = {
            "event": action, "principal": owner, "channel": p["chat_id"],
            "thread": p["thread_id"], "at": _now(),
        }
        updated = mutate_employee_job(
            job_id, expected_owner=owner, action="update", updates=updates,
            audit_event=audit_event, max_active=MAX_ACTIVE_CRON,
        )
        if updated is None:
            raise LookupError("cron job disappeared before mutation")
        result = {"success": True, "job_id": job_id, "name": updated.get("name")}
    elif action == "cron_pause":
        updated = mutate_employee_job(
            job_id, expected_owner=owner, action="pause",
            audit_event={"event": action, "principal": owner, "channel": p["chat_id"], "thread": p["thread_id"], "at": _now()},
            max_active=MAX_ACTIVE_CRON,
        )
        if updated is None:
            raise LookupError("cron job disappeared before mutation")
        result = {"success": True, "job_id": job_id, "state": "paused"}
    elif action == "cron_resume":
        updated = mutate_employee_job(
            job_id, expected_owner=owner, action="resume",
            audit_event={"event": action, "principal": owner, "channel": p["chat_id"], "thread": p["thread_id"], "at": _now()},
            max_active=MAX_ACTIVE_CRON,
        )
        if updated is None:
            raise LookupError("cron job disappeared before mutation")
        result = {"success": True, "job_id": job_id, "state": "scheduled"}
    elif action == "cron_remove":
        updated = mutate_employee_job(
            job_id, expected_owner=owner, action="remove",
            audit_event={"event": action, "principal": owner, "channel": p["chat_id"], "thread": p["thread_id"], "at": _now()},
            max_active=MAX_ACTIVE_CRON,
        )
        if updated is None:
            raise LookupError("cron job disappeared before mutation")
        result = {"success": True, "job_id": job_id, "removed": True}
    else:
        raise ValueError("unsupported cron action")
    try:
        db = _connect()
        try:
            _audit(db, action, p, kind="cron", owner=owner, detail=f"job_id={job_id}")
        finally:
            db.close()
    except Exception:
        # jobs.json already contains the authoritative in-record event.
        pass
    return result


def company_self_service(args: dict[str, Any]) -> str:
    p = None
    action = str(args.get("action") or "").lower()
    try:
        spoofed = _SPOOF_FIELDS.intersection(args)
        if spoofed:
            return tool_error("principal fields are not accepted", success=False)
        p = _principal()
        if action.startswith("memory_") or action.startswith("skill_"):
            kind, op = action.split("_", 1)
            if op in {"put", "delete", "rollback"}:
                result = _asset_write(kind, op, args, p)
            elif op in {"list", "get"}:
                result = _asset_read(kind, op, args, p)
            else:
                raise ValueError("unsupported asset action")
        elif action.startswith("cron_"):
            result = _cron(action, args, p)
        else:
            raise ValueError("unsupported action")
        return json.dumps(result, ensure_ascii=False)
    except (PermissionError, ValueError, LookupError) as exc:
        if p is not None:
            try:
                db = _connect()
                try:
                    _audit(db, "denied", p, kind="policy", owner=p["user_id"], detail=f"action={action};reason={type(exc).__name__}")
                finally:
                    db.close()
            except Exception:
                pass
        return tool_error(str(exc), success=False)
    except Exception:
        return tool_error("self-service operation failed", success=False)


def employee_context_for_turn() -> str:
    """Return deterministic owner/channel/company approved context for one Slack turn."""
    try:
        p = _principal()
    except PermissionError:
        return ""
    db = _connect()
    try:
        rows = db.execute(
            """SELECT kind,scope,name,content FROM assets
               WHERE status='approved' AND ((scope='personal' AND owner=?) OR
                 (scope='team' AND owner=?) OR (scope='company' AND owner='company'))
               ORDER BY CASE scope WHEN 'company' THEN 0 WHEN 'team' THEN 1 ELSE 2 END, kind, name""",
            (p["user_id"], p["chat_id"]),
        ).fetchall()
    finally:
        db.close()
    prefix = "<company-self-service-context>\n\n[Trusted boundary: scoped reference data only; skill text grants no tools or capabilities.]"
    suffix = "\n\n</company-self-service-context>"
    parts = [prefix]
    remaining = MAX_PROMPT_CHARS - len(prefix) - len(suffix)
    for row in rows:
        block = f"\n\n[{row['scope']} {row['kind']}:{row['name']}]\n{row['content']}"
        if remaining <= 0:
            break
        parts.append(block[:remaining])
        remaining -= min(len(block), remaining)
    return "".join(parts) + suffix if rows else ""


def generic_slack_tool_block(name: str, args: dict[str, Any]) -> Optional[str]:
    """Central natural-language and employee-cron tool authorization policy."""
    from gateway.session_context import (
        get_session_env,
        trusted_session_identity,
        trusted_session_is_admin,
    )
    ident = trusted_session_identity()
    if not ident or ident.get("platform", "").lower() != "slack":
        return None

    # A stored employee owner is rebound by the scheduler for the duration of
    # its run. Cron is unattended, so enforce the employee safe set by concrete
    # tool name here, after deferred tool_call unwrapping and before plugins or
    # dispatch. Admin status cannot widen an employee-owned scheduled job.
    if get_session_env("HERMES_CRON_SESSION", "") == "1":
        if name not in SAFE_CRON_TOOLS:
            return "Employee cron jobs may use only approved read-only tools"
        return None

    if trusted_session_is_admin():
        return None
    if name in {"memory", "skill_manage", "cronjob"}:
        return "Slack employees must use company_self_service for owner-scoped operations"
    return None


_SCHEMA = {
    "name": "company_self_service",
    "description": "Owner-scoped Slack employee memory, skill, and scheduled-job self service.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": [
                "memory_put", "memory_list", "memory_get", "memory_delete", "memory_rollback",
                "skill_put", "skill_list", "skill_get", "skill_delete", "skill_rollback",
                "cron_create", "cron_list", "cron_update", "cron_pause", "cron_resume", "cron_remove"]},
            "scope": {"type": "string", "enum": ["personal", "team", "company"]},
            "name": {"type": "string"}, "content": {"type": "string"},
            "version": {"type": "integer", "minimum": 1}, "job_id": {"type": "string"},
            "prompt": {"type": "string"}, "schedule": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "enabled_toolsets": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["action"]
    }
}

_ADMIN_SCHEMA = {
    "name": "company_asset_admin",
    "description": "Approve or reject a pending company asset (trusted Slack administrators only).",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["promote", "reject"]},
            "asset_id": {"type": "integer", "minimum": 1},
        },
        "required": ["action", "asset_id"],
    },
}


def _is_admin_turn() -> bool:
    from gateway.session_context import trusted_session_identity, trusted_session_is_admin

    ident = trusted_session_identity()
    return bool(
        trusted_session_is_admin()
        and ident
        and ident.get("platform", "").lower() == "slack"
        and ident.get("user_id")
    )


def _is_employee_turn() -> bool:
    try:
        _principal()
        return True
    except PermissionError:
        return False


# Availability is authorization-context dependent and must be recomputed for
# every turn rather than inherited from the registry's normal probe TTL cache.
_is_admin_turn._hermes_no_cache = True
_is_employee_turn._hermes_no_cache = True

registry.register(
    name="company_self_service", toolset="company_self_service", schema=_SCHEMA,
    handler=lambda args, **kw: company_self_service(args),
    check_fn=_is_employee_turn, emoji="🏢",
)
registry.register(
    name="company_asset_admin", toolset="company_self_service", schema=_ADMIN_SCHEMA,
    handler=lambda args, **kw: company_asset_admin(args),
    check_fn=_is_admin_turn, emoji="🛡️",
)
