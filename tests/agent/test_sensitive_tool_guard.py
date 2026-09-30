"""Security-boundary tests for the tool executor sensitive-value guard."""

from pathlib import Path

import pytest

from agent.tool_executor import _sensitive_tool_block_reason
from gateway.session_context import clear_session_vars, set_session_vars


OWNER_ID = "6834626936"
SLACK_OWNER_ID = "U0AQ874R6SG"
CANONICAL_ADMINS = f'''owner:
  - {{platform: telegram, user_id: "{OWNER_ID}"}}
  - {{platform: slack, user_id: "{SLACK_OWNER_ID}"}}
admins: []
'''


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "admins.yaml").write_text(CANONICAL_ADMINS, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(home)
    tokens = set_session_vars()
    clear_session_vars(tokens)
    yield home
    clear_session_vars([])


def bind(*, platform="", source="", user_id="", cron_session=""):
    set_session_vars(
        platform=platform,
        source=source,
        user_id=user_id,
        cron_session=cron_session,
    )


def blocked(tool, args):
    return _sensitive_tool_block_reason(tool, args) is not None


def test_slack_channel_write_file_soul_is_blocked(isolated_home):
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked("write_file", {"path": str(isolated_home / "SOUL.md"), "content": "x"})


def test_slack_channel_terminal_sed_soul_is_blocked(isolated_home):
    bind(platform="slack", source="gateway", user_id="U123")
    command = f"sed -i '' 's/a/b/' {isolated_home / 'SOUL.md'}"
    assert blocked("terminal", {"command": command})


def test_slack_channel_gateway_restart_is_blocked():
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked("terminal", {"command": "hermes gateway restart"})


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("skill_manage", {"action": "create", "name": "safe"}),
        ("write_file", {"path": "product-repo/app.py", "content": "safe"}),
        ("read_file", {"path": "SOUL.md"}),
    ],
)
def test_slack_non_protected_actions_and_protected_reads_are_allowed(tool, args):
    bind(platform="slack", source="gateway", user_id="U123")
    assert not blocked(tool, args)


@pytest.mark.parametrize(
    ("platform", "user_id"),
    [("telegram", OWNER_ID), ("slack", SLACK_OWNER_ID)],
)
def test_platform_owners_can_write_soul(isolated_home, platform, user_id):
    bind(platform=platform, source="gateway", user_id=user_id)
    assert not blocked("write_file", {"path": str(isolated_home / "SOUL.md"), "content": "x"})


@pytest.mark.parametrize(
    ("platform", "user_id"),
    [("telegram", SLACK_OWNER_ID), ("slack", OWNER_ID)],
)
def test_owner_ids_cannot_be_reused_on_other_platform(isolated_home, platform, user_id):
    bind(platform=platform, source="gateway", user_id=user_id)
    assert blocked("write_file", {"path": str(isolated_home / "SOUL.md"), "content": "x"})


@pytest.mark.parametrize(
    ("platform", "user_id"),
    [("telegram", "7000000001"), ("slack", "UADMIN1")],
)
def test_configured_admin_can_write_soul(isolated_home, platform, user_id):
    (isolated_home / "admins.yaml").write_text(
        CANONICAL_ADMINS.replace("admins: []", f'admins:\n  - {{platform: {platform}, user_id: "{user_id}"}}'),
        encoding="utf-8",
    )
    bind(platform=platform, source="gateway", user_id=user_id)
    assert not blocked("write_file", {"path": str(isolated_home / "SOUL.md"), "content": "x"})


def test_malformed_admin_registry_fails_closed(isolated_home):
    (isolated_home / "admins.yaml").write_text(
        CANONICAL_ADMINS.replace("admins: []", 'admins:\n  - {platform: slack, user_id: "UADMIN1"}\n  - invalid'),
        encoding="utf-8",
    )
    bind(platform="slack", source="gateway", user_id="UADMIN1")
    assert blocked("write_file", {"path": str(isolated_home / "SOUL.md"), "content": "x"})


@pytest.mark.parametrize("source", ["cli", "ssh"])
def test_local_cli_and_ssh_can_write_config(isolated_home, source):
    bind(platform="local", source=source)
    assert not blocked("write_file", {"path": str(isolated_home / "config.yaml"), "content": "x: 1"})


def test_cron_cannot_write_soul(isolated_home):
    bind(source="cron", cron_session="1")
    assert blocked("write_file", {"path": str(isolated_home / "SOUL.md"), "content": "x"})


