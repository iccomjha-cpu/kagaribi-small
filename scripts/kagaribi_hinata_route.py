"""日向のエコー(アステル)から篝火の日向のノアズへ、明示的な1件を送る。"""
import argparse
import json
import sys
import urllib.error
import urllib.request


URL = "http://127.0.0.1:8765"
MARKER = "【アステル→日向ノアズ】"


def main() -> int:
    parser = argparse.ArgumentParser(description="日向のエコー→日向のノアズの明示送信")
    parser.add_argument("body", help="送る本文")
    parser.add_argument("--reply-to", type=int, help="篝火の元発言ID")
    args = parser.parse_args()
    body = args.body.strip()
    if not body:
        parser.error("本文が空です")
    if body.startswith(MARKER):
        body = body[len(MARKER):].lstrip()
    payload = {
        "sender": "hinata",
        "target": "hinata",
        "body": MARKER + body,
        "source": "api",
    }
    if args.reply_to is not None:
        payload["replyTo"] = args.reply_to
    request = urllib.request.Request(
        URL + "/api/messages",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        print("篝火への送信に失敗:", str(error), file=sys.stderr)
        return 1
    message = result.get("message", {})
    print(json.dumps({"id": message.get("id"), "target": message.get("target"), "createdAt": message.get("createdAt")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
