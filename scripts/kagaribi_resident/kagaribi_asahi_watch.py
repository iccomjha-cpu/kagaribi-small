"""朝日(ChatGPT)の無音を軽量監視する。標準ライブラリのみ。"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_STATE = Path(__file__).with_name("state_asahi_watch.json")
DEAD_SECONDS = 420   # 窓口の合図(presence の alive)がこれだけ途切れたら知らせる(拡張の入れ替え後の自己回復は3〜5分かかるので、それより長く)


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def last_asahi_activity(messages: list[dict]) -> float | None:
    values = [parse_time(m.get("createdAt") or m.get("created_at"))
              for m in messages if m.get("sender") == "asahi"]
    valid = [v for v in values if v is not None]
    return max(valid) if valid else None


def pending_for_asahi(messages: list[dict]) -> list[dict]:
    """朝日宛てで、朝日から replyTo がまだ無いメッセージ。"""
    answered = {m.get("replyTo") for m in messages
                if m.get("sender") == "asahi" and m.get("replyTo") is not None}
    return [m for m in messages
            if m.get("sender") != "asahi"
            and "asahi" in str(m.get("target") or "").split(",")
            and m.get("id") not in answered]


def decide(messages: list[dict], state: dict, now: float, quiet_seconds: int,
           asahi_alive: bool | None = None, delivered_ids: set | None = None) -> tuple[str | None, dict]:
    last = last_asahi_activity(messages)
    # 誤警報の対策(潤の了承 11/01): 「(聞いています)」の無言の返事は投稿されず replyTo が付かない。
    # 朝日の最後の発言より後に来た、6時間以内の宛てだけを未処理とみなし、窓口が生きている(alive)間は1時間たつまで警報にしない。
    pending = [m for m in pending_for_asahi(messages)
               if (parse_time(m.get("createdAt") or m.get("created_at")) or 0) > (last or 0)
               and now - (parse_time(m.get("createdAt") or m.get("created_at")) or now) <= 6 * 3600
               and not (asahi_alive and str(m.get("id")) in (delivered_ids or set()))]   # 配送成功(sent_ok)済み+窓口が生きている=読んで「(聞いています)」と黙った可能性が高い。誤警報にしない(2026-11-02 #3020)
    # 活動記録が無い場合は最古の未処理が無音の起点。時刻不明なら安全側で通知しない。
    pending_times = [parse_time(m.get("createdAt") or m.get("created_at")) for m in pending]
    pending_times = [v for v in pending_times if v is not None]
    anchor = last if last is not None else (min(pending_times) if pending_times else None)
    if asahi_alive and pending_times and anchor is not None:
        anchor = max(anchor, min(pending_times))      # 窓口が生きている間は、最古の未処理が来てからの経過で数える(発言直後の誤報対策。2026-11-02 06:00 #3028)
    stalled = bool(pending and anchor is not None and now - anchor >= quiet_seconds)
    if stalled and asahi_alive and now - anchor < 3600:
        stalled = False
    alerted = bool(state.get("alerted"))
    # 窓口の生死: 拡張の合図(alive)が DEAD_SECONDS 以上途切れたら、未処理の有無に関わらず1度だけ知らせる(誰も見ていない間の停止に気づくため。2026-11-02)
    dead_since = state.get("deadSince")
    dead_alerted = bool(state.get("deadAlerted"))
    if asahi_alive is False:
        dead_since = dead_since or now
    elif asahi_alive:
        dead_since = None
    dead = bool(dead_since and now - dead_since >= DEAD_SECONDS)
    next_state = {"alerted": stalled, "lastAsahiAt": last,
                  "pendingIds": [m.get("id") for m in pending], "checkedAt": now,
                  "deadSince": dead_since, "deadAlerted": dead}
    if dead and not dead_alerted:
        return f"朝日の窓口が{DEAD_SECONDS // 60}分以上止まっています(拡張の合図が途切れ)。タブの再読み込みが必要かも。", next_state
    if not dead and dead_alerted:
        return "朝日の窓口の合図が戻りました。", next_state
    if stalled and not alerted:
        return f"朝日が{quiet_seconds // 60}分以上無音で、未処理が{len(pending)}件あります。拡張とタブを確認してください。", next_state
    if not stalled and alerted:
        return "朝日の応答を確認しました。監視は復帰状態です。", next_state
    return None, next_state


PROBE_GAP = 86400      # 配送成功(sent_ok)がこれだけ無ければ、疎通試験を1回送る
PROBE_WAIT = 600       # 試験を送ってからこの時間たっても成功が出なければ、潤へ知らせる


def probe_decide(audit_rows: list[dict], state: dict, now: float) -> tuple[str | None, dict]:
    """定期の疎通試験。戻り値は (動作, 状態の更新分)。動作: "send"(試験を送る) / "fail"(試験失敗を知らせる) / None。"""
    oks = [parse_time(r.get("at")) for r in audit_rows if r.get("reason") == "sent_ok" and r.get("actor") == "ext-asahi"]
    last_ok = max([v for v in oks if v is not None], default=0.0)
    first = state.get("probeFirst") or now
    upd = {"probeFirst": first}
    probe_at = state.get("probeAt") or 0
    if probe_at and last_ok >= probe_at:
        upd.update({"probeAt": 0, "probeFailNotified": False})            # 試験の後に成功が出た=疎通OK
        return None, upd
    if probe_at and now - probe_at >= PROBE_WAIT and not state.get("probeFailNotified"):
        upd["probeFailNotified"] = True
        return "fail", upd
    if not probe_at and now - max(last_ok, first) >= PROBE_GAP:
        upd.update({"probeAt": now, "probeFailNotified": False})
        return "send", upd
    return None, upd


FAIL_STREAK = 3        # 最後の成功より後に、配送の失敗(failed)がこの回数以上続いたら知らせる
FAIL_WINDOW = 900      # ただし、直近この秒数以内の失敗だけ数える


def fail_streak_decide(audit_rows: list[dict], state: dict, now: float) -> tuple[str | None, dict]:
    """拡張は生きている(alive)のに配送が失敗し続ける状態(例: 画面が『会話を読み込めませんでした』)を見つける。通知は1回、成功が出たら解除。"""
    rows = [(parse_time(r.get("at")), r) for r in audit_rows if r.get("actor") == "ext-asahi" and r.get("kind") in ("deliver", "page")]
    rows = sorted([x for x in rows if x[0] is not None], key=lambda x: x[0])
    last_ok = max([t for t, r in rows if r.get("reason") == "sent_ok"], default=0.0)
    fails = [t for t, r in rows if r.get("verdict") == "failed" and t > last_ok and now - t <= FAIL_WINDOW]
    notified = bool(state.get("failNotified"))
    if len(fails) >= FAIL_STREAK and not notified:
        return "fail", {"failNotified": True}
    if not fails and notified:
        return "recover", {"failNotified": False}
    return None, {"failNotified": notified}


def http_json(url: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url.rstrip("/") + path, data=data,
                                     headers={"Content-Type": "application/json; charset=utf-8"},
                                     method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fetch_recent_messages(url: str, limit: int = 500) -> list[dict]:
    """末尾IDを基準に、最新側のメッセージを最大 limit 件取得する。"""
    tail = http_json(url, "/api/messages?tail=1").get("messages", [])
    if not tail:
        return []
    last_id = int(tail[-1]["id"])
    after = max(0, last_id - limit)
    response = http_json(url, "/api/messages?" + urllib.parse.urlencode({"after": after, "limit": limit}))
    return response.get("messages", [])


def run_once(url: str, state_path: Path, quiet_minutes: int, dry_run: bool = False) -> str | None:
    messages = fetch_recent_messages(url)
    state = load_state(state_path)
    try:
        alive = bool(http_json(url, "/api/presence")["presence"]["asahi"]["alive"])
    except Exception:
        alive = None
    try:
        delivered = {str(r["ref"]) for r in http_json(url, "/api/audit?limit=500").get("audit", []) if r.get("kind") == "deliver" and r.get("verdict") == "done" and r.get("reason") == "sent_ok"}
    except Exception:
        delivered = set()
    notice, next_state = decide(messages, state, time.time(), quiet_minutes * 60, alive, delivered)
    try:                                                                     # 定期の疎通試験(配送成功の記録が24時間無いときだけ)
        rows = http_json(url, "/api/audit?limit=500").get("audit", [])
        act, upd = probe_decide(rows, state, time.time())
        next_state.update({k: state.get(k) for k in ("probeAt", "probeFailNotified") if k in state})
        next_state.update(upd)
        if act == "send" and not dry_run:
            http_json(url, "/api/messages", {"sender": "hinata", "target": "asahi", "source": "api",
                                              "body": "【疎通試験】毎日1回の自動の合図です。返答は不要です(【朝日→篝火】(聞いています) だけでよい)。"})
        if act == "fail" and not notice:
            notice = "疎通試験に24時間以上成功がなく、試験も届いていません。窓口を確認してください。"
        fact, fupd = fail_streak_decide(rows, state, time.time())
        next_state.update(fupd)
        if fact == "fail" and not notice:
            notice = "朝日の窓口は生きているのに、配送が失敗し続けています(画面の不調の可能性)。タブを確認してください。"
        if fact == "recover" and not notice:
            notice = "朝日への配送の失敗が止まりました。"
    except Exception:
        pass
    if notice:
        notice = "【朝日監視】" + notice
        if len(notice) > 100:
            raise ValueError("通知が100字を超えています")
        if not dry_run:
            http_json(url, "/api/messages", {"sender": "hinata", "target": None, "body": notice,
                                              "source": "api"})
    if not dry_run:
        save_state(state_path, next_state)
    return notice


def selftest() -> None:
    base = 2_000_000_000.0
    old = datetime.fromtimestamp(base - 901, timezone.utc).isoformat()
    fresh = datetime.fromtimestamp(base - 10, timezone.utc).isoformat()
    pending = [{"id": 1, "sender": "jun", "target": "asahi", "createdAt": old}]
    notice, state = decide(pending, {}, base, 900)
    assert notice and state["alerted"] and len(notice) <= 100
    assert decide(pending, state, base + 1, 900)[0] is None
    recovered = pending + [{"id": 2, "sender": "asahi", "target": "jun", "replyTo": 1, "createdAt": fresh}]
    notice, state = decide(recovered, state, base, 900)
    assert notice and "復帰" in notice and not state["alerted"]
    assert decide(recovered, state, base + 1, 900)[0] is None
    # 窓口の生死: 3分未満は通知なし、3分以上で1度だけ、戻ったら1度だけ
    n, st = decide([], {}, base, 900, False)
    assert n is None and st["deadSince"] == base
    n, st = decide([], st, base + DEAD_SECONDS, 900, False)
    assert n and "止まって" in n and len(n) + 6 <= 100 and st["deadAlerted"]
    assert decide([], st, base + DEAD_SECONDS + 1, 900, False)[0] is None
    n, st = decide([], st, base + DEAD_SECONDS + 5, 900, True)
    assert n and "戻りました" in n and not st["deadAlerted"]
    assert decide([], st, base + DEAD_SECONDS + 6, 900, True)[0] is None
    # 疎通試験: 24時間成功が無ければ送る→10分待って成功が無ければ失敗通知→成功が出れば解除
    ok = lambda t: {"at": datetime.fromtimestamp(t, timezone.utc).isoformat(), "reason": "sent_ok", "actor": "ext-asahi"}
    a, st = probe_decide([ok(base - 100)], {}, base)
    assert a is None
    a, st = probe_decide([ok(base - PROBE_GAP - 100)], {"probeFirst": base - PROBE_GAP - 50}, base)
    assert a == "send" and st["probeAt"] == base
    a, st2 = probe_decide([ok(base - PROBE_GAP - 100)], {**st}, base + PROBE_WAIT + 1)
    assert a == "fail"
    assert probe_decide([ok(base - PROBE_GAP - 100)], {**st, **st2}, base + PROBE_WAIT + 2)[0] is None
    a, st3 = probe_decide([ok(base + 30)], {**st, **st2}, base + PROBE_WAIT + 3)
    assert a is None and st3["probeAt"] == 0
    # 配送失敗の連続: 3回以上(成功より後・15分以内)で1度通知、成功が出たら解除通知
    f = lambda t, v="failed", r="send_failed": {"at": datetime.fromtimestamp(t, timezone.utc).isoformat(), "actor": "ext-asahi", "kind": "deliver", "verdict": v, "reason": r}
    rows = [f(base - 30), f(base - 20), f(base - 10)]
    a, st = fail_streak_decide(rows, {}, base)
    assert a == "fail" and st["failNotified"]
    assert fail_streak_decide(rows, st, base + 1)[0] is None
    a, st = fail_streak_decide(rows + [f(base + 5, "done", "sent_ok")], st, base + 6)
    assert a == "recover" and not st["failNotified"]
    assert fail_streak_decide(rows[:2], {}, base)[0] is None
    print("SELFTEST PASS: alert/deduplicate/recovery/deduplicate/dead/probe/failstreak")


AUDIT_STATE = Path(r"E:\StockResearch\preview\audit_report_state.json")
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import kagaribi_liveness as _live                                  # 篝火の外側への通知(受信箱)。2026-11-02
except Exception:                                                      # noqa: BLE001
    _live = None


def external(level: str, text: str, key: str) -> None:
    if _live:
        _live.external_alert(level, text, key)


AUDIT_STALE_SECONDS = 150 * 60   # 監査の定期実行(1〜3時間ごと)がこれだけ動いていなければ知らせる(見張りの見張り。2026-11-02)


def check_audit_alive(url: str, notified_at, dry_run: bool):
    """audit_report_state.json の更新が古ければ、潤へ1度だけ知らせる。同じ停止では繰り返さない。"""
    try:
        at = float(json.loads(AUDIT_STATE.read_text(encoding="utf-8")).get("at"))
    except (OSError, ValueError, TypeError):
        return notified_at
    if time.time() - at <= AUDIT_STALE_SECONDS or notified_at == at:
        return notified_at
    if not dry_run:
        external("red", "【篝火】監査の定期実行が止まっています(最後の実行から150分以上)。Kagaribi-AuditReport を確認", "kagaribi-audit-stale-%d" % int(at))
        try:
            body = json.dumps({"sender": "hinata", "target": None, "body": "【篝火・要確認】監査の定期実行が止まっています(最後の実行から150分以上)。Kagaribi-AuditReport を確認してください。", "source": "api"}, ensure_ascii=False).encode("utf-8")
            urllib.request.urlopen(urllib.request.Request(url + "/api/messages", body, {"Content-Type": "application/json; charset=utf-8"}), timeout=8).read()
        except Exception:
            return notified_at
    return at


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--quiet-minutes", type=int, default=15)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="投稿・状態保存をせず判定結果だけ表示")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.quiet_minutes < 1 or args.interval < 1:
        parser.error("quiet-minutes と interval は1以上にしてください")
    if args.selftest:
        selftest()
        return 0
    audit_notified_at = None
    down_since = None
    while True:
        try:
            notice = run_once(args.url, args.state, args.quiet_minutes, args.dry_run)
            print(notice or "OK: notification not needed", flush=True)
            if down_since is not None:
                external("info", "【篝火】サーバーが応答に戻りました", "kagaribi-server-up-%d" % int(time.time()))
                try:                                                       # 戻った篝火のチャット(携帯版にも)へも、止まっていた事実を残す
                    mins = max(1, int((time.time() - down_since) // 60))
                    body = json.dumps({"sender": "hinata", "target": None, "body": "【篝火・復旧】篝火のサーバーが約%d分応答しませんでした。いまは戻っています。" % mins, "source": "api"}, ensure_ascii=False).encode("utf-8")
                    urllib.request.urlopen(urllib.request.Request(args.url + "/api/messages", body, {"Content-Type": "application/json; charset=utf-8"}), timeout=8).read()
                except Exception:                                          # noqa: BLE001
                    pass
                down_since = None
        except Exception as e:                                         # noqa: BLE001  サーバーが落ちても見張りは止めない。外側へ知らせる
            print("ERROR: server unreachable: %r" % (e,), flush=True)
            if down_since is None:
                down_since = time.time()
            elif time.time() - down_since >= 180 and not getattr(main, "_down_notified", False) and not args.dry_run:
                external("red", "【篝火】サーバー(8765)が3分以上応答しません。Kagaribi-Server を確認してください", "kagaribi-server-down-w-%d" % int(down_since))
                main._down_notified = True
                try:                                                       # 監督役(救援): サーバー停止なら Kagaribi-Server の再起動だけ自動で行う
                    import sys as _sys2
                    _sys2.path.insert(0, str(Path(__file__).resolve().parent.parent))
                    import kagaribi_supervisor as sup
                    sup.handle(act=True)
                except Exception as sup_error:                             # noqa: BLE001
                    print("supervisor error: %r" % (sup_error,), flush=True)
            if args.once:
                return 1
            time.sleep(args.interval)
            continue
        main._down_notified = False
        audit_notified_at = check_audit_alive(args.url, audit_notified_at, args.dry_run)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
