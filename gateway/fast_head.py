"""Deterministic conservative routing for the gateway's optional fast head."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal


DEFAULT_FAST_HEAD_SYSTEM_PROMPT = (
    "Answer the user's question directly and concisely. Use only the conversation "
    "provided; do not claim to inspect current, external, or private state."
)


@dataclass(frozen=True)
class FastHeadConfig:
    enabled: bool = False
    system_prompt: str = DEFAULT_FAST_HEAD_SYSTEM_PROMPT


@dataclass(frozen=True)
class RouteDecision:
    mode: Literal["fast_head", "operator"]
    reason: str


# These are deliberately broad. False negatives only cost latency; false positives
# could deprive an action/current-state request of the tools it needs.
_OPERATOR_EN = re.compile(
    r"\b(?:execute|run|install|configure|change|apply|edit|fix|create|delete|"
    r"test|verify|deploy|commit|push|send|schedule|download|upload|search|"
    r"research|check|restart|start|stop|open|close|read|current|currently|"
    r"latest|today|tomorrow|yesterday|live|status|weather|temperature|price|"
    r"calendar|inbox|email|disk|cpu|battery|server|nginx|service|process|"
    r"database|logs?|files?|urls?)\b",
    re.IGNORECASE,
)
_OPERATOR_KO = re.compile(
    r"(?:실행|설치|설정|구성|변경|적용|수정|고쳐|생성|만들어|삭제|테스트|검증|"
    r"배포|커밋|푸시|전송|보내|예약|일정|다운로드|업로드|검색|찾아|조사|확인|"
    r"현재|최신|상태|날씨|시간|메일|디스크|서버|서비스|로그|파일|주소|링크)"
)
_POSSESSIVE_OR_PRIVATE_EN = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9_-]*(?:['’]s|s['’])|\b(?:my|mine|his|her|hers|our|ours|"
    r"their|theirs|your|yours)\b)",
    re.IGNORECASE,
)
_DEFINITE_ARBITRARY_QUERY_EN = re.compile(
    r"^(?:what\s+(?:is|are)\s+the\b|explain\s+the\s+|"
    r"explain\s+how\s+the\b|"
    r"how\s+(?:does|do)\s+the\b|what\s+does\s+the\b)|\bof\s+the\b",
    re.IGNORECASE,
)
_SAFE_DEFINITE_EXPLANATION_EN = re.compile(
    r"^explain\s+the\s+(?:concept|meaning|principle|architecture)\s+of\s+"
    r"(?!the\b).+\??$",
    re.IGNORECASE | re.DOTALL,
)
_PERSONAL_OR_PRIVATE_KO = re.compile(
    r"(?:^|[\s,.;:!?])(?:내|내가|나의|우리|우리의|제|제가|저의)(?=$|[\s,.;:!?])|"
    r"(?:개인|계정)"
)
_GENITIVE_KO = re.compile(r"(?:^|\s)\S+의")
_SAFE_DEMONSTRATIVE_MEANING_KO = re.compile(
    r"^(?:이|그|저)\s+[^\s의]+의\s*(?:의미|뜻).*(?:뭐|무엇).*[?.!\s]*$"
)
_CONFIDENTIAL_OR_LIVE_STATE_EN = re.compile(
    r"\b(?:passwords?|account\s+balances?|balances?|private\s+keys?|"
    r"address(?:es)?|credentials?|revenue)\b",
    re.IGNORECASE,
)
_CONFIDENTIAL_OR_LIVE_STATE_KO = re.compile(
    r"(?:비밀번호|계정\s*잔액|잔액|개인\s*키|비밀\s*키|"
    r"(?:집|IP|아이피|메일|도로명)?\s*주소|인증\s*정보|자격\s*증명|매출|수익)"
)
# Sensitive nouns can still be discussed as definitions or mechanisms.  Keep
# this allowlist intentionally grammatical and anchored: definite/possessive
# subjects ("the password", "Alice's password") are live-state requests.
_GENERIC_SENSITIVE_TOPIC_EN = re.compile(
    r"^(?:what\s+is\s+(?:a|an)\s+(?:password|account\s+balance|private\s+key|"
    r"(?:(?:home|ip|mailing|street)\s+)?address)|"
    r"what\s+is\s+revenue|what\s+are\s+credentials|"
    r"explain\s+(?:password\s+hashing|how\s+private\s+keys\s+work|"
    r"revenue\s+recognition))\??$",
    re.IGNORECASE,
)
_GENERIC_SENSITIVE_TOPIC_KO = re.compile(
    r"^(?:비밀번호\s*해싱|개인\s*키의?\s*작동\s*원리|매출\s*인식의?\s*개념)"
    r"(?:을|를)?\s*설명해\s*줘[?.!\s]*$"
)
_URL = re.compile(r"(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|dev|ai|co|kr)(?:/|\b))", re.I)
_PATH = re.compile(
    r"(?:^|\s)(?:\.{0,2}/|~/|/[A-Za-z0-9_.-]|[A-Za-z]:[\\/])|"
    r"\b[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|log|sh|csv|pdf)\b",
    re.I,
)
_SELF_CONTAINED_EN = re.compile(
    r"^(?:why\s+is\s+the\s+sky\s+blue\??|"
    r"how\s+(?:does|do)\s+.+\s+work\??|"
    r"what\s+does\s+.+\s+mean\??|"
    r"(?:what|how)\s+do\s+you\s+think\s+(?:about|of)\s+.+|"
    r"which\s+is\s+better[, :]\s*.+|compare\s+.+\s+(?:and|with|to)\s+.+|"
    r"explain\s+how\s+.+\s+works?|"
    r"explain\s+(?:(?:the\s+)?(?:concept|meaning|principle|architecture)\s+of\s+.+|"
    r".+\s+(?:concept|meaning|principle|architecture|algorithm)))\??$",
    re.I | re.S,
)
_SELF_CONTAINED_KO = re.compile(
    r"^(?:왜\s*(?:느려|하늘은\s*파래)|(?:이|그|저)\s*.+(?:의미|뜻).*(?:뭐|무엇).+|.+어떻게\s*생각해|"
    r".+(?:와|과).+중\s*(?:뭐|무엇).*(?:나아|좋아)|"
    r".+(?:의\s*)?(?:개념|의미|뜻|원리|구조|아키텍처|알고리즘)(?:을|를)?\s*설명해\s*줘|"
    r".+비교해\s*줘)[?.!\s]*$"
)
_CASUAL = re.compile(
    r"^(?:hi|hello|hey|thanks|thank you|good morning|good night|안녕|고마워|감사해)[!.?\s]*$",
    re.I,
)
_MEDIA_TYPES = frozenset({
    "attachment", "audio", "command", "document", "file", "image",
    "location", "media", "photo", "sticker", "video", "voice",
})


def _message_type_value(message_type: Any) -> str:
    return str(getattr(message_type, "value", message_type) or "").strip().lower()


def constrain_fast_head_route_for_runtime(
    decision: RouteDecision, runtime: Any, *, moa_active: bool = False
) -> RouteDecision:
    """Downgrade runtimes whose physical calls/tools escape Hermes' loop."""
    if decision.mode != "fast_head":
        return decision
    if not isinstance(runtime, dict):
        return RouteDecision("operator", "unsupported_runtime")
    api_mode = str(runtime.get("api_mode") or "").strip().lower()
    provider = str(runtime.get("provider") or "").strip().lower()
    base_url = str(runtime.get("base_url") or "").lower()
    approved_runtime_pairs = {
        ("chat_completions", "openai"),
        ("chat_completions", "openai-api"),
        ("chat_completions", "openai-compat"),
        ("chat_completions", "openrouter"),
        ("chat_completions", "nous"),
        ("anthropic_messages", "anthropic"),
    }
    if (moa_active or (api_mode, provider) not in approved_runtime_pairs
            or base_url.startswith(("acp://", "acp+tcp://"))):
        return RouteDecision("operator", "unsupported_runtime")
    return decision


