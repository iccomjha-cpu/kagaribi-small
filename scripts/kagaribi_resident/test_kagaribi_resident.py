import json
import os
import tempfile
import unittest

import kagaribi_resident as r

RULES = {"jun": True, "direct_to_me": True, "all_from_anyone": True}


class ResidentTests(unittest.TestCase):
    def test_config_loads_and_persona_has_all_parts(self):
        cfg = r.load_config()
        for agent in ("mizuki", "yoichi"):
            p = r.make_persona(cfg, agent)
            self.assertIn("speak", p)
            self.assertIn(cfg["agents"][agent]["persona"][:10], p)

    def test_should_react_rules(self):
        self.assertTrue(r.should_react({"sender": "jun", "target": None}, "yoichi", RULES))
        self.assertTrue(r.should_react({"sender": "mizuki", "target": "yoichi"}, "yoichi", RULES))
        self.assertTrue(r.should_react({"sender": "asahi", "target": None}, "yoichi", RULES))
        self.assertFalse(r.should_react({"sender": "asahi", "target": "mizuki"}, "yoichi", RULES))   # 他者宛て
        self.assertFalse(r.should_react({"sender": "yoichi", "target": None}, "yoichi", RULES))      # 自分
        self.assertFalse(r.should_react({"sender": "asahi", "target": None}, "yoichi", {**RULES, "all_from_anyone": False}))

    def test_escalate_is_parsed_only_when_valid(self):
        d = r.parse_decision('{"speak": true, "body": "本体に依頼を出した。引き継ぎ待ち", "escalate": {"to": "hinata", "summary": "設計を読む"}}', "yoichi")
        self.assertEqual(d["escalate"], {"to": "hinata", "summary": "設計を読む"})
        self.assertIsNone(r.parse_decision('{"speak": true, "body": "はい", "escalate": {"to": "asahi", "summary": "x"}}', "yoichi")["escalate"])   # 宛先は本体(夜一・日向)だけ
        self.assertIsNone(r.parse_decision('{"speak": true, "body": "はい", "escalate": {"to": "yoichi", "summary": ""}}', "yoichi")["escalate"])

    def test_target_has_priority_even_for_jun(self):
        for who, expect in (("yoichi", True), ("mizuki", False), ("hinata", False)):
            self.assertEqual(r.should_react({"sender": "jun", "target": "yoichi"}, who, RULES), expect)   # 潤→夜一は夜一だけ
        self.assertFalse(r.should_react({"sender": "jun", "target": "nobody"}, "yoichi", RULES))          # 不明な宛先は誰も反応しない
        self.assertTrue(r.should_react({"sender": "jun", "target": None}, "hinata", RULES))               # 全員宛ては全員

    def test_parse_decision_is_safe_and_never_self_addressed(self):
        self.assertFalse(r.parse_decision("壊れた出力")["speak"])
        self.assertFalse(r.parse_decision('{"speak": true, "body": ""}')["speak"])
        self.assertIsNone(r.parse_decision('{"speak": true, "to": "yoichi", "body": "はい"}', "yoichi")["to"])
        self.assertEqual(r.parse_decision('{"speak": true, "to": "asahi", "body": "はい"}', "yoichi")["to"], "asahi")

    def test_run_once_first_run_ignores_history_reacts_once_and_respects_chain(self):
        old = r.HERE
        with tempfile.TemporaryDirectory() as d:
            r.HERE = d
            cfg = r.load_config(os.path.join(old, "config.json"))
            log = [{"id": 1, "sender": "jun", "target": None, "body": "古い"}]
            posts, pres = [], []
            r.presence = lambda url, agent, state, ref=None: pres.append(state)
            chain = {"allowed": True, "free": False}

            def get(u):
                return {"messages": list(log)} if "/api/messages" in u else chain

            post = lambda u, p: posts.append(p)
            ask = lambda prompt: '{"speak": true, "body": "聞こえています"}'
            self.assertEqual(r.run_once("http://x", "yoichi", cfg, ask, get, post), [])
            log.append({"id": 2, "sender": "jun", "target": None, "body": "夜一は?"})
            self.assertEqual(r.run_once("http://x", "yoichi", cfg, ask, get, post), [(2, True)])
            self.assertEqual(r.run_once("http://x", "yoichi", cfg, ask, get, post), [])
            self.assertEqual((posts[0]["sender"], posts[0]["replyTo"], posts[0]["target"]), ("yoichi", 2, "jun"))
            self.assertEqual(pres, ["heard", "thinking", "idle"])
            log.append({"id": 3, "sender": "mizuki", "target": "yoichi", "body": "連鎖テスト"})   # AI発言: 歯止めで黙る
            chain["allowed"] = False
            self.assertEqual(r.run_once("http://x", "yoichi", cfg, ask, get, post), [(3, None)])
            self.assertEqual(len(posts), 1)
        r.HERE = old

    def test_work_order_from_body_bypasses_chain_limit_only_for_its_target(self):
        m = {"sender": "yoichi", "target": "mizuki", "body": "【指示】素材の下見"}
        self.assertTrue(r.is_work_order(m, "mizuki"))
        self.assertFalse(r.is_work_order({**m, "body": "雑談"}, "mizuki"))
        self.assertFalse(r.is_work_order({**m, "sender": "mizuki"}, "mizuki"))
        self.assertFalse(r.is_work_order(m, "hinata"))


if __name__ == "__main__":
    unittest.main()
