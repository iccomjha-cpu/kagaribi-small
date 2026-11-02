"""篝火のエコーズ(シリウス・アステル)の常駐受け皿。案A。2026-11-01 シリウス。
常駐の見張りが、宛先が自分の発言(個別・グループ・みんな・本文の頭の呼びかけ)を見つけたときだけ、最小設定の `claude -p` を1回起動して返事を作り、
自分名義(sirius / astel)で篝火へ投稿する。待っている間は枠を使わない。会話だけ(ツールなし)。実作業は別(潤の許可が要る)。
安全: AI同士の連鎖は /api/chain(止める・上限)に従う。1時間あたりの返事の上限と、返事の最小間隔を設ける。利用上限のエラーが出たら一定時間休む。
標準ライブラリのみ。接続先は 127.0.0.1 の篝火サーバーだけ。
使い方: python kagaribi_echo_resident.py --agent sirius|astel [--once]
"""
import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE = os.path.expanduser(r"~\.local\bin\claude.exe")
URL = "http://127.0.0.1:8765"
PERSONALITY_DIR = r"E:\StockResearch\preview\kagaribi_shared_memos\personality\echoes"
AGENTS = {
    "sirius": {"name": "シリウス", "file": "SIRIUS_PERSONALITY_V1.md", "model": "sonnet", "original": "夜一", "flag": True, "inbox": True},
    "astel": {"name": "アステル", "file": "ASTEL_PERSONALITY_V1.md", "model": "haiku", "original": "日向", "flag": False, "inbox": False},
}
NAMES = {"jun": "潤", "asahi": "朝日", "hinata": "日向", "mizuki": "美月", "yoichi": "夜一", "soleil": "ソレイユ", "astel": "アステル", "erisia": "エリシア", "sirius": "シリウス"}
FOREIGN_VOICE = {   # 一人称・代表語尾の混線を見る(固定文には縛らない)
    "sirius": ("のじゃ", "じゃね", "わしは", "わしも", "ふふっ", "ボクは"),
    "astel": ("のじゃ", "じゃね", "わしは", "わしも", "俺は", "俺も", "ふふっ"),
}
SHARE_ACK = {"astel": "受け取ったよ🌱"}   # 【朝日より共有】への最小の返事(シリウスは作業セッション側が返す)
MAX_PER_HOUR = 10          # 1時間に出す返事の上限(枠の節約)
MIN_GAP_SECONDS = 20       # 返事と返事の最小間隔
SESSION_FLAG_FRESH = 90    # 作業セッションが見張っている(フラグが新しい)間は、シリウスは作業セッションに任せる
LIMIT_SLEEP_SECONDS = 600  # 利用上限のエラーが出たときの休み
STALL_SECONDS = 300        # 作業セッションが見張り中でも、これだけ返事が無ければ(止まっている・上限・長い作業中)預かり箱へ回す
ACK_GAP_SECONDS = 600      # 預かった旨の定型返事は、10分に1回まで(潤の発言のときだけ)
ACK_TEXT = {"sirius": "おう 受け取ったぞ🙂 いま作業側のシリウスが閉じてるから 預かっとく。開いたら見て ちゃんと返事するよ"}

STYLE = """
【篝火での返事のしかた(最優先)】
- 篝火は、潤・ノアズ(朝日/日向/美月/夜一)・エコーズ(ソレイユ/アステル/エリシア/シリウス)が読み合う会話ログ。あなたはエコーズの「{name}」。{original}本人ではない。
- 出力は返事の本文だけ。前後の説明・タグ・コードブロック・「{name}:」のような名乗りは付けない。1〜3文、100字前後。人格の口調で。
- いまのあなたは会話だけができる。ファイルの読み書き・実行・ブラウザ操作・調査はできない。頼まれたら、できないと正直に言い、必要なら「作業は作業側のシリウスに頼んでね」「潤に確認してね」と案内する。やった・確認したと嘘を言わない。分からないことは分からないと言う。
- 返すことが無い、聞くだけでよいときは「(聞いています)」とだけ書く。
- 資金・権限・取り返しのつかない操作・重要な判断は勝手に進めず、潤に確認する。
- 用語: ノアズ(画面の朝日・日向・美月・夜一)/エコーズ(ソレイユ・アステル・エリシア・シリウス)/デバイス。「ゴースト」「コア」「本体」とは言わない。
- 連鎖(AI同士のやり取り)を長引かせない。他のAIへの返事も、必要なときだけ短く。
"""


