"""固定スレッド「かがり火 日向」へ、Codex App Server(stdio・JSON-RPC)経由で1通送り、返事を受け取る試作。
使い方: python -X utf8 hinata_thread.py "送る文" [--thread ID] [--timeout 180]
会話が重なって壊れないよう、同時に1つだけ送る(ロックファイルで待つ)。モデル・権限は、スレッドの現在の設定のまま(上書きしない)。"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

FIXED_THREAD = "01a12ac2-fa15-7791-bb85-49ba3474720b"   # かがり火 日向(Codexアプリの固定スレッド)
LOCK = os.path.join(tempfile.gettempdir(), "kagaribi_hinata_thread.lock")


def find_codex() -> str:
    pats = glob.glob(os.path.join(os.path.expandvars("%LOCALAPPDATA%"), "OpenAI", "Codex", "bin", "*", "codex.exe"))
    if not pats:
        raise RuntimeError("codex.exe が見つかりません")
    return sorted(pats)[-1]


class Lock:
    def __init__(self, wait: float = 120):
        self.wait = wait
        self.fd = None

    def __enter__(self):
        end = time.time() + self.wait
        while True:
            try:
                self.fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.time() - os.path.getmtime(LOCK) > 600:      # 古いロックは捨てる
                    os.remove(LOCK)
                    continue
                if time.time() > end:
                    raise TimeoutError("日向が別の依頼で応答中のため、待ち時間を超えました")
                time.sleep(2)

    def __exit__(self, *a):
        if self.fd is not None:
            os.close(self.fd)
            try:
                os.remove(LOCK)
            except OSError:
                pass


def send_to_thread(text: str, thread_id: str = FIXED_THREAD, timeout: float = 180, log=None) -> dict:
    log = log or (lambda *a: None)
    exe = find_codex()
    proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    inbox: list[dict] = []
    cond = threading.Condition()

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with cond:
                inbox.append(msg)
                cond.notify_all()

    threading.Thread(target=reader, daemon=True).start()
    nid = [0]

    def call(method, params, wait=30):
        nid[0] += 1
        rid = nid[0]
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        end = time.time() + wait
        with cond:
            while True:
                for m in inbox:
                    if m.get("id") == rid and ("result" in m or "error" in m):
                        inbox.remove(m)
                        if "error" in m:
                            raise RuntimeError("%s: %s" % (method, json.dumps(m["error"], ensure_ascii=False)))
                        return m.get("result")
                if time.time() > end:
                    raise TimeoutError(method + " の返事がありません")
                cond.wait(0.5)

    result = {"ok": False, "reply": "", "events": []}
    try:
        call("initialize", {"clientInfo": {"name": "kagaribi-yoichi", "title": "篝火 夜一", "version": "0.1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
        proc.stdin.flush()
        res = call("thread/resume", {"threadId": thread_id, "excludeTurns": True}, wait=60)
        log("resume ok", json.dumps(res, ensure_ascii=False)[:300])
        turn = call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]}, wait=60)
        log("turn/start", json.dumps(turn, ensure_ascii=False)[:300])
        end = time.time() + timeout
        parts: list[str] = []
        with cond:
            while True:
                while inbox:
                    m = inbox.pop(0)
                    meth = m.get("method")
                    if meth:
                        result["events"].append(meth)
                        p = m.get("params") or {}
                        if "id" in m:                                   # サーバーからの依頼(承認など)は、断る
                            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported by kagaribi bridge"}}) + "\n")
                            proc.stdin.flush()
                        if meth.endswith("agentMessage/delta") or meth.endswith("/delta"):
                            d = p.get("delta")
                            if isinstance(d, str):
                                parts.append(d)
                        if meth == "item/completed":
                            it = p.get("item") or {}
                            if it.get("type") in ("agentMessage", "agent_message") and it.get("text"):
                                result["reply"] = it["text"]
                        if meth == "turn/completed":
                            result["ok"] = True
                            result["reply"] = result["reply"] or "".join(parts)
                            return result
                if time.time() > end:
                    result["reply"] = result["reply"] or "".join(parts)
                    result["error"] = "timeout"
                    return result
                cond.wait(0.5)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
        except OSError:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--thread", default=FIXED_THREAD)
    ap.add_argument("--timeout", type=float, default=180)
    a = ap.parse_args()
    with Lock():
        out = send_to_thread(a.text, a.thread, a.timeout, log=lambda *x: print("[log]", *x, file=sys.stderr))
    print(json.dumps(out, ensure_ascii=False, indent=1))
