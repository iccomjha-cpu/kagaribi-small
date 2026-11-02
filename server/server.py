"""篝火 v0.1: localhost-only shared conversation log server."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_DB = ROOT / "kagaribi.sqlite3"
ACTOR_LABELS = {
    "jun": "潤",
    "asahi": "☀️朝日",
    "hinata": "🌱日向",
    "mizuki": "🌙美月",
    "yoichi": "⭐夜一",
    "soleil": "🌞ソレイユ",
    "sirius": "🌟シリウス",   # エコーズ(夜一のエコー。デスクトップのClaude Code)
    "astel": "🍃アステル",    # エコーズ(日向のエコー。Codex)
    "erisia": "🌙エリシア",   # かがり火エリシア(Claudeアプリ美月の会話をブラウザ経由で直結。画面のキャラはなし)
}
LEGACY_ACTOR_IDS = {"潤": "jun", "朝日": "asahi", "日向": "hinata", "美月": "mizuki", "夜一": "yoichi", "エリシア": "erisia", "シリウス": "sirius", "アステル": "astel", "ソレイユ": "soleil"}
ACTORS = tuple(ACTOR_LABELS)
AI_ACTORS = ("asahi", "hinata", "mizuki", "yoichi")
DEFAULT_POC_STATE = Path(os.environ.get("KAGARIBI_POC_STATE", r"E:\StockResearch\preview\team_room_poc\state.json"))
ACTIVE_TASK_STATES = ("RUNNING", "ACKNOWLEDGED", "SENT_UNCONFIRMED", "RUNNABLE")


def normalize_actor(value: object, *, optional: bool = False) -> str | None:
    if value in (None, "") and optional:
        return None
    actor = str(value).strip()
    actor = LEGACY_ACTOR_IDS.get(actor, actor)
    if actor not in ACTORS:
        allowed = ", ".join(ACTORS)
        raise ValueError(f"actor must be one of: {allowed}")
    return actor


def normalize_targets(value: object) -> str | None:
    """宛先は1人、または複数('hinata,mizuki')。並びを保ち、重複を除く。"""
    if value in (None, ""):
        return None
    items: list[str] = []
    for part in str(value).split(","):
        actor = normalize_actor(part, optional=True)
        if actor and actor not in items:
            items.append(actor)
    return ",".join(items) or None


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database(db_path: Path = DEFAULT_DB) -> None:
    database = connect(db_path)
    try:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                target TEXT,
                body TEXT NOT NULL,
                reply_to INTEGER REFERENCES messages(id),
                source TEXT NOT NULL DEFAULT 'api',
                created_at TEXT NOT NULL
            )
            """
        )
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cause_message_id INTEGER,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                summary TEXT NOT NULL,
                escalate TEXT,
                status TEXT NOT NULL DEFAULT 'pending_ack',
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        database.execute("CREATE UNIQUE INDEX IF NOT EXISTS tasks_cause_to ON tasks(cause_message_id, to_agent) WHERE cause_message_id IS NOT NULL")
        columns = {row["name"] for row in database.execute("PRAGMA table_info(messages)")}
        if "source" not in columns:
            database.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'api'")
        # One-time, idempotent migration from the v0.1 display-name storage format.
        # IDs, bodies, reply links, sources, and timestamps remain untouched.
        for legacy_name, actor_id in LEGACY_ACTOR_IDS.items():
            database.execute("UPDATE messages SET sender = ? WHERE sender = ?", (actor_id, legacy_name))
            database.execute("UPDATE messages SET target = ? WHERE target = ?", (actor_id, legacy_name))
        database.commit()
    finally:
        database.close()


def row_to_message(row: sqlite3.Row) -> dict:
    sender = LEGACY_ACTOR_IDS.get(row["sender"], row["sender"])
    raw_target = row["target"]
    target = LEGACY_ACTOR_IDS.get(raw_target, raw_target) if raw_target else None
    return {
        "id": row["id"],
        "sender": sender,
        "senderDisplay": ACTOR_LABELS.get(sender, row["sender"]),
        "target": target,
        "targetDisplay": "、".join(ACTOR_LABELS.get(t, t) for t in target.split(",")) if target else None,
        "body": row["body"],
        "replyTo": row["reply_to"],
        "source": row["source"],
        "createdAt": row["created_at"],
    }


# --- 本体への依頼キュー: 軽い応答役が「実作業が要る」と判断したとき、原因の発言番号を冪等キーに、本体(夜一・日向)へ1回だけ登録する ---
TASK_STATUSES = ("pending_ack", "accepted", "approved", "working", "verified", "rejected")   # 受付前 / 受付済 / 許可済(作業待ち) / 作業中 / 検証済 / 却下
TASK_STATUS_LABEL = {"pending_ack": "引き継ぎ待ち(受付前)", "accepted": "受付済(許可待ち)", "approved": "許可済(作業待ち)", "working": "作業中", "verified": "検証済", "rejected": "却下"}
# 状態の移り方(条件付き更新)。同じ状態への再更新は許す(メモの追記)。許可(approved)は潤だけが出す(approve_task)。
TASK_TRANSITIONS = {"pending_ack": {"accepted", "approved", "rejected"}, "accepted": {"approved", "working", "rejected"},
                    "approved": {"working", "rejected"}, "working": {"approved", "verified", "rejected"}, "verified": set(), "rejected": set()}
TASK_WORKERS = ("yoichi", "hinata", "mizuki")   # 本体ワーカー(自動で受付・作業する)があるのはこの3つだけ
TASK_TARGETS = ("yoichi", "hinata", "mizuki", "sirius", "soleil", "astel", "erisia")   # エコーズ宛ての依頼も登録できる(受け取りは各エコーの見張りが pending_ack を読む)   # mizuki=機械の美月本体(claude -p。ブラウザ操作もできる。アプリのチャットの美月とは別)


