"""The vat INFO memory path grammar is deliberately narrower than file reads."""
import pytest

from agent.execution_receipts import format_receipt_response


def render(path, record_id="M-0086", command=None, workdir=None):
    execution = {
        "tool_call_id": "memory-index-fixture", "name": "terminal",
        "arguments": {"command": command or "vat --workspace /Users/zeus/lukuku-os brain query fixture", "workdir": workdir},
        "result": {"exit_code": 0, "output": (
            f"INFO  {record_id}  fixture record  active\n"
            f"      {path}\n"
            "1 result. Open the records themselves; this is an index, not an answer.\n"
        )},
    }
    return format_receipt_response("답변\n근거: fake", [execution])


@pytest.mark.parametrize("month", [f"{month:02d}" for month in range(1, 13)])
def test_memory_info_monthly_record_is_receipted(month):
    assert render(f"memory/2026-{month}/M-0086-fixture.md") == "답변\n근거: brain M-0086(active)"


@pytest.mark.parametrize("path,record_id", [
    ("memory/2026-00/M-0086-fixture.md", "M-0086"),
    ("memory/2026-13/M-0086-fixture.md", "M-0086"),
    ("memory/2026-9/M-0086-fixture.md", "M-0086"),
    ("memory/2026-09/M-0087-fixture.md", "M-0086"),
    ("memory/2026-09/D-0086-fixture.md", "D-0086"),
    ("memory/2026-09/F-0086-fixture.md", "F-0086"),
    ("memory/M-0086-fixture.md", "M-0086"),
    ("memory/nested/2026-09/M-0086-fixture.md", "M-0086"),
    ("memory/2026-09/nested/M-0086-fixture.md", "M-0086"),
    ("memory/2026-09/../M-0086-fixture.md", "M-0086"),
    ("memory/2026-09/M-0086-secret.md", "M-0086"),
    ("memory/2026-09/M-0086-fixture.md/other.md", "M-0086"),
])
def test_memory_info_rejects_invalid_or_sensitive_paths(path, record_id):
    assert render(path, record_id) == "답변"


def test_root_workdir_single_vat_remains_allowed():
    assert render("memory/2026-09/M-0086.md", command="vat brain query fixture",
                  workdir="/Users/zeus/lukuku-os") == "답변\n근거: brain M-0086(active)"


@pytest.mark.parametrize("command,workdir", [
    ("cd /Users/zeus/lukuku-os && vat brain query fixture", None),
    ("vat --workspace /Users/zeus/lukuku-os brain query fixture > /tmp/out", None),
    ("vat brain query fixture", "/tmp"),
    ("python -c 'vat brain query fixture'", "/Users/zeus/lukuku-os"),
])
def test_memory_paths_do_not_expand_command_allowlist(command, workdir):
    assert render("memory/2026-09/M-0086-fixture.md", command=command, workdir=workdir) == "답변"
