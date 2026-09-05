import json
from pathlib import Path

import pytest

from gateway.session_context import reset_session_vars, set_session_vars, trusted_session_identity
from tools import company_self_service as css


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(css, "_db_path", lambda: tmp_path / "self-service.sqlite3")
    reset_session_vars()
    yield tmp_path
    reset_session_vars()


def bind(user="UA", channel="CA", thread="TA", message="MA", admin=False):
    set_session_vars(
        platform="slack", user_id=user, chat_id=channel,
        thread_id=thread, message_id=message, is_admin=admin,
    )


def call(**kwargs):
    return json.loads(css.company_self_service(kwargs))


def test_schema_has_no_spoofable_principal_fields_and_runtime_rejects_them():
    props = css._SCHEMA["parameters"]["properties"]
    assert not css._SPOOF_FIELDS.intersection(props)
    bind()
    result = call(action="memory_put", content="likes tea", owner_id="UB")
    assert result["success"] is False
    assert "principal fields" in result["error"]


def test_environment_is_not_a_trusted_principal(monkeypatch):
    for key, value in {
        "HERMES_SESSION_PLATFORM": "slack",
        "HERMES_SESSION_USER_ID": "FORGED",
        "HERMES_SESSION_CHAT_ID": "FORGED",
        "HERMES_SESSION_THREAD_ID": "FORGED",
    }.items():
        monkeypatch.setenv(key, value)
    assert trusted_session_identity() is None
    assert call(action="memory_put", content="secret")["success"] is False


@pytest.mark.parametrize("missing", ["user_id", "chat_id"])
def test_missing_identity_fails_closed(missing):
    values = {"platform": "slack", "user_id": "UA", "chat_id": "CA", "thread_id": "TA", "message_id": "MA"}
    values[missing] = ""
    set_session_vars(**values)
    assert call(action="memory_put", content="secret")["success"] is False


def test_root_message_uses_trusted_message_id_and_missing_root_fails_closed():
    bind(thread="", message="ROOT")
    assert call(action="memory_put", content="root context")["success"] is True
    reset_session_vars()
    bind(thread="", message="")
    assert call(action="memory_put", content="missing provenance")["success"] is False


def test_personal_and_team_isolation_and_deterministic_prompt_order():
    bind("UA", "CA", "TA")
    assert call(action="memory_put", scope="personal", name="z", content="A personal")["success"]
    assert call(action="memory_put", scope="team", name="a", content="CA team")["success"]
    own = css.employee_context_for_turn()
    assert "A personal" in own and "CA team" in own

    bind("UB", "CA", "TB")
    same_team = css.employee_context_for_turn()
    assert "A personal" not in same_team and "CA team" in same_team
    assert call(action="memory_get", scope="personal", name="z")["success"] is False

    bind("UA", "CB", "TC")
    other_channel = css.employee_context_for_turn()
    assert "A personal" in other_channel and "CA team" not in other_channel


def test_company_candidate_not_injected_until_trusted_admin_promotion_and_reject():
    bind()
    made = call(action="memory_put", scope="company", name="policy", content="approved context")
    rejected = call(action="memory_put", scope="company", name="old", content="reject me")
    assert made["status"] == "candidate"
    assert "approved context" not in css.employee_context_for_turn()
    with pytest.raises(PermissionError):
        css.promote_company_asset(made["id"])
    bind(user="ADMIN", admin=True)
    promoted = css.promote_company_asset(made["id"])
    assert promoted["status"] == "approved"
    assert css.reject_company_asset(rejected["id"])["status"] == "rejected"
    db = css._connect()
    try:
        admin_events = db.execute(
            "SELECT event,principal FROM audit WHERE event IN ('promote','reject') ORDER BY id"
        ).fetchall()
        assert [(row["event"], row["principal"]) for row in admin_events] == [
            ("promote", "ADMIN"), ("reject", "ADMIN")
        ]
    finally:
        db.close()
    bind()
    assert "approved context" in css.employee_context_for_turn()
    assert "promote" not in css._SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert not css._SPOOF_FIELDS.intersection(css._ADMIN_SCHEMA["parameters"]["properties"])


