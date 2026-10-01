"""Regression tests for the Heimdall-only gate in ``#error-alert``."""

import asyncio
import importlib.util
import sys
from pathlib import Path
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
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, ProcessingOutcome, SendResult  # noqa: E402
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
    # Load the isolated authoritative sibling plugin, never an installed profile.
    plugin = Path(__file__).resolve().parents[3] / "petasos/plugins/offon/__init__.py"
    spec = importlib.util.spec_from_file_location("plugins.offon", plugin,
        submodule_search_locations=[str(plugin.parent)])
    offon = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "plugins.offon", offon)
    spec.loader.exec_module(offon)
    offon.safety = sys.modules["plugins.offon.safety"]
    monkeypatch.setitem(sys.modules, "hermes_plugins.offon", offon)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(offon.tools.channels, "_hermes_home", lambda: tmp_path / ".hermes")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["planned", "active", "continuing", "done"])
async def test_fixed_manifest_excludes_normal_outside_window_history(catchup_adapter, monkeypatch, tmp_path, state):
    a = catchup_adapter
    recovery = importlib.import_module("plugins.offon.recovery")
    manifest_path = tmp_path / "fixed-manifest.json"
    manifest_path.write_text("{}")
    operator = MagicMock()
    operator.manifest_path = manifest_path
    operator.manifest.return_value = {"state": state}
    operator.round = AsyncMock(return_value=False)
    monkeypatch.setattr(recovery, "Operator", lambda: operator)
    monkeypatch.setattr(a, "_slack_history_catchup_channels", lambda: {"C_TEST"})
    history = AsyncMock()
    history.conversations_history.return_value = {"messages": [_history_event("1790672399"), _history_event("1790844901")]}
    a._team_clients = {"T_TEST": history}
    normal = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(observer_catchup, "run_workspace_catchups", normal)
    monkeypatch.setattr(a, "_dispatch_observer_bootstrap_message", dispatch)
    await a._run_observer_history_catchup()
    normal.assert_not_awaited()
    history.conversations_history.assert_not_awaited()
    dispatch.assert_not_awaited()
    if state == "planned":
        operator.round.assert_not_awaited()
    else:
        operator.round.assert_awaited_once_with(a, observer_catchup)


def _claims(adapter):
    return adapter._observer_catchup_conn.execute(
        "SELECT ts, status FROM claimed_messages ORDER BY CAST(ts AS REAL)"
    ).fetchall()


async def _settle():
    # Yield scheduling, not a wall-clock assumption about agent completion.
    for _ in range(20):
        await asyncio.sleep(0)


