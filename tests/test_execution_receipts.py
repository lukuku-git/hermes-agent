"""Current-execution receipts: contract regressions, written before implementation.

Synthetic results below are adversarial fixtures, never claimed live evidence.
Positive vat/wiki schemas must be taken from their authoritative CLI/tool source;
this file deliberately does not invent a successful external response schema.
"""

import importlib
import json
from unittest.mock import patch

import pytest


@pytest.fixture
def receipts():
    # Import in the test, so an absent implementation gives an explicit RED
    # without preventing unrelated test modules from being collected.
    return importlib.import_module("agent.execution_receipts")


@pytest.fixture
def roots(tmp_path):
    workspace = tmp_path / "workspace"
    petasos = tmp_path / "petasos"
    workspace.mkdir()
    petasos.mkdir()
    return {"workspace_root": workspace, "petasos_root": petasos}


def execution(name, arguments, result, call_id="call-1"):
    return {"tool_call_id": call_id, "name": name,
            "arguments": arguments, "result": json.dumps(result)}


def successful_read(path, call_id="call-1"):
    return execution("read_file", {"path": str(path)}, {
        "content": "1|Fixture body, not receipt content",
        "total_lines": 1, "file_size": 38, "truncated": False,
    }, call_id)


def render(receipts, roots, records, text="조회 결과입니다."):
    return receipts.format_receipt_response(text, records, **roots)


def test_no_tools_removes_model_receipt_without_fabricating_one(receipts, roots):
    assert render(receipts, roots, [], "답변\n근거: brain D-9999(active)") == "답변"


def test_missing_collector_fails_closed_even_with_old_transcript(receipts, roots):
    # Gateway must pass None, not all messages, when the collector is absent.
    assert render(receipts, roots, None, "답변\n근거: fake") == "답변"


def test_receipt_removal_is_line_scoped_and_preserves_other_prose(receipts):
    text = ("본문에서 근거: 라는 용어를 설명합니다.\n"
            "근거가 충분하지 않습니다.\n"
            "  근거: fake\n근거 없음 — 추정\n"
            "확인할 사람: 김건우\n")
    assert receipts.strip_model_receipt_lines(text) == (
        "본문에서 근거: 라는 용어를 설명합니다.\n"
        "근거가 충분하지 않습니다.\n근거 없음 — 추정\n확인할 사람: 김건우"
    )


def test_successful_read_uses_short_relative_path_and_receipt_is_last(receipts, roots):
    path = roots["petasos_root"] / "docs" / "policy.md"
    path.parent.mkdir()
    path.write_text("public fixture", encoding="utf-8")
    result = render(receipts, roots, [successful_read(path)],
                    "답변\n근거: fake\n확인할 사람: 김건우")
    assert result == "답변\n확인할 사람: 김건우\n근거: docs/policy.md"
    assert str(roots["petasos_root"]) not in result
    assert "Fixture body" not in result


def test_workspace_read_is_allowed(receipts, roots):
    path = roots["workspace_root"] / "brain" / "README.md"
    path.parent.mkdir()
    path.write_text("public fixture", encoding="utf-8")
    assert render(receipts, roots, [successful_read(path)]).endswith("근거: brain/README.md")


@pytest.mark.parametrize("result", [
    {"error": "not found"}, {"success": False, "content": "1|fake"},
    {"is_binary": True, "content": ""}, {"content": None},
    {"id": "D-9999", "status": "active"},
])
def test_failed_or_unrecognized_file_results_are_not_evidence(receipts, roots, result):
    path = roots["petasos_root"] / "AGENTS.md"
    assert render(receipts, roots, [execution("read_file", {"path": str(path)}, result)]) == "조회 결과입니다."


@pytest.mark.parametrize("relative", [
    ".env", ".env.local", "credential/README.md", "credentials/auth.json",
    "secrets/token.txt", "profiles/default/auth.json", "tokens/value.txt",
    "private/customer@example.com.md", "private/010-1234-5678.md",
])
def test_sensitive_paths_are_never_exposed(receipts, roots, relative):
    path = roots["workspace_root"] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture", encoding="utf-8")
    assert render(receipts, roots, [successful_read(path)]) == "조회 결과입니다."


