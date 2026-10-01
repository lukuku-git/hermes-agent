"""Chat failure notices name what actually failed."""
from cron.scheduler import _summarize_cron_failure_for_delivery


def test_inactivity_is_not_reported_as_provider_timeout():
    error = ("TimeoutError: Cron job Lukuku Slack export 분기 보존 idle for 601s "
             "(limit 600s) — last activity: executing tool: terminal")
    notice = _summarize_cron_failure_for_delivery({"name": "Lukuku Slack export 분기 보존"}, error)
    assert "provider" not in notice
    assert "no progress for 601s (inactivity limit 600s)" in notice
    assert "Last activity: executing tool: terminal." in notice


def test_provider_read_timeout_is_still_a_provider_timeout():
    notice = _summarize_cron_failure_for_delivery({"name": "x"}, "httpx.ReadTimeout: timed out")
    assert "provider timeout" in notice
