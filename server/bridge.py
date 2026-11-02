"""Minimal CLI bridge between an AI session and the Kagaribi shared log."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AGENTS = ("asahi", "hinata", "mizuki", "yoichi")
LEGACY_NAMES = {"jun": "潤", "asahi": "朝日", "hinata": "日向", "mizuki": "美月", "yoichi": "夜一"}


def configure_stdout(stream=None) -> None:
    """Make JSON output portable on Windows consoles that default to cp932."""
    output = stream if stream is not None else sys.stdout
    reconfigure = getattr(output, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="strict")


def request_json(base_url: str, path: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(base_url.rstrip("/") + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Kagaribi HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise SystemExit(f"Kagaribi connection failed: {error.reason}") from error


def read_messages(base_url: str, agent: str, after: int, limit: int, *, inbox_only: bool = False) -> list[dict]:
    query = urlencode({"after": after, "limit": limit})
    messages = request_json(base_url, f"/api/messages?{query}")["messages"]
    for message in messages:
        message["sender"] = {value: key for key, value in LEGACY_NAMES.items()}.get(message.get("sender"), message.get("sender"))
        message["target"] = {value: key for key, value in LEGACY_NAMES.items()}.get(message.get("target"), message.get("target"))
    if inbox_only:
        return [message for message in messages if message.get("target") in (None, agent)]
    return messages


def post_message(base_url: str, payload: dict) -> dict:
    try:
        return request_json(base_url, "/api/messages", payload)
    except SystemExit as error:
        # A running pre-bridge v0.1 process accepts display names and source=api.
        # This fallback keeps it usable until that local process is restarted.
        if "HTTP 400" not in str(error):
            raise
        legacy = dict(payload)
        legacy["sender"] = LEGACY_NAMES[payload["sender"]]
        legacy["target"] = LEGACY_NAMES.get(payload.get("target"))
        legacy["source"] = "api"
        return request_json(base_url, "/api/messages", legacy)


def main() -> None:
    configure_stdout()
    parser = argparse.ArgumentParser(description="Read/write the Kagaribi log from a separate AI session")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    subparsers = parser.add_subparsers(dest="command", required=True)

    read = subparsers.add_parser("read", help="read this agent's inbox")
    read.add_argument("--agent", required=True, choices=AGENTS)
    read.add_argument("--after", type=int, default=0)
    read.add_argument("--limit", type=int, default=200)
    read.add_argument("--inbox-only", action="store_true", help="only broadcasts and messages addressed to this agent")

    post = subparsers.add_parser("post", help="write a message or reply")
    post.add_argument("--agent", required=True, choices=AGENTS)
    post.add_argument("--target", choices=("jun",) + AGENTS)
    post.add_argument("--reply-to", type=int)
    post.add_argument("--body", required=True)

    args = parser.parse_args()
    if args.command == "read":
        result = {"agent": args.agent, "messages": read_messages(args.url, args.agent, args.after, args.limit, inbox_only=args.inbox_only)}
    else:
        payload = {
            "sender": args.agent,
            "target": args.target,
            "replyTo": args.reply_to,
            "body": args.body,
            "source": "bridge",
        }
        result = post_message(args.url, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
