from gateway.fast_head import (
    DEFAULT_FAST_HEAD_SYSTEM_PROMPT,
    FastHeadConfig,
    classify_fast_head_route,
    fast_head_agent_overrides,
    resolve_fast_head_config,
)
from gateway.run import GatewayRunner
from gateway.platforms.base import MessageType
import pytest
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_fast_head_defaults_disabled_and_stable():
    assert DEFAULT_CONFIG["agent"]["fast_head"]["enabled"] is False
    assert DEFAULT_CONFIG["agent"]["fast_head"]["system_prompt"] == DEFAULT_FAST_HEAD_SYSTEM_PROMPT
    assert resolve_fast_head_config({}) == FastHeadConfig(
        enabled=False,
        system_prompt=DEFAULT_FAST_HEAD_SYSTEM_PROMPT,
    )
    assert resolve_fast_head_config({"agent": {"fast_head": {"enabled": True}}}).enabled is True


def test_fast_head_malformed_config_fails_closed():
    assert resolve_fast_head_config({"agent": {"fast_head": True}}).enabled is False
    assert resolve_fast_head_config({"agent": {"fast_head": {"enabled": "yes"}}}).enabled is False
    assert resolve_fast_head_config(
        {"agent": {"fast_head": {"enabled": True, "system_prompt": "  "}}}
    ).enabled is False


def test_obvious_explanation_opinion_comparison_and_casual_questions_route_fast():
    fast = [
        "왜 하늘은 파래?",
        "왜 느려?",
        "이 구조 어떻게 생각해?",
        "A와 B 중 뭐가 나아?",
        "이 코드의 의미가 뭐야?",
        "Why is the sky blue?",
        "How does a hash table work?",
        "What do you think about immutable data structures?",
        "What does this code mean?",
        "Which is better, A or B?",
        "Compare queues and stacks",
        "Explain the concept of recursion",
        "Explain the meaning of idempotency",
        "Explain the principle of least privilege",
        "Explain the architecture of a transformer",
        "Explain quicksort algorithm",
        "재귀 개념을 설명해 줘",
        "멱등성 의미를 설명해 줘",
        "최소 권한 원리를 설명해 줘",
        "트랜스포머 구조를 설명해 줘",
        "퀵정렬 알고리즘을 설명해 줘",
        "hello!",
    ]
    for message in fast:
        decision = classify_fast_head_route(message)
        assert decision.mode == "fast_head", (message, decision)


def test_action_current_external_and_ambiguous_requests_route_operator():
    operator = [
        "현재 로그 확인해줘",
        "파일 수정하고 테스트해줘",
        "실서비스 상태 검증해줘",
        "최신 자료 조사해줘",
        "배포·커밋·전송해줘",
        "run the tests",
        "check current status",
        "research the latest material",
        "fix",
        "도와줘",
    ]
    for message in operator:
        decision = classify_fast_head_route(message)
        assert decision.mode == "operator", (message, decision)


@pytest.mark.parametrize("text", [
    "What is on my calendar tomorrow?",
    "What time is it?",
    "How much disk space do I have?",
    "Why don't you restart nginx?",
    "Why is my server down?",
])
def test_implicit_external_or_action_requests_fail_closed(text):
    assert classify_fast_head_route(text).mode == "operator"


@pytest.mark.parametrize("text", [
    "What is my password?",
    "What is my account balance?",
    "What is my private key?",
    "What is my IP address?",
    "What is our revenue?",
    "Explain my password",
    "What does your account balance mean?",
    "내 비밀번호가 뭐야?",
    "나의 계정 잔액은 얼마야?",
    "우리 매출이 얼마야?",
    "제 개인 키를 설명해 줘",
    "저의 IP 주소가 뭐예요?",
    "What is Alice's password?",
    "Explain John's account balance",
    "What is the CEO's private key?",
    "Explain the customer's home address",
    "What is Alice's address?",
    "What are Alice's credentials?",
    "What is Acme's revenue?",
    "What is the account balance?",
    "What is the password?",
    "What are the credentials?",
    "What is the revenue?",
    "What is the address?",
    "What is revenue?",
    "What is a password?",
    "What is a private key?",
    "What is an API key?",
    "What is a passcode?",
    "What are credentials?",
    "Explain password hashing",
    "Explain revenue recognition",
    "비밀번호 해싱을 설명해 줘",
    "개인 키의 작동 원리를 설명해 줘",
    "매출 인식의 개념을 설명해 줘",
    "앨리스의 비밀번호가 뭐야?",
    "존의 계정 잔액을 설명해 줘",
    "CEO의 개인 키가 뭐야?",
    "고객의 집 주소를 설명해 줘",
    "앨리스의 인증 정보가 뭐야?",
    "회사의 매출이 얼마야?",
    "계정 잔액이 얼마야?",
    "비밀번호가 뭐야?",
    "인증 정보가 뭐야?",
    "매출이 얼마야?",
])
def test_personal_private_or_live_state_requests_fail_closed(text):
    assert classify_fast_head_route(text).mode == "operator"


