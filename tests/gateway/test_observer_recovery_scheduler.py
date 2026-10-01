"""Network-free behavioral regressions; uses only a temporary catch-up DB."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

from plugins.platforms.slack import observer_catchup as oc


class Feed:
    def __init__(self, name="T1", failures=(), pages=3):
        self.name = name
        self.failures = set(failures)
        self.pages = pages
        self.calls = []

    async def conversations_history(self, *, channel, oldest, cursor, limit):
        self.calls.append((self.name, channel, cursor, limit))
        if channel in self.failures:
            raise RuntimeError("synthetic fetch failure")
        offset = int(cursor or 0)
        return {
            "messages": [{"ts": str(100 - offset), "user": "U_SYNTHETIC"}],
            "response_metadata": {
                "next_cursor": str(offset + 1) if offset + 1 < self.pages else ""
            },
        }


class SchedulerRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state" / "catchup.sqlite3"
        self.conn = oc.open_store(self.path)
        self.dispatch = AsyncMock()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def config(self, channels, budget=1):
        return oc.CatchupConfig(enabled=True, source_channels=frozenset(channels),
                                message_limit=budget, page_limit=1, page_size=200)

    async def run_once(self, feed, config):
        return await oc.run_catchup(
            client=feed, dispatch=self.dispatch, conn=self.conn,
            workspace_id="T1", allowed_channels=config.source_channels,
            config=config, now=200,
        )

    async def test_budget_one_rotates_to_unstarted_channel_after_reopen(self):
        feed = Feed()
        config = self.config(["C1", "C2", "C3"])
        await self.run_once(feed, config)
        self.conn.close()
        self.conn = oc.open_store(self.path)
        await self.run_once(feed, config)
        await self.run_once(feed, config)
        self.assertEqual([call[1] for call in feed.calls], ["C1", "C2", "C3"])
        for channel in config.source_channels:
            self.assertFalse(oc.is_channel_bootstrap_complete(self.conn, "T1", channel))

    async def test_one_failed_channel_does_not_stop_other_36_channels(self):
        channels = [f"C{i:02d}" for i in range(37)]
        feed = Feed(failures={"C00"}, pages=2)
        sleep = AsyncMock()
        totals = await oc.run_workspace_catchups(
            clients={"T1": feed}, dispatch=self.dispatch, conn=self.conn,
            config=self.config(channels, budget=37), bot_user_ids={},
            clock=lambda: 200, sleep=sleep,
        )
        self.assertEqual(totals["T1"]["C00"].attempts, 3)
        self.assertTrue(totals["T1"]["C00"].pending)
        for channel in channels[1:]:
            self.assertTrue(oc.is_channel_bootstrap_complete(self.conn, "T1", channel))
            self.assertEqual(totals["T1"][channel].dispatched, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])

    async def test_workspaces_get_rounds_before_first_workspace_finishes(self):
        order = []
        first, second = Feed("T1"), Feed("T2")

        async def record(event):
            order.append(event["team"])

        await oc.run_workspace_catchups(
            clients={"T1": first, "T2": second}, dispatch=record, conn=self.conn,
            config=self.config(["C1"]), bot_user_ids={}, clock=lambda: 200,
        )
        self.assertEqual(order[:4], ["T1", "T2", "T1", "T2"])

    async def test_dispatch_failure_is_isolated_and_not_automatically_retried(self):
        async def dispatch(event):
            if event["channel"] == "C1":
                raise RuntimeError("synthetic turn failure")

        feed = Feed(pages=1)
        totals = await oc.run_workspace_catchups(
            clients={"T1": feed}, dispatch=dispatch, conn=self.conn,
            config=self.config(["C1", "C2"], budget=2), bot_user_ids={},
            clock=lambda: 200,
        )
        self.assertEqual(totals["T1"]["C1"].attempts, 1)
        self.assertTrue(totals["T1"]["C1"].error.startswith("dispatch_failed:"))
        self.assertFalse(oc.is_channel_bootstrap_complete(self.conn, "T1", "C1"))
        self.assertTrue(oc.is_channel_bootstrap_complete(self.conn, "T1", "C2"))

    async def test_cursor_cycle_is_isolated_without_marking_complete(self):
        feed = Feed()
        feed.conversations_history = AsyncMock(return_value={
            "messages": [], "response_metadata": {"next_cursor": "same"},
        })
        totals = await oc.run_workspace_catchups(
            clients={"T1": feed}, dispatch=self.dispatch, conn=self.conn,
            config=self.config(["C1"]), bot_user_ids={}, clock=lambda: 200,
        )
        self.assertEqual(feed.conversations_history.await_count, 2)
        self.assertEqual(totals["T1"]["C1"].error, "history_cursor_repeated")
        self.assertFalse(oc.is_channel_bootstrap_complete(self.conn, "T1", "C1"))

    async def test_round_limit_retains_cursor_for_next_invocation(self):
        feed = Feed(pages=3)
        config = self.config(["C1"])
        totals = await oc.run_workspace_catchups(
            clients={"T1": feed}, dispatch=self.dispatch, conn=self.conn,
            config=config, bot_user_ids={}, clock=lambda: 200, max_rounds=1,
        )
        self.assertTrue(totals["T1"]["C1"].pending)
        await self.run_once(feed, config)
        self.assertEqual(feed.calls[1][2], "1")
        self.assertFalse(oc.is_channel_bootstrap_complete(self.conn, "T1", "C1"))

    async def test_truncated_roots_failure_does_not_skip_unseen_older_tail(self):
        oc.advance_channel_checkpoint(self.conn, "T1", "C1", "10", 200)
        oc.record_bootstrap_completion(self.conn, "T1", "C1", 200)
        feed = Feed()
        feed.conversations_history = AsyncMock(return_value={
            "messages": [{"ts": "40", "user": "U_SYNTHETIC"},
                         {"ts": "30", "user": "U_SYNTHETIC"}],
            "response_metadata": {"next_cursor": "older-unseen"},
        })

        async def dispatch(event):
            if event["ts"] == "40":
                raise RuntimeError("synthetic dispatch failure")

        self.dispatch = dispatch
        await self.run_once(feed, self.config(["C1"], budget=2))
        self.assertEqual(oc.get_channel_checkpoint(self.conn, "T1", "C1"), "10")

    async def test_truncated_replies_failure_does_not_skip_unseen_older_tail(self):
        feed = Feed()
        feed.conversations_replies = AsyncMock(return_value={
            "messages": [{"ts": "40", "user": "U_SYNTHETIC"},
                         {"ts": "30", "user": "U_SYNTHETIC"}],
            "response_metadata": {"next_cursor": "older-unseen"},
        })
        oc.advance_thread_checkpoint(self.conn, "T1", "C1", "5", "10", 200)

        async def dispatch(event):
            if event["ts"] == "40":
                raise RuntimeError("synthetic dispatch failure")

        result = oc.ChannelCatchupResult()
        ok, _ = await oc._drain_thread(
            client=feed, dispatch=dispatch, conn=self.conn, workspace_id="T1",
            channel_id="C1", root_ts="5", since="10", bot_user_id="",
            config=self.config(["C1"], budget=2), budget=2, result=result, now=200,
        )
        self.assertFalse(ok)
        self.assertEqual(oc.get_thread_checkpoint(self.conn, "T1", "C1", "5"), "10")

    async def test_no_progress_stops_without_completing_the_channel(self):
        stalled = oc.ChannelCatchupResult(pending=True, attempts=1)
        with patch.object(oc, "run_catchup", new=AsyncMock(return_value={"C1": stalled})) as run:
            totals = await oc.run_workspace_catchups(
                clients={"T1": Feed()}, dispatch=self.dispatch, conn=self.conn,
                config=self.config(["C1"]), bot_user_ids={}, clock=lambda: 200,
            )
        self.assertEqual(run.await_count, 1)
        self.assertEqual(totals["T1"]["C1"].error, "catchup_no_progress")
        self.assertTrue(totals["T1"]["C1"].pending)
        self.assertFalse(oc.is_channel_bootstrap_complete(self.conn, "T1", "C1"))

    async def test_fetch_retry_success_retains_first_error_and_clears_final_error(self):
        feed = Feed()
        feed.conversations_history = AsyncMock(side_effect=[
            RuntimeError("synthetic failure"),
            {"messages": [], "response_metadata": {"next_cursor": ""}},
        ])
        totals = await oc.run_workspace_catchups(
            clients={"T1": feed}, dispatch=self.dispatch, conn=self.conn,
            config=self.config(["C1"]), bot_user_ids={}, clock=lambda: 200,
            sleep=AsyncMock(),
        )
        result = totals["T1"]["C1"]
        self.assertIsNone(result.error)
        self.assertTrue(result.first_error.startswith("history_fetch_failed:"))
        self.assertFalse(result.pending)
        self.assertEqual(result.attempts, 2)
