import os
import unittest
from unittest.mock import patch, MagicMock
import radar
from radar import parse_arguments, is_single_run_mode, Post, SeenStore, poll_cycle, run_radar


class TestRadarSingleRun(unittest.TestCase):
    def test_parse_arguments_once(self):
        args = parse_arguments(["--once"])
        self.assertTrue(args.once)

    def test_parse_arguments_default(self):
        args = parse_arguments([])
        self.assertFalse(args.once)

    def test_is_single_run_mode_cli(self):
        args = parse_arguments(["--once"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(is_single_run_mode(args))

    def test_is_single_run_mode_env_true(self):
        args = parse_arguments([])
        for val in ("true", "True", "TRUE", "1", "yes", "YES"):
            with patch.dict(os.environ, {"RUN_ONCE": val}):
                self.assertTrue(is_single_run_mode(args), f"Failed for RUN_ONCE={val}")

    def test_is_single_run_mode_env_false(self):
        args = parse_arguments([])
        for val in ("false", "False", "0", "no", ""):
            with patch.dict(os.environ, {"RUN_ONCE": val}):
                self.assertFalse(is_single_run_mode(args), f"Failed for RUN_ONCE={val}")

    def test_is_single_run_mode_unset(self):
        args = parse_arguments([])
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_single_run_mode(args))

    @patch("radar.fetch_new_posts")
    @patch("radar.send_telegram")
    def test_poll_cycle_with_matches(self, mock_send, mock_fetch):
        mock_send.return_value = True
        test_posts = [
            Post(
                id="t3_123",
                title="Help with cold email bounce rate issues",
                selftext="My bounce rate is skyrocketing with Apollo data.",
                author="u/testuser",
                permalink="https://reddit.com/r/coldemail/comments/123",
                subreddit="coldemail",
            ),
            Post(
                id="t3_456",
                title="Unrelated post title",
                selftext="Just saying hello world.",
                author="u/otheruser",
                permalink="https://reddit.com/r/sales/comments/456",
                subreddit="sales",
            ),
        ]
        mock_fetch.return_value = test_posts

        # Mock SeenStore
        store = MagicMock(spec=SeenStore)
        store.is_empty.return_value = False
        store.has.side_effect = lambda post_id: False

        poll_cycle(store)

        # Matched post should trigger telegram alert and be marked seen
        mock_send.assert_called_once()
        store.add.assert_any_call("t3_123")
        # Non-matched post should also be marked seen
        store.add.assert_any_call("t3_456")

    @patch("radar.fetch_new_posts")
    @patch("radar.send_telegram")
    def test_poll_cycle_first_run_silence(self, mock_send, mock_fetch):
        test_posts = [
            Post(
                id="t3_999",
                title="apollo bounce problem",
                selftext="apollo data issues",
                author="u/user",
                permalink="https://reddit.com/r/coldemail/comments/999",
                subreddit="coldemail",
            )
        ]
        mock_fetch.return_value = test_posts

        store = MagicMock(spec=SeenStore)
        store.is_empty.return_value = True

        with patch("config.ALERT_ON_FIRST_RUN_BACKLOG", False):
            poll_cycle(store)

        # Should NOT send telegram on first run backlog
        mock_send.assert_not_called()
        # Should mark seen silently
        store.add.assert_called_once_with("t3_999")

    @patch("radar.poll_cycle")
    @patch("radar.SeenStore")
    def test_run_radar_once_exits_cleanly(self, mock_seen_store, mock_poll_cycle):
        # run_radar with once=True must call poll_cycle once and return immediately without looping
        run_radar(once=True)
        mock_poll_cycle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