async def _successful_silent_agent(event):
    event.record_execution_result({"completed": True, "api_calls": 1, "final_response": "NO_REPLY"})
    return None


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
        await _successful_silent_agent(event)

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
async def test_stalled_replay_releases_the_slot(catchup_adapter, monkeypatch):
    a = catchup_adapter
    monkeypatch.setattr(type(a), "OBSERVER_REPLAY_STALL_SECONDS", 0.2)
    never = asyncio.Event()
    seen = []

    async def agent(event):
        seen.append(event.message_id)
        if event.message_id == "101":
            await never.wait()
        await _successful_silent_agent(event)

    a._message_handler = agent
    try:
        with pytest.raises(observer_catchup.ReplayNotCompleted, match="dispatch_stalled"):
            await asyncio.wait_for(a._dispatch_observer_bootstrap_message(_history_event("101")), 3)
        await asyncio.wait_for(a._dispatch_observer_bootstrap_message(_history_event("102")), 3)
        assert seen == ["101", "102"]
    finally:
        never.set()
        await _settle()
        await a.cancel_background_tasks()


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
        await _successful_silent_agent(event)

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
        await _successful_silent_agent(event)

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
        await _successful_silent_agent(event)

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
            assert isinstance(outcome, observer_catchup.ReplayNotCompleted)
            assert _claims(a) == []
        a._message_handler = AsyncMock(side_effect=_successful_silent_agent)
        await a._dispatch_observer_bootstrap_message(_history_event("102"))
        a._message_handler.assert_awaited_once()
        assert not a._session_tasks
        assert not a._active_sessions
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        await a.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("replay_first", [False, True])
async def test_queued_replay_rebinds_source_for_two_real_tool_invocations(catchup_adapter, monkeypatch, replay_first):
    import json
    import threading
    from types import SimpleNamespace
    from gateway.run import GatewayRunner
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools.registry import ToolRegistry
    offon = sys.modules["plugins.offon"]
    safety, tools = offon.safety, offon.tools
    a = catchup_adapter
    started, release = asyncio.Event(), asyncio.Event()
    seen = []
    registry = ToolRegistry()
    offon.register(SimpleNamespace(profile_name="observer", register_tool=registry.register))
    handler = registry.get_entry(offon.schemas.FIND_TASK["name"]).handler
    assert handler is offon.tools.find_task
    assert handler.__globals__["safety"] is safety
    assert sys.modules["hermes_plugins.offon"].tools is tools
    runner = object.__new__(GatewayRunner)
    loop_thread = threading.get_ident()
    mapping = SimpleNamespace(workspace=SimpleNamespace(workspace_id="T_TEST"), pair_for=lambda _: None)
    monkeypatch.setattr(tools.channels, "load", lambda: mapping)
    lookup = MagicMock(return_value=SimpleNamespace(body={"found": False}))
    monkeypatch.setattr(tools.client, "find_task", lookup)
    monkeypatch.setattr(tools.client, "get_context", lambda: {"projects": [], "members": []})

    def tool_thread():
        assert threading.get_ident() != loop_thread
        current = safety.source()
        slack = tools.envelope.read_envelope()
        assert slack.channel_id == current.channel_id
        assert slack.message_ts == current.message_ts
        assert slack.request_ts == current.root_ts
        return (current.message_ts, current.root_ts, current.recovery,
                json.loads(handler({}))["found"])

    async def agent(event):
        tokens = set_session_vars(platform="slack", chat_id=event.source.chat_id,
            thread_id=event.source.thread_id or "", message_id=event.message_id,
            user_id=event.source.user_id, chat_name="synthetic")
        try:
            seen.append(await runner._run_in_executor_with_context(tool_thread))
        finally:
            clear_session_vars(tokens)
        if event.message_id == "1900000000.000001":
            started.set()
            await release.wait()
        await _successful_silent_agent(event)

    a._message_handler = agent
    replay = None
    try:
        if replay_first:
            replay = asyncio.create_task(a._dispatch_observer_bootstrap_message(
                _history_event("1900000000.000001")))
        else:
            await a._handle_slack_message(_history_event("1900000000.000001"))
        await asyncio.wait_for(started.wait(), 3)
        if replay_first:
            await a._handle_slack_message(_history_event("1900000001.000001", "1900000000.000001"))
        else:
            replay = asyncio.create_task(a._dispatch_observer_bootstrap_message(
                _history_event("1900000001.000001", "1900000000.000001")))
        await _settle()
        assert not replay.done()
        release.set()
        await replay
        assert seen == [("1900000000.000001", "1900000000.000001", replay_first, False),
                        ("1900000001.000001", "1900000000.000001", not replay_first, False)]
        assert lookup.call_count == 2
        assert all(call.args == ("T_TEST", "C_TEST", "1900000000.000001") for call in lookup.call_args_list)
        assert safety.source() is None
        assert not a._session_tasks
        assert not a._active_sessions
    finally:
        release.set()
        if replay is not None:
            await asyncio.gather(replay, return_exceptions=True)
        await a.cancel_background_tasks()
        runner._shutdown_executor()


