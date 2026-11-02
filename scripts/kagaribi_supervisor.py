"""篝火の監督役(救援)。2026-11-02 シリウス(潤の承認 #3225。第三の系統=ローカルAI Qwen3-8B)。
役割分担(比較実験 supervisor_eval/RESULT_20261102.md の結果):
  - 異常の「種類」と「復旧の動作」は、プログラムの決まり(表)で決める。AIの答えでは動かさない。
  - ローカルAI(qwen3:8b)は、状況の言い換え(平易な要約)と、プログラムの判定との食い違いの指摘だけ。食い違えば人(夜一→シリウス→潤)へ上げる。
  - 自動で動くのは「既存の常駐タスクの再起動」だけ。コード変更・タスクの新規作成・設定変更・売買/資金/権限には触れない。
  - 同じ復旧は15分に1回・1日6回まで。毎回 supervisor_log.jsonl に誰が・何を・結果を残す(G23)。
使い方: python kagaribi_supervisor.py            → 点検して、必要なら復旧
        python kagaribi_supervisor.py --no-act   → 点検だけ(復旧も投稿もしない)
        python kagaribi_supervisor.py --facts-file F.json --no-act   → 擬似の状況で判定の試験
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kagaribi_liveness as live                                      # noqa: E402

JST = timezone(timedelta(hours=9))
URL = "http://127.0.0.1:8765"
OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:8b"
STATE = HERE / "supervisor_state.json"
LOG = HERE / "supervisor_log.jsonl"
COOLDOWN_SECONDS = 900
MAX_RESTARTS_PER_DAY = 6
# 常駐 → 再起動してよい既存タスク(これ以外は絶対に再起動しない)
TASKS = {"server": "Kagaribi-Server", "echo-sirius": "Kagaribi-Echo-sirius", "echo-astel": "Kagaribi-Echo-astel",
         "asahi-watch": "Kagaribi-AsahiWatch", "mizuki-bridge": "Kagaribi-MizukiBridge", "mobile": "Kagaribi-Mobile"}


def get(path: str):
    with urllib.request.urlopen(URL + path, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def gather() -> dict:
    """状況(事実)を集める。読むだけ。"""
    facts = {"server_ok": live.server_ok(), "components": [], "presence": {}, "audit_1h": {}, "at": datetime.now(JST).isoformat(timespec="seconds")}
    facts["components"] = live.component_report()
    if facts["server_ok"]:
        try:
            facts["presence"] = {k: bool(v.get("alive")) for k, v in get("/api/presence")["presence"].items()}
            now = time.time()
            rows = [r for r in get("/api/audit?limit=500").get("audit", []) if now - datetime.fromisoformat(r["at"].replace("Z", "+00:00")).timestamp() <= 3600]
            facts["audit_1h"] = {"sent_ok": sum(1 for r in rows if r["reason"] == "sent_ok"), "send_failed": sum(1 for r in rows if r["reason"] == "send_failed")}
        except Exception:                                              # noqa: BLE001
            pass
    return facts


def classify(facts: dict) -> list[dict]:
    """決まりで種類と動作を決める。戻り値: [{category, key, action, task, detail}]。空=異常なし。"""
    out = []
    if not facts.get("server_ok"):
        out.append({"category": "server_down", "key": "server", "action": "restart", "task": TASKS["server"], "detail": "篝火サーバー(8765)が応答しない"})
        return out                                                      # サーバーが落ちている間は他の判定ができない
    for c in facts.get("components", []):
        if not c.get("ok") and c["id"] in TASKS:
            out.append({"category": "component_stopped", "key": c["id"], "action": "restart", "task": TASKS[c["id"]], "detail": "%s: %s" % (c["label"], c["detail"])})
        elif not c.get("ok"):                                           # ノアズ常駐(最後の実行が失敗): 再起動の対象外=人へ
            out.append({"category": "noir_failed", "key": c["id"], "action": "notify", "task": "", "detail": "%s: %s" % (c["label"], c["detail"])})
    pres = facts.get("presence", {})
    if pres.get("asahi") is False:
        out.append({"category": "asahi_window_stopped", "key": "asahi", "action": "notify", "task": "", "detail": "朝日の窓口が止まっている(ChatGPTのタブを開き直す人の操作が必要)"})
    a = facts.get("audit_1h", {})
    if a.get("send_failed", 0) >= 5 and a.get("sent_ok", 0) == 0:
        out.append({"category": "input_box_missing", "key": "send_failed", "action": "notify", "task": "", "detail": "直近1時間に送信失敗%d件・成功0件(拡張の修正が必要。再起動では直らない)" % a["send_failed"]})
    return out


def ask_ai(facts: dict, findings: list[dict]) -> str:
    """ローカルAIに、平易な要約と、判定との食い違いの指摘だけを頼む。動かす権限は与えない。落ちていても本処理は続ける。"""
    prompt = ("あなたは篝火(AIチームの会話システム)の見張り役です。次の【状況】と【機械の判定】を読み、1)状況を平易な日本語2文で要約し、"
              "2)機械の判定に疑問があれば『食い違い:』で始めて1文で書いてください。なければ『食い違い:なし』。復旧の指示は書かない。\n\n【状況】\n%s\n\n【機械の判定】\n%s"
              % (json.dumps(facts, ensure_ascii=False)[:1500], json.dumps(findings, ensure_ascii=False)[:800]))
    body = {"model": MODEL, "prompt": prompt, "stream": False, "think": False, "keep_alive": 0, "options": {"temperature": 0}}
    try:
        r = urllib.request.urlopen(urllib.request.Request(OLLAMA + "/api/generate", json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}), timeout=180)
        return str(json.loads(r.read().decode("utf-8")).get("response", "")).strip()[:400]
    except Exception as e:                                             # noqa: BLE001
        return "(ローカルAIは使えませんでした: %s。機械の判定だけで進めます)" % type(e).__name__


def restart(task: str) -> bool:
    if task not in TASKS.values():                                      # 許可リスト以外は絶対に動かさない
        return False
    ps = "Stop-ScheduledTask '%s' -ErrorAction SilentlyContinue; Start-Sleep 2; Start-ScheduledTask '%s'" % (task, task)
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60).returncode == 0
    except Exception:                                                   # noqa: BLE001
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def handle(act: bool = True, facts: dict | None = None) -> list[dict]:
    facts = facts or gather()
    findings = classify(facts)
    if not findings:
        return []
    st = load_state()
    now = time.time()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    if st.get("day") != today:
        st["day"], st["restarts"] = today, 0
    summary = ask_ai(facts, findings) if findings else ""
    results = []
    for f in findings:
        last = float(st.get("last", {}).get(f["key"], 0))
        res = {"at": datetime.now(JST).isoformat(timespec="seconds"), "who": "監督役(プログラム+%s)" % MODEL, "category": f["category"], "detail": f["detail"], "action": f["action"], "task": f["task"], "result": "", "ai": summary}
        if not act:
            res["result"] = "試験(--no-act)・動作なし"
        elif now - last < COOLDOWN_SECONDS:
            res["result"] = "見送り(同じ件は15分以内に対応済み)"
        elif f["action"] == "restart" and st.get("restarts", 0) >= MAX_RESTARTS_PER_DAY:
            res["result"] = "見送り(1日の再起動の上限)。人へ上げます"
        elif f["action"] == "restart":
            ok = restart(f["task"])
            st["restarts"] = st.get("restarts", 0) + 1
            st.setdefault("last", {})[f["key"]] = now
            res["result"] = "再起動を実行" if ok else "再起動に失敗(人へ)"
        else:
            st.setdefault("last", {})[f["key"]] = now
            res["result"] = "人へ通知(自動では直せない種類)"
        results.append(res)
        with LOG.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps(res, ensure_ascii=False) + "\n")
    if act:
        STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        notify(results, summary)
    return results


def notify(results: list[dict], summary: str) -> None:
    """人へ1行。サーバーが落ちている間は投稿できないので外側の受信箱(赤)へ。"""
    acted = [r for r in results if not r["result"].startswith("見送り(同じ")]
    if not acted:
        return
    text = "【篝火・要確認】監督役: " + " / ".join("%s→%s" % (r["detail"], r["result"]) for r in acted)
    if summary and "食い違い:なし" not in summary and "使えませんでした" not in summary:
        text += "。AIの見立て: " + summary.replace("\n", " ")[:160]
    try:
        body = json.dumps({"sender": "hinata", "target": None, "body": text[:300], "source": "api"}, ensure_ascii=False).encode("utf-8")
        urllib.request.urlopen(urllib.request.Request(URL + "/api/messages", body, {"Content-Type": "application/json; charset=utf-8"}), timeout=8).read()
    except Exception:                                                   # noqa: BLE001
        live.external_alert("red", "【篝火】" + text[:200], "kagaribi-supervisor-%s" % datetime.now(JST).strftime("%Y%m%d%H%M"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-act", action="store_true")
    ap.add_argument("--facts-file")
    args = ap.parse_args()
    facts = json.loads(Path(args.facts_file).read_text(encoding="utf-8")) if args.facts_file else None
    res = handle(act=not args.no_act, facts=facts)
    print(json.dumps(res, ensure_ascii=False, indent=1) if res else "異常なし")
    return 1 if res else 0


if __name__ == "__main__":
    sys.exit(main())