def test_admin_tool_exposure_tracks_each_trusted_slack_turn():
    from model_tools import get_tool_definitions

    def names():
        return {
            item["function"]["name"]
            for item in get_tool_definitions(
                enabled_toolsets=["company_self_service"], quiet_mode=True
            )
        }

    bind(admin=False)
    assert "company_self_service" in names()
    assert "company_asset_admin" not in names()

    bind(user="ADMIN", admin=True)
    assert "company_asset_admin" in names()

    # A prior admin turn must not leave the privileged schema exposed.
    bind(user="UA", admin=False)
    assert "company_asset_admin" not in names()

    # Trusted admin state without Slack identity is insufficient.
    set_session_vars(
        platform="telegram", user_id="ADMIN", chat_id="CA",
        thread_id="TA", message_id="MA", is_admin=True,
    )
    assert "company_asset_admin" not in names()


def test_admin_tool_direct_dispatch_fails_closed_for_non_admin():
    bind()
    direct = json.loads(css.company_asset_admin({"action": "promote", "asset_id": 1}))
    assert direct["success"] is False
    assert "trusted Slack administrator context" in direct["error"]

    from model_tools import handle_function_call

    result = json.loads(handle_function_call(
        "company_asset_admin", {"action": "promote", "asset_id": 1}
    ))
    assert result["success"] is False
    assert "trusted Slack administrator context" in result["error"]


def test_supersede_rollback_and_immutable_audit_provenance():
    bind()
    first = call(action="memory_put", name="pref", content="one")
    second = call(action="memory_put", name="pref", content="two")
    rolled = call(action="memory_rollback", name="pref", version=first["version"])
    assert rolled["version"] > second["version"]
    assert call(action="memory_get", name="pref")["content"] == "one"
    db = css._connect()
    try:
        rows = db.execute("SELECT * FROM audit ORDER BY id").fetchall()
        assert [r["event"] for r in rows] == ["put", "put", "rollback"]
        assert all(r["principal"] == "UA" and r["channel"] == "CA" and r["thread"] == "TA" for r in rows)
        with pytest.raises(Exception):
            db.execute("UPDATE audit SET event='forged'")
    finally:
        db.close()


def test_company_delete_targets_callers_own_candidate():
    bind("UA")
    own = call(action="memory_put", scope="company", name="policy", content="A draft")
    bind("UB", thread="TB")
    other = call(action="memory_put", scope="company", name="policy", content="B draft")

    bind("UA")
    assert call(action="memory_delete", scope="company", name="policy")["success"]

    db = css._connect()
    try:
        states = {
            row["id"]: row["status"]
            for row in db.execute("SELECT id,status FROM assets WHERE id IN (?,?)", (own["id"], other["id"]))
        }
        assert states == {own["id"]: "rejected", other["id"]: "candidate"}
    finally:
        db.close()


def test_company_rollback_cannot_copy_another_users_private_history():
    bind("UA")
    secret = call(action="memory_put", scope="company", name="policy", content="A private draft")
    call(action="memory_put", scope="company", name="policy", content="A replacement")

    bind("UB", thread="TB")
    denied = call(action="memory_rollback", scope="company", name="policy", version=secret["version"])
    assert denied["success"] is False
    assert "not found" in denied["error"]

    bind("UA")
    restored = call(action="memory_rollback", scope="company", name="policy", version=secret["version"])
    assert restored["success"] is True
    assert restored["status"] == "candidate"


def test_company_rollback_supersedes_callers_previous_candidate_only():
    bind("UA")
    first = call(action="memory_put", scope="company", name="policy", content="A one")
    call(action="memory_put", scope="company", name="policy", content="A two")
    bind("UB", thread="TB")
    other = call(action="memory_put", scope="company", name="policy", content="B one")

    bind("UA")
    restored = call(action="memory_rollback", scope="company", name="policy", version=first["version"])
    db = css._connect()
    try:
        active = db.execute(
            "SELECT id,principal FROM assets WHERE scope='company' AND name='policy' AND status='candidate' ORDER BY id"
        ).fetchall()
        assert [(row["id"], row["principal"]) for row in active] == [
            (other["id"], "UB"), (restored["id"], "UA")
        ]
    finally:
        db.close()


