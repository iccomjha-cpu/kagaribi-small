"""篝火へ1件投稿する小さな道具(シリウス用)。本文は標準入力(UTF-8)から読む。
使い方: python kagaribi_say.py --sender sirius --target jun [--reply-to 123] < body.txt
"""
import argparse, json, sys, urllib.request
ap = argparse.ArgumentParser()
ap.add_argument("--sender", default="sirius")
ap.add_argument("--target", default="")
ap.add_argument("--reply-to", type=int, default=None)
ap.add_argument("--url", default="http://127.0.0.1:8765")
a = ap.parse_args()
body = sys.stdin.buffer.read().decode("utf-8").strip()
if not body:
    sys.exit("本文が空です")
payload = {"sender": a.sender, "target": a.target or None, "body": body, "source": "api", "replyTo": a.reply_to}
req = urllib.request.Request(a.url + "/api/messages", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        m = json.loads(r.read().decode("utf-8"))["message"]
        print("投稿しました #%s" % m["id"])
except urllib.error.HTTPError as e:
    sys.exit("失敗 %s: %s" % (e.code, e.read().decode("utf-8", "replace")))