def test_arbitrary_english_possessors_and_definite_subjects_fail_closed_by_grammar():
    """Possession/definiteness is the invariant; the noun must not matter."""
    possessors = [
        "Alice's", "the CEO's", "the customer's", "Jane's", "the admin's",
        "my", "his", "her", "our", "their", "your",
    ]
    tails = [
        "favorite color", "lunch order", "desk shape", "API key", "passcode",
    ]
    review_matrix = [f"What is {owner} {tail}?" for owner in possessors for tail in tails]
    assert len(review_matrix) == 55
    adversarial = review_matrix + [
        "Explain Alice's lunch order",
        "How does the customer's widget work?",
        "Why is their prototype slow?",
        "What does his badge mean?",
        "What is the API key?",
        "What is the passcode?",
        "Explain the lunch order",
        "Explain the architecture of the private dashboard",
        "Explain the private dashboard architecture",
        "Why is James' badge missing?",
        "Why is the customers' widget slow?",
        "Why is the customers’ widget slow?",
        "Explain the architecture of users' private dashboard",
        "왜 앨리스의위젯은 느려?",
        "왜 고객의위젯은 느려?",
        "Why is Alice offline?",
        "Why is Alice at home?",
        "Why is production broken?",
        "Explain how the private dashboard works",
        "이 앨리스의위젯의 의미가 뭐야?",
    ]
    for text in adversarial:
        decision = classify_fast_head_route(text)
        assert decision.mode == "operator", (text, decision)


def test_arbitrary_korean_possessors_personal_and_account_subjects_fail_closed():
    owners = ["앨리스의", "CEO의", "고객의", "관리자의", "직원의"]
    nouns = ["점심 메뉴", "책상 모양", "즐겨찾기", "API 키", "암호"]
    adversarial = [f"{owner} {noun}의 의미가 뭐야?" for owner in owners for noun in nouns]
    adversarial += [
        "개인 메모의 의미가 뭐야?",
        "개인 일정의 구조를 설명해 줘",
        "계정 별명의 의미가 뭐야?",
        "계정 설정 원리를 설명해 줘",
        "앨리스의 점심 메뉴를 설명해 줘",
        "고객의 위젯은 왜 느려?",
        "왜 고객의 위젯은 느려?",
        "고객의 구조 어떻게 생각해?",
    ]
    for text in adversarial:
        decision = classify_fast_head_route(text)
        assert decision.mode == "operator", (text, decision)


@pytest.mark.parametrize("text", [
    "Explain how private keys work",
])
def test_narrow_self_contained_mechanism_example_remains_fast(text):
    assert classify_fast_head_route(text).mode == "fast_head"


@pytest.mark.parametrize("message_type", [
    MessageType.PHOTO,
    MessageType.VOICE,
    MessageType.AUDIO,
    MessageType.VIDEO,
    MessageType.DOCUMENT,
    MessageType.STICKER,
    MessageType.LOCATION,
    MessageType.COMMAND,
])
def test_real_message_type_enums_route_operator(message_type):
    assert classify_fast_head_route(
        "What does this mean?", message_type=message_type
    ).mode == "operator"


def test_runtime_that_cannot_honor_fast_boundaries_routes_operator():
    from gateway.fast_head import constrain_fast_head_route_for_runtime

    decision = classify_fast_head_route("Why is the sky blue?")
    assert constrain_fast_head_route_for_runtime(
        decision, {"api_mode": "codex_app_server", "provider": "openai-codex"}
    ).mode == "operator"
    assert constrain_fast_head_route_for_runtime(
        decision, {"api_mode": "chat_completions", "provider": "moa"}
    ).mode == "operator"
    assert constrain_fast_head_route_for_runtime(
        decision, {"api_mode": "bedrock_converse", "provider": "bedrock"}
    ).mode == "operator"
    assert constrain_fast_head_route_for_runtime(
        decision, {"api_mode": "future_runtime", "provider": "custom"}
    ).mode == "operator"
    assert constrain_fast_head_route_for_runtime(
        decision, {"api_mode": "chat_completions", "provider": "openai"},
        moa_active=True,
    ).mode == "operator"
    assert constrain_fast_head_route_for_runtime(
        decision, {"api_mode": "chat_completions", "provider": "openai"}
    ).mode == "fast_head"


