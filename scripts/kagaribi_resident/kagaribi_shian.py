"""思案依頼(第1弾・美月だけ): 潤の『区切り』の合図で、ここまでの会話を区切りメモにまとめ、美月のコアに読ませて、考えを会話ログへ返す。

許可の区分ではなく、独立した軽い経路(潤の指示 11/01): 道具なし・ファイルを触らない・考えるだけ。依頼(T番号)も許可待ちも作らない。
記録は shian_log.jsonl と美月の共有メモに残す。1回限りで終わる(ループは第2弾)。
使い方: python kagaribi_shian.py --cause <潤の発言ID> [--url http://127.0.0.1:8765]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import kagaribi_resident as res
import kagaribi_taskworker as tw

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "shian_state.json")
LEDGER = os.path.join(HERE, "shian_log.jsonl")
LOCK = os.path.join(HERE, "lock_shian")
MIN_MESSAGES = 2                 # 区間がこれより短いときは何もしない
MAX_MESSAGES = 40                # 1回に読む会話の上限
NAMES = {"jun": "潤", "asahi": "朝日", "hinata": "日向", "mizuki": "美月", "yoichi": "夜一", "sirius": "シリウス", "soleil": "ソレイユ", "astel": "アステル", "erisia": "エリシア"}


def jst():
    return datetime.now(timezone(timedelta(hours=9)))


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def ledger(entry):
    entry["at"] = jst().strftime("%m-%d %H:%M:%S")
    try:
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def fetch_segment(url, since_id, upto_id):
    data = res._http(url + "/api/messages?tail=120")
    msgs = data["messages"] if isinstance(data, dict) else data
    seg = [m for m in msgs if since_id < m["id"] <= upto_id]
    return seg[-MAX_MESSAGES:]


def transcript(seg):
    return "\n".join("#%s %s: %s" % (m["id"], NAMES.get(m["sender"], m["sender"]), " ".join(str(m["body"]).split())[:300]) for m in seg)


def make_memo(seg):
    """Haikuで区切りメモにする。失敗したときは None(何も渡さない)。"""
    prompt = ("次は篝火の会話ログの一区間です。区切りメモを作ってください。形式: 『決まったこと』『まだ残っている疑問』『潤の意向』の3項目、全体で300字以内、事実だけ。"
              "ログにないことは足さない。\n\n" + transcript(seg))
    try:
        out = (res.ask_claude(prompt, "あなたは会話ログを正確に要約する書記。日本語で簡潔に。", timeout=90, model="haiku") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return out or None


def mizuki_think(memo):
    """美月のコア(道具なし)に、区切りメモを読ませて考えを出させる。"""
    persona = tw.mizuki_worker_persona() + "\n\n【美月の共有メモ(3人の美月の共通の記憶)】\n" + res.mizuki_memory()
    prompt = ("潤の合図で、ここまでの会話の区切りメモが届きました。読んで、あなた自身の考えを2〜3文で出してください。"
              "内容: ①メモの理解(合っているか) ②足りない視点や気づき ③次に会話で取り上げるとよいこと。ファイルや道具は使えません。決めつけず、メモにないことは『未確認』と言う。\n\n【区切りメモ】\n" + memo)
    try:
        return (res.ask_claude(prompt, persona, timeout=150, model="sonnet") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def remember(memo, thought):
    """美月の共有メモに、区切りメモと考えを書き足す(会話の美月が次に読んで反映する)。"""
    try:
        with open(res.MIZUKI_MEMORY, "a", encoding="utf-8") as f:
            f.write("\n- %s (思案依頼) 区切りメモ: %s\n  美月の考え: %s\n" % (jst().strftime("%m-%d %H:%M"), " ".join(memo.split())[:400], " ".join(thought.split())[:400]))
    except OSError:
        pass


def run(url, cause):
    os.makedirs(HERE, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - os.path.getmtime(LOCK) < 600:
            print("思案中のため、今回は見送る")
            return 0
        os.remove(LOCK)
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        state = load_state()
        since = int(state.get("lastId", 0))
        seg = fetch_segment(url, since, cause)
        if len(seg) < MIN_MESSAGES:
            ledger({"cause": cause, "result": "skip", "why": "短い区間(%d件)" % len(seg)})
            return 0
        memo = make_memo(seg)
        if not memo:
            ledger({"cause": cause, "result": "fail", "why": "区切りメモを作れなかった"})
            return 1
        thought = mizuki_think(memo)
        if not thought:
            ledger({"cause": cause, "result": "fail", "why": "美月が考えを出せなかった", "memo": memo})
            return 1
        remember(memo, thought)
        tw.say(url, "mizuki", "区切りメモを読んだ。%s" % thought, cause, "asahi,jun")
        state["lastId"] = seg[-1]["id"]
        save_state(state)
        ledger({"cause": cause, "result": "ok", "range": [seg[0]["id"], seg[-1]["id"]], "memo": memo, "thought": thought})
        return 0
    finally:
        os.close(fd)
        try:
            os.remove(LOCK)
        except OSError:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cause", type=int, required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    a = ap.parse_args()
    sys.exit(run(a.url, a.cause))
