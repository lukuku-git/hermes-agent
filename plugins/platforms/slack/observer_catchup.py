"""Observer durable Slack history bootstrap and downtime catch-up.

Installed alongside the vendored Slack adapter by
``scripts/install.py::apply_vendor_patches`` (see ``OBSERVER_CATCHUP_*``
there), which also patches the adapter to import this module and call
``run_catchup`` after a successful connect/reconnect. Kept free of
``slack_sdk``/``slack_bolt`` imports so it is fully unit-testable with a
fake, network-free client.

The adapter is the only piece that dedups live delivery against catch-up
delivery (both funnel through ``SlackAdapter._handle_slack_message``, which
claims each ``(workspace, channel, ts)`` exactly once via ``claim_message``
/ ``complete_claim`` / ``release_claim`` below). This module never claims on
its own — it only fetches, orders, and hands messages to the caller-supplied
``dispatch``, then advances a checkpoint once ``dispatch`` returns without
raising. The Slack replay callback must wait for the actual background turn
and its owner/drain chain, not merely Base's immediate handoff return. This
is a lifecycle completion boundary, not proof of business/notification success.
The adapter must also inspect the event's trusted handler outcome because Base
logs and swallows handler exceptions before the owner finishes.

Claims are crash-safe: ``claim_message`` records a *pending* row, not a
permanent one. A caller that finishes must call ``complete_claim`` (making
the dedup permanent) or, on a caught failure, ``release_claim`` (freeing it
for immediate retry). A claim nobody ever resolves — the process died
mid-dispatch — is not stuck forever either: it is reclaimable once its
``pending_claim_ttl_seconds`` age passes, so a crash loses at most one TTL
window of dedup, never availability.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional
from contextlib import contextmanager
from contextvars import ContextVar


_CUSTOMER_OUTPUT_BLOCKED = ContextVar("observer_customer_output_blocked", default=False)


@contextmanager
def event_execution_scope(channel_id, root_ts, message_ts, replay, run_id=None):
    """Rebind immutable adapter source at the actual owner, including drains."""
    token = _CUSTOMER_OUTPUT_BLOCKED.set(replay)
    try:
        try:
            from hermes_plugins.offon import channels, safety
        except ImportError:
            try:
                from plugins.offon import channels, safety
            except ImportError:
                if replay:
                    raise
                yield
                return
        if run_id is None:
            run_id = execution_run_id(channel_id, root_ts, message_ts, replay)
        source = safety.Source(channel_id, root_ts, message_ts, recovery=replay, run_id=run_id)
        with channels.bootstrap_dispatch_scope(channel_id if replay else ""):
            with safety.execution_scope(source):
                yield
    finally:
        _CUSTOMER_OUTPUT_BLOCKED.reset(token)


def execution_run_id(channel_id, root_ts, message_ts, replay):
    """Capture an existing trusted Source run, never message metadata."""
    try:
        from hermes_plugins.offon import safety
    except ImportError:
        try:
            from plugins.offon import safety
        except ImportError:
            return ""
    current = safety.source()
    if current and replay and current.recovery and (
        current.channel_id, current.root_ts, current.message_ts
    ) == (channel_id, root_ts, message_ts):
        return current.run_id
    return ""


def customer_output(method):
    """Slack adapter boundary; internal Offon webhook is independent."""
    @wraps(method)
    async def wrapped(*args, **kwargs):
        if _CUSTOMER_OUTPUT_BLOCKED.get():
            error = "trusted_replay_customer_output_suppressed"
            if method.__name__ == "_standalone_send":
                return {"success": False, "error": error}
            if method.__name__ in {"_add_reaction", "_remove_reaction", "delete_message"}:
                return False
            if method.__name__ in {"send_typing", "stop_typing", "create_handoff_thread"}:
                return None
            from gateway.platforms.base import SendResult
            return SendResult(success=False, error=error)
        return await method(*args, **kwargs)
    return wrapped


class ReplayNotCompleted(RuntimeError):
    """Retryable handler failure/busy, never business success."""

DEFAULT_LOOKBACK_SECONDS = 24 * 60 * 60
DEFAULT_PAGE_LIMIT = 5
DEFAULT_PAGE_SIZE = 200
DEFAULT_MESSAGE_LIMIT = 500
# A pending claim older than this is assumed orphaned by a crashed process
# and may be reclaimed. Long enough that a slow-but-alive dispatch never
# collides with its own claim; short enough that a real crash does not
# suppress retry for long.
DEFAULT_PENDING_CLAIM_TTL_SECONDS = 300


def claim_gated_message(method):
    """Decorate a Slack message method with the shared live/replay claim gate.

    Preserve the implementation's metadata and introspection via ``wraps``.
    Completion still follows the method's return, including replay's owner
    and drain wait; failures release the pending claim for retry.
    """
    @wraps(method)
    async def wrapped(self, event: dict, payload: Optional[dict] = None) -> None:
        if not self._slack_history_catchup_enabled():
            await method(self, event, payload)
            return

        workspace_id = self._event_team_id(event, payload)
        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        if not (workspace_id and channel_id and ts):
            await method(self, event, payload)
            return

        try:
            store = self._observer_catchup_store()
            claimed = claim_message(store, workspace_id, channel_id, ts, time.time())
        except Exception:
            if _CUSTOMER_OUTPUT_BLOCKED.get():
                raise ReplayNotCompleted("replay_claim_store_failed")
            logging.getLogger(method.__module__).debug(
                "[Slack] Observer catch-up claim failed; processing live", exc_info=True
            )
            await method(self, event, payload)
            return

        if not claimed:
            if _CUSTOMER_OUTPUT_BLOCKED.get():
                row = store.execute(
                    "SELECT status FROM claimed_messages WHERE workspace_id=? AND channel_id=? AND ts=?",
                    (workspace_id, channel_id, ts),
                ).fetchone()
                if not row or row[0] != "completed":
                    raise ReplayNotCompleted("replay_claim_busy")
            return

        try:
            await method(self, event, payload)
        except BaseException:
            release_claim(store, workspace_id, channel_id, ts)
            raise
        else:
            complete_claim(store, workspace_id, channel_id, ts, time.time())

    return wrapped

# Subtypes that never represent user content worth filing — thread/channel
# housekeeping events Slack includes in conversations.history alongside real
# messages.
_NON_CONTENT_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "message_deleted",
    "message_changed",
    "bot_add",
    "bot_remove",
    "pinned_item",
    "unpinned_item",
}


@dataclass(frozen=True)
class CatchupConfig:
    enabled: bool = False
    source_channels: frozenset = field(default_factory=frozenset)
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS
    page_limit: int = DEFAULT_PAGE_LIMIT
    page_size: int = DEFAULT_PAGE_SIZE
    message_limit: int = DEFAULT_MESSAGE_LIMIT
    pending_claim_ttl_seconds: float = DEFAULT_PENDING_CLAIM_TTL_SECONDS


@dataclass
class ChannelCatchupResult:
    dispatched: int = 0
    skipped: int = 0
    error: Optional[str] = None
    pending: bool = False
    progressed: bool = False
    first_error: Optional[str] = None
    attempts: int = 0


def open_store(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the persistent claim/checkpoint database.

    Mode 0600, parent directory 0700 — this file lives under a profile's own
    ``$HERMES_HOME`` and holds a durable delivery ledger, not secrets, but is
    scoped as tightly as the credential files it sits next to.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    is_new = not path.exists()
    connection = sqlite3.connect(path, isolation_level=None)
    if is_new:
        os.chmod(path, 0o600)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS claimed_messages (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                completed_at REAL,
                PRIMARY KEY (workspace_id, channel_id, ts)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_checkpoints (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                last_root_ts TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (workspace_id, channel_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_checkpoints (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                root_ts TEXT NOT NULL,
                last_reply_ts TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (workspace_id, channel_id, root_ts)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_bootstraps (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                history_cursor TEXT NOT NULL,
                high_watermark TEXT,
                history_complete INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (workspace_id, channel_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_bootstraps (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                root_ts TEXT NOT NULL,
                reply_cursor TEXT NOT NULL,
                last_reply_ts TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (workspace_id, channel_id, root_ts)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bootstrap_completions (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                completed_at REAL NOT NULL,
                PRIMARY KEY (workspace_id, channel_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bootstrap_cursor_history (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                root_ts TEXT NOT NULL,
                cursor TEXT NOT NULL,
                PRIMARY KEY (workspace_id, channel_id, kind, root_ts, cursor)
            )
            """
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS catchup_schedule ("
            "workspace_id TEXT NOT NULL, channel_id TEXT NOT NULL, "
            "attempt_order INTEGER NOT NULL, PRIMARY KEY (workspace_id, channel_id))"
        )
    except BaseException:
        connection.close()
        raise
    os.chmod(path, 0o600)
    return connection