def resolve_fast_head_config(config: Any) -> FastHeadConfig:
    """Resolve ``agent.fast_head``; malformed opt-ins fail closed."""
    if not isinstance(config, dict):
        return FastHeadConfig()
    agent = config.get("agent")
    if not isinstance(agent, dict):
        return FastHeadConfig()
    raw = agent.get("fast_head")
    if raw is None:
        return FastHeadConfig()
    if not isinstance(raw, dict) or not isinstance(raw.get("enabled", False), bool):
        return FastHeadConfig()
    prompt = raw.get("system_prompt", DEFAULT_FAST_HEAD_SYSTEM_PROMPT)
    if not isinstance(prompt, str) or not prompt.strip():
        return FastHeadConfig()
    return FastHeadConfig(enabled=raw.get("enabled", False), system_prompt=prompt.strip())


def fast_head_agent_overrides(config: FastHeadConfig) -> dict[str, Any]:
    """Return the constructor restrictions that make the fast head bounded."""
    return {
        "max_iterations": 1,
        "enabled_toolsets": [],
        "disabled_toolsets": None,
        "ephemeral_system_prompt": None,
        "prefill_messages": None,
        "reasoning_config": {"enabled": False, "effort": "none"},
        "skip_context_files": True,
        "skip_memory": True,
        "fast_head_system_prompt": config.system_prompt,
        "tool_free": True,
    }


