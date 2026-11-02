"""篝火の「本体への依頼キュー」の操作(本体の夜一・日向が使う)。2026-10-30 夜一。
軽い応答役(美月・夜一・日向の常駐)が、実作業を頼まれたときに登録した依頼を、一覧・受付・作業中・検証済・却下にする。
受付が確認されるまでは「引き継ぎ待ち」と表示される(引き継ぎ済みとは言わない)。
使い方: python kagaribi_tasks.py list [--to yoichi|hinata] [--open] / accept|work|verify|reject <ID> [--note 文]
"""
import argparse
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8765"
NEXT = {"accept": "accepted", "work": "working", "verify": "verified", "reject": "rejected"}


def call(path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list"] + list(NEXT))
    ap.add_argument("id", nargs="?", type=int)
    ap.add_argument("--to")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--note")
    a = ap.parse_args()
    if a.cmd == "list":
        q = "?" + "&".join(x for x in (("to=" + a.to) if a.to else "", "open=1" if a.open else "") if x)
        for t in call("/api/tasks" + q)["tasks"]:
            print("T%d [%s] %s→%s (発言#%s) %s%s" % (t["id"], t["statusLabel"], t["from"], t["to"], t["causeMessageId"], t["summary"], (" / メモ: " + t["note"]) if t["note"] else ""))
    else:
        t = call("/api/tasks/update", {"id": a.id, "status": NEXT[a.cmd], "note": a.note})["task"]
        print("T%d → %s" % (t["id"], t["statusLabel"]))
