import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import KagaribiHandler, KagaribiServer, create_message, derive_topic, initialize_database, list_messages
from bridge import configure_stdout, post_message, read_messages
from asahi_bridge import SafeFileReader, meeting_export


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.sqlite3"
        initialize_database(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_message_roundtrip_and_reply(self):
        first = create_message(self.db, {"sender": "潤", "target": "日向", "body": "まず通そう"})
        second = create_message(self.db, {"sender": "日向", "target": "潤", "body": "了解", "replyTo": first["id"]})
        messages = list_messages(self.db)
        self.assertEqual(["jun", "hinata"], [item["sender"] for item in messages])
        self.assertEqual("🌱日向", second["senderDisplay"])
        self.assertEqual(first["id"], second["replyTo"])

    def test_rejects_unknown_actor_and_missing_reply(self):
        with self.assertRaises(ValueError):
            create_message(self.db, {"sender": "不明", "body": "test"})
        with self.assertRaises(ValueError):
            create_message(self.db, {"sender": "夜一", "body": "test", "replyTo": 999})

    def test_formal_ids_and_legacy_names_share_one_timeline(self):
        create_message(self.db, {"sender": "asahi", "body": "ID形式"})
        create_message(self.db, {"sender": "美月", "target": "夜一", "body": "旧表示名形式"})
        messages = list_messages(self.db)
        self.assertEqual(["asahi", "mizuki"], [item["sender"] for item in messages])
        self.assertEqual("yoichi", messages[1]["target"])

    def test_initialize_migrates_legacy_actors_without_changing_message_data(self):
        database = __import__("sqlite3").connect(self.db)
        database.execute(
            "INSERT INTO messages(id, sender, target, body, reply_to, source, created_at) VALUES (10, ?, ?, ?, NULL, ?, ?)",
            ("潤", "夜一", "移行前本文", "web", "2026-10-29T00:00:00.000Z"),
        )
        database.execute(
            "INSERT INTO messages(id, sender, target, body, reply_to, source, created_at) VALUES (11, ?, ?, ?, 10, ?, ?)",
            ("夜一", "潤", "移行前返信", "api", "2026-10-29T00:00:01.000Z"),
        )
        database.commit()
        database.close()

        initialize_database(self.db)
        migrated = list_messages(self.db)
        self.assertEqual(
            [(10, "jun", "yoichi", "移行前本文", None, "web", "2026-10-29T00:00:00.000Z"),
             (11, "yoichi", "jun", "移行前返信", 10, "api", "2026-10-29T00:00:01.000Z")],
            [(item["id"], item["sender"], item["target"], item["body"], item["replyTo"], item["source"], item["createdAt"]) for item in migrated],
        )

    def test_bridge_configures_utf8_stdout_for_emoji_labels(self):
        class Output:
            settings = None

            def reconfigure(self, **settings):
                self.settings = settings

        output = Output()
        configure_stdout(output)
        self.assertEqual({"encoding": "utf-8", "errors": "strict"}, output.settings)

    def test_topic_prefers_active_task_and_falls_back_without_ai_summary(self):
        state = Path(self.temp.name) / "state.json"
        state.write_text(json.dumps({"tasks": [
            {"title": "完了済み", "status": "AGREED", "updated_at": "2026-10-29T01:00:00Z"},
            {"title": "現在の会議", "status": "RUNNING", "updated_at": "2026-10-29T00:00:00Z"},
        ], "events": []}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual("現在の会議", derive_topic(state)["title"])
        state.write_text(json.dumps({"tasks": [], "events": [{"text": "要約してはいけない"}]}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual({"title": "議題なし", "source": None, "status": None, "available": False}, derive_topic(state))

    def test_asahi_file_bridge_allows_named_root_and_rejects_escape_or_secret(self):
        allowed_root = Path(self.temp.name) / "allowed"
        allowed_root.mkdir()
        (allowed_root / "note.md").write_text("朝日が読む本文", encoding="utf-8")
        reader = SafeFileReader({"test": allowed_root}, Path(self.temp.name) / "audit.jsonl")
        self.assertEqual("朝日が読む本文", reader.read("test", "note.md")["content"])
        with self.assertRaises(ValueError):
            reader.read("test", "../outside.md")
        (allowed_root / "secrets.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            reader.read("test", "secrets.json")
        (allowed_root / "holdings_snapshot.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            reader.read("test", "holdings_snapshot.json")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.topic_state = Path(self.temp.name) / "state.json"
        self.topic_state.write_text(json.dumps({"tasks": [{"title": "API議題", "status": "RUNNING"}], "events": []}, ensure_ascii=False), encoding="utf-8")
        self.server = KagaribiServer(("127.0.0.1", 0), KagaribiHandler, Path(self.temp.name) / "api.sqlite3", self.topic_state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, payload=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_health_post_and_get(self):
        status, health = self.request("GET", "/api/health")
        self.assertEqual(200, status)
        self.assertTrue(health["ok"])
        status, posted = self.request("POST", "/api/messages", {"sender": "美月", "target": "夜一", "body": "見えてるよ"})
        self.assertEqual(201, status)
        self.assertEqual("mizuki", posted["message"]["sender"])
        status, listed = self.request("GET", "/api/messages")
        self.assertEqual(200, status)
        self.assertEqual(1, len(listed["messages"]))
        status, topic = self.request("GET", "/api/topic")
        self.assertEqual(200, status)
        self.assertEqual("API議題", topic["topic"]["title"])

    def test_bridge_inbox_reads_broadcast_and_addressed_messages(self):
        base = f"http://127.0.0.1:{self.port}"
        self.request("POST", "/api/messages", {"sender": "jun", "body": "全員へ"})
        self.request("POST", "/api/messages", {"sender": "hinata", "target": "yoichi", "body": "夜一へ"})
        self.request("POST", "/api/messages", {"sender": "hinata", "target": "asahi", "body": "朝日へ"})
        inbox = read_messages(base, "yoichi", 0, 200, inbox_only=True)
        self.assertEqual(["全員へ", "夜一へ"], [message["body"] for message in inbox])
        timeline = read_messages(base, "asahi", 0, 200)
        self.assertEqual(["全員へ", "夜一へ", "朝日へ"], [message["body"] for message in timeline])
        exported = meeting_export(base, 0, 200)
        self.assertEqual(3, exported["latestId"])
        self.assertEqual(3, len(exported["messages"]))

    def test_asahi_bridge_can_post_to_the_local_api(self):
        base = f"http://127.0.0.1:{self.port}"
        posted = post_message(base, {"sender": "asahi", "target": "jun", "body": "朝日ローカル投稿試験", "replyTo": None, "source": "bridge"})
        self.assertEqual("asahi", posted["message"]["sender"])
        self.assertEqual("jun", posted["message"]["target"])

    def test_ui_strips_display_emoji_beside_separate_avatar(self):
        app = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('actorLabel.replace(/^(?:☀️|🌱|🌙|⭐)\\s*/, "")', app)
        self.assertIn('message.source === "bridge" ? "ブリッジ"', app)

    def test_ui_contract_has_roundtable_topic_history_switch_and_ratio(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app = (static / "app.js").read_text(encoding="utf-8")
        css = (static / "style.css").read_text(encoding="utf-8")
        for marker in ('id="topic-title"', 'data-view="scene"', 'data-view="history"', 'id="view-ratio"'):
            self.assertIn(marker, html)
        self.assertIn('fetch("/api/topic"', app)
        self.assertIn('body[data-view="history"]', css)
        self.assertIn('.mobile-tabs', css)


if __name__ == "__main__":
    unittest.main()