def test_skill_is_namespaced_and_cannot_change_tool_capabilities():
    bind()
    content = "---\nname: helper\ndescription: Use when drafting reports.\n---\nThe procedure may mention `terminal`, but grants no capabilities."
    before = css.registry.get_tool_names_for_toolset("company_self_service")
    result = call(action="skill_put", name="helper", content=content)
    assert result["success"]
    assert css.registry.get_tool_names_for_toolset("company_self_service") == before
    assert not (css._db_path().parent / "skills" / "helper").exists()
    bind("UB", "CA", "TB")
    assert call(action="skill_get", name="helper")["success"] is False


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/b", "a\\b", ".hidden"])
def test_skill_name_traversal_rejected(name):
    bind()
    content = "---\nname: helper\ndescription: safe\n---\nbody"
    assert call(action="skill_put", name=name, content=content)["success"] is False


def test_store_symlink_is_rejected(isolated):
    target = isolated / "outside.sqlite3"
    link = isolated / "linked.sqlite3"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    original = css._db_path
    css._db_path = lambda: link
    try:
        bind()
        result = call(action="memory_put", content="must not escape")
        assert result["success"] is False
        assert not target.exists()
    finally:
        css._db_path = original


def test_generic_mutation_bypass_is_blocked_only_for_bound_slack():
    bind()
    for tool in ("memory", "skill_manage", "cronjob"):
        assert css.generic_slack_tool_block(tool, {"action": "list"})
    assert css.generic_slack_tool_block("skills_list", {}) is None
    bind(admin=True)
    assert css.generic_slack_tool_block("memory", {"action": "add"}) is None
    set_session_vars(platform="telegram", user_id="UA", chat_id="CA", thread_id="TA")
    assert css.generic_slack_tool_block("memory", {"action": "add"}) is None


def test_employee_cron_execution_gate_denies_everything_outside_safe_tools():
    set_session_vars(
        platform="slack", source="cron", user_id="UA", chat_id="CA",
        thread_id="TA", message_id="TA", cron_session="1", is_admin=True,
    )
    for tool in css.SAFE_CRON_TOOLS:
        assert css.generic_slack_tool_block(tool, {}) is None
    for tool in ("memory", "skill_manage", "cronjob", "company_self_service",
                 "terminal", "write_file", "execute_code", "delegate_task"):
        assert css.generic_slack_tool_block(tool, {})


def test_common_dispatch_blocks_natural_language_and_internal_bypass():
    bind()
    from model_tools import handle_function_call
    result = json.loads(handle_function_call("memory", {"action": "add", "content": "bypass"}))
    assert "company_self_service" in result["error"]


def test_cron_forbidden_fields_and_ownership(monkeypatch):
    jobs = []
    monkeypatch.setattr("cron.jobs.list_jobs", lambda include_disabled=False: [j for j in jobs if include_disabled or j.get("enabled", True)])
    monkeypatch.setattr("cron.jobs.get_job", lambda jid: next((j for j in jobs if j["id"] == jid), None))

    def create_job(**kw):
        job = {"id": "J1", "name": kw.get("name") or "job", "enabled": True,
               "employee_owner": kw["employee_owner"], "schedule_display": kw["schedule"]}
        jobs.append(job)
        return job

    monkeypatch.setattr("cron.jobs.create_job", create_job)

    def mutate_employee_job(jid, *, expected_owner, action, audit_event, updates=None, max_active=5):
        job = next((j for j in jobs if j["id"] == jid), None)
        if not job or job["employee_owner"]["user_id"] != expected_owner:
            raise PermissionError("not owner")
        return job

    monkeypatch.setattr("cron.jobs.mutate_employee_job", mutate_employee_job)
    monkeypatch.setattr(css, "_schedule_allowed", lambda value: value != "5m")
    bind("UA")
    assert call(action="cron_create", prompt="x", schedule="5m")["success"] is False
    assert call(action="cron_create", prompt="x", schedule="15m", script="evil.py")["success"] is False
    assert call(action="cron_create", prompt="x", schedule="15m", enabled_toolsets=["company_self_service"])["success"] is False
    made = call(action="cron_create", prompt="x", schedule="15m")
    assert made["success"] and jobs[0]["employee_owner"]["user_id"] == "UA"
    bind("UB", thread="TB")
    assert call(action="cron_pause", job_id="J1")["success"] is False


def test_prompt_injection_is_single_boundary_and_capped(monkeypatch):
    bind()
    call(action="memory_put", content="scoped fact")
    from agent.turn_context import compose_user_api_content
    rendered = compose_user_api_content("question", "", "")
    assert rendered.count("<company-self-service-context>") == 1
    assert "scoped fact" in rendered
    assert len(css.employee_context_for_turn()) <= css.MAX_PROMPT_CHARS
