import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import kagaribi_mobile as m


class FakeUpstream(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _reply(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        FakeUpstream.seen.append(("GET", self.path, None))
        self._reply({"messages": [], "tasks": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
        FakeUpstream.seen.append(("POST", self.path, body))
        self._reply({"ok": True})


class MobileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.srv = m.MobileServer(("127.0.0.1", 0), "abcdefghjkmn", "http://127.0.0.1:%d" % cls.up.server_address[1])
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.up.shutdown()

    def req(self, method, path, body=None, token="abcdefghjkmn"):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers={"X-Kagaribi-Token": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def setUp(self):
        FakeUpstream.seen.clear()
        self.srv.fails.clear()

    def test_page_is_public_but_api_needs_token(self):
        with urllib.request.urlopen(self.base + "/m", timeout=5) as r:
            self.assertIn("篝火", r.read().decode())
        self.assertEqual(self.req("GET", "/api/messages", token="wrong")[0], 401)
        self.assertEqual(self.req("GET", "/api/messages", token="")[0], 401)
        self.assertEqual(FakeUpstream.seen, [])                                  # 合言葉が違えば、上流に届かない

    def test_whitelist_blocks_dangerous_paths(self):
        for method, path in (("GET", "/api/presence"), ("POST", "/api/tasks/update"), ("POST", "/api/tasks"), ("POST", "/api/presence"), ("GET", "/etc/passwd")):
            self.assertEqual(self.req(method, path, {"id": 1, "status": "verified"} if method == "POST" else None)[0], 404, path)
        self.assertEqual(FakeUpstream.seen, [])

    def test_post_is_rebuilt_sender_fixed_to_jun(self):
        code, _ = self.req("POST", "/api/messages", {"sender": "yoichi", "target": "asahi", "body": "やあ", "source": "bridge", "extra": 1})
        self.assertEqual(code, 200)
        self.assertEqual(FakeUpstream.seen[-1][2], {"sender": "jun", "target": "asahi", "body": "やあ", "replyTo": None, "source": "web"})
        self.assertEqual(self.req("POST", "/api/messages", {"body": "x", "target": "jun"})[0], 400)    # 不明な宛先
        self.assertEqual(self.req("POST", "/api/messages", {"body": ""})[0], 400)
        self.assertEqual(self.req("POST", "/api/messages", {"body": "あ" * 2001})[0], 400)

    def test_approve_passes_only_id(self):
        code, _ = self.req("POST", "/api/tasks/approve", {"id": 9, "note": "勝手なメモ", "status": "verified"})
        self.assertEqual(code, 200)
        self.assertEqual(FakeUpstream.seen[-1], ("POST", "/api/tasks/approve", {"id": 9, "note": "潤がスマホで許可"}))

    def test_repeated_failures_lock_the_client(self):
        for _ in range(m.FAIL_LIMIT):
            self.req("GET", "/api/messages", token="bad")
        self.assertEqual(self.req("GET", "/api/messages")[0], 429)               # 正しい合言葉でも、失敗が続いたら一時停止

    def test_identity_lets_owner_devices_in_without_token(self):
        calls = []
        lookup = lambda ip: calls.append(ip) or ("owner@example.com" if ip == "127.0.0.1" else "other@example.com")
        self.srv.identity = m.make_identity("owner@example.com", lookup)
        try:
            self.assertEqual(self.req("GET", "/api/messages", token="")[0], 200)             # 持ち主の端末(この試験では127.0.0.1)は、合言葉なしで通る
            self.assertEqual(self.req("GET", "/api/messages", token="")[0], 200)
            self.assertEqual(calls, ["127.0.0.1"])                                          # 結果は覚えている(毎回は問い合わせない)
            lookup2 = lambda ip: "other@example.com"
            self.srv.identity = m.make_identity("owner@example.com", lookup2)
            self.assertEqual(self.req("GET", "/api/messages", token="")[0], 401)           # 他人の端末は、合言葉が要る
            self.assertEqual(self.req("GET", "/api/messages")[0], 200)                       # 予備の合言葉は、そのまま使える
            self.srv.identity = m.make_identity("owner@example.com", lambda ip: None)
            self.assertEqual(self.req("GET", "/api/messages", token="")[0], 401)           # 問い合わせ失敗は、通さない
            self.srv.identity = m.make_identity("", lambda ip: "")
            self.assertEqual(self.req("GET", "/api/messages", token="")[0], 401)           # 持ち主が不明なら、全員を通さない
        finally:
            self.srv.identity = None

    def test_last_seen_is_recorded_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            self.srv.seen_file = os.path.join(d, "seen.json")
            try:
                self.assertEqual(self.req("POST", "/api/messages", {"body": "ひみつの本文"})[0], 200)
                seen = open(self.srv.seen_file, encoding="utf-8").read()
                self.assertIn('"how": "token"', seen)
                self.assertNotIn("ひみつの本文", seen)                                   # 本文は残さない
                self.assertNotIn("abcdefghjkmn", seen)                                  # 合言葉も残さない
            finally:
                self.srv.seen_file = None

    def test_identity_does_not_open_blocked_paths(self):
        self.srv.identity = m.make_identity("owner@example.com", lambda ip: "owner@example.com")
        try:
            self.assertEqual(self.req("POST", "/api/tasks/update", {"id": 1, "status": "verified"}, token="")[0], 404)   # 端末認証でも、許可リストの外は通さない
        finally:
            self.srv.identity = None

    def test_token_file_is_created_long_enough(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "token.txt")
            t1 = m.load_token(path)
            self.assertEqual(len(t1), 12)
            self.assertEqual(m.norm_token("ABCD-efgh 2345"), "abcdefgh2345")      # 大文字・ハイフン・空白の揺れは許す
            self.assertEqual(m.load_token(path), t1)                              # 2回目は同じ値

    def test_refuses_non_tailscale_bind(self):
        self.assertEqual(m.allowed("PUT", "/api/messages", ""), False)


if __name__ == "__main__":
    unittest.main()