def claim_message(
    conn: sqlite3.Connection,
    workspace_id: str,
    channel_id: str,
    ts: str,
    now: float,
    pending_claim_ttl_seconds: float = DEFAULT_PENDING_CLAIM_TTL_SECONDS,
) -> bool:
    """Atomically claim ``(workspace_id, channel_id, ts)`` as *pending*.

    Returns True when this call wins the claim: either no row existed yet,
    or an existing *pending* row is older than ``pending_claim_ttl_seconds``
    (its prior claimant is assumed crashed). Returns False when another
    claimant holds a fresh pending claim, or the message is already
    ``completed`` — that dedup is permanent and never reclaimed regardless
    of age.

    A True return is not itself a durability guarantee: the caller must
    follow up with ``complete_claim`` on success or ``release_claim`` on a
    caught failure, or the claim simply ages out and gets reclaimed later.
    """
    cursor = conn.execute(
        """
        INSERT INTO claimed_messages (workspace_id, channel_id, ts, status, claimed_at, completed_at)
        VALUES (?, ?, ?, 'pending', ?, NULL)
        ON CONFLICT(workspace_id, channel_id, ts) DO UPDATE SET
            claimed_at = excluded.claimed_at
        WHERE claimed_messages.status = 'pending'
          AND claimed_messages.claimed_at < ?
        """,
        (workspace_id, channel_id, ts, now, now - pending_claim_ttl_seconds),
    )
    return cursor.rowcount == 1


