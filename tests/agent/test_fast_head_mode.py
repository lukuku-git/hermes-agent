from types import SimpleNamespace
from unittest.mock import patch

from agent import system_prompt
from agent.agent_init import _load_initial_tools
from run_agent import AIAgent


def test_fast_head_system_prompt_is_literal_and_does_not_read_context(monkeypatch):
    prompt = "Small stable prompt."
    agent = SimpleNamespace(_fast_head_system_prompt=prompt)
    monkeypatch.setattr(system_prompt, "_ra", lambda: (_ for _ in ()).throw(AssertionError("context lookup")))

    assert system_prompt.build_system_prompt_parts(agent) == {
        "stable": prompt,
        "context": "",
        "volatile": "",
    }
    assert system_prompt.build_system_prompt(agent) == prompt
    assert agent._cached_system_prompt_static == prompt


def test_tool_free_agent_skips_tool_schema_discovery():
    discover = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("tool discovery"))
    assert _load_initial_tools(
        tool_free=True,
        loader=discover,
        enabled_toolsets=None,
        disabled_toolsets=None,
        quiet_mode=True,
    ) == []


def test_aiagent_forwards_fast_head_construction_flags():
    with patch("agent.agent_init.init_agent") as init:
        AIAgent(fast_head_system_prompt="Fast prompt", tool_free=True)

    kwargs = init.call_args.kwargs
    assert kwargs["fast_head_system_prompt"] == "Fast prompt"
    assert kwargs["tool_free"] is True


def test_fast_head_initializes_hard_transport_budget_and_disables_compression():
    with patch("agent.agent_init.init_agent") as init:
        AIAgent(fast_head_system_prompt="Fast prompt", tool_free=True)
    assert init.call_args.kwargs["fast_head_system_prompt"] == "Fast prompt"


def test_fast_head_finalizer_never_uses_summary_fallback():
    from agent.turn_finalizer import fast_head_allows_summary_fallback

    assert fast_head_allows_summary_fallback(SimpleNamespace(_fast_head_execution=True)) is False
    assert fast_head_allows_summary_fallback(SimpleNamespace(_fast_head_execution=False)) is True


def test_fast_head_transport_budget_rejects_second_physical_call():
    from agent.conversation_loop import claim_fast_head_transport_call

    agent = SimpleNamespace(
        _fast_head_execution=True,
        _fast_head_transport_calls=0,
        _fast_head_transport_call_budget=1,
    )
    claim_fast_head_transport_call(agent)
    assert agent._fast_head_transport_calls == 1
    try:
        claim_fast_head_transport_call(agent)
    except RuntimeError as exc:
        assert "transport call budget" in str(exc)
    else:
        raise AssertionError("second transport call was not rejected")
