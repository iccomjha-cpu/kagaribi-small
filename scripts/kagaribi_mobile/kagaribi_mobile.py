"""篝火のスマホ用の窓口(中継)。2026-10-30 夜一。標準ライブラリのみ。
Tailscale側のIPだけで受け、許可した操作(会話の読み書き・許可待ちの一覧と許可)だけを、PC内の篝火サーバーへ中継する。
・本体の篝火サーバー(127.0.0.1)は変更しない。公開もしない。この中継を止めれば、スマホからは何も届かない。
・合言葉(トークン)が合わない要求は、通さない。失敗が続いたら、しばらく止める。
・投稿者は常に潤に固定(スマホから他人の名前では書けない)。依頼の状態を直接書き換える口(/api/tasks/update)は通さない。
使い方: python kagaribi_mobile.py [--bind 100.x.y.z] [--port 8766] [--upstream http://127.0.0.1:8765]
"""
import argparse
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.txt")
PAGE = os.path.join(HERE, "m.html")
ICON_DIR = "C:/Users/Master_of_epic/Desktop/StockResearch/preview/kagaribi_v01/static/assets"
OUTBOX = os.path.join(HERE, "outbox")
FILE_PATH = re.compile(r"^/f/([A-Za-z0-9_.\-぀-ヿ一-鿿]{1,100}[.](pdf|html|htm|png|jpg|jpeg|gif|txt|md|csv))$")
FILE_TYPES = {"pdf": "application/pdf", "html": "text/html; charset=utf-8", "htm": "text/html; charset=utf-8", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8", "csv": "text/plain; charset=utf-8"}
PHONE_TAG = ("〔スマホモード:潤はスマホで篝火だけを見ています。①報告・質問・結果はすべて篝火に出す(全員通達。作業窓やPC側だけに書かない)。"
             "②成果物・報告書はPDFかHTMLにして E:\StockResearch\preview\kagaribi_mobile\outbox に置き、篝火に /f/ファイル名 の形で書く(スマホでタップすると開ける)。PCのパスだけは不可。〕")
ICON_PATH = re.compile(r"^/icons/(asahi|hinata|mizuki|yoichi|jun)[.]png$")
TAILSCALE = r"C:\Program Files\Tailscale\tailscale.exe"
MAX_BODY = 16384                           # 日本語は1文字3バイトなので、2000文字+JSONの余白が入る大きさ
FAIL_LIMIT, FAIL_WINDOW = 10, 300          # 5分に10回まちがえたら、その端末からの要求を5分止める


ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"       # まぎらわしい文字(i l o 0 1)を除く


def norm_token(text):
    """入力の揺れ(大文字・空白・ハイフン)をそろえる。"""
    return "".join(ch for ch in str(text or "").lower() if not ch.isspace() and ch != "-")


def load_token(path=TOKEN_FILE):
    """合言葉を読む。無ければ作る(12文字。スマホで手入力しやすい長さ。失敗が続くと止まるので、この長さで足りる)。"""
    try:
        with open(path, encoding="utf-8") as f:
            token = norm_token(f.read())
            if len(token) >= 12:
                return token
    except OSError:
        pass
    token = "".join(secrets.choice(ALPHABET) for _ in range(12))
    with open(path, "w", encoding="utf-8") as f:
        f.write("-".join(token[i:i + 4] for i in range(0, 12, 4)) + chr(10))
    return token


def allowed(method, path, query):
    """通してよい要求か。(method, path)の許可リスト。"""
    if method == "GET":
        return path in ("/api/messages", "/api/tasks", "/api/chain")
    if method == "POST":
        return path in ("/api/messages", "/api/tasks/approve", "/api/chain")
    return False


def clean_post(path, payload):
    """スマホから来た投稿を、安全な形に作り直す。投稿者は潤に固定。"""
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    if path == "/api/chain":                          # 連鎖の停止/再開だけ(上限・司会の切替はスマホからは触れない)
        return {"stop": bool(payload.get("stop"))}
    if path == "/api/tasks/approve":
        return {"id": int(payload.get("id", 0)), "note": "潤がスマホで許可"}
    body = str(payload.get("body") or "").strip()
    if not body or len(body) > 2000:
        raise ValueError("body must be 1 to 2000 characters")
    target = payload.get("target") or None
    if target is not None:                            # 複数宛先(カンマ区切り)。既知の名前だけ、重複なし
        parts = str(target).split(",")
        if not parts or any(x not in ("asahi", "hinata", "mizuki", "yoichi", "soleil", "astel", "erisia", "sirius") for x in parts) or len(set(parts)) != len(parts):
            raise ValueError("bad target")
        raise ValueError("bad target")
    if payload.get("phoneMode"):                      # スマホモード: 指示を毎回こちらで付ける(端末の改ざんに依存しない)
        body = body + chr(10) + PHONE_TAG
    reply = payload.get("replyTo")
    return {"sender": "jun", "target": target, "body": body, "replyTo": reply if isinstance(reply, int) else None, "source": "web"}


class MobileHandler(BaseHTTPRequestHandler):
    server_version = "kagaribi-mobile"

    def log_message(self, fmt, *args):            # アクセスログは出さない(合言葉や本文が残らないように)
        pass

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def client_blocked(self):
        ip = self.client_address[0]
        now = time.time()
        fails = [t for t in self.server.fails.get(ip, []) if now - t < FAIL_WINDOW]
        self.server.fails[ip] = fails
        return len(fails) >= FAIL_LIMIT

    def authorized(self):
        if self.server.identity and self.server.identity(self.client_address[0]):     # 潤のTailscaleアカウントの端末なら、合言葉なしで通す
            self.note_seen("device")
            return True
        ok = self.token_ok()
        if ok:
            self.note_seen("token")
        return ok

    def note_seen(self, how):
        """最後に通った相手を、1行だけ記録する(接続の確認用。本文・合言葉は残さない)。"""
        path = self.server.seen_file
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ip": self.client_address[0], "how": how, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "path": urlparse(self.path).path}, f, ensure_ascii=False)
        except OSError:
            pass

    def cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "kagaribi_token":
                return urllib.parse.unquote(v)
        return ""

    def token_ok(self):
        if self.client_blocked():
            return False
        given = norm_token(self.headers.get("X-Kagaribi-Token", "") or self.cookie_token())
        ok = hmac.compare_digest(given.encode("utf-8"), self.server.token.encode("utf-8"))
        if not ok:
            self.server.fails.setdefault(self.client_address[0], []).append(time.time())
        return ok

    def relay(self, method, path, query, payload=None):
        url = self.server.upstream + path + (("?" + query) if query else "")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except ValueError:
                return e.code, {"error": "upstream error"}
        except (urllib.error.URLError, OSError):
            return 502, {"error": "篝火サーバーに届きません"}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/m", "/m.html"):          # ページ自体には秘密を入れない(合言葉は、ページの中で入力する)
            try:
                with open(PAGE, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        fm = FILE_PATH.match(urllib.parse.unquote(parsed.path))
        if fm:                                                # outbox のファイルだけ(認証必須。列挙・上位フォルダは不可)
            if not self.authorized():
                self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            path = os.path.join(OUTBOX, fm.group(1))
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", FILE_TYPES[fm.group(2).lower()])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        icon = ICON_PATH.match(parsed.path)
        if icon:                                              # 丸アイコン(5枚だけ・秘密ではない)。合言葉なしで返す
            try:
                with open(os.path.join(ICON_DIR, "icon_" + icon.group(1) + ".png"), "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return
        if not allowed("GET", parsed.path, parsed.query):
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.authorized():
            self.send_json({"error": "unauthorized"}, HTTPStatus.TOO_MANY_REQUESTS if self.client_blocked() else HTTPStatus.UNAUTHORIZED)
            return
        status, payload = self.relay("GET", parsed.path, parsed.query)
        self.send_json(payload, status)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not allowed("POST", parsed.path, parsed.query):
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.authorized():
            self.send_json({"error": "unauthorized"}, HTTPStatus.TOO_MANY_REQUESTS if self.client_blocked() else HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("request body must be between 1 and %d bytes" % MAX_BODY)
            payload = clean_post(parsed.path, json.loads(self.rfile.read(length).decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/messages":                # スマホからの発言では、司会(朝日)への全体共有を必ずオンにしてから送る(選択不要・潤 11/02 09:40)
            self.relay("POST", "/api/chain", "", {"moderator": True})
        status, out = self.relay("POST", parsed.path, "", payload)
        self.send_json(out, status)


class MobileServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token, upstream, identity=None):
        self.token = token
        self.upstream = upstream.rstrip("/")
        self.fails = {}
        self.identity = identity                  # アクセス元のIPが、潤の端末か(Trueなら合言葉は不要)。Noneなら合言葉だけ
        self.seen_file = None                     # 最後に通った相手の記録先(接続の確認用)
        super().__init__(address, MobileHandler)


def whois_login(ip):
    """TailscaleにアクセスIP元を問い合わせて、その端末の持ち主のログイン名を返す(タグ付きの端末や、問い合わせ失敗は None)。"""
    try:
        r = subprocess.run([TAILSCALE, "whois", "--json", ip], capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = json.loads(r.stdout)
        if (info.get("Node") or {}).get("Tags"):
            return None
        return (info.get("UserProfile") or {}).get("LoginName") or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def make_identity(owner_login, lookup=whois_login, ttl=60):
    """アクセス元が、owner_login(潤のTailscaleアカウント)の端末かを返す関数を作る。結果は ttl 秒だけ覚える。"""
    cache = {}

    def check(ip):
        now = time.time()
        hit = cache.get(ip)
        if hit and now - hit[0] < ttl:
            return hit[1]
        ok = bool(owner_login) and lookup(ip) == owner_login
        cache[ip] = (now, ok)
        return ok
    return check


def tailscale_ip():
    try:
        out = subprocess.run([TAILSCALE, "ip", "-4"], capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip().splitlines()
        return out[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", help="受けるIP。省略するとTailscaleのIP(100.x)を自動で使う。127.0.0.1は、試験用の --loopback と一緒のときだけ")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--upstream", default="http://127.0.0.1:8765")
    ap.add_argument("--loopback", action="store_true", help="試験用: 127.0.0.1で受ける")
    ap.add_argument("--token-file", default=TOKEN_FILE)
    ap.add_argument("--no-identity", action="store_true", help="Tailscaleの端末認証を使わず、合言葉だけで受ける")
    a = ap.parse_args()
    token = load_token(a.token_file)
    bind = a.bind
    while not bind:                                    # Tailscaleがまだ立ち上がっていなければ、立ち上がるまで待つ
        bind = tailscale_ip()
        if not bind:
            time.sleep(10)
    if not (bind.startswith("100.") or (a.loopback and bind == "127.0.0.1")):
        print("安全のため、Tailscale側のIP(100.x)以外では受けません: " + bind, file=sys.stderr)
        return 2
    identity = None
    if not a.loopback and not a.no_identity:          # このPC自身のTailscaleアカウントを、持ち主として使う
        owner = whois_login(bind)
        if owner:
            identity = make_identity(owner)
            print("端末認証: %s の端末なら合言葉なしで通す" % owner, flush=True)
        else:
            print("端末認証: 持ち主を確認できないため、合言葉だけで受ける", flush=True)
    server = MobileServer((bind, a.port), token, a.upstream, identity)
    server.seen_file = os.path.join(HERE, "last_seen.json")
    print("スマホ用の窓口: http://%s:%d/m" % (bind, a.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