def complete_claim(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, ts: str, now: float
) -> None:
    """Mark a claimed message as permanently done — never reclaimed again."""
    conn.execute(
        "UPDATE claimed_messages SET status = 'completed', completed_at = ? "
        "WHERE workspace_id = ? AND channel_id = ? AND ts = ?",
        (now, workspace_id, channel_id, ts),
    )


def release_claim(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, ts: str
) -> None:
    """Give up a pending claim after a caught failure so retry need not wait for TTL.

    A no-op if the claim was already completed (completion is permanent and
    must not be undone by a late/duplicate release call).
    """
    conn.execute(
        "DELETE FROM claimed_messages "
        "WHERE workspace_id = ? AND channel_id = ? AND ts = ? AND status = 'pending'",
        (workspace_id, channel_id, ts),
    )


def get_channel_checkpoint(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str
) -> Optional[str]:
    row = conn.execute(
        "SELECT last_root_ts FROM channel_checkpoints WHERE workspace_id = ? AND channel_id = ?",
        (workspace_id, channel_id),
    ).fetchone()
    return row[0] if row else None


def advance_channel_checkpoint(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, ts: str, now: float
) -> None:
    current = get_channel_checkpoint(conn, workspace_id, channel_id)
    if current is not None and float(current) >= float(ts):
        return
    conn.execute(
        """
        INSERT INTO channel_checkpoints (workspace_id, channel_id, last_root_ts, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(workspace_id, channel_id)
        DO UPDATE SET last_root_ts = excluded.last_root_ts, updated_at = excluded.updated_at
        """,
        (workspace_id, channel_id, ts, now),
    )


def get_thread_checkpoint(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, root_ts: str
) -> Optional[str]:
    row = conn.execute(
        "SELECT last_reply_ts FROM thread_checkpoints "
        "WHERE workspace_id = ? AND channel_id = ? AND root_ts = ?",
        (workspace_id, channel_id, root_ts),
    ).fetchone()
    return row[0] if row else None


def advance_thread_checkpoint(
    conn: sqlite3.Connection,
    workspace_id: str,
    channel_id: str,
    root_ts: str,
    last_reply_ts: str,
    now: float,
) -> None:
    current = get_thread_checkpoint(conn, workspace_id, channel_id, root_ts)
    if current is not None and float(current) >= float(last_reply_ts):
        return
    conn.execute(
        """
        INSERT INTO thread_checkpoints
            (workspace_id, channel_id, root_ts, last_reply_ts, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, channel_id, root_ts)
        DO UPDATE SET last_reply_ts = excluded.last_reply_ts, updated_at = excluded.updated_at
        """,
        (workspace_id, channel_id, root_ts, last_reply_ts, now),
    )