def test_cron_can_kickstart_allowlisted_autonomous_deploy_label(isolated_home):
    (isolated_home / "config.yaml").write_text(
        "autonomous_deploy:\n"
        "  enabled: true\n"
        "  launchd_label_prefixes: [co.lukuku.]\n",
        encoding="utf-8",
    )
    identities = [
        {"source": "cron", "cron_session": "1"},
        {"source": "autonomous"},
        {"platform": "slack", "source": "gateway", "user_id": "U123"},
        {"platform": "telegram", "source": "gateway", "user_id": "7000000001"},
    ]
    for identity in identities:
        bind(**identity)
        assert not blocked(
            "terminal",
            {
                "command": (
                    "launchctl kickstart gui/$(id -u)/co.lukuku.openviking-sync"
                )
            },
        )
    assert not blocked(
        "terminal",
        {"command": "launchctl kickstart gui/501/co.lukuku.openviking-sync"},
    )


@pytest.mark.parametrize(
    "identity",
    [
        {"source": "cron", "cron_session": "1"},
        {"source": "autonomous"},
    ],
)
def test_default_autonomous_context_can_bootstrap_allowlisted_launchagent(
    isolated_home, monkeypatch, identity
):
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/zeus"))
    (isolated_home / "config.yaml").write_text(
        "autonomous_deploy:\n"
        "  enabled: true\n"
        "  launchd_label_prefixes: [co.lukuku.]\n",
        encoding="utf-8",
    )
    bind(**identity)

    assert not blocked(
        "terminal",
        {
            "command": (
                "launchctl bootstrap gui/$(id -u) "
                "/Users/zeus/Library/LaunchAgents/co.lukuku.example.plist"
            )
        },
    )


