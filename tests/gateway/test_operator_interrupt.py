"""Operator interrupt stops only matching running turns."""
from types import SimpleNamespace

import pytest

from gateway import interrupt_control as ic
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL


class FakeAgent:
    def __init__(self):
        self.stopped = None

    def hard_interrupt(self, message=None):
        self.stopped = message


def test_matches_chat_segment_and_start_time():
    key = "agent:main:slack:group:T1:C0ALERT:1790.1"
    assert ic.matches(key, ["C0ALERT"], 100.0, 50.0)
    assert not ic.matches(key, ["C0ALERT"], 10.0, 50.0)
    assert not ic.matches(key, ["C0OTHER"], 100.0, None)
    assert not ic.matches("agent:main:slack:group:T1:C0ALERTX:1", ["C0ALERT"], 100.0, None)


def test_request_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "get_hermes_home", lambda: tmp_path)
    ic.write_request("r1", ["C1", " ", "C1"], 5.0, "burst")
    request = ic.read_request()
    assert request["chat_ids"] == ["C1"] and request["started_after"] == 5.0
    ic.finish("r1", ["k"])
    assert ic.read_request() is None
    assert (tmp_path / ic.RESULT_NAME).exists()
    with pytest.raises(ValueError):
        ic.write_request("r2", [], None, "x")


def test_runner_interrupts_only_matching_turns():
    alert, other, late = FakeAgent(), FakeAgent(), FakeAgent()
    runner = SimpleNamespace(
        _running_agents={
            "agent:main:slack:group:T1:C0ALERT:1": alert,
            "agent:main:slack:group:T1:C0WORK:2": other,
            "agent:main:slack:group:T1:C0ALERT:3": late,
            "agent:main:slack:group:T1:C0ALERT:4": _AGENT_PENDING_SENTINEL,
        },
        _running_agents_ts={"agent:main:slack:group:T1:C0ALERT:1": 100.0,
                            "agent:main:slack:group:T1:C0WORK:2": 100.0,
                            "agent:main:slack:group:T1:C0ALERT:3": 10.0},
    )
    hit = GatewayRunner._apply_operator_interrupt(
        runner, {"request_id": "r", "chat_ids": ["C0ALERT"], "started_after": 50.0, "reason": "burst"})
    assert hit == ["agent:main:slack:group:T1:C0ALERT:1"]
    assert alert.stopped and "burst" in alert.stopped
    assert other.stopped is None and late.stopped is None


def test_startup_does_not_wait_for_the_channel_directory():
    from pathlib import Path
    source = Path(__file__).resolve().parents[2].joinpath("gateway", "run.py").read_text(encoding="utf-8")
    assert "directory = await build_channel_directory(self.adapters)\n            ch_count" not in source.split("_build_channel_directory_in_background")[0]
    assert "_spawn_supervised(\n            _build_channel_directory_in_background" in source
