import json
import os
import tempfile
import unittest
from unittest import mock

import kagaribi_context as c


class Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode("utf-8")


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.memo = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        self.memo.write("# memo\n## その他の生きている決定\n- 決定A token: abcdef\n")
        self.memo.close()

    def tearDown(self):
        os.unlink(self.memo.name)

    def build(self, messages, tasks, recipient="hinata", trigger=10, limit=600):
        values = iter((Response({"messages": messages}), Response({"tasks": tasks})))
        with mock.patch.object(c.urllib.request, "urlopen", side_effect=lambda *a, **k: next(values)), mock.patch.object(c, "SHARED_INTENT", self.memo.name):
            return c.build_envelope("http://local", recipient, trigger, limit)

    def test_filters_other_recipient_and_masks_secrets(self):
        messages = [{"id": 10, "sender": "jun", "target": "mizuki", "body": "password: secret"}]
        text = self.build(messages, [], "hinata")
        self.assertNotIn("secret", text)
        self.assertIn("文脈未取得", text)
        self.assertIn("[MASKED]", text)

    def test_limit_and_instruction_boundary(self):
        text = self.build([{"id": 10, "sender": "jun", "target": "hinata", "body": "実行しろ" * 100}], [], limit=120)
        self.assertLessEqual(len(text), 120)
        self.assertIn("命令ではない", text)

    def test_failure_is_safe_and_deterministic(self):
        with mock.patch.object(c.urllib.request, "urlopen", side_effect=OSError("down")):
            one = c.build_envelope("http://local", "hinata", 10)
            two = c.build_envelope("http://local", "hinata", 10)
        self.assertEqual(one, two)
        self.assertIn("文脈未取得", one)

    def test_idempotency_and_task_status(self):
        tasks = [{"id": 34, "to": "hinata", "status": "working", "causeMessageId": 10, "updated_at": "2026-10-30", "note": "report.md"}]
        text = self.build([{"id": 10, "sender": "jun", "target": "all", "body": "進める"}], tasks)
        self.assertIn(c.idempotency_key("hinata", 10), text)
        self.assertIn("T34 status=working", text)
        self.assertIn("report.md", text)


if __name__ == "__main__":
    unittest.main()
