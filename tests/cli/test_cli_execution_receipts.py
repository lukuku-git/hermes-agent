"""Exercise production CLI boundaries; model/tool results are explicit fixtures."""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import cli
from hermes_cli import oneshot


MODEL_TEXT = "답변\n근거: fabricated\n확인할 사람: 담당자"
EXPECTED = "답변\n확인할 사람: 담당자\n근거: brain D-0086(active)"


def run_result(with_receipts=True):
    result = {
        "final_response": MODEL_TEXT,
        "messages": [{"role": "assistant", "content": MODEL_TEXT}],
        "completed": True,
        "partial": True,  # no unrelated title-generation network request
    }
    if with_receipts:
        result["tool_execution_results"] = [{
            "tool_call_id": "fixture-vat",
            "name": "terminal",
            "arguments": {"command": "vat --workspace /Users/zeus/lukuku-os brain query D-0086", "timeout": 60},
            "result": {"exit_code": 0, "output": (
                "INFO  D-0086  fixture decision  active\n"
                "      decisions/D-0086-fixture.md\n"
                "1 result. Open the records themselves; this is an index, not an answer.\n"
            )},
        }]
    return result


@pytest.fixture
def cli_instance(monkeypatch):
    monkeypatch.setattr(cli, "get_tool_definitions", lambda **kwargs: [])
    instance = cli.HermesCLI(toolsets=["terminal"])
    monkeypatch.setattr(instance, "_ensure_runtime_credentials", lambda: True)
    monkeypatch.setattr(instance, "_resolve_turn_agent_config", lambda message: {
        "signature": instance._active_agent_route_signature,
        "model": None, "runtime": None, "request_overrides": None,
    })
    monkeypatch.setattr(instance, "_init_agent", lambda **kwargs: True)
    monkeypatch.setattr(instance, "_claim_active_session", lambda *args, **kwargs: True)
    monkeypatch.setattr(instance, "_show_security_advisories", lambda: None)
    monkeypatch.setattr(instance, "_print_exit_summary", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_finalize_single_query", lambda instance: None)
    monkeypatch.setattr(cli.atexit, "register", lambda *args, **kwargs: None)
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    instance._voice_tts = False
    instance._voice_mode = False
    instance.bell_on_complete = False
    return instance


@pytest.mark.parametrize("surface", ["interactive", "q", "Q"])
@pytest.mark.parametrize("with_receipts", [True, False])
def test_actual_cli_final_output_is_code_owned(surface, with_receipts, cli_instance, monkeypatch, capsys):
    instance = cli_instance
    result = run_result(with_receipts)
    expected = EXPECTED if with_receipts else "답변\n확인할 사람: 담당자"
    calls = []

    def run_conversation(**kwargs):
        calls.append(kwargs)
        # Tokens split inside the fake receipt prefix, plus an intermediate
        # tool boundary: nothing unfinalized may reach the display.
        if surface != "Q":
            instance._stream_delta("답변\n근")
            instance._stream_delta("거: fabricated\n")
            instance._stream_delta(None)
            instance._stream_delta(MODEL_TEXT)
            assert "fabricated" not in capsys.readouterr().out
        return result

    instance.agent = SimpleNamespace(
        run_conversation=run_conversation, session_id=instance.session_id,
        platform="cli", max_iterations=500,
    )
    class ExistingCLI(cli.HermesCLI):
        def __new__(cls, **kwargs):
            return instance

    monkeypatch.setattr(cli, "HermesCLI", ExistingCLI)
    if surface == "interactive":
        response = instance.chat("fixture question")
        assert response == expected
    elif surface == "Q":
        with pytest.raises(SystemExit) as exc:
            cli.main(query="fixture question", quiet=True, toolsets="terminal")
        assert exc.value.code == 0
    else:
        cli.main(query="fixture question", quiet=False, toolsets="terminal")
    output = capsys.readouterr().out
    assert "fabricated" not in output
    assert output.count("brain D-0086(active)") == int(with_receipts)
    assert "답변" in output
    assert len(calls) == 1
    assert result["final_response"] == MODEL_TEXT
    assert result["messages"][0]["content"] == MODEL_TEXT


@pytest.mark.parametrize("entrypoint", ["helper", "stdout"])
@pytest.mark.parametrize("with_receipts", [True, False])
def test_oneshot_run_agent_finalizes_return_without_rewriting_result(with_receipts, entrypoint, monkeypatch, capsys):
    result = run_result(with_receipts)
    agent = MagicMock()
    agent.run_conversation.return_value = result
    monkeypatch.setattr("run_agent.AIAgent", lambda **kwargs: agent)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kwargs: {})
    monkeypatch.setattr("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build", lambda **kwargs: None)
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    expected = EXPECTED if with_receipts else "답변\n확인할 사람: 담당자"
    if entrypoint == "helper":
        response, returned = oneshot._run_agent("fixture", model="", use_config_toolsets=False)
        assert response == expected
        assert returned is result
    else:
        # Real -z stdout boundary and helper, not a wrapper that monkeypatches
        # the missing formatter into the path under test.
        monkeypatch.setenv("HERMES_YOLO_MODE", "0")
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "0")
        monkeypatch.setattr(oneshot, "declare_stateless_channel", lambda: None)
        disabled = logging.root.manager.disable
        try:
            assert oneshot.run_oneshot("fixture", model="") == 0
        finally:
            logging.disable(disabled)
        assert capsys.readouterr().out == expected + "\n"
    assert result["final_response"] == MODEL_TEXT
    assert result["messages"][0]["content"] == MODEL_TEXT
    agent.run_conversation.assert_called_once_with("fixture")
    agent.close.assert_called_once()


def test_interrupted_cli_receipt_is_after_runtime_footer(cli_instance, capsys):
    result = run_result()
    result.update(interrupted=True, interrupt_message="next question")
    cli_instance.agent = SimpleNamespace(
        run_conversation=lambda **kwargs: result,
        session_id=cli_instance.session_id, platform="cli", max_iterations=500,
    )
    response = cli_instance.chat("fixture")
    assert response.endswith("확인할 사람: 담당자\n근거: brain D-0086(active)")
    assert response.count("근거:") == 1
    assert "Interrupted" in response
    assert "fabricated" not in capsys.readouterr().out
