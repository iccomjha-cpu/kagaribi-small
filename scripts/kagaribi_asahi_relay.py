"""篝火 ⇄ 朝日(ChatGPT) 往復の半自動ヘルパー。2026-10-29 夜一。
配送(ChatGPTへの送信)と返答の取得は、夜一(Claude Code)がChromeで行う。この道具は、その前後の確実さを担う:
  pending : 朝日宛ての新着で、まだ朝日の返答がないものを一覧する(元メッセージID付き)
  packet  : 指定IDの配送文(ChatGPTへ貼る文)を作る
  post    : 朝日の返答を sender=asahi・source=bridge で共通ログへ書き戻し、台帳に記録する(同じ元IDへの二重投稿は拒否)
台帳: kagaribi_asahi_ledger.jsonl(元ID・配送時刻・返答本文・書き戻しID)。標準ライブラリのみ。127.0.0.1の篝火サーバーだけに接続する。
使い方: python kagaribi_asahi_relay.py pending|packet <id>|post <id> --body 本文 [--delivered HH:MM:SS]
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

URL = "http://127.0.0.1:8765"
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "kagaribi_asahi_ledger.jsonl")
JST = timezone(timedelta(hours=9))


def _http(path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def addressed_to_asahi(m):
    """朝日宛て: target が asahi、または 潤が全体宛てで『朝日』に触れた発言。朝日自身の発言は除く。"""
    if m.get("sender") == "asahi":
        return False
    if m.get("target") == "asahi":
        return True
    body = m.get("body") or ""   # 全体宛ては『短い発言で朝日に触れたもの』だけ候補にする(長い指示文は朝日への質問ではない)。候補は夜一が最終判断する
    return m.get("target") in (None, "") and m.get("sender") == "jun" and "朝日" in body and len(body) <= 60


def pending(msgs):
    answered = {m.get("replyTo") for m in msgs if m.get("sender") == "asahi" and m.get("replyTo")}
    return [m for m in msgs if addressed_to_asahi(m) and m["id"] not in answered]


def packet(m):
    to = m.get("targetDisplay") or "全員"
    return ("【篝火からの配送・夜一(Claude Code)】篝火の共通ログ #%s(%s→%s)を届けます。本文:「%s」。朝日として、短く(1〜3文)返答してください。先頭に【朝日→篝火】を付けてください。"
            % (m["id"], m.get("senderDisplay") or m.get("sender"), to, (m.get("body") or "").replace("\n", " ")))


def post(msgs, source_id, body, delivered=None, target="jun"):
    if any(m.get("sender") == "asahi" and m.get("replyTo") == source_id for m in msgs):
        raise SystemExit("拒否: #%s には既に朝日の返答があります(二重投稿の防止)" % source_id)
    res = _http("/api/messages", {"sender": "asahi", "target": None if target in ("all", "") else target, "body": body, "replyTo": source_id, "source": "bridge"})["message"]
    rec = {"sourceId": source_id, "deliveredAt": delivered, "replyBody": body, "postedId": res["id"], "postedAt": res["createdAt"]}
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pending", "packet", "post"])
    ap.add_argument("id", nargs="?", type=int)
    ap.add_argument("--body")
    ap.add_argument("--delivered")
    ap.add_argument("--target", default="jun")
    a = ap.parse_args()
    msgs = _http("/api/messages?after=0&limit=500")["messages"]
    if a.cmd == "pending":
        for m in pending(msgs):
            print("#%s %s→%s: %s" % (m["id"], m.get("sender"), m.get("target") or "全体", (m.get("body") or "")[:80].replace("\n", " ")))
    elif a.cmd == "packet":
        print(packet(next(m for m in msgs if m["id"] == a.id)))
    else:
        print(json.dumps(post(msgs, a.id, a.body, a.delivered, a.target), ensure_ascii=False))