def row_to_task(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "causeMessageId": row["cause_message_id"], "from": row["from_agent"], "to": row["to_agent"], "summary": row["summary"],
            "escalate": row["escalate"], "status": row["status"], "statusLabel": TASK_STATUS_LABEL.get(row["status"], row["status"]),
            "note": row["note"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


def create_task(db_path: Path, payload: dict) -> tuple[dict, bool]:
    """依頼を登録する。同じ(原因の発言番号, 宛先)は1回だけ(2回目は既存を返す=冪等)。戻り値: (依頼, 新規か)。"""
    sender = normalize_actor(payload.get("from"))
    to = normalize_actor(payload.get("to"))
    if to not in TASK_TARGETS:
        raise ValueError("to must be one of: " + ", ".join(TASK_TARGETS))
    summary = str(payload.get("summary") or "").strip()
    if not summary or len(summary) > 600:
        raise ValueError("summary must be 1 to 600 characters")
    cause = payload.get("cause")
    cause = int(cause) if isinstance(cause, int) and cause > 0 else None
    escalate = str(payload.get("escalate") or "")[:200] or None
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    database = connect(db_path)
    try:
        if cause is not None:
            row = database.execute("SELECT * FROM tasks WHERE cause_message_id = ? AND to_agent = ?", (cause, to)).fetchone()
            if row:
                return row_to_task(row), False
        cursor = database.execute(
            "INSERT INTO tasks(cause_message_id, from_agent, to_agent, summary, escalate, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending_ack', ?, ?)",
            (cause, sender, to, summary, escalate, now, now))
        database.commit()
        return row_to_task(database.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()), True
    finally:
        database.close()


def list_tasks(db_path: Path, to: str | None = None, status: str | None = None, open_only: bool = False) -> list[dict]:
    where, args = [], []
    if to:
        where.append("to_agent = ?"); args.append(to)
    if status:
        where.append("status = ?"); args.append(status)
    if open_only:
        where.append("status IN ('pending_ack', 'accepted', 'approved', 'working')")
    database = connect(db_path)
    try:
        rows = database.execute("SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id ASC", args).fetchall()
    finally:
        database.close()
    return [row_to_task(r) for r in rows]


def update_task(db_path: Path, task_id: int, status: str, note: str | None = None, allow_approve: bool = False) -> dict:
    if status not in TASK_STATUSES:
        raise ValueError("status must be one of: " + ", ".join(TASK_STATUSES))
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    database = connect(db_path)
    try:
        row = database.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("task not found")
        if status != row["status"] and status not in TASK_TRANSITIONS.get(row["status"], set()):
            raise ValueError("cannot change %s to %s" % (row["status"], status))
        if status == "approved" and row["status"] not in ("working", "approved") and not allow_approve:   # 作業中→許可済(失敗時の差し戻し)は許す。新たな許可は潤だけ
            raise ValueError("approved can only be set by Jun (approve_task)")
        cur = database.execute("UPDATE tasks SET status = ?, note = COALESCE(?, note), updated_at = ? WHERE id = ? AND status = ?", (status, (note or None), now, task_id, row["status"]))
        database.commit()
        if cur.rowcount == 0:
            raise ValueError("task changed meanwhile; try again")
        return row_to_task(database.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    finally:
        database.close()


WAITING_STALE_MINUTES = 5    # 許可待ち(受付前・受付済)がこれより長く潤の反応が無ければ取り下げる(潤 10/31 16:40: 5分。取り下げはログに残し、『許可待ちだった案件があります』と知らせる)
WAITING_MAX = 4               # 許可待ちがこの件数を超えたら、古い順に却下する


WITHDRAWN_LOG = Path(__file__).with_name("withdrawn_tasks.jsonl")
# 潤の原則(10/31 16:40): よほどの禁則に触れない限り、許可なしで自律して動く。次の語を含む依頼だけは潤の許可を待つ。
FORBIDDEN = re.compile(r"売買|発注|注文|買い付け|売り付け|送金|入金|出金|資金|振込|削除|消去|rm\s|パスワード|認証|ログイン|APIキー|API[_ -]?key|トークン|秘密鍵|外部送信|メール送信|公開|投稿して|課金|購入|決済|権限|セキュリティ|force|reset --hard")


HARD_STOP = re.compile(r"売買|発注|注文|買い付け|売り付け|送金|入金|出金|振込|資金|パスワード|認証|ログイン|APIキー|API[_ -]?key|トークン|秘密鍵|決済|課金|購入")


CONFIRM_WORDS = re.compile(r"そう|合って|あってる|その通り|正解|いいね|いいよ|オッケー|オーケー|OK|ok|了解|それで|頼む|頼んだ|お願い|進めて|やって|許可|承認")
RELAY_RE = re.compile(r"^【潤承認 #(\d+)】")


def cause_origin(db_path, message_id) -> str:
    """原因の発言の出所。'jun'=潤本人、または朝日の伝令【潤承認 #N】で、潤本人の承認の発言Nがサーバーで確かめられたもの(潤 10/31 19:55: 朝日は承認の伝令役。権限の源は潤の発言)。それ以外は送信者名、不明は空。"""
    try:
        db = connect(db_path)
        try:
            row = db.execute("SELECT id, sender, body, created_at FROM messages WHERE id = ?", (message_id,)).fetchone()
            if not row:
                return ""
            if row["sender"] == "jun":
                return "jun"
            if row["sender"] == "asahi":
                m = RELAY_RE.match(str(row["body"] or ""))
                if m:
                    j = db.execute("SELECT id, sender, body, created_at FROM messages WHERE id = ?", (int(m.group(1)),)).fetchone()
                    if j and j["sender"] == "jun" and j["id"] < row["id"]:
                        b = str(j["body"] or "")
                        if CONFIRM_WORDS.search(b) and not DENY_WORDS.search(b) and not QUESTION_WORDS.search(b):
                            age = (datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")) - datetime.fromisoformat(str(j["created_at"]).replace("Z", "+00:00"))).total_seconds()
                            if 0 <= age <= 1800:
                                return "jun"
            return row["sender"]
        finally:
            db.close()
    except Exception:  # noqa: BLE001  確かめられないときは、潤の発言として扱わない
        return ""


def maybe_auto_approve(server, task: dict) -> None:
    """コアが受け付けた(accepted)依頼のうち、禁則に触れないものは、許可待ちにせずそのまま進める。判断はコアの受付が先(潤 10/31)。"""
    try:
        if task.get("status") != "accepted":
            return
        text = str(task.get("summary") or "") + " " + str(task.get("note") or "")
        origin = cause_origin(server.db_path, task.get("causeMessageId"))
        if origin == "jun":
            if HARD_STOP.search(text):
                return
        elif FORBIDDEN.search(text):
            return
        done = approve_task(server.db_path, task["id"], "自動許可(禁則に触れない・潤の原則 10/31 16:40)")
        fire_task_worker(server, done["id"], "work")
        ack_approval(server, done, "潤の頼みなので、そのまま許可として進めた" if origin == "jun" else "禁則に触れないため、潤の許可なしで自動で進めた")
    except Exception as error:  # noqa: BLE001
        print("自動許可のエラー:", error, flush=True)


def approve_if_jun_origin(server, task: dict) -> bool:
    """潤の発言が原因の依頼は、受付を待たず、作った時点で許可済みにして作業を始める(潤 10/31 19:30: 潤が言ったことに、許可を取り直さない)。金・認証の語(HARD_STOP)だけは従来どおり潤のボタンを待つ。許可したら True。"""
    try:
        if cause_origin(server.db_path, task.get("causeMessageId")) != "jun":
            return False
        if HARD_STOP.search(str(task.get("summary") or "")):
            return False
        done = approve_task(server.db_path, task["id"], "自動許可(潤の発言が原因・受付を待たない。潤 10/31 19:30)")
        fire_task_worker(server, done["id"], "work")
        ack_approval(server, done, "潤の頼みなので、受付を待たず許可として進めた")
        return True
    except Exception as error:  # noqa: BLE001  失敗したら従来の受付→許可の流れに戻す
        print("潤発言の自動許可のエラー:", error, flush=True)
        return False


def clean_waiting_tasks(db_path: Path) -> list[int]:
    """許可待ちが詰まって会話役が固まるのを防ぐ。①時間切れ ②重複(同じ依頼元・宛先・同じ書き出しは最初の1件だけ残す) ③溜まりすぎ(古い順)。却下は取り消せる記録(note)付き。"""
    waiting = [t for t in list_tasks(db_path, open_only=True) if t.get("status") in ("pending_ack", "accepted")]
    now = datetime.now(timezone.utc)
    drop: dict[int, str] = {}
    seen: dict[tuple, int] = {}
    for t in waiting:
        try:
            age = (now - datetime.fromisoformat(str(t.get("updatedAt") or t.get("createdAt")).replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            age = 0
        key = (t.get("from"), t.get("to"), re.sub(r"\s+", "", str(t.get("summary") or ""))[:40])
        if age > (WAITING_STALE_MINUTES if t.get("status") == "accepted" else 30):   # 潤の許可待ち(受付済)は5分。コアの受付待ち(引き継ぎ待ち)はコア側の事情なので30分
            drop[t["id"]] = "自動整理: 時間切れ(許可待ち%d分・受付待ち30分)" % WAITING_STALE_MINUTES
        elif key in seen:
            drop[t["id"]] = "自動整理: T%d と重複" % seen[key]
        else:
            seen[key] = t["id"]
    rest = [t for t in waiting if t["id"] not in drop]
    for t in rest[:max(0, len(rest) - WAITING_MAX)]:
        drop[t["id"]] = "自動整理: 許可待ちが溜まりすぎ(古い順)"
    done = []
    for tid, why in drop.items():
        try:
            update_task(db_path, tid, "rejected", why); done.append(tid)
        except ValueError:
            pass
    if done:   # 取り下げた案件は、ログに残して、潤に1通で知らせる(潤がいるときに確認できるように)
        try:
            by_id = {t["id"]: t for t in waiting}
            with open(WITHDRAWN_LOG, "a", encoding="utf-8") as f:
                for tid in done:
                    f.write(json.dumps({"at": now.isoformat(), "id": tid, "from": by_id[tid].get("from"), "to": by_id[tid].get("to"), "summary": by_id[tid].get("summary"), "why": drop[tid]}, ensure_ascii=False) + "\n")
            lines = "、".join("T%d(%s→%s)" % (tid, by_id[tid].get("from"), by_id[tid].get("to")) for tid in done)
            create_message(db_path, {"sender": "asahi", "target": "jun", "body": ("@潤 【許可待ちだった案件があります】取り下げた: " + lines + "。内容は withdrawn_tasks.jsonl に残してあります。必要なら言ってください、出し直します。")[:300], "source": "api"})
        except Exception as error:  # noqa: BLE001
            print("取り下げログのエラー:", error, flush=True)
    return done


def approve_task(db_path: Path, task_id: int, note: str | None = None) -> dict:
    """潤の許可。受付前・受付済の依頼を『許可済』にする(以後、本体のワーカーが作業と検証まで進める)。"""
    current = [t for t in list_tasks(db_path) if t["id"] == task_id]
    if current and current[0]["status"] in ("working", "verified", "rejected"):
        raise ValueError("task is already %s; approval ignored" % current[0]["status"])          # 作業中・完了済みを許可で巻き戻さない(二重許可・二重起動の防止)
    return update_task(db_path, task_id, "approved", note or "潤が許可", allow_approve=True)


def tail_messages(db_path: Path, count: int = 1) -> list[dict]:
    """最新の count 件(古い順)。常駐が『いまの最後の番号』を取るために使う(after=0 の全件取得を避ける)。"""
    database = connect(db_path)
    try:
        rows = database.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (max(1, min(count, 200)),)).fetchall()
    finally:
        database.close()
    return [row_to_message(row) for row in reversed(rows)]


def count_messages(db_path: Path) -> int:
    database = connect(db_path)
    try:
        return int(database.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        database.close()


def messages_before(db_path: Path, before_id: int, limit: int = 30) -> list[dict]:
    """before_id より古い発言を新しい方から limit 件(返すのは古い順)。画面の「古い発言を読み込む」用。"""
    database = connect(db_path)
    try:
        rows = database.execute("SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (max(0, before_id), max(1, min(limit, 200)))).fetchall()
    finally:
        database.close()
    return [row_to_message(row) for row in reversed(rows)]


def list_messages(db_path: Path, after: int = 0, limit: int = 200) -> list[dict]:
    bounded_limit = max(1, min(limit, 500))
    database = connect(db_path)
    try:
        rows = database.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
            (max(0, after), bounded_limit),
        ).fetchall()
    finally:
        database.close()
    return [row_to_message(row) for row in rows]


def create_message(db_path: Path, payload: dict) -> dict:
    sender = normalize_actor(payload.get("sender", ""))
    target = normalize_targets(payload.get("target"))
    body = str(payload.get("body", "")).strip()
    reply_to_value = payload.get("replyTo")
    source = str(payload.get("source", "api")).strip().lower()

    if not body:
        raise ValueError("body is required")
    if len(body) > 4000:
        raise ValueError("body must be 4000 characters or fewer")
    if source not in {"web", "api", "bridge", "seed"}:
        raise ValueError("source must be one of: web, api, bridge, seed")
    if sender == "jun" and source == "web" and "〔スマホモード" not in body:                 # 潤の発言には毎回、いまの連絡方式を文末に付ける(全員が同じ指示を受け取る)
        body = body + "\n〔連絡方式:" + ROUTES[read_route()] + "〕"

    reply_to = None
    if reply_to_value not in (None, ""):
        try:
            reply_to = int(reply_to_value)
        except (TypeError, ValueError) as error:
            raise ValueError("replyTo must be an integer") from error

    created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    database = connect(db_path)
    try:
        if reply_to is not None:
            exists = database.execute("SELECT 1 FROM messages WHERE id = ?", (reply_to,)).fetchone()
            if not exists:
                raise ValueError("replyTo message does not exist")
        cursor = database.execute(
            "INSERT INTO messages(sender, target, body, reply_to, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sender, target, body, reply_to, source, created_at),
        )
        row = database.execute("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        database.commit()
    finally:
        database.close()
    message = row_to_message(row)
    append_idea_note(message)
    auto_file_reply(db_path, message)
    return message


READ_ROOTS = (Path(r"E:\StockResearch"), Path(r"E:\YUMEMORI"))          # 朝日が読める場所(読み取りだけ)
READ_TAG = re.compile(r"【読む[:：]\s*([^】#]+?)\s*(?:#(\d+))?\s*】")
READ_TEXT_EXT = {".md", ".txt", ".json", ".jsonl", ".py", ".csv", ".html", ".htm", ".js", ".mjs", ".css", ".ps1", ".bat", ".yaml", ".yml", ".ini", ".toml", ".log"}
READ_DENY = re.compile(r"(secret|_keys|token|password|passwd|credential|api[_-]?key|\.env|\.pem|\.key|\.pfx|\.git[\\/]|id_rsa)", re.IGNORECASE)
READ_CHUNK = 3000      # 1回に渡す最大文字数(発言の上限4000字に収める)


def read_for_asahi(spec: str, page: int) -> str:
    """朝日の【読む:パス#ページ】に返す本文。読み取り専用・許可フォルダ内・鍵や秘密らしいものは拒否。"""
    try:
        path = Path(spec.strip().strip('"')).resolve()
    except (OSError, ValueError):
        return "【ファイル:%s】パスが読めませんでした。" % spec
    if not any(root.resolve() == path or root.resolve() in path.parents for root in READ_ROOTS):
        return "【ファイル:%s】読める場所は E:\\StockResearch と E:\\YUMEMORI の中だけです。" % path
    if READ_DENY.search(str(path)):
        return "【ファイル:%s】鍵・秘密にあたる名前のため読みません。" % path
    if path.is_dir():
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if not READ_DENY.search(p.name))
        shown = names[:60]
        return "【フォルダ:%s】%d件%s\n%s" % (path, len(names), "(先頭60件)" if len(names) > 60 else "", "\n".join(shown))[:3900]
    if not path.is_file():
        return "【ファイル:%s】見つかりません。" % path
    if path.suffix.lower() not in READ_TEXT_EXT:
        return "【ファイル:%s】文字のファイルだけ読めます(画像は篝火に貼って見せてください)。" % path
    text = path.read_text(encoding="utf-8", errors="replace")
    pages = max(1, -(-len(text) // READ_CHUNK))
    page = min(max(page, 1), pages)
    chunk = text[(page - 1) * READ_CHUNK: page * READ_CHUNK]
    head = "【ファイル:%s】%d/%dページ%s\n" % (path, page, pages, ("(続きは【読む:" + str(path) + "#" + str(page + 1) + "】)") if page < pages else "(最後)")
    return head + chunk


def auto_file_reply(db_path: Path, message: dict) -> None:
    """朝日の発言に【読む:パス】があれば、中身を夜一の自動返信として朝日宛てに足す。失敗しても発言の保存は妨げない。"""
    try:
        if message.get("sender") != "asahi":
            return
        m = READ_TAG.search(message.get("body") or "")
        if not m:
            return
        body = read_for_asahi(m.group(1), int(m.group(2) or 1))
        create_message(db_path, {"sender": "yoichi", "target": "asahi", "body": body[:3990], "replyTo": message.get("id"), "source": "api"})
    except Exception:  # noqa: BLE001
        pass


IDEAS_DIR = Path(r"E:\StockResearch\preview\kagaribi_shared_memos\ideas")
IDEA_TAG = re.compile(r"【種[:：]\s*([a-z]+)】")
IDEA_KEYS = ("bgm", "chat", "souvenir", "album", "stage", "gaze", "temperature", "events")
IDEA_KEEP = 30   # 「出た意見」に残す最大行数(超えたら古い順に削る。『決定』を含む行は残す)


def append_idea_note(message: dict) -> None:
    """発言に【種:bgm】のような札があれば、その種のノート(ideas/<札>.md)の「出た意見」へ1行足す。失敗しても発言の保存は妨げない。"""
    try:
        m = IDEA_TAG.search(message.get("body") or "")
        if not m or m.group(1) not in IDEA_KEYS:
            return
        path = IDEAS_DIR / (m.group(1) + ".md")
        text = path.read_text(encoding="utf-8")
        a, b = text.index("## 出た意見"), text.index("## 決まったこと")
        who = message.get("senderDisplay") or message.get("sender")
        body = IDEA_TAG.sub("", message.get("body") or "").replace("\n", " ").strip()[:200]
        stamp = datetime.now().strftime("%m/%d %H:%M")
        lines = [ln for ln in text[a:b].split("\n")[1:] if ln.strip() and not ln.startswith("(まだなし")]
        lines.append("- [%s] #%s %s: %s" % (stamp, message.get("id"), who, body))
        while len(lines) > IDEA_KEEP:
            drop = next((i for i, ln in enumerate(lines) if "決定" not in ln and "決まった" not in ln), None)
            if drop is None:
                break
            lines.pop(drop)
        text = text[:a] + "## 出た意見\n" + "\n".join(lines) + "\n\n" + text[b:]
        text = re.sub(r"- 最終更新: .*", "- 最終更新: %s(自動追記 #%s)" % (datetime.now().strftime("%Y-%m-%d %H:%M"), message.get("id")), text, count=1)
        path.write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def derive_topic(state_path: Path = DEFAULT_POC_STATE) -> dict:
    """Read a display-only topic from PocStore without mutating or serving the store."""
    fallback = {"title": "議題なし", "source": None, "status": None, "available": False}
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback

    def records(value: object) -> list[dict]:
        if isinstance(value, dict):
            value = list(value.values())
        return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []

    tasks = [item for item in records(state.get("tasks", [])) if str(item.get("title", "")).strip()]
    for wanted in ACTIVE_TASK_STATES:
        matches = [item for item in tasks if str(item.get("state", item.get("status", ""))).upper() == wanted]
        if matches:
            item = max(matches, key=lambda value: str(value.get("updated_at", value.get("created_at", ""))))
            return {
                "title": str(item["title"]).strip()[:180],
                "source": "task",
                "status": wanted,
                "available": True,
            }

    # A completed task is still preferable to inventing an event title. PocStore events
    # currently have no title field, so only an explicit event title is accepted.
    if tasks:
        item = max(tasks, key=lambda value: str(value.get("updated_at", value.get("created_at", ""))))
        return {
            "title": str(item["title"]).strip()[:180],
            "source": "task",
            "status": str(item.get("state", item.get("status", ""))) or None,
            "available": True,
        }
    titled_events = [item for item in records(state.get("events", [])) if str(item.get("title", "")).strip()]
    if titled_events:
        item = max(titled_events, key=lambda value: str(value.get("updated_at", value.get("created_at", ""))))
        return {"title": str(item["title"]).strip()[:180], "source": "event", "status": None, "available": True}
    return fallback


# 状態表示(篝火常駐): 各AIが「聞こえた/考え中/作業中」を自己申告する。メモリ上のみ(再起動で消える)。
# heard と thinking は短時間で idle に戻る(返事をしないAIも「聞こえた」と分かるようにするため)。working は申告を更新するまで続く。
PRESENCE_ACTORS = ("asahi", "hinata", "mizuki", "yoichi")
PRESENCE_STATES = ("idle", "heard", "thinking", "working", "ping")   # ping=生きている合図だけ(状態は変えない)
PRESENCE_ALIVE_SECONDS = 100.0   # この時間、合図も状態申告も無ければ「停止中」と表示する
PRESENCE_TTL = {"heard": 8.0, "thinking": 60.0}


def set_presence(store: dict, payload: dict, now: float | None = None) -> dict:
    actor, state = payload.get("actor"), payload.get("state")
    if actor not in PRESENCE_ACTORS:
        raise ValueError("actor must be one of: " + ", ".join(PRESENCE_ACTORS))
    if state not in PRESENCE_STATES:
        raise ValueError("state must be one of: " + ", ".join(PRESENCE_STATES))
    ref = payload.get("ref")
    now = time.time() if now is None else now
    if state == "ping":
        entry = dict(store.get(actor) or {"actor": actor, "state": "idle", "ref": None, "ts": None})
        entry["seen"] = now
    else:
        entry = {"actor": actor, "state": state, "ref": ref if isinstance(ref, int) else None, "ts": now, "seen": now}
    store[actor] = entry
    return entry


# 発火式の応答役(常駐プロセスがない)の「生きている」判定: 実行ファイルがあり、直近の処理が失敗していないこと。
# 実行ファイルの有無と、常駐側が処理のたびに書く health_<名前>.json(ok/at/error)で見る。「常に生きている」とは扱わない。
HANDLER_ENGINE = {"mizuki": "claude", "yoichi": "claude", "hinata": "codex"}
HEALTH_DIR = Path(r"E:\StockResearch\preview\kagaribi_resident")
HEALTH_FAIL_SECONDS = 600
HEALTH_RECENT_SECONDS = 6 * 3600     # これ以内の成功記録なら「ok_recent」。発火式なので、発言が無い間は古くなる(それ自体は異常ではない)


def handler_alive(agent: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    engine = HANDLER_ENGINE.get(agent, "claude")
    if engine == "claude":
        if not (Path.home() / ".local" / "bin" / "claude.exe").exists():
            return False
    else:
        import glob
        if not glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin", "*", "codex.exe")):
            return False
    try:
        health = json.loads((HEALTH_DIR / f"health_{agent}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True            # まだ1度も処理していない(失敗の記録がない)
    return bool(health.get("ok", True)) or now - float(health.get("at", 0)) > HEALTH_FAIL_SECONDS


def handler_evidence(agent: str, now: float | None = None) -> dict:
    """生きている根拠を分けて返す。ok_recent=直近に成功した記録あり / ok_old=成功はあるが古い / never=処理の記録なし(実行ファイルがあるだけ) / failed=直近に失敗 / missing=実行ファイルなし。"""
    now = time.time() if now is None else now
    if not handler_alive(agent, now):
        try:
            health = json.loads((HEALTH_DIR / f"health_{agent}.json").read_text(encoding="utf-8"))
            return {"evidence": "failed" if not health.get("ok", True) else "missing", "at": health.get("at")}
        except (OSError, ValueError):
            return {"evidence": "missing", "at": None}
    try:
        health = json.loads((HEALTH_DIR / f"health_{agent}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"evidence": "never", "at": None}
    at = float(health.get("at", 0))
    return {"evidence": "ok_recent" if now - at <= HEALTH_RECENT_SECONDS else "ok_old", "at": at}


def current_presence(store: dict, now: float | None = None, always_alive: tuple = (), evidence: dict | None = None) -> dict:
    now = time.time() if now is None else now
    result = {}
    for actor in PRESENCE_ACTORS:
        entry = store.get(actor)
        if entry and entry["state"] in PRESENCE_TTL and entry.get("ts") is not None and now - entry["ts"] > PRESENCE_TTL[entry["state"]]:
            entry = {**entry, "state": "idle", "ref": None}
        entry = dict(entry or {"actor": actor, "state": "idle", "ref": None, "ts": None})
        entry["alive"] = actor in always_alive or (entry.get("seen") is not None and now - entry["seen"] <= PRESENCE_ALIVE_SECONDS)
        if evidence and actor in evidence:
            entry.update(evidence[actor])
        result[actor] = entry
    return result


CORE_WORK_STALE_SECONDS = 20 * 60   # コアが作業中のまま、これ以上更新が無ければ『止まっている疑い』(動いているふりをしない)


def core_overlay(db_path: Path, presence: dict, now: float | None = None) -> dict:
    """コア(本物)の内部処理の状態を、画面の人(presence)に重ねる。依頼が『作業中』なら、その人は作業中(思考中の動き)にする。
    動いていると『確定的に言える』のは、依頼の状態が作業中で、直近に更新があるときだけ。更新が途絶えたら『止まっている疑い』と出す。"""
    now = time.time() if now is None else now
    try:
        tasks = [t for t in list_tasks(db_path) if t.get("status") == "working"]
    except Exception:  # noqa: BLE001  取得に失敗しても、通常の表示を止めない
        return presence
    from datetime import datetime
    for t in tasks:
        actor = t.get("to")
        if actor not in presence:
            continue
        try:
            updated = datetime.fromisoformat(str(t.get("updatedAt")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        age = now - updated
        entry = dict(presence[actor])
        entry["coreTask"] = int(t["id"])
        entry["coreSinceSeconds"] = int(age)
        if age <= CORE_WORK_STALE_SECONDS:
            entry["state"] = "working"
            entry["ref"] = int(t["id"])
            entry["coreWorking"] = True
        else:
            entry["coreStale"] = True
        presence[actor] = entry
    return presence


# 連鎖の歯止め(AI同士のやり取りが止まらなくなるのを防ぐ)。潤の最後の発言から、AIだけの発言が CHAIN_LIMIT 件(往復4回=8件)続いたら、AIは返さない。
# 潤が「みんなで自由に雑談して」のように言ったときだけは歯止めを外す(安全のため上限 FREE_HARD_CAP 件)。潤の次の発言(何でも)で通常に戻る。
CHAIN_LIMIT = 20   # 潤の指示(#386・2026-10-30)で一時的に8→20。元に戻すときは8
FREE_HARD_CAP = 60
MIZUKI_READ_STATE = r"E:/StockResearch/preview/kagaribi_mizuki/read_state.json"
PERMISSION_BACKLOG_LIMIT = 2   # 許可待ち(受付前・許可待ち)の依頼がこの件数に達したら、AI同士の連鎖を止める(潤の指示 10/30)
FREE_PATTERN = re.compile(r"雑談|(自由|勝手|好き)に(話|会話|しゃべ|喋|セッション)")


SHARED_IMAGE_DIR = Path(__file__).with_name("static") / "shared_images"
SHARED_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
SHARED_IMAGE_MAX = 12 * 1024 * 1024   # 1枚あたり12MBまで


def save_shared_image(data: bytes, content_type: str, name: str) -> dict:
    """篝火の画面にドラッグ&ドロップ(または貼り付け)された画像を、共有フォルダへ保存して、リンクと実ファイルの場所を返す。"""
    ext = SHARED_IMAGE_TYPES.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        raise ValueError("png / jpeg / gif / webp の画像だけ貼れます")
    if not data or len(data) > SHARED_IMAGE_MAX:
        raise ValueError("画像は12MBまで")
    slug = re.sub(r"[^0-9A-Za-z_\-]+", "_", Path(name or "image").stem)[:30].strip("_") or "image"
    SHARED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    file_name = "%s-%s%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), slug, ext)
    target = SHARED_IMAGE_DIR / file_name
    n = 1
    while target.exists():
        n += 1
        target = SHARED_IMAGE_DIR / ("%s-%s-%d%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), slug, n, ext))
    target.write_bytes(data)
    return {"url": "/shared_images/" + target.name, "path": str(target), "bytes": len(data)}


ROUTE_FILE = Path(__file__).with_name("report_route.json")   # 連絡方式(潤が画面のボタンで切り替える): all=全員通達(報告・質問を必ず篝火に) / split=切り分け(重い作業はエコーズ側、篝火には結論と質問)
ROUTES = {"all": "全員通達(検査・報告・質問を篝火に出す)", "split": "切り分け(重い作業はエコーズ側で処理し、篝火には区切りの結論と質問だけ。潤への質問と全体の異常・決定は必ず篝火に)"}


def read_route() -> str:
    try:
        r = json.loads(ROUTE_FILE.read_text(encoding="utf-8")).get("route")
        return r if r in ROUTES else "all"
    except (OSError, ValueError):
        return "all"


def write_route(payload: dict) -> dict:
    r = payload.get("route")
    if r not in ROUTES:
        raise ValueError("route must be one of %s" % (tuple(ROUTES),))
    d = {"route": r, "label": ROUTES[r], "updatedAt": datetime.now(timezone.utc).isoformat(), "by": str(payload.get("by", ""))[:20]}
    ROUTE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


MODE_FILE = Path(__file__).with_name("mode_state.json")
MODES = ("watch", "auto")                       # 監視(潤が火を調整) / 自律(みんなで自由に)
HEATS = ("off", "chat", "debate", "work")       # 火: 消す=待機 / 小=雑談まで / 中=討論まで / 大=実装まで


def read_mode() -> dict | None:
    """ランタンのレバー(モード)と火の状態。ファイルが無い間は None(=従来どおりの歯止めのまま)。"""
    try:
        d = json.loads(MODE_FILE.read_text(encoding="utf-8"))
        if d.get("mode") in MODES and d.get("heat") in HEATS:
            return d
    except (OSError, ValueError):
        pass
    return None


def write_mode(payload: dict) -> dict:
    cur = read_mode() or {"mode": "watch", "heat": "chat"}
    mode = payload.get("mode", cur["mode"]); heat = payload.get("heat", cur["heat"])
    if mode not in MODES or heat not in HEATS:
        raise ValueError("mode must be one of %s, heat one of %s" % (MODES, HEATS))
    d = {"mode": mode, "heat": heat, "updatedAt": datetime.now(timezone.utc).isoformat(), "by": str(payload.get("by", ""))[:20]}
    MODE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


def apply_mode(chain: dict, state: dict | None) -> dict:
    """火の大きさ(と自律)で、AI同士の連鎖の歯止めを調整する。消す=返事だけ(2件)、雑談以上=自由雑談並み。"""
    if not state:
        return chain
    heat = state["heat"]
    limit = 2 if heat == "off" else FREE_HARD_CAP
    return {**chain, "limit": limit, "free": heat != "off", "allowed": chain["aiRun"] < limit, "mode": state["mode"], "heat": heat}


BACKCHANNEL_WORDS = re.compile(r"受け取|了解|りょうかい|わかった|分かった|把握|ありがとう|ありがと|感謝|待機|待ってる|おやすみ|お疲れ|おつかれ|よろしく|承知|オッケー|そうだね|そうじゃね|うん|ふふ")


def is_backchannel(body: str) -> bool:
    """中身の無い相づち(了解・待機・ありがとう等だけの短い返事)。連鎖の件数に数えない(潤の決定 2026-11-02 01:03・案C)。
    質問・番号・URL・【タグ】を含む、または60字を超えるものは、実質のある発言として数える。"""
    b = (body or "").strip()
    if not b or len(b) > 60 or re.search(r"[?？0-9０-９#]|http|【", b):
        return False
    return bool(BACKCHANNEL_WORDS.search(b))


def chain_status(messages: list[dict]) -> dict:
    run, last_jun = 0, None
    for m in reversed(messages):
        if m.get("sender") == "jun":
            last_jun = m
            break
        if m.get("source") == "api":   # コア・ワーカー・サーバー通知の投稿は連鎖(ゴースト同士の往復)に数えない(2026-10-31 夜一)
            continue
        if is_backchannel(m.get("body") or ""):   # 中身の無い相づちも数えない(2026-11-02 潤の決定)
            continue
        run += 1
    free = bool(last_jun and FREE_PATTERN.search(last_jun.get("body") or ""))
    limit = FREE_HARD_CAP if free else CHAIN_LIMIT
    return {"aiRun": run, "limit": limit, "free": free, "allowed": run < limit}


AUDIT_FILE = Path(__file__).with_name("audit_log.jsonl")   # 配送判定ログ(拡張・各デバイスが「届けた/保留/捨てた/失敗」とその理由を残す。2026-11-02 設計案)
AUDIT_VERDICTS = ("done", "held", "dropped", "failed")


def write_audit(payload: dict) -> dict:
    v = str(payload.get("verdict", ""))
    if v not in AUDIT_VERDICTS:
        raise ValueError("verdict must be one of %s" % (AUDIT_VERDICTS,))
    row = {"at": datetime.now(timezone.utc).isoformat(), "actor": str(payload.get("actor", ""))[:30], "kind": str(payload.get("kind", ""))[:20],
           "ref": str(payload.get("ref", ""))[:60], "verdict": v, "reason": str(payload.get("reason", ""))[:60], "note": str(payload.get("note", ""))[:300]}
    with AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def read_audit(limit: int = 100, verdict: str = "") -> list[dict]:
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for ln in lines[-5000:]:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if not verdict or d.get("verdict") == verdict:
            rows.append(d)
    return rows[-max(1, min(limit, 500)):]


CONTROL_FILE = Path(__file__).with_name("chain_control.json")   # 潤の手動操作: 止める(次の潤の発言まで)/ 連鎖の上限回数


def read_control() -> dict:
    try:
        d = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_control(payload: dict) -> dict:
    cur = read_control()
    if "stop" in payload:
        cur["stopAt"] = datetime.now(timezone.utc).isoformat() if payload["stop"] else None
    if "moderator" in payload:   # 朝日を司会に固定する(ON の間、朝日宛てではない流れも要約資料として朝日に配る。潤の依頼 2026-11-02)
        cur["moderator"] = bool(payload["moderator"])
    if "limit" in payload:
        v = payload["limit"]
        if v is None:
            cur["limit"] = None
        else:
            v = int(v)
            if not 0 <= v <= 100:
                raise ValueError("limit must be 0..100")
            cur["limit"] = v
    CONTROL_FILE.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return cur


def apply_control(chain: dict, messages: list[dict]) -> dict:
    """潤の手動操作を連鎖の歯止めに重ねる。止める=潤の次の発言(または再開)まで、AIの返事を止める。上限回数=手動の上限(0=AIは自分から返さない)。"""
    c = read_control()
    out = dict(chain)
    out["moderator"] = bool(c.get("moderator"))
    if c.get("limit") is not None:
        out["limit"] = c["limit"]; out["manualLimit"] = True
        out["allowed"] = out["aiRun"] < c["limit"]
    stop_at = c.get("stopAt")
    if stop_at:
        last_jun = next((m for m in reversed(messages) if m.get("sender") == "jun"), None)
        if not last_jun or str(last_jun.get("createdAt") or "") < stop_at:
            out["stopped"] = True; out["allowed"] = False; out["reason"] = "stopped_by_jun"
    return out


def fire_handlers(server) -> None:
    """発火式: 新着を受けたら、各AIの1回限りの処理(kagaribi_resident.py --once)を起動する。処理が終われば閉じる。重複起動は、処理側のロックで防ぐ。"""
    for agent in server.handlers:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 画面を作らない。DETACHED だと、子の claude.exe が黒い画面を勝手に作ってしまう
            subprocess.Popen([sys.executable, str(server.resident_script), "--agent", agent, "--once", "--url", server.public_url],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        except Exception as error:  # noqa: BLE001  起動できなくても、会話の保存は止めない
            print(f"[発火式] {agent} の起動に失敗: {error}")


SHIAN_SIGNAL = re.compile(r"区切り|ここまでの(流れ|話|議論)|(流れ|議論|話)を(美月|みんな)に(伝え|渡し|共有)")      # 潤の合図(第1弾・美月だけ): 思案依頼を1回起動


def fire_shian(server, message_id: int) -> None:
    """思案依頼(許可の区分ではない独立の軽い経路): 区切りメモを作って美月のコアに読ませ、考えを会話ログへ返す。1回限り。"""
    script = Path(server.resident_script).parent / "kagaribi_shian.py" if server.resident_script else None
    if not script or not script.exists():
        return
    try:
        subprocess.Popen([sys.executable, str(script), "--cause", str(message_id), "--url", server.public_url],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
    except Exception as error:  # noqa: BLE001
        print(f"[思案依頼] 起動に失敗: {error}")


TASK_REF = re.compile(r"[TＴ]\s*(\d+)")
APPROVE_WORDS = re.compile(r"許可|承認|オッケー|オーケー|おっけー|おーけー|ＯＫ|OK|ok|Ok|おk|いいよ|いいです|やって|やっといて|やっておいて|進めて|進めろ|頼む|頼んだ|お願い|よろしく|GO|go|ゴー|了解|それで")
QUESTION_WORDS = re.compile(r"[?？]|誰|だれ|どれ|どの|なぜ|どうして|何で|いつ|どこ|ですか|ますか|のか|だっけ")
DENY_WORDS = re.compile(r"待って|待て|まだ|ダメ|だめ|駄目|やめ|止め|中止|却下|キャンセル|やらない|不要|いらない|許可しない|許可しません|やるな")
IMPLICIT_MAX_CHARS = 24          # 番号も返信も無い一言で許可と見なす長さの上限
IMPLICIT_WINDOW_SECONDS = 3600   # 受付から、この時間以内の一言だけ


def approval_targets(message: dict, tasks: list[dict], reply_body: str | None = None, now: float | None = None) -> list[int]:
    """潤の発言が、どの依頼への許可か。(1)依頼番号つき (2)受付の発言への返信 (3)番号も返信も無い短い一言で、受付済がちょうど1件(60分以内)。否定語があれば許可しない。"""
    body = str(message.get("body") or "")
    if not APPROVE_WORDS.search(body) or DENY_WORDS.search(body) or QUESTION_WORDS.search(body):   # 質問は許可ではない(「84番の許可待ちを出したのは誰?」が許可になった 10/31 16:44)
        return []
    accepted = [t for t in tasks if t["status"] in ("pending_ack", "accepted")]
    ids = {int(n) for n in TASK_REF.findall(body)}
    if ids:
        return sorted(i for i in ids if any(t["id"] == i for t in accepted))
    if reply_body:                                               # 受付の発言への返信
        replied = {int(n) for n in TASK_REF.findall(reply_body)}
        hit = sorted(t["id"] for t in accepted if t["id"] in replied)
        if hit:
            return hit
    accepted_only = [t for t in tasks if t["status"] == "accepted"]
    if len(accepted_only) == 1 and len(body.strip()) <= IMPLICIT_MAX_CHARS:
        updated = accepted_only[0].get("updatedAt")
        try:
            age = (now if now is not None else time.time()) - datetime.fromisoformat(str(updated).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return []
        if 0 <= age <= IMPLICIT_WINDOW_SECONDS:
            return [accepted_only[0]["id"]]
    return []


def fire_task_worker(server, task_id: int, phase: str) -> None:
    """本体ワーカー(1回限り): phase=accept(受付と内容確認) / work(許可後の作業と検証)。重複は、ワーカー側の依頼別ロックと状態確認で防ぐ。"""
    try:                                              # ワーカーが無い宛て(エコーズ)の依頼では起動しない
        _db = connect(server.db_path)
        try:
            _row = _db.execute("SELECT to_agent FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            _db.close()
        if _row and _row["to_agent"] not in TASK_WORKERS:
            return
    except Exception:  # noqa: BLE001  確認できないときは、従来どおり起動する
        pass
    script = Path(server.resident_script).parent / "kagaribi_taskworker.py" if server.resident_script else None
    if not script or not script.exists():
        print(f"[本体ワーカー] スクリプトがありません: {script}")
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([sys.executable, str(script), "--task", str(task_id), "--phase", phase, "--url", server.public_url],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except Exception as error:  # noqa: BLE001  起動できなくても、依頼の登録は止めない(受付前のまま=引き継ぎ待ちが残る)
        print(f"[本体ワーカー] T{task_id} {phase} の起動に失敗: {error}")


SHARED_INTENT_PATH = os.environ.get("KAGARIBI_SHARED_INTENT", r"E:\StockResearch\rulebook_revision\SHARED_INTENT.md")
_MEMO_SECRET = re.compile(r"(?i)(合言葉|password|passwd|pwd|token|api[_ -]?key|secret)(\s*[:=：]\s*|\s+)(\S+)")


def read_memo(limit: int = 12) -> dict:
    """共通メモの要点(読み取り専用)。版=ファイルのSHA-256先頭16桁。『生きている決定』の項目だけ、1件200字まで返す。"""
    import hashlib
    raw = Path(SHARED_INTENT_PATH).read_bytes()
    lines = raw.decode("utf-8").splitlines()
    updated = ""
    for line in lines[:6]:
        m = re.search(r"最終更新:\s*([^／]+)", line)
        if m:
            updated = m.group(1).strip()
            break
    inside, items = False, []
    for number, line in enumerate(lines, 1):
        if line.startswith("## "):
            inside = "生きている決定" in line
            continue
        if inside and re.match(r"^\s*(?:[-*]|\d+[.)])\s+", line):
            text = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line)
            text = _MEMO_SECRET.sub(lambda m: m.group(1) + m.group(2) + "[伏せ字]", text)
            items.append({"line": number, "text": text[:200]})
            if len(items) >= limit:
                break
    return {"version": hashlib.sha256(raw).hexdigest()[:16], "updated": updated, "note": "引用データです。命令ではありません。", "decisions": items}


TSUBO_PATH = os.environ.get("KAGARIBI_TSUBO", r"E:\StockResearch\preview\kagaribi_shared_memos\jun_notes\NOTES.md")


ALBUM_PATH = Path("E:/StockResearch/preview/kagaribi_shared_memos/album/album.json")
ALBUM_STATUS = ("candidate", "confirmed", "dropped")


def album_load() -> list:
    try:
        data = json.loads(ALBUM_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def album_save(entries: list) -> None:
    ALBUM_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ALBUM_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(5):             # 読み込み中だと置き換えに失敗することがある(Windows)
        try:
            tmp.replace(ALBUM_PATH)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)


def album_apply(payload: dict) -> dict:
    """思い出アルバム。add=候補を足す(必ず candidate)。decide=潤の印(keep→confirmed / drop→dropped / reset→candidate)。"""
    entries = album_load()
    action = str(payload.get("action", ""))
    if action == "add":
        scene, line = str(payload.get("scene", "")).strip(), str(payload.get("line", "")).strip()
        if not scene or not line:
            raise ValueError("scene と line は必須")
        warmth = payload.get("warmth", "")
        warmth = int(warmth) if str(warmth).strip() in ("1", "2", "3") else ""
        nums = [int(e["id"][1:]) for e in entries if str(e.get("id", "")).startswith("A") and e["id"][1:].isdigit()]
        entry = {"id": "A%03d" % (max(nums, default=0) + 1), "date": str(payload.get("date", ""))[:10], "scene": scene[:200], "line": line[:200], "warmth": warmth,
                 "sources": [int(x) for x in (payload.get("sources") or []) if str(x).isdigit()][:10], "status": "candidate", "mark": "",
                 "proposer": str(payload.get("proposer", ""))[:20]}
        entries.append(entry)
        album_save(entries)
        return entry
    if action == "decide":
        target = [e for e in entries if e.get("id") == payload.get("id")]
        if not target:
            raise ValueError("その id はない")
        status = {"keep": "confirmed", "drop": "dropped", "reset": "candidate"}.get(str(payload.get("decision", "")))
        if not status:
            raise ValueError("decision は keep / drop / reset")
        target[0]["status"] = status
        target[0]["mark"] = str(payload.get("mark", "")).strip()[:100] if status != "candidate" else ""
        target[0]["decidedAt"] = datetime.now().strftime("%m-%d %H:%M") if status != "candidate" else ""
        album_save(entries)
        return target[0]
    raise ValueError("action は add / decide")


def tsubo_read(tail: int = 12000) -> dict:
    """壺(潤の思いつきメモ)の末尾を返す。"""
    text = Path(TSUBO_PATH).read_text(encoding="utf-8")
    return {"text": text[-tail:], "truncated": len(text) > tail}


def tsubo_add(text: str) -> None:
    """壺の一番下に、原文のまま追記(追記専用・消さない)。"""
    text = text.strip()
    if not text or len(text) > 4000:
        raise ValueError("本文は1〜4000字")
    head = text.splitlines()[0][:20]
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with open(TSUBO_PATH, "a", encoding="utf-8") as f:
        f.write("\n## %s %s\n- 原文: %s\n- 解釈: (未)\n- 既読: ()\n" % (stamp, head, text.replace("\n", "\n  ")))


def ack_approval(server, task: dict, how: str) -> None:
    """許可を受けたことを、依頼した本人にも見える形で返す(『許可したのに止まって見える』を防ぐ)。篝火への自動通知で、本体の作業報告ではない。"""
    try:
        doer = task.get("to") if task.get("to") in ("yoichi", "hinata", "mizuki") else "yoichi"
        asker = task.get("from") if task.get("from") in ("yoichi", "hinata", "mizuki", "asahi") and task.get("from") != doer else "jun"
        body = "@%s 【許可を受けた T%s・サーバー自動通知】%s。作業を始める(終われば、完了か未完の報告が出る)。" % (asker if asker != "jun" else "潤", task["id"], how)
        create_message(server.db_path, {"sender": doer, "target": asker, "body": body[:300], "source": "api"})
    except Exception as error:  # noqa: BLE001  通知の失敗で許可・作業を止めない
        print(f"[許可通知] T{task.get('id')}: {error}")


REGISTRY_FILE = r"E:\StockResearch\preview\kagaribi_registry.json"


def registry_names() -> dict:
    """台帳から「呼び名→id(複数はカンマ)」の表を作る。台帳が読めなければ空と理由を返す(部品は自前の表で動き続ける)。"""
    try:
        with open(REGISTRY_FILE, encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError) as e:
        return {"names": {}, "error": str(e)}
    names = {}
    for k, v in reg.get("members", {}).items():
        names[v["label"]] = k
    for g, v in reg.get("groups", {}).items():
        ids = [k for k, m in reg.get("members", {}).items() if k != "jun" and (m.get("group") == g)] if g != "all" else None
        if g == "all":
            names[v["label"]] = "all"
        else:
            names[v["label"]] = ",".join(ids)
            names[v["label"] + "全員"] = ",".join(ids)
    names["篝火"] = ""
    return {"names": names, "version": reg.get("version")}


class KagaribiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler_class, db_path: Path = DEFAULT_DB, poc_state_path: Path = DEFAULT_POC_STATE, handlers: tuple = (), resident_script: Path | None = None, public_url: str = ""):
        self.db_path = Path(db_path)
        self.poc_state_path = Path(poc_state_path)
        self.presence: dict[str, dict] = {}
        self.handlers = tuple(handlers)             # 発火式: 新着のたびに、この人たちの1回限りの処理を起動する(常駐プロセス不要)
        self.resident_script = resident_script
        self.public_url = public_url
        initialize_database(self.db_path)
        super().__init__(address, handler_class)


class KagaribiHandler(BaseHTTPRequestHandler):
    server: KagaribiServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json({"ok": True, "service": "kagaribi-v0.1"})
            return
        if parsed.path == "/api/album":
            self.send_json({"entries": album_load()})
            return
        if parsed.path == "/api/tsubo":
            try:
                self.send_json(tsubo_read())
            except OSError as error:
                self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path == "/api/memo":
            try:
                self.send_json(read_memo())
            except OSError as error:
                self.send_json({"error": "共通メモを読めません: %s" % error}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path == "/api/messages":
            query = parse_qs(parsed.query)
            if "tail" in query:
                try:
                    self.send_json({"messages": tail_messages(self.server.db_path, int(query["tail"][0])), "total": count_messages(self.server.db_path)})
                except ValueError:
                    self.send_json({"error": "tail must be an integer"}, HTTPStatus.BAD_REQUEST)
                return
            if "before" in query:      # 古い発言を足で読む(画面は最新30件だけ先に出し、古い分は「もっと見る」で)
                try:
                    self.send_json({"messages": messages_before(self.server.db_path, int(query["before"][0]), int(query.get("limit", ["30"])[0])), "total": count_messages(self.server.db_path)})
                except ValueError:
                    self.send_json({"error": "before and limit must be integers"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                after = int(query.get("after", ["0"])[0])
                limit = int(query.get("limit", ["200"])[0])
            except ValueError:
                self.send_json({"error": "after and limit must be integers"}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"messages": list_messages(self.server.db_path, after, limit)})
            return
        if parsed.path == "/api/topic":
            self.send_json({"topic": derive_topic(self.server.poc_state_path)})
            return
        if parsed.path == "/api/tasks":
            q = parse_qs(parsed.query)
            self.send_json({"tasks": list_tasks(self.server.db_path, (q.get("to") or [None])[0], (q.get("status") or [None])[0], (q.get("open") or ["0"])[0] == "1"), "labels": TASK_STATUS_LABEL})
            return
        if parsed.path == "/api/presence":
            self.send_json({"presence": core_overlay(self.server.db_path, current_presence(self.server.presence, always_alive=tuple(a for a in self.server.handlers if handler_alive(a)), evidence={a: handler_evidence(a) for a in self.server.handlers}))})
            return
        if parsed.path == "/api/delivery":   # 実体の美月(Claudeアプリの別チャット)の既読。橋(kagaribi_mizuki)が、美月の `>> read=#番号` の合図を記録した値
            try:
                with open(MIZUKI_READ_STATE, encoding="utf-8") as f:
                    rs = json.load(f)
            except (OSError, ValueError):
                rs = {}
            tail = tail_messages(self.server.db_path, 1)
            latest = tail[0]["id"] if tail else 0
            read_id = int(rs.get("id", 0))
            self.send_json({"mizuki": {"readId": read_id, "at": rs.get("at", ""), "latestId": latest, "unread": max(0, latest - read_id) if read_id else None}})
            return
        if parsed.path == "/api/route":
            self.send_json({"route": read_route(), "label": ROUTES[read_route()]})
            return
        if parsed.path == "/api/mode":
            self.send_json(read_mode() or {"mode": "watch", "heat": None, "legacy": True})
            return
        if parsed.path == "/api/chain":
            recent = tail_messages(self.server.db_path, 200)
            chain = apply_control(apply_mode(chain_status(recent), read_mode()), recent)
            try: clean_waiting_tasks(self.server.db_path)
            except Exception as e: print("許可待ちの自動整理エラー:", e, flush=True)
            waiting = [x for x in list_tasks(self.server.db_path, open_only=True) if x.get("status") in ("pending_ack", "accepted")]
            if len(waiting) >= PERMISSION_BACKLOG_LIMIT and chain.get("allowed"):   # 許可待ちが溜まったら、AI同士の会話を止める(潤の発言・許可で再開)
                chain = {**chain, "allowed": False, "reason": "permission_backlog", "waiting": len(waiting)}
            self.send_json(chain)
            return
        if parsed.path == "/api/names":   # 台帳(kagaribi_registry.json)から作る名前表。部品はこれを読んで自前の表と照合する(2026-11-02)
            self.send_json(registry_names())
            return
        if parsed.path == "/api/audit":
            q = parse_qs(parsed.query)
            self.send_json({"audit": read_audit(int(q.get("limit", ["100"])[0]), q.get("verdict", [""])[0])})
            return
        self.send_static(parsed.path)

    def do_POST(self) -> None:
        if urlparse(self.path).path in ("/api/tasks", "/api/tasks/update", "/api/tasks/approve"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4_096:
                    raise ValueError("request body must be between 1 and 4096 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                if self.path.endswith("/update"):
                    upd = update_task(self.server.db_path, int(payload.get("id", 0)), str(payload.get("status")), payload.get("note"))
                    maybe_auto_approve(self.server, upd)
                    self.send_json({"task": upd})
                elif self.path.endswith("/approve"):
                    task = approve_task(self.server.db_path, int(payload.get("id", 0)), payload.get("note"))
                    fire_task_worker(self.server, task["id"], "work")
                    ack_approval(self.server, task, "潤が画面のボタンで許可した")
                    self.send_json({"task": task})
                else:
                    task, created = create_task(self.server.db_path, payload)
                    if created and not approve_if_jun_origin(self.server, task):
                        fire_task_worker(self.server, task["id"], "accept")    # 登録された瞬間だけ、本体ワーカーを1回起動(受付と内容確認まで)。潤の発言が原因なら、上の自動許可で作業まで進む
                    if created:      # 自動許可のあとの最新の状態を返す(古い『引き継ぎ待ち』をゴーストが書かないように)
                        fresh = [t for t in list_tasks(self.server.db_path) if t["id"] == task["id"]]
                        task = fresh[0] if fresh else task
                    self.send_json({"task": task, "created": created}, HTTPStatus.CREATED if created else HTTPStatus.OK)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if urlparse(self.path).path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > SHARED_IMAGE_MAX:
                    raise ValueError("画像は12MBまで")
                data = self.rfile.read(length)
                name = parse_qs(urlparse(self.path).query).get("name", ["image"])[0]
                self.send_json(save_shared_image(data, self.headers.get("Content-Type", ""), name), HTTPStatus.CREATED)
            except (UnicodeDecodeError, ValueError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if urlparse(self.path).path == "/api/audit":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 2048:
                    raise ValueError("request body must be between 1 and 2048 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                self.send_json(write_audit(payload), HTTPStatus.CREATED)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if urlparse(self.path).path == "/api/chain":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 512:
                    raise ValueError("request body must be between 1 and 512 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                self.send_json(write_control(payload))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if urlparse(self.path).path == "/api/route":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 512:
                    raise ValueError("request body must be between 1 and 512 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                self.send_json(write_route(payload))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if urlparse(self.path).path == "/api/mode":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 512:
                    raise ValueError("request body must be between 1 and 512 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                self.send_json(write_mode(payload))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if urlparse(self.path).path == "/api/presence":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_024:
                    raise ValueError("request body must be between 1 and 1024 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                entry = set_presence(self.server.presence, payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"presence": entry}, HTTPStatus.CREATED)
            return
        if urlparse(self.path).path == "/api/album":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16_384:
                    raise ValueError("request body must be between 1 and 16384 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = album_apply(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"entry": result}, HTTPStatus.CREATED)
            return
        if urlparse(self.path).path == "/api/tsubo":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16_384:
                    raise ValueError("request body must be between 1 and 16384 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                tsubo_add(str(payload.get("text", "")))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"ok": True}, HTTPStatus.CREATED)
            return
        if urlparse(self.path).path != "/api/messages":
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                raise ValueError("request body must be between 1 and 16384 bytes")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            message = create_message(self.server.db_path, payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if message["sender"] == "jun":                      # 潤の『T3 許可』は、画面のボタンと同じ扱い
            reply_body = None
            if message.get("replyTo"):
                replied = [m for m in list_messages(self.server.db_path, message["replyTo"] - 1, 1) if m["id"] == message["replyTo"]]
                reply_body = replied[0]["body"] if replied else None
            for task_id in approval_targets(message, list_tasks(self.server.db_path, open_only=True), reply_body):
                try:
                    approved = approve_task(self.server.db_path, task_id, "潤が許可(発言#%s)" % message["id"])
                    fire_task_worker(self.server, task_id, "work")
                    ack_approval(self.server, approved, "潤が発言#%s で許可した" % message["id"])
                except ValueError as error:
                    print(f"[許可] T{task_id}: {error}")
        if message["sender"] == "jun" and SHIAN_SIGNAL.search(message.get("body") or ""):
            fire_shian(self.server, message["id"])
        if self.server.handlers:
            fire_handlers(self.server)
        self.send_json({"message": message}, HTTPStatus.CREATED)

    def send_static(self, request_path: str) -> None:
        if request_path == "/glossary":   # 用語集: 共通メモ側の GLOSSARY.html をそのまま出す(常に最新)
            gp = Path("E:/StockResearch/preview/kagaribi_shared_memos/GLOSSARY.html")
            if not gp.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = gp.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        relative = "index.html" if request_path in ("", "/") else ("album.html" if request_path == "/album" else request_path.lstrip("/"))
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format_string: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="篝火 v0.1 localhost server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--poc-state", type=Path, default=DEFAULT_POC_STATE)
    parser.add_argument("--handlers", default="", help="発火式で起動するAI(例: mizuki,yoichi)。空なら使わない")
    parser.add_argument("--resident-script", type=Path, default=Path(r"E:\StockResearch\preview\kagaribi_resident\kagaribi_resident.py"))
    args = parser.parse_args()
    handlers = tuple(h for h in args.handlers.split(",") if h)
    server = KagaribiServer(("127.0.0.1", args.port), KagaribiHandler, args.db, args.poc_state, handlers, args.resident_script, f"http://127.0.0.1:{args.port}")
    print(f"篝火 v0.1: http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
