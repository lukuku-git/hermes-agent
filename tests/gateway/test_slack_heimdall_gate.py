"""Regression tests for the Heimdall-only gate in ``#error-alert``."""

import asyncio
import sys
from contextlib import nullcontext
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        (
            "slack_bolt.adapter.socket_mode.async_handler",
            slack_bolt.adapter.socket_mode.async_handler,
        ),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_slack_mock()

from plugins.platforms.slack.adapter import _is_heimdall_bot_event  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter, SendResult  # noqa: E402
from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402
from plugins.platforms.slack import observer_catchup  # noqa: E402


def test_accepts_current_incoming_webhook_without_app_id():
    event = {
        "subtype": "bot_message",
        "bot_id": "B0C0FPVJR3P",
        "bot_profile": {"app_id": None},
    }

    assert _is_heimdall_bot_event(event) is True


def test_accepts_heimdall_app_id_when_slack_includes_it():
    event = {
        "subtype": "bot_message",
        "bot_id": "B_SOME_ROTATED_ID",
        "app_id": "A0C0PS76UGL",
    }

    assert _is_heimdall_bot_event(event) is True


def test_rejects_an_unrelated_bot():
    event = {
        "subtype": "bot_message",
        "bot_id": "B_OTHER",
        "app_id": "A_OTHER",
    }

    assert _is_heimdall_bot_event(event) is False


@pytest.fixture
def catchup_adapter(tmp_path, monkeypatch):
    # offon is an externally installed gate plugin, absent in this checkout.
    # Stub only its scope; Slack conversion and Base task ownership stay real.
    offon = ModuleType("plugins.offon")
    offon.channels = MagicMock()
    offon.channels.bootstrap_dispatch_scope.side_effect = lambda channel: nullcontext()
    monkeypatch.setitem(sys.modules, "plugins.offon", offon)
    adapter = SlackAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    adapter.config.extra.update(history_catchup_enabled=True, reactions=False)
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_BOT"
    adapter._running = True
    adapter._resolve_user_name = AsyncMock(return_value="testuser")
    adapter._fetch_thread_context = AsyncMock(return_value="")
    adapter._fetch_thread_parent_text = AsyncMock(return_value="")
    adapter._stop_typing_refresh = AsyncMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True))
    store = observer_catchup.open_store(tmp_path / "ledger" / "catchup.sqlite3")
    adapter._observer_catchup_conn = store
    yield adapter
    store.close()


def _history_event(ts, thread_ts=None):
    event = dict(channel="C_TEST", channel_type="channel", team="T_TEST",
                 user="U_USER", text="<@U_BOT> history", ts=ts)
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def _claims(adapter):
    return adapter._observer_catchup_conn.execute(
        "SELECT ts, status FROM claimed_messages ORDER BY CAST(ts AS REAL)"
    ).fetchall()


async def _settle():
    # Yield scheduling, not a wall-clock assumption about agent completion.
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_catchup_waits_for_turn_before_claim_and_checkpoint(catchup_adapter):
    a = catchup_adapter
    assert a.handle_message.__func__ is BasePlatformAdapter.handle_message
    started, finish = asyncio.Event(), asyncio.Event()
    seen = []

    async def agent(event):
        seen.append(event.message_id)
        started.set()
        await finish.wait()

    a._message_handler = agent
    client = AsyncMock()
    client.conversations_history.return_value = {
        "messages": [_history_event("102"), _history_event("101")]
    }
    store = a._observer_catchup_conn
    observer_catchup.record_bootstrap_completion(store, "T_TEST", "C_TEST", 100)
    observer_catchup.advance_channel_checkpoint(store, "T_TEST", "C_TEST", "100", 100)
    task = asyncio.create_task(observer_catchup.run_catchup(
        client=client, dispatch=a._dispatch_observer_bootstrap_message,
        conn=store, workspace_id="T_TEST", allowed_channels={"C_TEST"},
        config=observer_catchup.CatchupConfig(enabled=True, source_channels=frozenset({"C_TEST"})),
        now=200,
    ))
    try:
        await asyncio.wait_for(started.wait(), 3)
        await _settle()
        assert not task.done()
        assert seen == ["101"]
        assert _claims(a) == [("101", "pending")]
        assert observer_catchup.get_channel_checkpoint(store, "T_TEST", "C_TEST") == "100"
    finally:
        finish.set()
        await task
        await a.cancel_background_tasks()
    assert seen == ["101", "102"]
    assert _claims(a) == [("101", "completed"), ("102", "completed")]
    assert observer_catchup.get_channel_checkpoint(store, "T_TEST", "C_TEST") == "102"
    await a._dispatch_observer_bootstrap_message(_history_event("101"))
    assert seen == ["101", "102"]