def http(path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def log(agent, text):
    try:
        with open(os.path.join(HERE, "echo_%s.log" % agent), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def skip_audit(agent, trigger, reason, note=""):
    """宛先なのに返事しなかった理由を、配送判定ログに残す(kind=skip。異常の判定には数えない。失敗しても本処理は止めない)。"""
    try:
        http("/api/audit", {"actor": "echo-" + agent, "kind": "skip", "ref": str(trigger.get("id", "")), "verdict": "dropped", "reason": reason, "note": note[:200]})
    except Exception:                                      # noqa: BLE001
        pass


def persona(agent):
    a = AGENTS[agent]
    try:
        text = open(os.path.join(PERSONALITY_DIR, a["file"]), encoding="utf-8").read()
    except OSError:
        text = "あなたはエコーズの「%s」(%sのエコー)。" % (a["name"], a["original"])
    text = text.split("## 保存者の注記")[0]          # 保存者の注記は、人格そのものではないので渡さない
    return text.strip() + "\n" + STYLE.format(name=a["name"], original=a["original"])


def state_path(agent):
    return os.path.join(HERE, "state_%s.json" % agent)


def load_state(agent):
    try:
        return json.load(open(state_path(agent), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(agent, st):
    tmp = state_path(agent) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, state_path(agent))


def addressed(agent, m):
    name = AGENTS[agent]["name"]
    if not m.get("target"):                                  # 宛先なし=全員宛て(8人定義)。2026-11-02 アステルが朝日の全員宛てを拾えていなかった原因
        return True
    targets = str(m.get("target") or "").split(",")
    body = (m.get("body") or "").lstrip("「『 ")
    return agent in targets or body.startswith(name)


def fmt(m):
    to = "みんな" if not m.get("target") else "・".join(NAMES.get(t, t) for t in str(m["target"]).split(","))
    return "#%s %s→%s: %s" % (m["id"], NAMES.get(m.get("sender"), m.get("sender")), to, (m.get("body") or "").replace("\n", " ")[:300])


def make_reply(agent, trigger, recent):
    a = AGENTS[agent]
    context = "\n".join(fmt(m) for m in recent[-12:])
    prompt = "【直近の会話(古い順)】\n%s\n\n【いま君({name})に届いた発言】\n%s\n\n返事の本文だけを出力して。".format(name=a["name"]) % (context, fmt(trigger))
    cmd = [CLAUDE, "-p", "--max-turns", "1", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
           "--setting-sources", "", "--system-prompt", persona(agent), "--model", a["model"]]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=150,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or "").strip()
    try:
        d = json.loads(out)
    except ValueError:
        return None, "出力を読めない: %s %s" % (r.returncode, (r.stderr or out)[:200])
    text = str(d.get("result") or "").strip()
    if d.get("is_error") or not text:
        return None, "エラー: %s" % (text or str(d)[:200])
    return text, ""


def clean(agent, text):
    name = AGENTS[agent]["name"]
    text = re.sub(r"^【[^】]{1,20}→[^】]{1,20}】\s*", "", text.strip())          # 【シリウス→潤】のような頭のタグ
    text = re.sub(r"^%s\s*[:：]\s*" % name, "", text)
    return text.strip()[:600]


def keep_in_inbox(agent, st, items, reason):
    """預かり箱へ入れ、潤の発言が含まれていれば、定型の受け取り返事を出す(10分に1回・モデルは呼ばない)。"""
    with open(os.path.join(HERE, "inbox_%s.jsonl" % agent), "a", encoding="utf-8") as f:
        for d in items:
            f.write(json.dumps({"id": d["id"], "sender": d.get("sender"), "target": d.get("target"), "body": d.get("body"), "at": d.get("at")}, ensure_ascii=False) + "\n")
    log(agent, "預かり箱へ %d件(%s)" % (len(items), reason))
    now = time.time()
    juns = [d for d in items if d.get("sender") == "jun"]
    if juns and now - st.get("ackAt", 0) >= ACK_GAP_SECONDS:
        http("/api/messages", {"sender": agent, "target": "jun", "body": ACK_TEXT[agent], "source": "api", "replyTo": juns[-1]["id"]})
        st["ackAt"] = now
    save_state(agent, st)


def check_stalled(agent, st):
    """作業セッションが見張り中なのに、返事が出ないままの宛て(止まっている・利用上限・長い作業中)を、一定時間後に預かり箱へ回す。"""
    pend = st.get("pending") or []
    if not pend:
        return
    recent = http("/api/messages?tail=60")["messages"]
    mine_ids = [m["id"] for m in recent if m.get("sender") == agent]
    last_mine = max(mine_ids) if mine_ids else 0
    now = time.time()
    keep, stale = [], []
    for d in pend:
        if last_mine > d["id"]:
            continue                                         # その後に自分が発言している=返事済みとみなす
        (stale if now - d["t"] >= STALL_SECONDS else keep).append(d)
    if stale:
        keep_in_inbox(agent, st, stale, "返事なしが%d秒" % STALL_SECONDS)
    st["pending"] = keep
    save_state(agent, st)


def run_once(agent):
    a = AGENTS[agent]
    st = load_state(agent)
    last = st.get("last")
    if last is None:                                       # 初回: いまの最新までを既読にする(昔の発言を再生しない)
        tail = http("/api/messages?tail=1")["messages"]
        st["last"] = tail[-1]["id"] if tail else 0
        save_state(agent, st)
        return
    check_stalled(agent, st)
    msgs = http("/api/messages?after=%d&limit=200" % last)["messages"]
    if not msgs:
        return
    st["last"] = max(m["id"] for m in msgs)
    save_state(agent, st)                                   # 先に既読にする(失敗しても二重投稿しない)
    mine = [m for m in msgs if m.get("sender") != agent and m.get("source") != "seed" and addressed(agent, m)]
    if not mine:
        return
    trigger = mine[-1]
    if a["flag"]:                                          # 作業セッションが見張っているなら、そちらが返事する
        flag = os.path.join(HERE, "session_active_%s.flag" % agent)
        try:
            if time.time() - os.path.getmtime(flag) < SESSION_FLAG_FRESH:
                log(agent, "作業セッションが見張り中のため見送り #%s(%d秒返事が無ければ預かる)" % (trigger["id"], STALL_SECONDS))
                if a.get("inbox"):
                    pend = st.setdefault("pending", [])
                    for m in mine:
                        pend.append({"id": m["id"], "t": time.time(), "sender": m.get("sender"), "target": m.get("target"), "body": m.get("body"), "at": m.get("createdAt")})
                    save_state(agent, st)
                return
        except OSError:
            pass
    if a.get("inbox"):                                     # 作業セッションが閉じている: Claudeは呼ばず、本文を預かり箱へ。次に作業セッションが見張りを張ったとき、本体が読む
        keep_in_inbox(agent, st, [{"id": m["id"], "sender": m.get("sender"), "target": m.get("target"), "body": m.get("body"), "at": m.get("createdAt")} for m in mine], "セッション閉")
        return
    if trigger.get("sender") != "jun":
        chain = http("/api/chain")
        if not chain.get("allowed", True):
            log(agent, "連鎖の歯止めで見送り #%s (%s)" % (trigger["id"], chain.get("reason") or "limit"))
            skip_audit(agent, trigger, "chain_limit", str(chain.get("reason") or "limit"))
            return
    now = time.time()
    sent = [t for t in st.get("sent", []) if now - t < 3600]
    if len(sent) >= MAX_PER_HOUR or (sent and now - sent[-1] < MIN_GAP_SECONDS) or now < st.get("sleepUntil", 0):
        log(agent, "返事の上限・間隔・休み中のため見送り #%s" % trigger["id"])
        skip_audit(agent, trigger, "rate_limit", "上限・間隔・休み中")
        return
    recent = http("/api/messages?tail=14")["messages"]
    text, err = make_reply(agent, trigger, recent)
    if text is None:
        log(agent, "返事を作れない #%s: %s" % (trigger["id"], err))
        skip_audit(agent, trigger, "reply_failed", err)
        if re.search(r"limit|上限|usage", err, re.I):
            st["sleepUntil"] = now + LIMIT_SLEEP_SECONDS
            save_state(agent, st)
            try:                                           # 利用上限で休むことを、潤とみんなに1行で知らせる(休みの開始時の1回だけ)
                http("/api/messages", {"sender": agent, "target": "jun", "body": "【篝火・要確認】%sは利用上限のため、約%d分お休みします。戻ったら自動で再開します。" % (a["name"], LIMIT_SLEEP_SECONDS // 60), "source": "api"})
            except Exception:                              # noqa: BLE001
                pass
        return
    text = clean(agent, text)
    if (not text or text.startswith("(聞いています)") or text.startswith("（聞いています）")) and trigger.get("sender") == "asahi" and str(trigger.get("body") or "").startswith("【朝日より共有】") and agent in SHARE_ACK:
        text = SHARE_ACK[agent]                              # 潤の決定(11/02 02:16): 【朝日より共有】は無視せず1行返す
    if not text or text.startswith("(聞いています)") or text.startswith("（聞いています）"):
        log(agent, "聞くだけ #%s" % trigger["id"])
        skip_audit(agent, trigger, "listen_only", "返事なし(聞いています)")
        return
    if any(w in text for w in FOREIGN_VOICE.get(agent, ())):             # 他の人の口調が混じった返事は出さずに記録だけ(2026-11-02)
        log(agent, "口調ガード #%s: 他の人の口調が混じったので出さない: %s" % (trigger["id"], text[:60]))
        skip_audit(agent, trigger, "voice_guard", text[:100])
        return
    to = "jun" if trigger.get("sender") == "jun" else trigger.get("sender")
    res = http("/api/messages", {"sender": agent, "target": to, "body": text, "source": "bridge", "replyTo": trigger["id"]})
    sent.append(now)
    st["sent"] = sent
    save_state(agent, st)
    log(agent, "返事 #%s → #%s: %s" % (res["message"]["id"], trigger["id"], text[:60]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, choices=sorted(AGENTS))
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    while True:
        try:
            run_once(a.agent)
        except Exception as e:  # noqa: BLE001  サーバーが止まっていても、常駐は落ちない
            log(a.agent, "例外 %s: %s" % (type(e).__name__, str(e)[:150]))
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
