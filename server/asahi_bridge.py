"""Read-first local bridge and meeting envelope for 朝日 (normal ChatGPT).

This process never contacts ChatGPT by itself.  It produces bounded JSON that a
Codex chat can deliver to the authorized ChatGPT conversation, and it accepts an
explicit reply for posting as ``asahi`` to the existing Kagaribi API.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from bridge import post_message, read_messages


ROOT = Path(__file__).resolve().parent
DEFAULT_ROOTS = {
    "desktop": Path(r"C:\Users\Master_of_epic\Desktop\StockResearch"),
    "primary": Path(r"E:\StockResearch"),
}
ALLOWED_SUFFIXES = {
    ".md", ".txt", ".json", ".jsonl", ".py", ".js", ".css", ".html",
    ".ps1", ".yaml", ".yml", ".toml", ".csv", ".tsv",
}
DENIED_PARTS = {".git", ".claude", ".codex", "secrets", "credentials", "tokens", "private_keys"}
DENIED_NAME_FRAGMENTS = (
    "secret", "credential", "password", "passwd", "token", "api_key", "apikey", "private_key",
    "rakuten_data", "holdings_snapshot", "portfolio_state", "orders", "account_balance",
)
MAX_BYTES = 512 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SafeFileReader:
    def __init__(self, roots: dict[str, Path] | None = None, audit_path: Path | None = None):
        self.roots = {name: Path(path).resolve() for name, path in (roots or DEFAULT_ROOTS).items()}
        self.audit_path = Path(audit_path or ROOT / "asahi_bridge_audit.jsonl")

    def _audit(self, *, root: str, relative: str, outcome: str, size: int | None = None) -> None:
        record = {"ts": utc_now(), "operation": "read", "root": root, "path": relative, "outcome": outcome}
        if size is not None:
            record["bytes"] = size
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def read(self, root_name: str, relative: str) -> dict:
        if root_name not in self.roots:
            raise ValueError("unknown root")
        rel = Path(relative)
        if rel.is_absolute() or not relative or any(part in ("", ".", "..") for part in rel.parts):
            raise ValueError("path must be a non-empty relative path without traversal")
        lowered = [part.lower() for part in rel.parts]
        if any(part in DENIED_PARTS for part in lowered) or any(fragment in rel.name.lower() for fragment in DENIED_NAME_FRAGMENTS):
            raise ValueError("sensitive path is not readable")
        if rel.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError("file type is not allowed")

        root = self.roots[root_name]
        candidate = (root / rel).resolve(strict=True)
        if candidate != root and root not in candidate.parents:
            raise ValueError("path escapes the allowed root")
        cursor = root
        for part in rel.parts:
            cursor = cursor / part
            attributes = getattr(cursor.stat(), "st_file_attributes", 0)
            if cursor.is_symlink() or os.path.islink(cursor) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError("links are not readable")
        if not candidate.is_file():
            raise ValueError("path is not a file")
        size = candidate.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(f"file exceeds {MAX_BYTES} bytes")
        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise ValueError("file is not UTF-8 text") from error
        self._audit(root=root_name, relative=rel.as_posix(), outcome="allowed", size=size)
        return {"root": root_name, "path": rel.as_posix(), "bytes": size, "content": content}


def meeting_export(base_url: str, after: int, limit: int) -> dict:
    messages = read_messages(base_url, "asahi", after, limit, inbox_only=False)
    return {
        "bridge": "kagaribi-asahi-v1",
        "generatedAt": utc_now(),
        "after": after,
        "latestId": max([after] + [int(item["id"]) for item in messages]),
        "messages": messages,
        "note": "通常ChatGPTへの配送はCodex側が行う。ローカルプロセス単独の直結ではない。",
    }


def main() -> None:
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    if stdout_reconfigure:
        stdout_reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description="朝日のread-firstローカル/篝火ブリッジ")
    subparsers = parser.add_subparsers(dest="command", required=True)

    read = subparsers.add_parser("read-file", help="read one allowlisted UTF-8 text file")
    read.add_argument("--root", required=True, choices=tuple(DEFAULT_ROOTS))
    read.add_argument("--path", required=True)

    export = subparsers.add_parser("meeting-export", help="export the shared meeting log for delivery to ChatGPT")
    export.add_argument("--url", default="http://127.0.0.1:8765")
    export.add_argument("--after", type=int, default=0)
    export.add_argument("--limit", type=int, default=200)

    reply = subparsers.add_parser("meeting-reply", help="post an explicit ChatGPT reply as asahi")
    reply.add_argument("--url", default="http://127.0.0.1:8765")
    reply.add_argument("--target", choices=("jun", "hinata", "mizuki", "yoichi"))
    reply.add_argument("--reply-to", type=int)
    reply.add_argument("--body", required=True)

    args = parser.parse_args()
    if args.command == "read-file":
        result = SafeFileReader().read(args.root, args.path)
    elif args.command == "meeting-export":
        result = meeting_export(args.url, args.after, args.limit)
    else:
        result = post_message(args.url, {
            "sender": "asahi", "target": args.target, "replyTo": args.reply_to,
            "body": args.body, "source": "bridge",
        })
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