@pytest.mark.asyncio
async def test_replay_suppresses_real_customer_send_edit_and_base_error_keeps_internal_notice(catchup_adapter, monkeypatch):
    offon = sys.modules["plugins.offon"]
    a = catchup_adapter
    # Undo fixture transport substitution: test the real final output gates.
    a.send = SlackAdapter.send.__get__(a)
    internal = MagicMock(return_value=True)
    monkeypatch.setattr(offon.tools.notify, "announce", internal)

    async def agent(event):
        sent = await a.send("C_TEST", "synthetic customer response")
        edited = await a.edit_message("C_TEST", "1900000000.000001", "synthetic update")
        assert sent.error == "trusted_replay_customer_output_suppressed"
        assert edited.error == "trusted_replay_customer_output_suppressed"
        offon.tools.notify.announce("SYNTH_WEBHOOK", "synthetic internal task notice")
        raise RuntimeError("synthetic handler failure")

    a._message_handler = agent
    with pytest.raises(observer_catchup.ReplayNotCompleted):
        await a._dispatch_observer_bootstrap_message(_history_event("1900000000.000001"))
    assert _claims(a) == []
    assert "1900000000.000001" not in a._processed_message_ts
    internal.assert_called_once_with("SYNTH_WEBHOOK", "synthetic internal task notice")
    a._app.client.chat_postMessage.assert_not_awaited()
    a._app.client.chat_update.assert_not_awaited()
    a._message_handler = AsyncMock(side_effect=_successful_silent_agent)
    await a._dispatch_observer_bootstrap_message(_history_event("1900000000.000001"))
    assert _claims(a) == [("1900000000.000001", "completed")]
    await a.cancel_background_tasks()


@pytest.mark.asyncio
async def test_handler_failure_cannot_advance_actual_catchup_checkpoint(catchup_adapter):
    a = catchup_adapter
    a._message_handler = AsyncMock(side_effect=RuntimeError("synthetic failure"))
    store = a._observer_catchup_conn
    observer_catchup.record_bootstrap_completion(store, "T_TEST", "C_TEST", 100)
    observer_catchup.advance_channel_checkpoint(store, "T_TEST", "C_TEST", "100", 100)
    client = AsyncMock()
    client.conversations_history.return_value = {"messages": [_history_event("101")]}
    result = await observer_catchup.run_catchup(client=client,
        dispatch=a._dispatch_observer_bootstrap_message, conn=store, workspace_id="T_TEST",
        allowed_channels={"C_TEST"}, config=observer_catchup.CatchupConfig(
            enabled=True, source_channels=frozenset({"C_TEST"})), now=200)
    assert result["C_TEST"].error
    assert _claims(a) == []
    assert observer_catchup.get_channel_checkpoint(store, "T_TEST", "C_TEST") == "100"
    await a.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["factory", "enter"])
