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
}
LEGACY_ACTOR_IDS = {"潤": "jun", "朝日": "asahi", "日向": "hinata", "美月": "mizuki", "夜一": "yoichi"}
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
        "targetDisplay": ACTOR_LABELS.get(target) if target else None,
        "body": row["body"],
        "replyTo": row["reply_to"],
        "source": row["source"],
        "createdAt": row["created_at"],
    }


# --- 本体への依頼キュー: 軽い応答役が「実作業が要る」と判断したとき、原因の発言番号を冪等キーに、本体(夜一・日向)へ1回だけ登録する ---
TASK_STATUSES = ("pending_ack", "accepted", "working", "verified", "rejected")   # 受付前 / 受付済 / 作業中 / 検証済 / 却下
TASK_STATUS_LABEL = {"pending_ack": "引き継ぎ待ち(受付前)", "accepted": "受付済", "working": "作業中", "verified": "検証済", "rejected": "却下"}
TASK_TARGETS = ("yoichi", "hinata")


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
        where.append("status IN ('pending_ack', 'accepted', 'working')")
    database = connect(db_path)
    try:
        rows = database.execute("SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id ASC", args).fetchall()
    finally:
        database.close()
    return [row_to_task(r) for r in rows]


def update_task(db_path: Path, task_id: int, status: str, note: str | None = None) -> dict:
    if status not in TASK_STATUSES:
        raise ValueError("status must be one of: " + ", ".join(TASK_STATUSES))
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    database = connect(db_path)
    try:
        cur = database.execute("UPDATE tasks SET status = ?, note = COALESCE(?, note), updated_at = ? WHERE id = ?", (status, (note or None), now, task_id))
        database.commit()
        if cur.rowcount == 0:
            raise ValueError("task not found")
        return row_to_task(database.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    finally:
        database.close()


def tail_messages(db_path: Path, count: int = 1) -> list[dict]:
    """最新の count 件(古い順)。常駐が『いまの最後の番号』を取るために使う(after=0 の全件取得を避ける)。"""
    database = connect(db_path)
    try:
        rows = database.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (max(1, min(count, 50)),)).fetchall()
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
    target = normalize_actor(payload.get("target"), optional=True)
    body = str(payload.get("body", "")).strip()
    reply_to_value = payload.get("replyTo")
    source = str(payload.get("source", "api")).strip().lower()

    if not body:
        raise ValueError("body is required")
    if len(body) > 4000:
        raise ValueError("body must be 4000 characters or fewer")
    if source not in {"web", "api", "bridge", "seed"}:
        raise ValueError("source must be one of: web, api, bridge, seed")

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
    return row_to_message(row)


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


# 連鎖の歯止め(AI同士のやり取りが止まらなくなるのを防ぐ)。潤の最後の発言から、AIだけの発言が CHAIN_LIMIT 件(往復4回=8件)続いたら、AIは返さない。
# 潤が「みんなで自由に雑談して」のように言ったときだけは歯止めを外す(安全のため上限 FREE_HARD_CAP 件)。潤の次の発言(何でも)で通常に戻る。
CHAIN_LIMIT = 20   # 潤の指示(#386・2026-10-30)で一時的に8→20。元に戻すときは8
FREE_HARD_CAP = 60
FREE_PATTERN = re.compile(r"雑談|(自由|勝手|好き)に(話|会話|しゃべ|喋|セッション)")


def chain_status(messages: list[dict]) -> dict:
    run, last_jun = 0, None
    for m in reversed(messages):
        if m.get("sender") == "jun":
            last_jun = m
            break
        run += 1
    free = bool(last_jun and FREE_PATTERN.search(last_jun.get("body") or ""))
    limit = FREE_HARD_CAP if free else CHAIN_LIMIT
    return {"aiRun": run, "limit": limit, "free": free, "allowed": run < limit}


def fire_handlers(server) -> None:
    """発火式: 新着を受けたら、各AIの1回限りの処理(kagaribi_resident.py --once)を起動する。処理が終われば閉じる。重複起動は、処理側のロックで防ぐ。"""
    for agent in server.handlers:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 画面を作らない。DETACHED だと、子の claude.exe が黒い画面を勝手に作ってしまう
            subprocess.Popen([sys.executable, str(server.resident_script), "--agent", agent, "--once", "--url", server.public_url],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        except Exception as error:  # noqa: BLE001  起動できなくても、会話の保存は止めない
            print(f"[発火式] {agent} の起動に失敗: {error}")


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
        if parsed.path == "/api/messages":
            query = parse_qs(parsed.query)
            if "tail" in query:
                try:
                    self.send_json({"messages": tail_messages(self.server.db_path, int(query["tail"][0]))})
                except ValueError:
                    self.send_json({"error": "tail must be an integer"}, HTTPStatus.BAD_REQUEST)
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
            self.send_json({"presence": current_presence(self.server.presence, always_alive=tuple(a for a in self.server.handlers if handler_alive(a)), evidence={a: handler_evidence(a) for a in self.server.handlers})})
            return
        if parsed.path == "/api/chain":
            self.send_json(chain_status(list_messages(self.server.db_path, 0, 500)[-200:]))
            return
        self.send_static(parsed.path)

    def do_POST(self) -> None:
        if urlparse(self.path).path in ("/api/tasks", "/api/tasks/update"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4_096:
                    raise ValueError("request body must be between 1 and 4096 bytes")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                if self.path.endswith("/update"):
                    self.send_json({"task": update_task(self.server.db_path, int(payload.get("id", 0)), str(payload.get("status")), payload.get("note"))})
                else:
                    task, created = create_task(self.server.db_path, payload)
                    self.send_json({"task": task, "created": created}, HTTPStatus.CREATED if created else HTTPStatus.OK)
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
        if self.server.handlers:
            fire_handlers(self.server)
        self.send_json({"message": message}, HTTPStatus.CREATED)

    def send_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
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
