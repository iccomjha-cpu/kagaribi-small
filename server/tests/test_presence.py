import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import CHAIN_LIMIT, chain_status, current_presence, set_presence


class PresenceTests(unittest.TestCase):
    def test_heard_returns_to_idle_but_working_stays(self):
        store = {}
        set_presence(store, {"actor": "asahi", "state": "heard", "ref": 51}, now=100.0)
        set_presence(store, {"actor": "yoichi", "state": "working"}, now=100.0)
        self.assertEqual(current_presence(store, now=105.0)["asahi"]["state"], "heard")
        self.assertEqual(current_presence(store, now=120.0)["asahi"]["state"], "idle")
        self.assertEqual(current_presence(store, now=999.0)["yoichi"]["state"], "working")
        self.assertEqual(current_presence(store, now=999.0)["mizuki"]["state"], "idle")

    def test_alive_follows_ping_and_expires(self):
        store = {}
        self.assertFalse(current_presence(store, now=0.0)["hinata"]["alive"])
        set_presence(store, {"actor": "mizuki", "state": "ping"}, now=100.0)
        self.assertTrue(current_presence(store, now=120.0)["mizuki"]["alive"])
        self.assertEqual(current_presence(store, now=120.0)["mizuki"]["state"], "idle")
        self.assertFalse(current_presence(store, now=250.0)["mizuki"]["alive"])

    def test_rejects_unknown_actor_or_state(self):
        with self.assertRaises(ValueError):
            set_presence({}, {"actor": "jun", "state": "heard"})
        with self.assertRaises(ValueError):
            set_presence({}, {"actor": "asahi", "state": "dancing"})

    def test_chain_limit_and_free_mode(self):
        ai = lambda i: {"id": i, "sender": "mizuki" if i % 2 else "asahi", "body": "x"}
        jun = lambda body: {"sender": "jun", "body": body}
        self.assertTrue(chain_status([jun("こんにちは")] + [ai(i) for i in range(CHAIN_LIMIT - 1)])["allowed"])
        self.assertFalse(chain_status([jun("こんにちは")] + [ai(i) for i in range(CHAIN_LIMIT)])["allowed"])
        free = chain_status([jun("みんなで自由に雑談して")] + [ai(i) for i in range(30)])
        self.assertTrue(free["free"] and free["allowed"])
        self.assertFalse(chain_status([jun("みんなで雑談して")] + [ai(i) for i in range(60)])["allowed"])   # 上限
        self.assertFalse(chain_status([jun("雑談して"), jun("やめて")] + [ai(i) for i in range(CHAIN_LIMIT)])["allowed"])  # 次の潤の発言で通常に戻る
        self.assertTrue(chain_status([ai(1)])["allowed"])


class HandlerEvidenceTests(unittest.TestCase):
    def test_evidence_separates_recent_old_never_failed(self):
        import json
        import tempfile
        import server
        old_dir, old_alive = server.HEALTH_DIR, server.handler_alive
        with tempfile.TemporaryDirectory() as d:
            server.HEALTH_DIR = Path(d)
            health = Path(d) / "health_yoichi.json"
            server.handler_alive = lambda agent, now=None: not health.exists() or json.loads(health.read_text()).get("ok", True)
            try:
                now = 1_000_000.0
                self.assertEqual(server.handler_evidence("yoichi", now)["evidence"], "never")
                health.write_text(json.dumps({"ok": True, "at": now - 60}))
                self.assertEqual(server.handler_evidence("yoichi", now)["evidence"], "ok_recent")
                health.write_text(json.dumps({"ok": True, "at": now - 7 * 3600}))
                self.assertEqual(server.handler_evidence("yoichi", now)["evidence"], "ok_old")
                health.write_text(json.dumps({"ok": False, "at": now - 5}))
                self.assertEqual(server.handler_evidence("yoichi", now)["evidence"], "failed")
            finally:
                server.HEALTH_DIR, server.handler_alive = old_dir, old_alive


if __name__ == "__main__":
    unittest.main()
