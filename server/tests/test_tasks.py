import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import create_task, initialize_database, list_tasks, update_task


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "t.sqlite3"
        initialize_database(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_same_cause_and_target_registers_once(self):
        t1, new1 = create_task(self.db, {"from": "hinata", "to": "yoichi", "summary": "設計の読み込み", "cause": 350})
        t2, new2 = create_task(self.db, {"from": "hinata", "to": "yoichi", "summary": "設計の読み込み(再送)", "cause": 350})
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(t1["id"], t2["id"])                                  # 冪等: 2回目は既存を返す
        self.assertEqual(len(list_tasks(self.db)), 1)
        _, new3 = create_task(self.db, {"from": "hinata", "to": "hinata", "summary": "別の宛先は別件", "cause": 350})
        self.assertTrue(new3)

    def test_status_flow_and_labels(self):
        t, _ = create_task(self.db, {"from": "mizuki", "to": "yoichi", "summary": "画面の確認", "cause": 10})
        self.assertEqual(t["status"], "pending_ack")
        self.assertIn("引き継ぎ待ち", t["statusLabel"])                      # 受付前は「引き継ぎ済み」と表示しない
        for status in ("accepted", "working", "verified"):
            self.assertEqual(update_task(self.db, t["id"], status, "メモ")["status"], status)
        self.assertEqual(len(list_tasks(self.db, open_only=True)), 0)         # 検証済みは、未完了の一覧に出ない

    def test_validation(self):
        with self.assertRaises(ValueError):
            create_task(self.db, {"from": "mizuki", "to": "asahi", "summary": "x"})       # 本体の宛先は夜一・日向だけ
        with self.assertRaises(ValueError):
            create_task(self.db, {"from": "mizuki", "to": "yoichi", "summary": ""})
        with self.assertRaises(ValueError):
            update_task(self.db, 999, "working")
        t, _ = create_task(self.db, {"from": "mizuki", "to": "yoichi", "summary": "a"})
        with self.assertRaises(ValueError):
            update_task(self.db, t["id"], "done")

    def test_approval_gate_and_transitions(self):
        from server import approve_task
        t, _ = create_task(self.db, {"from": "mizuki", "to": "hinata", "summary": "確認", "cause": 77})
        with self.assertRaises(ValueError):
            update_task(self.db, t["id"], "approved")                         # 許可は潤の入口(approve_task)からだけ
        with self.assertRaises(ValueError):
            update_task(self.db, t["id"], "verified")                         # 受付前から検証済へは飛べない
        update_task(self.db, t["id"], "accepted", "受付")
        self.assertEqual(approve_task(self.db, t["id"])["status"], "approved")
        self.assertEqual(update_task(self.db, t["id"], "working")["status"], "working")
        self.assertEqual(update_task(self.db, t["id"], "approved", "未完")["status"], "approved")   # 失敗時は許可済に戻せる(再許可で再実行)
        update_task(self.db, t["id"], "working")
        update_task(self.db, t["id"], "verified", "ok")
        with self.assertRaises(ValueError):
            update_task(self.db, t["id"], "working")                          # 検証済からは戻せない
        with self.assertRaises(ValueError):
            approve_task(self.db, t["id"])                                    # 完了済みを許可で巻き戻さない
        t2, _ = create_task(self.db, {"from": "mizuki", "to": "hinata", "summary": "二重許可", "cause": 78})
        update_task(self.db, t2["id"], "accepted")
        approve_task(self.db, t2["id"])
        update_task(self.db, t2["id"], "working")
        with self.assertRaises(ValueError):
            approve_task(self.db, t2["id"])                                   # 作業中に重ねて許可しても巻き戻らない

    def test_fuzzy_approval(self):
        from datetime import datetime, timezone
        from server import approval_targets
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        acc = lambda i: {"id": i, "status": "accepted", "updatedAt": now}
        say = lambda body, **k: approval_targets({"body": body}, k.pop("tasks", [acc(4)]), **k)
        self.assertEqual(say("T4 許可"), [4])
        self.assertEqual(say("T4 やっといて"), [4])
        self.assertEqual(say("T3 許可。あと T12を承認して", tasks=[acc(3), acc(12)]), [3, 12])
        self.assertEqual(say("T4の件どうなった?"), [])                                  # 許可の言葉が無い
        self.assertEqual(say("T4 まだやらないで"), [])                                   # 否定語
        self.assertEqual(say("オッケー"), [4])                                          # 受付が1件・短い一言
        self.assertEqual(say("オッケー", tasks=[acc(4), acc(5)]), [])                    # 2件あるなら曖昧なので許可しない
        self.assertEqual(say("オッケー", reply_body="T5 受付した。理解: ...", tasks=[acc(4), acc(5)]), [5])   # 受付への返信なら特定できる
        self.assertEqual(say("それでいい。ただ、別件でオッケーも出したい気持ちはある、という長い話をしています"), [])   # 長い一言は許可と見なさない
        self.assertEqual(approval_targets({"body": "オッケー"}, [{"id": 4, "status": "accepted", "updatedAt": "2020-01-01T00:00:00Z"}]), [])   # 60分を過ぎた受付
        self.assertEqual(approval_targets({"body": "オッケー"}, [{"id": 4, "status": "working", "updatedAt": now}]), [])   # 作業中の依頼は対象外


if __name__ == "__main__":
    unittest.main()