@pytest.mark.parametrize("queue_drain", [False, True])
async def test_base_scope_admission_failure_finishes_lifetime(catchup_adapter, failure_at, queue_drain):
    a = catchup_adapter
    source = a.build_source(chat_id="C_TEST", chat_type="group", user_id="U_USER",
                            thread_id="1900000000.000001")
    failed = MessageEvent(text="synthetic request", source=source, message_id="1900000000.000001")
    followup = MessageEvent(text="synthetic followup", source=source, message_id="1900000001.000001",
                            execution_context=lambda: nullcontext())

    class EnterFailure:
        def __enter__(self):
            raise RuntimeError("synthetic scope enter failure")

        def __exit__(self, *exc):
            raise AssertionError("failed enter must not call exit")

    def factory_failure():
        raise RuntimeError("synthetic scope factory failure")

    failed.execution_context = factory_failure if failure_at == "factory" else EnterFailure
    completions = []
    seen = []
    deferred = MagicMock()

    async def hook(name, event, outcome=None):
        if name == "on_processing_complete":
            completions.append((event, outcome))
            if event is failed and queue_drain:
                # The failed owner still holds its guard here. Use Base's
                # actual busy path instead of replacing the drain function.
                await a.handle_message(followup)

    async def agent(event):
        seen.append(event)

    a._run_processing_hook = hook
    a._message_handler = agent
    await a.handle_message(failed)
    key = next(iter(a._session_tasks))
    owner = a._session_tasks[key]
    a._post_delivery_callbacks[key] = deferred
    await asyncio.wait_for(asyncio.shield(owner), 3)
    while a._session_tasks:
        drain = next(iter(a._session_tasks.values()))
        assert not drain.done(), "finished owner retained in session map"
        await asyncio.wait_for(asyncio.shield(drain), 3)
    await _settle()
    assert failed.handler_outcome is ProcessingOutcome.FAILURE
    assert completions[0] == (failed, ProcessingOutcome.FAILURE)
    assert seen == ([followup] if queue_drain else [])
    if queue_drain:
        assert followup.handler_outcome is ProcessingOutcome.SUCCESS
    deferred.assert_not_called()
    a.send.assert_not_awaited()
    assert not a._active_sessions
    assert not a._session_tasks
    assert not a._pending_messages
    assert not a._post_delivery_callbacks
    assert not a._background_tasks
    assert not a._text_debounce_store()
    # Another actual owner must be able to run without stale-lock healing.
    await a.handle_message(followup)
    await asyncio.wait_for(asyncio.shield(next(iter(a._session_tasks.values()))), 3)
    assert seen[-1] is followup
    assert not a._active_sessions
    assert not a._session_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("result,error", [
    ({"failed": True, "api_calls": 1, "error": "synthetic failure", "final_response": "NO_REPLY"}, "model_error"),
    ({"failed": True, "api_calls": 1, "error": "busy", "final_response": "NO_REPLY"}, "model_busy"),
    ({"completed": True, "api_calls": 0, "final_response": "NO_REPLY"}, "model_error"),
    (None, "model_outcome_missing"),
])
async def test_actual_model_failure_busy_and_missing_result_never_complete(catchup_adapter, result, error):
    a = catchup_adapter
    async def agent(event):
        if result is not None:
            event.record_execution_result(result)
        return None
    a._message_handler = agent
    with pytest.raises(observer_catchup.ReplayNotCompleted, match=error):
        await a._dispatch_observer_bootstrap_message(_history_event("1900000000.000001"))
    assert _claims(a) == []
    assert "1900000000.000001" not in a._processed_message_ts
    await a.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,silent,completed", [
    ("source_event_has_no_mutating_request", True, True),
    ("source_event_has_no_mutating_request", False, False),
    ("completion_or_request_ambiguous", True, False),
])
async def test_uncertain_rejection_requires_known_model_skip(catchup_adapter, reason, silent, completed):
    a = catchup_adapter
    offon = sys.modules["plugins.offon"]
    async def agent(event):
        store = offon.tools.delivery.Store()
        store.begin("create")
        store.finish("create", {"success": False, "skipped": False, "safetyState": "uncertain", "reason": reason})
        event.record_execution_result({"completed": True, "api_calls": 1,
                                       "final_response": "NO_REPLY" if silent else "needs review"})
        return None
    a._message_handler = agent
    if completed:
        await a._dispatch_observer_bootstrap_message(_history_event("1900000000.000001"))
        assert _claims(a) == [("1900000000.000001", "completed")]
    else:
        with pytest.raises(observer_catchup.ReplayNotCompleted, match="business_outcome_unknown"):
            await a._dispatch_observer_bootstrap_message(_history_event("1900000000.000001"))
        assert _claims(a) == []
    await a.cancel_background_tasks()


@pytest.mark.asyncio
async def test_known_customer_output_boundaries_never_reach_rpc(catchup_adapter):
    a = catchup_adapter
    a.send = SlackAdapter.send.__get__(a)
    with observer_catchup.event_execution_scope("C_TEST", "1900000000.000001", "1900000000.000001", True):
        results = [
            await a.send("C_TEST", "synthetic"),
            await a.edit_message("C_TEST", "1900000000.000001", "synthetic"),
            await a.send_private_notice("C_TEST", "U_USER", "synthetic"),
            await a.send_or_update_status("C_TEST", "synthetic", "synthetic"),
            await a.send_image("C_TEST", "https://example.invalid/synthetic.png"),
            await a.send_document("C_TEST", "/synthetic/not-a-real-document"),
        ]
        assert all(result.error == "trusted_replay_customer_output_suppressed" for result in results)
        assert await a.delete_message("C_TEST", "1900000000.000001") is False
        assert await a._add_reaction("C_TEST", "1900000000.000001", "eyes") is False
        assert await a.send_typing("C_TEST") is None
        assert await a.stop_typing("C_TEST") is None
        assert await a.create_handoff_thread("C_TEST", "synthetic") is None
    assert a._app.client.mock_calls == []
