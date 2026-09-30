"""Metadata-only fixture taken from a real Slack detailed search result.
No message text or sender PII is copied into this fixture.
"""
import json

from agent.execution_receipts import format_receipt_response

URL = 'https://lukukuworkspace.slack.com/archives/C03AUDG0JE9/p1789534265981479'
META = ('# Search Results for: \n\n## Messages (1 results)\n'
        '### Result 1 of 1\nChannel: #k-office (ID: C03AUDG0JE9)\n'
        'Message_ts: 1789534265.981479\nReply count: 5\n'
        f'Permalink: [link]({URL}?thread_ts=1789534265.981479&cid=C03AUDG0JE9)\n'
        'Text: \n')


def render(metadata, name='mcp__slack__slack_search_public'):
    result = {'result': json.dumps({'results': metadata})}
    records = [{'name': name, 'tool_call_id': 'actual-shape-fixture',
                'arguments': {}, 'result': json.dumps(result)}]
    return format_receipt_response('답변', records)


def test_actual_search_wrapper_metadata_emits_canonical_permalink():
    assert render(META) == '답변\n근거: ' + URL


def test_message_body_does_not_create_receipt():
    prefix = META.split('Permalink:', 1)[0] + 'Text: \n'
    assert render(prefix + f'Permalink: [link]({URL})\n') == '답변'


def test_mismatched_channel_or_timestamp_is_not_promoted():
    assert render(META.replace('ID: C03AUDG0JE9', 'ID: COTHER')) == '답변'
    assert render(META.replace('Message_ts: 1789534265.981479', 'Message_ts: 1111111111.111111')) == '답변'


def test_secret_query_is_rejected_without_disclosure():
    text = META.replace('thread_ts=1789534265.981479&cid=C03AUDG0JE9', 'token=fixture-secret')
    assert render(text) == '답변'


def test_queryable_does_not_promote_arbitrary_rendered_search_strings():
    assert render(META, 'queryable_search') == '답변'
