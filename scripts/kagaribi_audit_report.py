"""篝火の機械的な監査の報告(読み取りのみ・標準ライブラリだけ・LLM不要)。2026-11-02 シリウス。
見るもの: ①直近の配送判定ログ(失敗・保留・捨てた) ②窓口の生死 ③名前表と台帳の照合(actor_table_check) ④経路の未確認・古い確認
使い方: python kagaribi_audit_report.py            → 結果を表示
        python kagaribi_audit_report.py --notify   → 異常があるときだけ、潤へ1行で篝火に投稿(api, 連鎖に数えない)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import kagaribi_liveness as live

URL = "http://127.0.0.1:8765"
HERE = Path(__file__).resolve().parent


def get(path: str) -> dict:
    with urllib.request.urlopen(URL + path, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path: str, body: dict) -> None:
    req = urllib.request.Request(URL + path, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 {"Content-Type": "application/json; charset=utf-8"})
    urllib.request.urlopen(req, timeout=8).read()


def ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def has_unplanned(problems: list[str]) -> bool:
    """予定済みの既知の異常(audit_known.json の known に含まれる文言)だけなら False。新しい異常・予定外が混じれば True(潤へ上げる条件)。"""
    try:
        known = json.loads((HERE / "audit_known.json").read_text(encoding="utf-8")).get("known", [])
    except (OSError, ValueError):
        known = []
    return any(not any(k in p for k in known) for p in problems)


def simulate(wait: int) -> int:
    """擬似の異常を1件作り、通知→解消通知を出して結果を保存する。本物の判定ログ・状態ファイルは触らない。"""
    res = {"at": datetime.now(timezone.utc).isoformat(), "steps": [], "pass": False}
    live.popup("篝火 試験", "監査機械の無人テスト中です(本物の異常ではありません)。この窓は2分で自動で閉じます。", False)   # お知らせ窓が画面に出るかの確認も兼ねる
    try:
        post("/api/messages", {"sender": "hinata", "target": "asahi,sirius", "body": "【監査機械・試験】擬似の異常を出しました(無人テスト。本物の異常ではありません)。", "source": "api"})
        res["steps"].append("notify_ok")
        time.sleep(wait)
        post("/api/messages", {"sender": "hinata", "target": "asahi,sirius", "body": "【監査機械・試験】擬似の異常は解消しました。無人テスト合格。", "source": "api"})
        res["steps"].append("resolve_ok")
        res["pass"] = True
    except Exception as e:                                               # noqa: BLE001
        res["error"] = repr(e)
    (HERE / "audit_selftest_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if res["pass"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--stale-minutes", type=int, default=150)
    ap.add_argument("--simulate", action="store_true", help="無人テスト: 擬似の異常で通知→解消通知まで通し、結果を audit_selftest_result.json に残す")
    ap.add_argument("--sim-wait", type=int, default=180, help="擬似異常から解消までの待ち秒")
    args = ap.parse_args()
    if args.simulate:
        return simulate(args.sim_wait)
    now = time.time()
    problems: list[str] = []
    lines: list[str] = []
    if not live.server_ok():                                          # 篝火サーバーが応答しない=篝火の中には知らせられない。外側(受信箱)へ赤
        msg = "篝火サーバー(8765)が応答しません。Kagaribi-Server を確認してください"
        print("→ 異常あり: " + msg)
        live.write_status([], [msg], False)
        if args.notify:
            live.external_alert("red", "【篝火】" + msg, "kagaribi-server-down-" + datetime.now().strftime("%Y%m%d%H"))
        return 1

    rows = [r for r in get("/api/audit?limit=500").get("audit", []) if now - ts(r["at"]) <= args.hours * 3600]
    c = Counter((r["verdict"], r["reason"]) for r in rows)
    lines.append("判定ログ(直近%d時間 %d件): " % (args.hours, len(rows)) + (", ".join("%s/%s×%d" % (v, rs, n) for (v, rs), n in sorted(c.items())) or "なし"))
    # 最後の成功より後の失敗・保留が残っているか(配送ごとに最後の判定を見る)
    last: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x["at"]):
        if r["kind"] in ("deliver", "digest"):
            last[r["ref"]] = r
    try:                                                                  # 対処済み(要点を再送した等)の発言番号。audit_acknowledged.json の refs に入れると異常から外れる
        acked = {str(x) for x in json.loads((HERE / "audit_acknowledged.json").read_text(encoding="utf-8")).get("refs", [])}
    except (OSError, ValueError):
        acked = set()
    stuck = [r for r in last.values() if r["verdict"] in ("held", "failed", "dropped") and now - ts(r["at"]) > 600 and r["ref"] not in acked]
    cand = []
    for r in stuck:                                                    # 再送候補: 届かなかった発言の中身(要点)を残す。再送は人かAIが判断する
        try:
            mid = int(r["ref"])
            m = next((x for x in get("/api/messages?after=%d&limit=1" % (mid - 1)).get("messages", []) if x["id"] == mid), None)
        except (ValueError, OSError):
            m = None
        if m:
            cand.append({"id": m["id"], "from": m["sender"], "to": m.get("target"), "reason": r["reason"], "body": (m.get("body") or "")[:200]})
    (HERE / "audit_resend_candidates.json").write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "candidates": cand}, ensure_ascii=False, indent=1), encoding="utf-8")
    if cand:
        lines.append("再送候補 %d 件 → audit_resend_candidates.json: " % len(cand) + " / ".join("#%s %s→%s" % (c["id"], c["from"], c["to"]) for c in cand[:8]))
    if stuck:
        problems.append("最後の判定が 保留/失敗/捨て のまま10分以上: " + ", ".join("#%s(%s)" % (r["ref"], r["reason"]) for r in stuck[:8]))

    pres = get("/api/presence")["presence"]
    dead = [k for k, v in pres.items() if not v.get("alive")]
    lines.append("窓口の生死: " + ", ".join("%s=%s" % (k, "生" if v.get("alive") else "停止") for k, v in pres.items()))
    if "asahi" in dead:
        problems.append("朝日の窓口が停止中")

    chk = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "actor_table_check.py")], capture_output=True, text=True, encoding="utf-8")
    out = (chk.stdout or "").strip()
    lines.append("名前表・台帳の点検: " + out.replace("\n", " / "))
    if chk.returncode != 0:
        problems.append("名前表または台帳に抜け・ずれ(actor_table_check)")
    if "14日超" in out:
        problems.append("14日を超えた古い経路確認があります(再試験候補)")

    try:                                                                  # 見張りの見張り: 前回の実行が2.5倍の間隔より古ければ、定期実行が止まっている
        prev_at = json.loads((HERE / "audit_report_state.json").read_text(encoding="utf-8")).get("at")
        if prev_at and now - float(prev_at) > args.stale_minutes * 60:
            problems.append("監査の定期実行が%d分以上動いていません" % args.stale_minutes)
    except (OSError, ValueError):
        pass
    comps = live.component_report()                                     # 全員の常駐の生死(プロセス+ノアズの最後の実行結果)
    bad = [c for c in comps if not c["ok"]]
    lines.append("常駐の点検: " + (", ".join("%s=%s" % (c["label"], "生" if c["ok"] else "停止") for c in comps) or "取得できず(判定しない)"))
    if bad:
        problems.append("常駐が止まっている/失敗: " + ", ".join("%s(%s)" % (c["label"], c["detail"]) for c in bad))
    chain = get("/api/chain")
    lines.append("連鎖: %s/%s allowed=%s moderator=%s" % (chain.get("aiRun"), chain.get("limit"), chain.get("allowed"), chain.get("moderator")))
    if not chain.get("allowed"):
        problems.append("連鎖の歯止めで止まっています")

    print("\n".join(lines))
    print("→ " + ("異常あり: " + " / ".join(problems) if problems else "異常なし"))
    live.write_status(comps, problems, True)                             # 潤が見る状態表示(kagaribi_status.html)
    state_file = HERE / "audit_report_state.json"
    if args.notify:
        try:
            st = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        sig = "|".join(problems)
        text = " / ".join(problems)
        if problems and sig != st.get("sig"):                         # 新しい異常 → 全員へ(一次=夜一が見る)。同じ異常は繰り返さない
            post("/api/messages", {"sender": "hinata", "target": None, "body": ("【篝火・要確認】一次: 夜一。" + text)[:300], "source": "api"})
            st.update({"sig": sig, "first": now, "level": 0})
            try:                                                       # 監督役(救援): 決まりで種類を判定し、既存の常駐タスクの再起動だけ自動で行う(失敗しても本処理は続ける)
                import kagaribi_supervisor as sup
                sup.handle(act=True)
            except Exception as e:                                     # noqa: BLE001
                print("監督役でエラー:", repr(e))
            if any(k in text for k in ("常駐が止まっている", "監査の定期実行", "朝日の窓口が停止中")):
                live.external_alert("red", "【篝火】" + text[:200], "kagaribi-audit-%s" % datetime.now().strftime("%Y%m%d%H"))
        elif problems:                                                # 同じ異常が続いている → 未対応のままなら段階的に上へ
            age = now - float(st.get("first") or now)
            lv = int(st.get("level") or 0)
            if lv < 1 and age >= 3600:
                post("/api/messages", {"sender": "hinata", "target": None, "body": ("【篝火・要確認】エスカレーション1:1時間未対応。責任者に追加: シリウス(二次確認)。朝日は要約。全員にも見える形のまま。" + text)[:300], "source": "api"})
                st["level"] = 1
            elif lv < 2 and age >= 3 * 3600 and has_unplanned(problems):
                post("/api/messages", {"sender": "hinata", "target": None, "body": ("【篝火・要確認】エスカレーション2:3時間未対応・予定外あり。責任者に追加: 潤(判断)。全員にも見える形のまま。" + text)[:300], "source": "api"})
                st["level"] = 2
        elif st.get("sig"):
            post("/api/messages", {"sender": "hinata", "target": None, "body": "【篝火・復旧】異常は解消しました。", "source": "api"})
            st = {}
        jst_now = datetime.now(timezone(timedelta(hours=9)))
        today = jst_now.strftime("%Y-%m-%d")
        if not problems and jst_now.hour >= 7 and st.get("okDay") != today:     # 毎日、最初の正常な点検で1回だけ『正常稼働』を受信箱へ(情報)
            live.external_alert("info", "【篝火】正常稼働中(%s 点検。常駐%d件ともOK・配送の滞り無し)。状態表示: preview/kagaribi_status.html" % (jst_now.strftime("%H:%M"), len(comps)), "kagaribi-ok-" + today)
            st["okDay"] = today
        st["at"] = now
        state_file.write_text(json.dumps(st), encoding="utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