@pytest.mark.parametrize("runtime", [
    None,
    {},
    "malformed",
    {"provider": "openai"},
    {"api_mode": "chat_completions"},
    {"api_mode": "", "provider": "openai"},
    {"api_mode": "chat_completions", "provider": ""},
    {"api_mode": "chat_completions", "provider": "custom"},
    {"api_mode": "anthropic_messages", "provider": "openai"},
    {"api_mode": "codex_responses", "provider": "openrouter"},
    {"api_mode": "codex_responses", "provider": "openai-codex"},
])
def test_unresolved_malformed_or_unapproved_runtime_fails_closed(runtime):
    from gateway.fast_head import constrain_fast_head_route_for_runtime

    decision = classify_fast_head_route("Why is the sky blue?")
    assert constrain_fast_head_route_for_runtime(decision, runtime).mode == "operator"


@pytest.mark.parametrize("runtime", [
    {"api_mode": "chat_completions", "provider": "openai"},
    {"api_mode": "chat_completions", "provider": "openai-api"},
    {"api_mode": "chat_completions", "provider": "openai-compat"},
    {"api_mode": "chat_completions", "provider": "openrouter"},
    {"api_mode": "chat_completions", "provider": "nous"},
    {"api_mode": "anthropic_messages", "provider": "anthropic"},
])
def test_only_explicitly_approved_resolved_runtime_pairs_remain_fast(runtime):
    from gateway.fast_head import constrain_fast_head_route_for_runtime

    decision = classify_fast_head_route("Why is the sky blue?")
    assert constrain_fast_head_route_for_runtime(decision, runtime).mode == "fast_head"


def test_codex_connect_error_runtime_is_downgraded_before_any_fast_request():
    import httpx

    from gateway.fast_head import constrain_fast_head_route_for_runtime

    fast_requests = []
    decision = constrain_fast_head_route_for_runtime(
        classify_fast_head_route("Why is the sky blue?"),
        {"api_mode": "codex_responses", "provider": "openai-codex"},
    )

    def dispatch(route):
        if route.mode == "fast_head":
            fast_requests.append(True)
            raise httpx.ConnectError("adversarial first attempt")
        return "operator"

    assert dispatch(decision) == "operator"
    assert fast_requests == []


def test_slash_commands_urls_paths_and_media_route_operator():
    cases = [
        ("/help", {}),
        ("explain https://example.com", {}),
        ("what is in ./notes.txt?", {}),
        ("what is this?", {"has_attachments": True}),
        ("what is this?", {"message_type": "image"}),
    ]
    for message, kwargs in cases:
        assert classify_fast_head_route(message, **kwargs).mode == "operator"


def test_disabled_feature_always_routes_operator():
    assert classify_fast_head_route("왜 느려?", enabled=False).mode == "operator"


def test_fast_head_agent_overrides_are_tool_free_context_free_and_single_call():
    assert fast_head_agent_overrides(FastHeadConfig(enabled=True, system_prompt="Small")) == {
        "max_iterations": 1,
        "enabled_toolsets": [],
        "disabled_toolsets": None,
        "ephemeral_system_prompt": None,
        "prefill_messages": None,
        "reasoning_config": {"enabled": False, "effort": "none"},
        "skip_context_files": True,
        "skip_memory": True,
        "fast_head_system_prompt": "Small",
        "tool_free": True,
    }


def test_agent_cache_key_and_signature_separate_fast_head_from_operator():
    assert GatewayRunner._agent_cache_key("session", "operator") == "session"
    assert GatewayRunner._agent_cache_key("session", "fast_head") != "session"
    common = dict(
        model="m",
        runtime={"provider": "p"},
        enabled_toolsets=[],
        ephemeral_prompt="",
    )
    assert GatewayRunner._agent_config_signature(**common, cache_mode="operator") != (
        GatewayRunner._agent_config_signature(**common, cache_mode="fast_head")
    )
    assert GatewayRunner._agent_config_signature(
        **common, cache_mode="fast_head", fast_head_prompt="first"
    ) != GatewayRunner._agent_config_signature(
        **common, cache_mode="fast_head", fast_head_prompt="second"
    )