def classify_fast_head_route(
    message: Any,
    *,
    enabled: bool = True,
    has_attachments: bool = False,
    message_type: Any = None,
) -> RouteDecision:
    """Route only obvious, self-contained conversational questions to fast head."""
    if not enabled:
        return RouteDecision("operator", "disabled")
    normalized_type = _message_type_value(message_type)
    if has_attachments or normalized_type in _MEDIA_TYPES or normalized_type not in {"", "text"}:
        return RouteDecision("operator", "media")
    if not isinstance(message, str) or not message.strip():
        return RouteDecision("operator", "empty_or_non_text")

    text = message.strip()
    if text.startswith("/"):
        return RouteDecision("operator", "slash_command")
    if _URL.search(text):
        return RouteDecision("operator", "url")
    if _PATH.search(text):
        return RouteDecision("operator", "path")
    if _POSSESSIVE_OR_PRIVATE_EN.search(text):
        return RouteDecision("operator", "possessive_or_definite_subject")
    if (
        _DEFINITE_ARBITRARY_QUERY_EN.search(text)
        and not _SAFE_DEFINITE_EXPLANATION_EN.fullmatch(text)
    ):
        return RouteDecision("operator", "possessive_or_definite_subject")
    if _PERSONAL_OR_PRIVATE_KO.search(text):
        return RouteDecision("operator", "personal_or_private_state")
    if _GENITIVE_KO.search(text) and not _SAFE_DEMONSTRATIVE_MEANING_KO.fullmatch(text):
        return RouteDecision("operator", "possessive_or_definite_subject")
    sensitive_subject = (
        _CONFIDENTIAL_OR_LIVE_STATE_EN.search(text)
        or _CONFIDENTIAL_OR_LIVE_STATE_KO.search(text)
    )
    safe_generic_topic = (
        _GENERIC_SENSITIVE_TOPIC_EN.fullmatch(text)
        or _GENERIC_SENSITIVE_TOPIC_KO.fullmatch(text)
    )
    if sensitive_subject and not safe_generic_topic:
        return RouteDecision("operator", "confidential_or_live_state")

    if _OPERATOR_EN.search(text) or _OPERATOR_KO.search(text):
        return RouteDecision("operator", "operator_signal")
    if _CASUAL.fullmatch(text):
        return RouteDecision("fast_head", "casual")
    if _SELF_CONTAINED_EN.fullmatch(text) or _SELF_CONTAINED_KO.fullmatch(text):
        return RouteDecision("fast_head", "self_contained")

    # A punctuation-only question marker is not enough: requests such as
    # "help?" or one-word imperatives are intentionally ambiguous.
    return RouteDecision("operator", "uncertain")