@pytest.mark.asyncio
async def test_catchup_serializes_concurrent_dispatch_but_not_live(catchup_adapter):
    a = catchup_adapter
    started = asyncio.Queue()
    finish = asyncio.Event()
    active = maximum = 0

    async def agent(event):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await started.put(event.message_id)
        try:
            await finish.wait()
        finally:
            active -= 1

    a._message_handler = agent
    tasks = [asyncio.create_task(a._dispatch_observer_bootstrap_message(_history_event(str(ts))))
             for ts in range(101, 105)]
    try:
        assert await asyncio.wait_for(started.get(), 3) == "101"
        await _settle()
        assert maximum == 1
        assert started.empty()
        # Live delivery stays fast and is not subject to the catch-up slot.
        await asyncio.wait_for(a._handle_slack_message(_history_event("200")), 3)
        assert await asyncio.wait_for(started.get(), 3) == "200"
        assert maximum == 2
    finally:
        finish.set()
        await asyncio.gather(*tasks)
        await a.cancel_background_tasks()
    assert not a._session_tasks
    assert not a._active_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
async def test_catchup_waits_for_entire_owner_drain_chain(catchup_adapter, late):
    a = catchup_adapter
    started = asyncio.Queue()
    finishes = {ts: asyncio.Event() for ts in ("101", "102", "103")}

    async def agent(event):
        await started.put(event.message_id)
        await finishes[event.message_id].wait()

    a._message_handler = agent
    task = asyncio.create_task(a._dispatch_observer_bootstrap_message(_history_event("101")))
    try:
        assert await asyncio.wait_for(started.get(), 3) == "101"
        if late:
            async def post_delivery():
                # Even an inherited replay context must not make a different
                # live event await its own background owner.
                await a._handle_slack_message(_history_event("102", "101"))
            key = next(iter(a._session_tasks))
            a._post_delivery_callbacks[key] = post_delivery
        else:
            await a._handle_slack_message(_history_event("102", "101"))
        finishes["101"].set()
        assert await asyncio.wait_for(started.get(), 3) == "102"
        await _settle()
        assert not task.done()
        assert ("101", "pending") in _claims(a)
        await a._handle_slack_message(_history_event("103", "101"))
        finishes["102"].set()
        assert await asyncio.wait_for(started.get(), 3) == "103"
        await _settle()
        assert not task.done()
    finally:
        for finish in finishes.values():
            finish.set()
        await task
        await a.cancel_background_tasks()
    assert ("101", "completed") in _claims(a)


@pytest.mark.asyncio
async def test_cancelled_dispatch_keeps_claim_and_slot_until_turn_finishes(catchup_adapter):
    a = catchup_adapter
    started, finish = asyncio.Event(), asyncio.Event()
    seen = []

    async def agent(event):
        seen.append(event.message_id)
        started.set()
        await finish.wait()

    a._message_handler = agent
    task = asyncio.create_task(a._dispatch_observer_bootstrap_message(_history_event("101")))
    retry = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        await _settle()
        task.cancel()  # Repeated cancellation must not detach the underlying turn.
        retry = asyncio.create_task(a._dispatch_observer_bootstrap_message(_history_event("101")))
        await _settle()
        assert _claims(a) == [("101", "pending")]
        assert not retry.done()
        assert seen == ["101"]
    finally:
        finish.set()
        await asyncio.gather(task, *([retry] if retry else []), return_exceptions=True)
        await a.cancel_background_tasks()
    assert task.cancelled()
    assert seen == ["101"]
    assert _claims(a) == [("101", "completed")]
    assert not a._session_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_catchup_failure_or_owner_cancel_does_not_leak(catchup_adapter, cancel_owner):
    a = catchup_adapter
    started, finish = asyncio.Event(), asyncio.Event()

    async def agent(event):
        started.set()
        await finish.wait()
        raise RuntimeError("mock agent failure")

    a._message_handler = agent
    task = asyncio.create_task(a._dispatch_observer_bootstrap_message(_history_event("101")))
    try:
        await asyncio.wait_for(started.wait(), 3)
        await _settle()
        assert not task.done()
        if cancel_owner:
            next(iter(a._session_tasks.values())).cancel()
        finish.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        if cancel_owner:
            assert isinstance(outcome, asyncio.CancelledError)
            assert _claims(a) == []
        else:
            # Base logs/notifies the error and swallows it: waiting is not a
            # new success signal and must not silently redesign that contract.
            assert outcome is None
            assert _claims(a) == [("101", "completed")]
            a.send.assert_awaited_once()
        a._message_handler = AsyncMock(return_value=None)
        await a._dispatch_observer_bootstrap_message(_history_event("102"))
        a._message_handler.assert_awaited_once()
        assert not a._session_tasks
        assert not a._active_sessions
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        await a.cancel_background_tasks()
