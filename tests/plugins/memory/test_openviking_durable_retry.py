"""Explicit memory mirrors to OpenViking survive failures (brain G-0077).

A memory addition succeeded locally but its OpenViking mirror only logged on
failure, so the two stores drifted apart silently. The mirror is now written
to a private pending record before the network attempt, cleared on success,
and replayed for the same actor when a client next attaches.
"""
import json
import threading
from unittest.mock import MagicMock

import plugins.memory.openviking as openviking_module
from plugins.memory.openviking import OpenVikingMemoryProvider


def _provider(tmp_path, agent="hermes"):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://test"
    provider._api_key = ""
    provider._account = "acct"
    provider._user = "usr"
    provider._agent = agent
    provider._hermes_home = str(tmp_path)
    return provider


def _stub_client(monkeypatch, *, fail, calls):
    class StubClient:
        def __init__(self, *a, **kw):
            pass

        def post(self, path, payload=None, **kwargs):
            calls.append((path, dict(payload or {})))
            if fail:
                raise RuntimeError("openviking unavailable")
            return {}

    monkeypatch.setattr(openviking_module, "_VikingClient", StubClient)


def _drain(provider):
    with provider._memory_write_lock:
        workers = list(provider._memory_write_threads)
    for worker in workers:
        worker.join(timeout=2.0)


def _pending(tmp_path, agent="hermes"):
    directory = tmp_path / "openviking" / "pending_memory_writes" / agent
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def test_failed_mirror_leaves_a_private_pending_record(tmp_path, monkeypatch):
    calls = []
    _stub_client(monkeypatch, fail=True, calls=calls)
    provider = _provider(tmp_path)

    provider.on_memory_write("add", "user", "remember this")
    _drain(provider)

    records = _pending(tmp_path)
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["content"] == "remember this"
    assert record["uri"] == calls[0][1]["uri"]
    assert record["uri"].startswith("viking://user/peers/hermes/memories/")
    assert records[0].stat().st_mode & 0o777 == 0o600


def test_successful_mirror_leaves_nothing_behind(tmp_path, monkeypatch):
    calls = []
    _stub_client(monkeypatch, fail=False, calls=calls)
    provider = _provider(tmp_path)

    provider.on_memory_write("add", "user", "remember this")
    _drain(provider)

    assert len(calls) == 1
    assert _pending(tmp_path) == []


def test_mirror_without_a_client_is_kept_for_later(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    provider._client = None
    monkeypatch.setattr(provider, "_ensure_client", lambda: False)

    provider.on_memory_write("add", "user", "remember offline")

    records = _pending(tmp_path)
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["content"] == "remember offline"


def test_recovery_replays_the_same_uri_and_clears_on_success(tmp_path, monkeypatch):
    failing = []
    _stub_client(monkeypatch, fail=True, calls=failing)
    provider = _provider(tmp_path)
    provider.on_memory_write("add", "user", "remember this")
    _drain(provider)
    uri = failing[0][1]["uri"]

    replayed = []
    _stub_client(monkeypatch, fail=False, calls=replayed)
    provider._recover_pending_memory_writes()
    _drain(provider)

    assert [payload["uri"] for _, payload in replayed] == [uri]
    assert replayed[0][1]["content"] == "remember this"
    assert _pending(tmp_path) == []


def test_recovery_only_replays_records_of_its_own_actor(tmp_path, monkeypatch):
    failing = []
    _stub_client(monkeypatch, fail=True, calls=failing)
    tars = _provider(tmp_path, agent="tars")
    tars.on_memory_write("add", "user", "tars memory")
    _drain(tars)

    replayed = []
    _stub_client(monkeypatch, fail=False, calls=replayed)
    default = _provider(tmp_path, agent="hermes")
    default._recover_pending_memory_writes()
    _drain(default)

    assert replayed == []
    assert len(_pending(tmp_path, agent="tars")) == 1


def test_a_record_that_keeps_failing_is_dropped_after_the_cap(tmp_path, monkeypatch):
    calls = []
    _stub_client(monkeypatch, fail=True, calls=calls)
    provider = _provider(tmp_path)
    provider.on_memory_write("add", "user", "never lands")
    _drain(provider)

    for _ in range(openviking_module._PENDING_MEMORY_WRITE_MAX_ATTEMPTS):
        provider._recover_pending_memory_writes()
        _drain(provider)

    assert _pending(tmp_path) == []
    assert len(calls) == openviking_module._PENDING_MEMORY_WRITE_MAX_ATTEMPTS


def test_shutdown_still_waits_for_replay_workers(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class SlowClient:
        def __init__(self, *a, **kw):
            pass

        def post(self, path, payload=None, **kwargs):
            started.set()
            release.wait(timeout=2.0)
            return {}

    failing = []
    _stub_client(monkeypatch, fail=True, calls=failing)
    provider = _provider(tmp_path)
    provider.on_memory_write("add", "user", "slow")
    _drain(provider)

    monkeypatch.setattr(openviking_module, "_VikingClient", SlowClient)
    provider._recover_pending_memory_writes()
    assert started.wait(timeout=2.0)
    release.set()
    provider.shutdown()
    assert provider._memory_write_threads == set()