def list_open_threads(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, min_root_ts: float
) -> list:
    rows = conn.execute(
        "SELECT root_ts, last_reply_ts FROM thread_checkpoints "
        "WHERE workspace_id = ? AND channel_id = ? AND CAST(root_ts AS REAL) >= ? "
        "ORDER BY CAST(root_ts AS REAL)",
        (workspace_id, channel_id, min_root_ts),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def prune_stale_threads(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, min_root_ts: float
) -> None:
    conn.execute(
        "DELETE FROM thread_checkpoints "
        "WHERE workspace_id = ? AND channel_id = ? AND CAST(root_ts AS REAL) < ?",
        (workspace_id, channel_id, min_root_ts),
    )


def _bootstrap_state(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, now: float
) -> tuple[str, Optional[str], bool]:
    conn.execute(
        "INSERT OR IGNORE INTO channel_bootstraps "
        "(workspace_id, channel_id, history_cursor, high_watermark, history_complete, updated_at) "
        "VALUES (?, ?, '', NULL, 0, ?)",
        (workspace_id, channel_id, now),
    )
    row = conn.execute(
        "SELECT history_cursor, high_watermark, history_complete FROM channel_bootstraps "
        "WHERE workspace_id = ? AND channel_id = ?",
        (workspace_id, channel_id),
    ).fetchone()
    return str(row[0]), row[1], bool(row[2])


def _save_bootstrap_history(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, cursor: str,
    high_watermark: Optional[str], complete: bool, now: float,
) -> None:
    conn.execute(
        "UPDATE channel_bootstraps SET history_cursor = ?, high_watermark = ?, "
        "history_complete = ?, updated_at = ? WHERE workspace_id = ? AND channel_id = ?",
        (cursor, high_watermark, int(complete), now, workspace_id, channel_id),
    )


def _queue_bootstrap_thread(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, root_ts: str, now: float
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO thread_bootstraps "
        "(workspace_id, channel_id, root_ts, reply_cursor, last_reply_ts, updated_at) "
        "VALUES (?, ?, ?, '', ?, ?)",
        (workspace_id, channel_id, root_ts, root_ts, now),
    )


def is_channel_bootstrap_complete(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str
) -> bool:
    return conn.execute(
        "SELECT 1 FROM bootstrap_completions "
        "WHERE workspace_id = ? AND channel_id = ?",
        (workspace_id, channel_id),
    ).fetchone() is not None


def record_bootstrap_completion(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str, now: float
) -> None:
    conn.execute(
        "INSERT INTO bootstrap_completions (workspace_id, channel_id, completed_at) "
        "VALUES (?, ?, ?) ON CONFLICT(workspace_id, channel_id) "
        "DO UPDATE SET completed_at = excluded.completed_at",
        (workspace_id, channel_id, now),
    )


def _remember_bootstrap_cursor(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str,
    kind: str, root_ts: str, cursor: str,
) -> bool:
    """Persist a continuation cursor; False means a cross-run cycle."""
    inserted = conn.execute(
        "INSERT OR IGNORE INTO bootstrap_cursor_history "
        "(workspace_id, channel_id, kind, root_ts, cursor) VALUES (?, ?, ?, ?, ?)",
        (workspace_id, channel_id, kind, root_ts, cursor),
    )
    return inserted.rowcount == 1


def _clear_bootstrap_cursors(
    conn: sqlite3.Connection, workspace_id: str, channel_id: str,
    kind: str, root_ts: str,
) -> None:
    conn.execute(
        "DELETE FROM bootstrap_cursor_history WHERE workspace_id = ? AND channel_id = ? "
        "AND kind = ? AND root_ts = ?",
        (workspace_id, channel_id, kind, root_ts),
    )


async def _drain_bootstrap_threads(
    *, client: Any, dispatch: Callable[[dict], Awaitable[None]], conn: sqlite3.Connection,
    workspace_id: str, channel_id: str, bot_user_id: str, config: CatchupConfig,
    budget: int, result: ChannelCatchupResult, now: float,
) -> tuple[bool, int]:
    rows = conn.execute(
        "SELECT root_ts, reply_cursor, last_reply_ts FROM thread_bootstraps "
        "WHERE workspace_id = ? AND channel_id = ? ORDER BY CAST(root_ts AS REAL)",
        (workspace_id, channel_id),
    ).fetchall()
    for root_ts, stored_cursor, stored_last_reply in rows:
        cursor = stored_cursor or None
        last_reply_ts = stored_last_reply
        pages = 0
        while pages < config.page_limit and budget > 0:
            try:
                response = await client.conversations_replies(
                    channel=channel_id, ts=root_ts, oldest="0", cursor=cursor,
                    limit=min(config.page_size, budget),
                )
            except Exception as exc:
                result.error = f"replies_fetch_failed:{root_ts}: {exc}"
                return False, budget
            replies = response.get("messages") or []
            budget -= len(replies)
            pages += 1
            for reply in sorted(replies, key=lambda item: float(item.get("ts") or 0.0)):
                reply_ts = str(reply.get("ts") or "")
                if not reply_ts or reply_ts == root_ts:
                    continue
                if _is_dispatchable(reply, bot_user_id):
                    try:
                        await dispatch(_build_event(
                            reply, channel_id=channel_id, workspace_id=workspace_id,
                            thread_ts=root_ts,
                        ))
                    except Exception as exc:
                        result.error = f"dispatch_failed:{reply_ts}: {exc}"
                        return False, budget
                    result.dispatched += 1
                else:
                    result.skipped += 1
                if float(reply_ts) > float(last_reply_ts):
                    last_reply_ts = reply_ts
            next_cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
            if next_cursor and not _remember_bootstrap_cursor(
                conn, workspace_id, channel_id, "thread", root_ts, next_cursor
            ):
                result.error = f"replies_cursor_repeated:{root_ts}"
                return False, budget
            if not next_cursor:
                advance_thread_checkpoint(
                    conn, workspace_id, channel_id, root_ts, last_reply_ts, now
                )
                conn.execute(
                    "DELETE FROM thread_bootstraps WHERE workspace_id = ? AND channel_id = ? "
                    "AND root_ts = ?", (workspace_id, channel_id, root_ts),
                )
                _clear_bootstrap_cursors(
                    conn, workspace_id, channel_id, "thread", root_ts
                )
                result.progressed = True
                break
            conn.execute(
                "UPDATE thread_bootstraps SET reply_cursor = ?, last_reply_ts = ?, updated_at = ? "
                "WHERE workspace_id = ? AND channel_id = ? AND root_ts = ?",
                (next_cursor, last_reply_ts, now, workspace_id, channel_id, root_ts),
            )
            result.progressed = True
            cursor = next_cursor
        if budget <= 0:
            return False, budget
    return True, budget


async def _run_channel_bootstrap(
    *, client: Any, dispatch: Callable[[dict], Awaitable[None]], conn: sqlite3.Connection,
    workspace_id: str, channel_id: str, bot_user_id: str, config: CatchupConfig,
    budget: int, result: ChannelCatchupResult, now: float,
) -> int:
    result.pending = True
    if config.page_limit <= 0 or config.page_size <= 0:
        result.error = "invalid_pagination_config"
        return budget
    cursor, high_watermark, history_complete = _bootstrap_state(
        conn, workspace_id, channel_id, now
    )
    ok, budget = await _drain_bootstrap_threads(
        client=client, dispatch=dispatch, conn=conn, workspace_id=workspace_id,
        channel_id=channel_id, bot_user_id=bot_user_id, config=config,
        budget=budget, result=result, now=now,
    )
    if not ok:
        return budget

    pages = 0
    while not history_complete and pages < config.page_limit and budget > 0:
        request_cursor = cursor or None
        try:
            response = await client.conversations_history(
                channel=channel_id, oldest="0", cursor=request_cursor,
                limit=min(config.page_size, budget),
            )
        except Exception as exc:
            result.error = f"history_fetch_failed: {exc}"
            return budget
        roots = response.get("messages") or []
        budget -= len(roots)
        pages += 1
        for root in sorted(roots, key=lambda item: float(item.get("ts") or 0.0)):
            root_ts = str(root.get("ts") or "")
            if not root_ts:
                continue
            if _is_dispatchable(root, bot_user_id):
                try:
                    await dispatch(_build_event(
                        root, channel_id=channel_id, workspace_id=workspace_id
                    ))
                except Exception as exc:
                    result.error = f"dispatch_failed:{root_ts}: {exc}"
                    return budget
                result.dispatched += 1
            else:
                result.skipped += 1
            if high_watermark is None or float(root_ts) > float(high_watermark):
                high_watermark = root_ts
            if root.get("reply_count"):
                _queue_bootstrap_thread(conn, workspace_id, channel_id, root_ts, now)

        next_cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
        if next_cursor and not _remember_bootstrap_cursor(
            conn, workspace_id, channel_id, "history", "", next_cursor
        ):
            result.error = "history_cursor_repeated"
            return budget
        history_complete = not next_cursor
        cursor = next_cursor
        _save_bootstrap_history(
            conn, workspace_id, channel_id, cursor, high_watermark, history_complete, now
        )
        result.progressed = True
        ok, budget = await _drain_bootstrap_threads(
            client=client, dispatch=dispatch, conn=conn, workspace_id=workspace_id,
            channel_id=channel_id, bot_user_id=bot_user_id, config=config,
            budget=budget, result=result, now=now,
        )
        if not ok:
            return budget

    pending = conn.execute(
        "SELECT 1 FROM thread_bootstraps WHERE workspace_id = ? AND channel_id = ? LIMIT 1",
        (workspace_id, channel_id),
    ).fetchone()
    if history_complete and pending is None:
        advance_channel_checkpoint(
            conn, workspace_id, channel_id, high_watermark or "0.000000", now
        )
        conn.execute(
            "DELETE FROM channel_bootstraps WHERE workspace_id = ? AND channel_id = ?",
            (workspace_id, channel_id),
        )
        _clear_bootstrap_cursors(conn, workspace_id, channel_id, "history", "")
        # Written last: a crash before this point remains fail-closed and
        # safely restarts through the durable claim ledger.
        record_bootstrap_completion(conn, workspace_id, channel_id, now)
        result.pending = False
        result.progressed = True
    return budget


def _is_dispatchable(message: dict, bot_user_id: str) -> bool:
    if not isinstance(message, dict):
        return False
    if not message.get("ts"):
        return False
    if message.get("subtype") in _NON_CONTENT_SUBTYPES:
        return False
    if bot_user_id and message.get("user") == bot_user_id:
        return False
    return True


async def _paginate(
    fetch: Callable[..., Awaitable[dict]], *, page_limit: int, page_size: int, budget: int
):
    """Fetch up to ``page_limit`` pages (bounded further by ``budget``).

    Continuation is driven solely by a non-empty ``response_metadata.
    next_cursor`` — ``has_more`` is not trusted as a stop signal on its own
    since it is sometimes absent even when a valid cursor is present.

    Returns ``(messages, remaining_budget, exhausted)``. ``exhausted`` is
    True only when pagination ended because the API reported no further
    cursor — i.e. every message between the request's ``oldest`` bound and
    now was actually fetched. It is False when ``page_limit``/``budget`` cut
    the fetch short, or when a cursor repeated from a prior page (a
    misbehaving API or fake) ended pagination defensively: in both cases
    there may be older messages this call never saw, so the caller must not
    treat the newest-fetched timestamp as "everything before this is done".
    """
    messages: list = []
    cursor = None
    seen_cursors: set = set()
    pages = 0
    exhausted = False
    while pages < page_limit and budget > 0:
        response = await fetch(cursor=cursor, limit=min(page_size, budget))
        page_messages = response.get("messages") or []
        messages.extend(page_messages)
        budget -= len(page_messages)
        pages += 1
        metadata = response.get("response_metadata") or {}
        next_cursor = metadata.get("next_cursor") or ""
        if not next_cursor:
            exhausted = True
            break
        if next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return messages, budget, exhausted


def _build_event(message: dict, *, channel_id: str, workspace_id: str, thread_ts: Optional[str] = None) -> dict:
    event = dict(message)
    event["channel"] = channel_id
    event.setdefault("team", workspace_id)
    if thread_ts is not None:
        event.setdefault("thread_ts", thread_ts)
    return event


async def _drain_thread(
    *,
    client: Any,
    dispatch: Callable[[dict], Awaitable[None]],
    conn: sqlite3.Connection,
    workspace_id: str,
    channel_id: str,
    root_ts: str,
    since: str,
    bot_user_id: str,
    config: CatchupConfig,
    budget: int,
    result: ChannelCatchupResult,
    now: float,
) -> tuple:
    """Fetch and dispatch replies newer than ``since``; advance the thread checkpoint.

    Returns ``(ok, budget)`` — ``ok`` is False only when a fetch or dispatch
    failure aborted this thread; the caller stops touching this channel for
    the rest of the run so ordering (and therefore checkpoint safety) holds.

    The checkpoint only moves past ``since`` when the fetch was exhausted,
    including on partial dispatch failure, meaning there is
    no unseen older reply hiding behind a truncated page. A budget-truncated
    fetch that dispatches everything it saw without error still leaves the
    checkpoint at ``since``, because Slack returns replies newest-first: a
    truncated page proves only that we saw *some* recent replies, not that
    nothing older and unseen remains.
    """
    try:
        replies, budget, exhausted = await _paginate(
            lambda cursor, limit: client.conversations_replies(
                channel=channel_id, ts=root_ts, oldest=since, cursor=cursor, limit=limit
            ),
            page_limit=config.page_limit,
            page_size=config.page_size,
            budget=budget,
        )
    except Exception as exc:
        result.error = f"replies_fetch_failed:{root_ts}: {exc}"
        return False, budget

    replies = [r for r in replies if str(r.get("ts") or "") and str(r.get("ts")) != root_ts]
    replies.sort(key=lambda m: float(m.get("ts") or 0.0))

    last_reply_ts = since
    for reply in replies:
        reply_ts = str(reply.get("ts") or "")
        if not reply_ts:
            continue
        if not _is_dispatchable(reply, bot_user_id):
            result.skipped += 1
            last_reply_ts = reply_ts
            continue
        event = _build_event(reply, channel_id=channel_id, workspace_id=workspace_id, thread_ts=root_ts)
        try:
            await dispatch(event)
        except Exception as exc:
            result.error = f"dispatch_failed:{reply_ts}: {exc}"
            if exhausted:
                advance_thread_checkpoint(conn, workspace_id, channel_id, root_ts, last_reply_ts, now)
            return False, budget
        result.dispatched += 1
        result.progressed = True
        last_reply_ts = reply_ts

    if exhausted:
        advance_thread_checkpoint(conn, workspace_id, channel_id, root_ts, last_reply_ts, now)
        if float(last_reply_ts) > float(since):
            result.progressed = True
    return True, budget


async def run_catchup(
    *,
    client: Any,
    dispatch: Callable[[dict], Awaitable[None]],
    conn: sqlite3.Connection,
    workspace_id: str,
    allowed_channels: Iterable[str],
    config: CatchupConfig,
    bot_user_id: str = "",
    now: float,
) -> dict:
    """Backfill approved channel history for one workspace.

    For each channel in ``config.source_channels & allowed_channels``, a new
    channel first walks history from inception with durable root/reply cursors.
    The bootstrap checkpoint is committed only after every cursor is empty.
    Completed channels fetch roots newer than the channel checkpoint (bounded by lookback/page/
    message limits), dispatch them oldest-first through the same path live
    messages use, then drain replies for any thread with new activity —
    both newly-seen roots and threads already known from a prior run whose
    root fell outside this run's lookback window. The channel checkpoint
    only advances past a message once it dispatches without raising; a
    failure stops that channel for this run without touching the checkpoint
    for anything after the last success. On a clean run (no failures) the
    checkpoint still only advances once the history fetch was exhausted —
    reaching page_limit or message_limit before the API reports "no more"
    is a bounded-budget stop, not proof of full coverage, since Slack
    returns messages newest-first and a truncated fetch can hide an older,
    unfetched tail. A gap that large is a documented residual limit of a
    *bounded* catch-up, not something this function can silently paper
    over; it dispatches what it safely can and leaves the checkpoint where
    the next run can retry with no gap.
    """
    results: dict = {}
    if not config.enabled:
        return results

    channels = sorted(set(config.source_channels) & set(allowed_channels))
    # Persist scheduling before awaiting a slow turn, including across restart.
    attempted = dict(conn.execute(
        "SELECT channel_id, attempt_order FROM catchup_schedule WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall())
    channels.sort(
        key=lambda channel_id: (
            is_channel_bootstrap_complete(conn, workspace_id, channel_id),
            attempted.get(channel_id, -1),
            channel_id,
        )
    )
    lookback_floor = now - config.lookback_seconds
    slots = config.message_limit
    quota = max(1, config.message_limit // max(1, len(channels)))
    attempt_order = max(attempted.values(), default=0)

    for channel_id in channels:
        result = ChannelCatchupResult()
        results[channel_id] = result
        if config.message_limit <= 0:
            result.error = "invalid_message_limit"
            continue
        if slots <= 0:
            # The budget is shared across channels for this bounded run. A
            # later channel that has not had a turn is still resumable; it is
            # not a Slack/API failure and must not stop the in-connect retry.
            result.pending = True
            continue

        budget = min(quota, slots)
        slots -= budget
        attempt_order += 1
        conn.execute(
            "INSERT INTO catchup_schedule VALUES (?, ?, ?) "
            "ON CONFLICT(workspace_id, channel_id) DO UPDATE SET attempt_order = excluded.attempt_order",
            (workspace_id, channel_id, attempt_order),
        )
        result.attempts = 1
        channel_checkpoint = get_channel_checkpoint(conn, workspace_id, channel_id)
        if not is_channel_bootstrap_complete(conn, workspace_id, channel_id):
            budget = await _run_channel_bootstrap(
                client=client,
                dispatch=dispatch,
                conn=conn,
                workspace_id=workspace_id,
                channel_id=channel_id,
                bot_user_id=bot_user_id,
                config=config,
                budget=budget,
                result=result,
                now=now,
            )
            continue
        if channel_checkpoint is None:
            result.error = "bootstrap_completion_missing_checkpoint"
            continue

        prune_stale_threads(conn, workspace_id, channel_id, lookback_floor)
        known_threads = list_open_threads(conn, workspace_id, channel_id, lookback_floor)

        oldest = max(float(channel_checkpoint), lookback_floor)

        try:
            roots, budget, roots_exhausted = await _paginate(
                lambda cursor, limit: client.conversations_history(
                    channel=channel_id, oldest=str(oldest), cursor=cursor, limit=limit
                ),
                page_limit=config.page_limit,
                page_size=config.page_size,
                budget=budget,
            )
        except Exception as exc:
            result.error = f"history_fetch_failed: {exc}"
            continue

        roots = [
            r
            for r in roots
            if str(r.get("ts") or "")
            and (not channel_checkpoint or float(r["ts"]) > float(channel_checkpoint))
        ]
        roots.sort(key=lambda m: float(m.get("ts") or 0.0))

        aborted = False
        last_root_ts = channel_checkpoint

        for root in roots:
            ts = str(root.get("ts") or "")
            if not ts:
                continue
            if not _is_dispatchable(root, bot_user_id):
                result.skipped += 1
                last_root_ts = ts
            else:
                event = _build_event(root, channel_id=channel_id, workspace_id=workspace_id)
                try:
                    await dispatch(event)
                except Exception as exc:
                    result.error = f"dispatch_failed:{ts}: {exc}"
                    aborted = True
                    break
                result.dispatched += 1
                result.progressed = True
                last_root_ts = ts

            reply_count = root.get("reply_count") or 0
            if reply_count and budget > 0:
                ok, budget = await _drain_thread(
                    client=client,
                    dispatch=dispatch,
                    conn=conn,
                    workspace_id=workspace_id,
                    channel_id=channel_id,
                    root_ts=ts,
                    since=ts,
                    bot_user_id=bot_user_id,
                    config=config,
                    budget=budget,
                    result=result,
                    now=now,
                )
                if not ok:
                    aborted = True
                    break

        # Even on failure, newest-first truncated pages can hide older roots.
        # Exhaustion is required before checkpointing any successful prefix.
        if last_root_ts and (not channel_checkpoint or float(last_root_ts) > float(channel_checkpoint)):
            if roots_exhausted:
                advance_channel_checkpoint(conn, workspace_id, channel_id, last_root_ts, now)
                result.progressed = True

        if aborted:
            continue

        for root_ts, last_reply_ts in known_threads:
            if budget <= 0:
                break
            ok, budget = await _drain_thread(
                client=client,
                dispatch=dispatch,
                conn=conn,
                workspace_id=workspace_id,
                channel_id=channel_id,
                root_ts=root_ts,
                since=last_reply_ts,
                bot_user_id=bot_user_id,
                config=config,
                budget=budget,
                result=result,
                now=now,
            )
            if not ok:
                break

    return results


async def run_workspace_catchups(
    *, clients: dict, dispatch: Callable[[dict], Awaitable[None]],
    conn: sqlite3.Connection, config: CatchupConfig, bot_user_ids: dict,
    max_rounds: int = 100, fetch_retries: int = 2,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict:
    """Bounded fair workspace rounds with channel-local retry and totals.

    Fetch failures retry with backoff; dispatch/cursor errors and stalls are
    isolated for this invocation. Durable cursors survive the round limit.
    Nothing here equates a pending/stalled channel with bootstrap completion.
    """
    if max_rounds <= 0 or fetch_retries < 0:
        raise ValueError("invalid catch-up retry bounds")
    remaining = {workspace: set(config.source_channels) for workspace in clients}
    totals = {workspace: {} for workspace in clients}
    failures = {}
    for _ in range(max_rounds):
        if not any(remaining.values()):
            break
        delay = 0.0
        for workspace, client in clients.items():
            if not remaining[workspace]:
                continue
            current = await run_catchup(
                client=client, dispatch=dispatch, conn=conn, workspace_id=workspace,
                allowed_channels=remaining[workspace],
                config=replace(config, source_channels=frozenset(remaining[workspace])),
                bot_user_id=bot_user_ids.get(workspace, ""), now=clock(),
            )
            next_channels = set()
            for channel, result in current.items():
                total = totals[workspace].setdefault(channel, ChannelCatchupResult())
                total.dispatched += result.dispatched
                total.skipped += result.skipped
                total.attempts += result.attempts
                total.progressed |= result.progressed
                total.error, total.pending = result.error, result.pending
                if result.error:
                    total.first_error = total.first_error or result.error
                    key = (workspace, channel)
                    failures[key] = failures.get(key, 0) + 1
                    total.pending = True
                    if result.error.startswith(("history_fetch_failed:", "replies_fetch_failed:")):
                        if failures[key] <= fetch_retries:
                            next_channels.add(channel)
                            delay = max(delay, min(2.0, 0.5 * 2 ** (failures[key] - 1)))
                elif result.pending:
                    if result.progressed or result.attempts == 0:
                        next_channels.add(channel)
                    else:
                        total.error = "catchup_no_progress"
                        total.first_error = total.first_error or total.error
            remaining[workspace] = next_channels
        if delay and any(remaining.values()):
            await sleep(delay)
    return totals