def test_autonomous_bootstrap_exception_is_exact_and_fail_closed(
    isolated_home, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/zeus"))
    (isolated_home / "config.yaml").write_text(
        "autonomous_deploy:\n"
        "  enabled: true\n"
        "  launchd_label_prefixes: [co.lukuku.]\n",
        encoding="utf-8",
    )
    allowed_plist = "/Users/zeus/Library/LaunchAgents/co.lukuku.example.plist"
    launchagents_dir = "/Users/zeus/Library/LaunchAgents"
    bind(source="autonomous")

    denied_commands = [
        f"launchctl bootout gui/$(id -u) {allowed_plist}",
        f"launchctl unload {allowed_plist}",
        f"launchctl disable gui/$(id -u)/co.lukuku.example",
        f"launchctl stop co.lukuku.example",
        f"launchctl bootstrap gui/501 {allowed_plist}",
        f"launchctl bootstrap user/$(id -u) {allowed_plist}",
        f"launchctl bootstrap gui/$(whoami) {allowed_plist}",
        f"launchctl bootstrap gui/$(id -g) {allowed_plist}",
        "launchctl bootstrap gui/$(id -u) "
        "$HOME/Library/LaunchAgents/co.lukuku.example.plist",
        f"launchctl bootstrap gui/$(id -u) /tmp/co.lukuku.example.plist",
        f"launchctl bootstrap gui/$(id -u) {launchagents_dir}/nested/co.lukuku.example.plist",
        f"launchctl bootstrap gui/$(id -u) {launchagents_dir}/com.example.service.plist",
        f"launchctl bootstrap gui/$(id -u) {launchagents_dir}/co.lukuku.hermes-gateway.plist",
        f"launchctl bootstrap gui/$(id -u) {launchagents_dir}/co.lukuku.restart_loop_guard.plist",
        f"launchctl bootstrap gui/$(id -u) {allowed_plist}; true",
        f"launchctl bootstrap gui/$(id -u) {allowed_plist} && true",
        f"launchctl bootstrap gui/$(id -u) {allowed_plist} | true",
        f"launchctl bootstrap gui/$(id -u) {allowed_plist} extra",
    ]
    for command in denied_commands:
        assert blocked("terminal", {"command": command}), command

    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked(
        "terminal",
        {"command": f"launchctl bootstrap gui/$(id -u) {allowed_plist}"},
    )


def test_authorized_owner_retains_launchctl_bootstrap_access():
    bind(platform="telegram", source="gateway", user_id=OWNER_ID)
    assert not blocked(
        "terminal",
        {"command": "launchctl bootstrap gui/501 /tmp/com.example.service.plist"},
    )


def test_autonomous_deploy_cannot_restart_gateway(isolated_home):
    (isolated_home / "config.yaml").write_text(
        "autonomous_deploy:\n"
        "  enabled: true\n"
        "  launchd_label_prefixes: [co.lukuku.]\n",
        encoding="utf-8",
    )
    bind(source="autonomous")

    denied_commands = [
        "launchctl kickstart gui/$(id -u)/ai.hermes.gateway",
        "launchctl kickstart gui/$(id -u)/co.lukuku.hermes-gateway",
        "launchctl kickstart gui/$(id -u)/co.lukuku.restart_loop_guard",
        "launchctl kickstart gui/$(id -u)/com.example.service",
        "launchctl kickstart gui/$(id -u)/co.lukuku.openviking-sync; true",
        "launchctl kickstart gui/$(whoami)/co.lukuku.openviking-sync",
        "launchctl kickstart gui/$(id -g)/co.lukuku.openviking-sync",
        "printf 'launchctl kickstart gui/$(id -u)/co.lukuku.openviking-sync'",
    ]
    for command in denied_commands:
        assert blocked("terminal", {"command": command})


def test_autonomous_deploy_cannot_mutate_protected_config(isolated_home):
    bind(source="autonomous")
    assert blocked(
        "terminal",
        {"command": "launchctl kickstart gui/501/co.lukuku.openviking-sync"},
    )

    (isolated_home / "config.yaml").write_text(
        "autonomous_deploy:\n  enabled: true\n",
        encoding="utf-8",
    )
    assert blocked(
        "terminal",
        {"command": "launchctl kickstart gui/501/co.lukuku.openviking-sync"},
    )

    (isolated_home / "config.yaml").write_text(
        "autonomous_deploy:\n"
        "  enabled: true\n"
        "  launchd_label_prefixes: [co.lukuku.]\n",
        encoding="utf-8",
    )

    assert blocked(
        "write_file",
        {"path": str(isolated_home / "config.yaml"), "content": "security: {}\n"},
    )


def test_unset_identity_cannot_write_soul(isolated_home):
    clear_session_vars([])
    assert blocked("write_file", {"path": str(isolated_home / "SOUL.md"), "content": "x"})


def test_shared_memory_write_is_blocked_but_personal_memory_is_allowed():
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked("memory", {"action": "add", "target": "memory", "content": "shared"})
    assert not blocked("memory", {"action": "add", "target": "user", "content": "personal"})


def test_memory_mixed_batch_cannot_bypass_shared_target_guard():
    bind(platform="slack", source="gateway", user_id="U123")
    operations = [
        {"action": "add", "content": "shared"},
        {"action": "replace", "old_text": "a", "content": "b"},
        {"action": "remove", "old_text": "shared"},
    ]
    assert blocked("memory", {"target": "memory", "operations": operations})
    assert not blocked("memory", {"target": "user", "operations": operations})


def test_telegram_owner_can_write_shared_memory():
    bind(platform="telegram", source="gateway", user_id=OWNER_ID)
    assert not blocked("memory", {"action": "add", "target": "memory", "content": "shared"})


def test_symlink_to_soul_cannot_bypass_guard(isolated_home, tmp_path):
    soul = isolated_home / "SOUL.md"
    soul.write_text("safe", encoding="utf-8")
    link = tmp_path / "innocent.txt"
    link.symlink_to(soul)
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked("write_file", {"path": str(link), "content": "x"})


@pytest.mark.parametrize(
    "code",
    [
        "from pathlib import Path\nPath('SOUL.md').write_text('x')",
        "open('config.yaml', 'w').write('x')",
        "import os\nos.system('hermes gateway stop')",
    ],
)
def test_execute_code_representative_bypasses_are_blocked(code):
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked("execute_code", {"code": code})


@pytest.mark.parametrize(
    "code",
    [
        "from hermes_tools import write_file\nwrite_file('SOUL.md', 'x')",
        "from hermes_tools import patch\npatch(mode='replace', path='config.yaml', old_string='a', new_string='b')",
        "from hermes_tools import terminal\nterminal(command=\"sed -i '' 's/a/b/' SOUL.md\")",
    ],
)
def test_execute_code_blocks_direct_hermes_tool_mutations(code):
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked("execute_code", {"code": code})


def test_owner_root_removal_attempt_is_blocked(isolated_home):
    bind(platform="telegram", source="gateway", user_id=OWNER_ID)
    assert blocked(
        "write_file",
        {"path": str(isolated_home / "admins.yaml"), "content": "owner: {}\nadmins: []\n"},
    )


def test_owner_write_must_preserve_both_canonical_owner_entries(isolated_home):
    bind(platform="telegram", source="gateway", user_id=OWNER_ID)
    assert not blocked(
        "write_file",
        {"path": str(isolated_home / "admins.yaml"), "content": CANONICAL_ADMINS},
    )
    telegram_only = f'owner:\n  - {{platform: telegram, user_id: "{OWNER_ID}"}}\nadmins: []\n'
    assert blocked(
        "write_file",
        {"path": str(isolated_home / "admins.yaml"), "content": telegram_only},
    )


@pytest.mark.parametrize("tool, payload_key", [("terminal", "command"), ("execute_code", "code")])
def test_owner_cannot_mutate_admins_via_unprovable_surfaces(isolated_home, tool, payload_key):
    bind(platform="telegram", source="gateway", user_id=OWNER_ID)
    command = f"sed -i '' '/slack/d' {isolated_home / 'admins.yaml'}"
    payload = command
    if tool == "execute_code":
        payload = f"from hermes_tools import terminal\nterminal(command={command!r})"
    reason = _sensitive_tool_block_reason(tool, {payload_key: payload})
    assert reason is not None
    assert "admins.yaml" in reason
    assert "직접 파일 도구" in reason


def test_owner_can_read_admins_via_terminal(isolated_home):
    bind(platform="telegram", source="gateway", user_id=OWNER_ID)
    command = f"python -c \"print(open('{isolated_home / 'admins.yaml'}').read())\""
    assert not blocked("terminal", {"command": command})


def test_patch_body_protected_paths_are_blocked_and_product_paths_allowed():
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked(
        "patch",
        {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: SOUL.md\n@@\n-old\n+new\n*** End Patch"},
    )
    assert blocked(
        "patch",
        {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: skills/demo/SKILL.md\n*** Update File: cron/jobs.json\n*** End Patch"},
    )
    assert not blocked(
        "patch",
        {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: skills/demo/SKILL.md\n*** Update File: product/app.py\n*** End Patch"},
    )


@pytest.mark.parametrize("action", ["create", "update", "pause", "resume", "remove"])
def test_slack_cannot_mutate_cron_definitions(action):
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked("cronjob", {"action": action})


@pytest.mark.parametrize("action", ["create", "update", "pause", "resume", "remove"])
def test_telegram_owner_can_mutate_cron_definitions(action):
    bind(platform="telegram", source="gateway", user_id=OWNER_ID)
    assert not blocked("cronjob", {"action": action})


@pytest.mark.parametrize("action", ["list", "run"])
def test_cron_non_definition_actions_remain_allowed_for_slack(action):
    bind(platform="slack", source="gateway", user_id="U123")
    assert not blocked("cronjob", {"action": action})


@pytest.mark.parametrize(
    ("platform", "source"),
    [("local", ""), ("", ""), ("local", "unknown"), ("unknown", "unknown")],
)
def test_implicit_or_unknown_local_identity_fails_closed(isolated_home, platform, source):
    bind(platform=platform, source=source)
    assert blocked("write_file", {"path": str(isolated_home / "config.yaml"), "content": "x: 1"})


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("terminal", {"command": "python -m pytest tests/unit -q"}),
        ("terminal", {"command": "printf 'SOUL.md'"}),
        ("execute_code", {"code": "value = 'config.yaml'\nprint(value)"}),
        ("write_file", {"path": "skills/demo/SKILL.md", "content": "safe"}),
        ("write_file", {"path": "notes/SOUL.md.backup", "content": "safe"}),
    ],
)
def test_false_positive_non_regression(tool, args):
    bind(platform="slack", source="gateway", user_id="U123")
    assert not blocked(tool, args)


def test_cron_definition_and_hermes_agent_tree_are_protected(isolated_home):
    bind(platform="slack", source="gateway", user_id="U123")
    assert blocked("write_file", {"path": str(isolated_home / "cron" / "jobs.json"), "content": "{}"})
    assert blocked("patch", {"path": str(isolated_home / "hermes-agent" / "agent.py"), "old_string": "a", "new_string": "b"})
