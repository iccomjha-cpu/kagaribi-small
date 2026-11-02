import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("asahi_watch", HERE / "kagaribi_asahi_watch.py")
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)


class AsahiWatchTest(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000.0
        self.old = datetime.fromtimestamp(self.now - 901, timezone.utc).isoformat()
        self.fresh = datetime.fromtimestamp(self.now - 10, timezone.utc).isoformat()
        self.pending = [{"id": 10, "sender": "jun", "target": "asahi", "createdAt": self.old}]

    def test_alert_once_and_recovery_once(self):
        notice, state = watch.decide(self.pending, {}, self.now, 900)
        self.assertIn("未処理が1件", notice)
        self.assertIsNone(watch.decide(self.pending, state, self.now + 1, 900)[0])
        replied = self.pending + [{"id": 11, "sender": "asahi", "replyTo": 10,
                                   "createdAt": self.fresh}]
        notice, state = watch.decide(replied, state, self.now, 900)
        self.assertIn("復帰", notice)
        self.assertIsNone(watch.decide(replied, state, self.now + 1, 900)[0])

    def test_no_pending_means_no_alert(self):
        messages = [{"id": 1, "sender": "asahi", "createdAt": self.old}]
        self.assertIsNone(watch.decide(messages, {}, self.now, 900)[0])

    def test_unrelated_and_answered_are_not_pending(self):
        messages = self.pending + [
            {"id": 20, "sender": "jun", "target": "mizuki", "createdAt": self.old},
            {"id": 21, "sender": "asahi", "replyTo": 10, "createdAt": self.fresh},
        ]
        self.assertEqual([], watch.pending_for_asahi(messages))

    def test_run_once_posts_and_persists_then_deduplicates(self):
        calls = []
        def fake_http(_url, path, payload=None):
            calls.append((path, payload))
            if payload is not None:
                return {"message": {"id": 99}}
            if "tail=1" in path:
                return {"messages": [{"id": 10}]}
            return {"messages": self.pending}
        with tempfile.TemporaryDirectory() as folder, patch.object(watch, "http_json", side_effect=fake_http), patch.object(watch.time, "time", return_value=self.now):
            state_path = Path(folder) / "state.json"
            first = watch.run_once("http://test", state_path, 15)
            second = watch.run_once("http://test", state_path, 15)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            posts = [c for c in calls if c[1] is not None]
            self.assertEqual(1, len(posts))
            self.assertEqual("hinata", posts[0][1]["sender"])
            self.assertEqual("jun", posts[0][1]["target"])
            self.assertTrue(posts[0][1]["body"].startswith("【朝日監視】"))
            self.assertLessEqual(len(posts[0][1]["body"]), 100)
            self.assertTrue(json.loads(state_path.read_text(encoding="utf-8"))["alerted"])

    def test_fetches_latest_window_from_tail(self):
        paths = []
        def fake_http(_url, path, payload=None):
            paths.append(path)
            return {"messages": [{"id": 578}]} if "tail=1" in path else {"messages": [{"id": 578}]}
        with patch.object(watch, "http_json", side_effect=fake_http):
            self.assertEqual([{"id": 578}], watch.fetch_recent_messages("http://test"))
        self.assertEqual(["/api/messages?tail=1", "/api/messages?after=78&limit=500"], paths)

    def test_dry_run_does_not_post_or_save_state(self):
        calls = []
        def fake_http(_url, path, payload=None):
            calls.append((path, payload))
            if "tail=1" in path:
                return {"messages": [{"id": 10}]}
            return {"messages": self.pending}
        with tempfile.TemporaryDirectory() as folder, patch.object(watch, "http_json", side_effect=fake_http), patch.object(watch.time, "time", return_value=self.now):
            state_path = Path(folder) / "state.json"
            notice = watch.run_once("http://test", state_path, 15, dry_run=True)
            self.assertTrue(notice.startswith("【朝日監視】"))
            self.assertFalse(state_path.exists())
            self.assertFalse(any(payload is not None for _, payload in calls))

    def test_five_identical_alert_recovery_cycles(self):
        for _ in range(5):
            notice, state = watch.decide(self.pending, {}, self.now, 900)
            self.assertIn("未処理が1件", notice)
            self.assertIsNone(watch.decide(self.pending, state, self.now + 1, 900)[0])
            replied = self.pending + [{"id": 11, "sender": "asahi", "replyTo": 10,
                                       "createdAt": self.fresh}]
            notice, state = watch.decide(replied, state, self.now, 900)
            self.assertIn("復帰", notice)
            self.assertIsNone(watch.decide(replied, state, self.now + 1, 900)[0])


if __name__ == "__main__":
    unittest.main()