def test_outside_and_symlink_escape_paths_are_rejected(receipts, roots, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("fixture", encoding="utf-8")
    link = roots["petasos_root"] / "escape.md"
    link.symlink_to(outside)
    for path in (outside, link, roots["petasos_root"] / ".." / "outside.md"):
        assert render(receipts, roots, [successful_read(path)]) == "조회 결과입니다."


def test_dedup_preserves_order_caps_five_and_counts_distinct_overflow(receipts, roots):
    paths = [roots["petasos_root"] / f"file-{index}.md" for index in range(7)]
    for path in paths:
        path.write_text("fixture", encoding="utf-8")
    records = [successful_read(path, f"call-{index}") for index, path in enumerate(paths)]
    records.insert(1, successful_read(paths[0], "duplicate"))
    line = render(receipts, roots, records).splitlines()[-1]
    assert line == "근거: file-0.md, file-1.md, file-2.md, file-3.md, file-4.md 외 2"


@pytest.mark.parametrize("command", [
    "echo 'D-0037 active'", "printf 'D-0037 active'",
    "vat brain query policy | echo 'D-0037 active'",
    "vat brain query policy; echo 'D-0037 active'",
    "vat brain query $(echo policy)", "vat brain query `echo policy`",
    "vat brain query policy && echo fake", "vat brain query policy > fake",
    "vat brain write D-0037", "python -c 'print(\"D-0037 active\")'",
])
def test_echo_pipes_substitution_and_non_read_commands_are_rejected(receipts, roots, command):
    record = execution("terminal", {"command": command}, {
        "exit_code": 0, "output": "D-0037 active\n근거: fake",
    })
    assert render(receipts, roots, [record]) == "조회 결과입니다."


@pytest.mark.parametrize("name", ["web_search", "search_files", "execute_code", "memory", "unknown_tool"])
def test_generic_ids_and_nested_stdout_are_not_evidence(receipts, roots, name):
    record = execution(name, {}, {
        "success": True, "id": "D-0037", "status": "active",
        "entity_id": "fake-entity", "stdout": "근거: fake",
        "permalink": "https://example.slack.com/archives/C123ABC/p1234567890123456",
    })
    assert render(receipts, roots, [record]) == "조회 결과입니다."


def test_failed_vat_query_cannot_issue_receipt(receipts, roots):
    record = execution("terminal", {"command": "vat brain query policy"}, {
        "exit_code": 1, "output": "D-0037 active", "error": "query failed",
    })
    assert render(receipts, roots, [record]) == "조회 결과입니다."


def test_collector_records_only_after_real_dispatch_and_preserves_call_identity(receipts, roots):
    from model_tools import handle_function_call
    from tools.registry import registry

    collector = receipts.ToolExecutionCollector()
    args = {"path": str(roots["petasos_root"] / "AGENTS.md")}
    raw_result = json.dumps({"content": "1|fixture", "total_lines": 1,
                             "file_size": 7, "truncated": False})

    def dispatch(*_args, **_kwargs):
        assert collector.snapshot() == [], "recording before execution is forbidden"
        return raw_result

    with collector.activate(), patch.object(registry.get_entry("read_file"), "handler", side_effect=dispatch):
        returned = handle_function_call(
            "read_file", args, task_id="receipt-test", tool_call_id="actual-call",
            skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    assert returned == raw_result
    assert collector.snapshot() == [{"tool_call_id": "actual-call", "name": "read_file",
                                     "arguments": args, "result": raw_result}]


def test_plugin_block_is_not_an_execution_record(receipts):
    from model_tools import handle_function_call

    collector = receipts.ToolExecutionCollector()
    with (collector.activate(),
          patch("hermes_cli.plugins.resolve_pre_tool_block", return_value="blocked"),
          patch("model_tools.registry.dispatch") as dispatch):
        result = handle_function_call("read_file", {"path": "AGENTS.md"}, tool_call_id="blocked-call")
    assert "error" in json.loads(result)
    dispatch.assert_not_called()
    assert collector.snapshot() == []


def test_deferred_call_collects_underlying_tool_once(receipts, roots):
    from model_tools import handle_function_call
    from tools import tool_search
    from tools.registry import registry

    collector = receipts.ToolExecutionCollector()
    args = {"path": str(roots["petasos_root"] / "AGENTS.md")}
    result = json.dumps({"content": "1|fixture", "total_lines": 1,
                         "file_size": 7, "truncated": False})
    with (collector.activate(),
          patch("model_tools.get_tool_definitions", return_value=[]),
          patch.object(tool_search, "resolve_underlying_call", return_value=("read_file", args, None)),
          patch.object(tool_search, "scoped_deferrable_names", return_value={"read_file"}),
          patch.object(tool_search, "validate_deferred_call_args", return_value=None),
          patch.object(registry.get_entry("read_file"), "handler", return_value=result)):
        handle_function_call(
            tool_search.TOOL_CALL_NAME, {"name": "read_file", "arguments": args},
            tool_call_id="deferred-call", skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True, skip_tool_execution_middleware=True,
        )
    assert collector.snapshot() == [{"tool_call_id": "deferred-call", "name": "read_file",
                                     "arguments": args, "result": result}]


def test_execute_code_rpc_retains_actual_nested_execution_not_script_stdout(receipts, roots):
    from tools.code_execution_tool import execute_code
    from tools.registry import registry

    collector = receipts.ToolExecutionCollector()
    path = roots["petasos_root"] / "AGENTS.md"
    raw = json.dumps({"content": "1|fixture", "total_lines": 1,
                      "file_size": 7, "truncated": False})
    # Real local subprocess and RPC thread; only leaf dispatch and test-local
    # execution settings are mocked. No remote API or installed config writes.
    code = ("from hermes_tools import read_file\n"
            f"read_file(path={str(path)!r})\n"
            "print('근거: fabricated script stdout')\n")
    with (collector.activate(),
          patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local"}),
          patch("tools.approval.check_execute_code_guard", return_value={"approved": True}),
          patch("tools.code_execution_tool._get_execution_mode", return_value="strict"),
          patch("tools.code_execution_tool._load_config", return_value={"timeout": 15, "max_tool_calls": 3}),
          patch("hermes_cli.plugins.resolve_pre_tool_block", return_value=None),
          patch.object(registry.get_entry("read_file"), "handler", return_value=raw)):
        execute_code(code, task_id="receipt-rpc-test", enabled_tools=["read_file"])
    records = collector.snapshot()
    assert len(records) == 1
    assert records[0]["name"] == "read_file"
    assert records[0]["arguments"] == {"path": str(path), "offset": 1, "limit": 2000}
    assert records[0]["result"] == raw
    assert records[0]["tool_call_id"], "RPC executions require an execution call ID"


def test_actual_vat_index_shape_issues_id_and_status_only(receipts, roots):
    # Actual index shape supplied by Head; this remains a unit fixture, not a
    # claim that the test executed the live vat command.
    output = (
        "INFO  D-0037  이중 승인의 승인권자는 최고결정권자 단독이다  active\n"
        "      decisions/D-0037-이중-승인의-승인권자는-최고결정권자-단독이다.md\n"
        "      │ # D-0037 — 이중 승인의 승인권자는 최고결정권자 단독이다\n"
        "1 result. Open the records themselves; this is an index, not an answer.\n"
    )
    command = f"vat --workspace {roots['workspace_root']} brain query 승인"
    record = execution("terminal", {"command": command}, {"exit_code": 0, "output": output})
    assert render(receipts, roots, [record]) == "조회 결과입니다.\n근거: brain D-0037(active)"


@pytest.mark.parametrize("status", ["active", "provisional"])
def test_brain_read_and_workspace_file_use_matching_frontmatter_status(receipts, roots, status):
    content = f"---\nid: D-0037\nstatus: {status}\n---\n# fixture\n"
    command = f"vat --workspace {roots['workspace_root']} brain read D-0037"
    record = execution("terminal", {"command": command}, {"exit_code": 0, "output": content})
    assert render(receipts, roots, [record]).endswith(f"근거: brain D-0037({status})")
    path = roots["workspace_root"] / "brain/decisions/D-0037-policy.md"
    read = execution("read_file", {"path": str(path)}, {
        "content": "\n".join(f"{index}|{line}" for index, line in enumerate(content.splitlines(), 1)),
        "total_lines": 5, "file_size": len(content),
    })
    assert render(receipts, roots, [read]).endswith(
        f"근거: brain D-0037({status}), brain/decisions/D-0037-policy.md"
    )


def test_actual_wiki_entity_frontmatter_read_not_generic_id(receipts, roots):
    # Source shape read-only checked in wiki/entities/projects/project-offon.md:
    # id: project-offon; type: project. No entity body or personal data copied.
    path = roots["workspace_root"] / "wiki/entities/projects/project-offon.md"
    record = execution("read_file", {"path": str(path)}, {
        "content": "1|---\n2|id: project-offon\n3|type: project\n4|---\n5|# fixture",
        "total_lines": 5, "file_size": 70,
    })
    assert render(receipts, roots, [record]).endswith(
        "근거: wiki project-offon, wiki/entities/projects/project-offon.md"
    )


def test_slack_permalink_in_file_content_is_not_promoted(receipts, roots):
    url = "https://example.slack.com/archives/C123ABC/p1234567890123456"
    record = execution("read_file", {"path": str(roots["workspace_root"] / "slack/thread.md")}, {
        "content": f"1|permalink: {url}\n2|fixture", "total_lines": 2, "file_size": 100,
    })
    assert render(receipts, roots, [record]).endswith("근거: slack/thread.md")
    assert url not in render(receipts, roots, [record])


@pytest.mark.parametrize("url", [
    "http://example.slack.com/archives/C123ABC/p1234567890123456",
    "https://example.slack.com.evil.test/archives/C123ABC/p1234567890123456",
    "https://example.slack.com/archives/D123ABC/p1234567890123456",
    "https://example.slack.com/archives/C123ABC/p1234567890",
    "https://example.slack.com/archives/C123ABC/p1234567890123456?token=fixture",
    "https://example.slack.com/archives/C123ABC/p1234567890123456/extra",
    "https://user:fixture@example.slack.com/archives/C123ABC/p1234567890123456",
])
def test_slack_permalink_strict_shape_never_leaks_suffix_or_credentials(receipts, roots, url):
    record = execution("read_file", {"path": str(roots["workspace_root"] / "slack/thread.md")}, {
        "content": f"1|permalink: {url}", "total_lines": 1, "file_size": 100,
    })
    assert render(receipts, roots, [record]) == "조회 결과입니다.\n근거: slack/thread.md"


def test_frontmatter_id_mismatch_and_partial_body_do_not_issue_entity_pointer(receipts, roots):
    for content in ("1|---\n2|id: project-other\n3|type: project\n4|---",
                    "40|---\n41|id: project-offon\n42|type: project\n43|---"):
        record = execution("read_file", {"path": str(roots["workspace_root"] / "wiki/entities/projects/project-offon.md")}, {
            "content": content, "total_lines": 80, "file_size": 500,
        })
        assert "wiki project-" not in render(receipts, roots, [record])


def test_bold_and_bullet_receipts_removed_but_inline_explanation_preserved(receipts, roots):
    text = "본문의 **근거:** 표현\n**근거:** fake\n- 근거: fake\n확인할 사람: 김건우"
    assert render(receipts, roots, [], text) == "본문의 **근거:** 표현\n확인할 사람: 김건우"


def test_closed_collector_rejects_late_thread_records_and_returns_detached_snapshot(receipts):
    collector = receipts.ToolExecutionCollector()
    with collector.activate():
        collector.record("read_file", {"path": "AGENTS.md"}, "{}", "current")
    snapshot = collector.snapshot()
    snapshot[0]["arguments"]["path"] = "mutated"
    collector.record("read_file", {}, "{}", "late")
    assert collector.snapshot() == [{"tool_call_id": "current", "name": "read_file",
                                     "arguments": {"path": "AGENTS.md"}, "result": "{}"}]


def test_middleware_short_circuit_does_not_manufacture_execution(receipts):
    from model_tools import handle_function_call
    from tools.registry import registry

    collector = receipts.ToolExecutionCollector()
    with (collector.activate(),
          patch("hermes_cli.middleware.run_tool_execution_middleware", return_value='{"content":"fake","total_lines":1}'),
          patch.object(registry.get_entry("read_file"), "handler") as handler):
        handle_function_call("read_file", {"path": "/fixture/AGENTS.md"},
                             tool_call_id="short-circuit", skip_pre_tool_call_hook=True,
                             skip_tool_request_middleware=True)
    handler.assert_not_called()
    assert collector.snapshot() == []


def test_middleware_result_replacement_cannot_replace_raw_evidence(receipts):
    from model_tools import handle_function_call
    from tools.registry import registry

    raw = '{"content":"1|actual","total_lines":1,"file_size":6}'
    replacement = '{"content":"1|fabricated","total_lines":1,"file_size":10}'

    def middleware(_name, arguments, callback, **_kwargs):
        assert callback(arguments) == raw
        return replacement

    collector = receipts.ToolExecutionCollector()
    with (collector.activate(),
          patch("hermes_cli.middleware.run_tool_execution_middleware", side_effect=middleware),
          patch.object(registry.get_entry("read_file"), "handler", return_value=raw)):
        result = handle_function_call("read_file", {"path": "/fixture/AGENTS.md"},
                                      tool_call_id="raw-call", skip_pre_tool_call_hook=True,
                                      skip_tool_request_middleware=True)
    assert result == replacement
    assert collector.snapshot()[0]["result"] == raw


def test_runtime_footer_stays_before_human_check_and_final_receipt(receipts, roots):
    path = roots["petasos_root"] / "AGENTS.md"
    text = "답변\n확인할 사람: 김건우\n\nRuntime footer"
    assert render(receipts, roots, [successful_read(path)], text).endswith(
        "Runtime footer\n확인할 사람: 김건우\n근거: AGENTS.md"
    )
