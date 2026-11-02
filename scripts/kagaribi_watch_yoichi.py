"""篝火の共通ログを見張り、夜一(Claude Code=この作業セッション)宛て・全体宛ての新着を1行ずつ標準出力へ出す。2026-10-29 作成(夜一)。
読むだけ(書き込みはしない)。日向のbridge.pyと同じAPI(GET /api/messages?after=<ID>)を使う標準ライブラリのみ。
出力1行=新着1件。自分(yoichi)の投稿と、他者宛ては出さない。接続が切れたら1回だけ知らせ、復旧したら1回だけ知らせる。
使い方: python kagaribi_watch_yoichi.py [--agent yoichi] [--url http://127.0.0.1:8765] [--interval 3]
"""
import argparse
import json
import os
import sys
import time
import urllib.request

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kagaribi_watch_yoichi.state.json")


def load_last():
    try:
        with open(STATE, encoding="utf-8") as f:
            return int(json.load(f)["last"])
    except (OSError, ValueError, KeyError):
        return None


def save_last(n):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last": n}, f)
    os.replace(tmp, STATE)


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="yoichi")
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--relay", default="asahi", help="夜一が配送を担う相手(宛先がこの相手の発言も出す)。無効にするには none")
    ap.add_argument("--direct-only", action="store_true", help="依頼の通知と、夜一宛て・潤の「本体」呼びかけだけ出す(全体宛ての雑談は出さない)")
    ap.add_argument("--only-addressed", action="store_true", help="自分の名前が宛先にある発言(と、宛先なしの潤の発言)だけ出す。シリウス用(雑談や他者宛てで起こさない)")
    ap.add_argument("--call-name", default="", help="宛先が別でも、本文の頭がこの名前(例: シリウス)で始まる発言も出す(朝日が夜一宛てで「シリウス、…」と書く場合の取りこぼし防止)")
    ap.add_argument("--inbox", default="", help="常駐の受け皿が預かった発言のファイル(jsonl)。見張りの開始時に1件ずつ表示して、読み終えた印のファイルへ移す")
    ap.add_argument("--touch-flag", default="", help="毎回の見張りのたびにこのファイルの更新時刻を更新する(常駐の受け皿が『作業セッションが見張り中』と判断するための合図)")
    a = ap.parse_args()
    global STATE
    if a.agent != "yoichi":                       # エージェントごとに既読IDを分ける(シリウスが夜一の既読を上書きしない)
        STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kagaribi_watch_%s.state.json" % a.agent)
    try:
        sys.stdout.reconfigure(encoding="utf-8")    # 監視の標準出力がcp932でも、日本語の出力で落ちない
    except Exception:  # noqa: BLE001
        pass
    if not a.inbox and a.agent == "yoichi":                  # 夜一は指定が無くても、ノアズの常駐が預かった箱を見る
        a.inbox = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kagaribi_resident", "inbox_yoichi.jsonl")
    if a.inbox and os.path.exists(a.inbox):
        try:
            lines = [json.loads(x) for x in open(a.inbox, encoding="utf-8").read().splitlines() if x.strip()]
            for d in lines:
                print("[篝火 預かり #%s] %s→%s: %s" % (d.get("id"), d.get("sender"), d.get("target") or "全体", str(d.get("body") or "").replace("\n", " ")[:400]), flush=True)
            os.replace(a.inbox, a.inbox + "." + time.strftime("%Y%m%d%H%M%S") + ".done")
        except Exception as e:  # noqa: BLE001
            print("[篝火] 預かり箱を読めません(%s)" % type(e).__name__, flush=True)
    last, down = load_last(), False
    if last is None:                       # 初回(状態なし): いまの最新の番号を取る(after=0 の先頭200件で決めると、古い発言を再生してしまう)
        try:
            tail = get("%s/api/messages?tail=1" % a.url)["messages"]
            last = tail[-1]["id"] if tail else 0
            save_last(last)
        except Exception:  # noqa: BLE001
            pass      # 停止中の未読を再生するため、既読IDを永続化(状態が無い初回だけ、いまの最後までを既読にする)
    first = last is None
    announced = set()
    while True:
        try:
            try:                                    # 本体の夜一への依頼(受付前)が新しく登録されたら知らせる
                for t in get("%s/api/tasks?to=%s&status=pending_ack" % (a.url, a.agent))["tasks"]:
                    if t["id"] not in announced:
                        announced.add(t["id"])
                        print("[篝火 依頼 T%d] %s→%s(発言#%s): %s" % (t["id"], t["from"], t["to"], t["causeMessageId"], t["summary"][:300]), flush=True)
            except Exception:  # noqa: BLE001  依頼の確認に失敗しても、発言の見張りは止めない
                pass
            msgs = get("%s/api/messages?after=%d&limit=200" % (a.url, last or 0))["messages"]
            if down:
                print("[篝火] 接続が復旧しました", flush=True)
                down = False
            if first:                              # 初回(状態なし)は、いまの最後のIDまでを既読にする
                last = max([m["id"] for m in msgs], default=0)
                save_last(last)
                first = False
            else:
                for m in msgs:
                    last = max(last, m["id"])
                    save_last(last)
                    if m.get("sender") == a.agent:
                        continue
                    called = bool(a.call_name) and (m.get("body") or "").lstrip("「『 ").startswith(a.call_name)
                    if m.get("target") not in (None, "") and a.agent not in str(m.get("target")).split(",") and not called:   # 複数宛先(カンマ区切り)も自分宛てとして扱う
                        continue
                    if a.only_addressed and not called and a.agent not in str(m.get("target") or "").split(",") and not (not m.get("target")):   # 宛先なし=全員宛て(潤・朝日のみんな宛て・監査機械の通知)も自分宛て。2026-11-02
                        continue
                    if a.direct_only and a.agent not in str(m.get("target") or "").split(",") and m.get("sender") != "jun":
                        continue            # 依頼(上の通知)と、本体宛て(宛先=夜一)、潤本人の発言(宛先を問わない。許可の「OK」なども見えるように)だけ起こす
                    body = (m.get("body") or "").replace("\n", " ")
                    print("[篝火 #%d] %s→%s: %s" % (m["id"], m.get("senderDisplay") or m.get("sender"), m.get("targetDisplay") or "全体", body[:400]), flush=True)
        except Exception as e:  # noqa: BLE001
            if not down:
                print("[篝火] 接続できません(%s)。復旧まで待ちます" % type(e).__name__, flush=True)
                down = True
        if a.touch_flag:
            try:
                with open(a.touch_flag, "a"):
                    os.utime(a.touch_flag, None)
            except OSError:
                pass
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
