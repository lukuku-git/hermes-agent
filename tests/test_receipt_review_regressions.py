"""Moody blockers: file-body URLs are not provenance; task paths are resolved."""
import json
from unittest.mock import MagicMock, patch
from tools.file_operations import ReadResult

from agent.execution_receipts import format_receipt_response
from tools.file_tools import read_file_tool, reset_file_dedup


def record(path, result):
    return [{'name': 'read_file', 'tool_call_id': 'review-regression',
             'arguments': {'path': path}, 'result': json.dumps(result)}]


def test_general_file_body_permalink_is_not_promoted(tmp_path):
    path = tmp_path / 'policy.md'
    result = {'content': '1|permalink: https://example.slack.com/archives/C123ABC/p1234567890123456',
              'total_lines': 1, 'file_size': 100}
    rendered = format_receipt_response('답변', record(str(path), result),
                                       petasos_root=tmp_path, workspace_root=tmp_path / 'workspace')
    assert rendered == '답변\n근거: policy.md'


def test_actual_relative_read_has_handler_resolved_path(tmp_path):
    path = tmp_path / 'policy.md'
    path.write_text('fixture\n', encoding='utf-8')
    task_id = 'receipt-relative-regression'
    reset_file_dedup(task_id)
    ops = MagicMock()
    ops.read_file.return_value = ReadResult(content='1|fixture', total_lines=1, file_size=8)
    with (patch('tools.file_tools._resolve_path_for_task', return_value=path),
          patch('tools.file_tools._get_file_ops', return_value=ops)):
        result = json.loads(read_file_tool('policy.md', task_id=task_id))
    assert result.get('resolved_path') == str(path)
    rendered = format_receipt_response('답변', record('policy.md', result),
                                       petasos_root=tmp_path, workspace_root=tmp_path / 'workspace')
    assert rendered == '답변\n근거: policy.md'
