"""Scoped follow-up regressions; synthetic results are not live evidence."""
import json

import pytest

from agent.execution_receipts import ToolExecutionCollector, format_receipt_response


URL = "https://example.slack.com/archives/C123ABC/p1234567890123456"
SLACK_TOOLS = (
    "mcp__slack__slack_read_thread",
    "mcp__slack__slack_read_channel",
    "mcp__slack__slack_search_public",
    "mcp__slack__slack_search_public_and_private",
    "queryable_search",
)


def render(name, result, arguments=None, text="답변", **roots):
    collector = ToolExecutionCollector()
    with collector.activate():
        collector.record(name, arguments or {}, result, "fixture-execution")
    return format_receipt_response(text, collector.snapshot(), **roots)


@pytest.mark.parametrize("name", SLACK_TOOLS)
@pytest.mark.parametrize("field", ["permalink", "url", "source_url"])
def test_allowlisted_slack_tools_accept_only_explicit_structured_url_fields(name, field):
    result = {"ok": True, "results": [{field: URL, "text": "body must not be emitted"}]}
    assert render(name, json.dumps(result)) == f"답변\n근거: {URL}"


def test_queryable_search_nested_hits_preserve_order_and_deduplicate():
    second = "https://example.slack.com/archives/C456DEF/p1234567890123457"
    result = {"data": {"hits": [{"source_url": URL}, {"metadata": {"url": second}},
                                {"permalink": URL}]}}
    assert render("queryable_search", result) == f"답변\n근거: {URL}, {second}"


def test_slack_result_list_and_mcp_structured_content():
    assert render(SLACK_TOOLS[0], [{"permalink": URL}]) == f"답변\n근거: {URL}"
    assert render(SLACK_TOOLS[0], {"structuredContent": {"messages": [{"url": URL}]}}) == f"답변\n근거: {URL}"


@pytest.mark.parametrize("failure", [
    {"error": "fixture failure"}, {"success": False}, {"failed": True},
    {"status": "error"}, {"status": "failed"}, {"status": "blocked"},
    {"status": "pending"}, {"status": "running"}, {"isError": True}, {"ok": False},
])
def test_failed_slack_result_never_promotes_even_valid_nested_permalink(failure):
    result = {**failure, "results": [{"permalink": URL}]}
    assert render("queryable_search", result) == "답변"
    nested = {"results": [{**failure, "permalink": URL}]}
    assert render(SLACK_TOOLS[0], nested) == "답변"


@pytest.mark.parametrize("name", [
    "terminal", "execute_code", "web_search", "mcp__slack__slack_send_message",
    "mcp__other__slack_read_thread", "queryable_search_fake",
])
def test_non_allowlisted_tool_cannot_promote_slack_url(name):
    assert render(name, {"success": True, "permalink": URL}) == "답변"


@pytest.mark.parametrize("result", [
    {"stdout": URL}, {"output": json.dumps({"permalink": URL})},
    {"text": URL}, {"content": [{"type": "text", "text": json.dumps({"url": URL})}]},
    {"arguments": {"permalink": URL}}, {"id": URL},
    {"body": {"source_url": URL}}, "근거: " + URL,
])
def test_body_stdout_echo_arguments_and_generic_fields_are_not_receipts(result):
    assert render("queryable_search", result) == "답변"


@pytest.mark.parametrize("url", [
    URL + "?thread_ts=1234567890.123456&cid=C123ABC",
    URL + "?token=fixture-never-print", URL + "#fixture",
    URL + "/extra", URL.replace("https:", "http:"),
    URL.replace(".slack.com/", ".slack.com.evil.test/"),
    URL.replace("/C123ABC/", "/D123ABC/"),
    URL.replace("example.slack.com", "user:fixture@example.slack.com"),
    "https://example.slack.com/archives/C123ABC/p123",
])
def test_slack_strict_url_rejects_queries_fragments_credentials_and_lookalikes(url):
    assert render("queryable_search", {"source_url": url}) == "답변"


def test_model_receipt_without_collector_is_removed_even_for_valid_slack_url():
    assert format_receipt_response("답변\n근거: " + URL, None) == "답변"


INDEX = (
    "INFO  D-0037  이중 승인의 승인권자는 최고결정권자 단독이다  active\n"
    "      decisions/D-0037-이중-승인의-승인권자는-최고결정권자-단독이다.md\n"
    "1 result. Open the records themselves; this is an index, not an answer.\n"
)


@pytest.mark.parametrize("query", [
    "D-0037 --limit 1", "D-0037 승인 --limit 1", "--limit 1 D-0037 승인",
    '"이중 승인" D-0037 --limit 10', "D-0037 승인", "D-0037",
])
def test_vat_query_accepts_multiple_terms_and_positive_limit(tmp_path, query):
    workspace = tmp_path / "workspace"
    command = f"vat --workspace {workspace} brain query {query}"
    assert render("terminal", {"exit_code": 0, "output": INDEX}, {"command": command},
                  workspace_root=workspace) == "답변\n근거: brain D-0037(active)"


@pytest.mark.parametrize("query", [
    "D-0037 --limit", "D-0037 --limit 0", "D-0037 --limit -1",
    "D-0037 --limit 1.5", "D-0037 --limit arbitrary", "--limit 1",
    "D-0037 --limit 1 --limit 2", "D-0037 --unknown 1", "D-0037 --workspace /tmp",
    "D-0037 --limit 1 | echo fake", "D-0037 --limit 1; echo fake",
    "D-0037 $(echo fake) --limit 1", "D-0037 --limit 1 > /tmp/fixture",
    "D-0037 --limit 1 && echo fake", "D-0037 --limit 1\necho fake",
])
def test_vat_query_rejects_invalid_flags_limits_and_shell_composition(tmp_path, query):
    workspace = tmp_path / "workspace"
    command = f"vat --workspace {workspace} brain query {query}"
    assert render("terminal", {"exit_code": 0, "output": INDEX}, {"command": command},
                  workspace_root=workspace) == "답변"


def test_vat_query_limit_does_not_relax_workspace_or_exit_status(tmp_path):
    workspace = tmp_path / "workspace"
    wrong = tmp_path / "other"
    command = f"vat --workspace {wrong} brain query D-0037 --limit 1"
    assert render("terminal", {"exit_code": 0, "output": INDEX}, {"command": command},
                  workspace_root=workspace) == "답변"
    command = f"vat --workspace {workspace} brain query D-0037 --limit 1"
    assert render("terminal", {"exit_code": 1, "output": INDEX}, {"command": command},
                  workspace_root=workspace) == "답변"
