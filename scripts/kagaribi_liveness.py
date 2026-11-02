"""篝火の部品の生死を見る(読み取りのみ・LLM不要)。2026-11-02 シリウス(潤の依頼 #3088)。
- component_report(): 常駐プロセス・サーバー・各常駐の最後の実行結果を点検して一覧を返す
- write_status(): 潤が見られる状態表示(kagaribi_status.html / .json)を書く
- external_alert(): 篝火の外側への通知。既存の異常の受信箱(alerts.py。2026-10-11 本人承認)に赤/情報を書く
  赤は claude_wake.py がスマホ・PCへプッシュする。篝火のサーバーが落ちていても書ける。
"""
from __future__ import annotations

import html
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
URL = "http://127.0.0.1:8765"

# 常に動いているはずの常駐: (id, 表示名, コマンドラインの正規表現)
EXPECTED = [
    ("server", "篝火サーバー", r"server\.py.*--port 8765"),
    ("echo-sirius", "エコー常駐(シリウス)", r"kagaribi_echo_resident\.py.*--agent sirius"),
    ("echo-astel", "エコー常駐(アステル)", r"kagaribi_echo_resident\.py.*--agent astel"),
    ("asahi-watch", "朝日ウォッチ", r"kagaribi_asahi_watch\.py"),
    ("mizuki-bridge", "美月の橋渡し", r"kagaribi_mizuki_bridge\.py"),
    ("mobile", "携帯版", r"kagaribi_mobile\.py"),
]
# ノアズの常駐は、発言のたびに1回動く方式。最後の実行が失敗(ok=false)かだけ見る
NOIRS_HEALTH = [("yoichi", "夜一"), ("hinata", "日向"), ("mizuki", "美月")]


def _commandlines() -> list[str]:
    cmd = "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' } | ForEach-Object { $_.CommandLine }"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd],
                             capture_output=True, text=True, encoding="utf-8", timeout=40).stdout
    except Exception:                                                    # noqa: BLE001
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def component_report() -> list[dict]:
    import re
    lines = _commandlines()
    rows: list[dict] = []
    if not lines:                                                         # 一覧が取れなかった=判定しない(誤報を避ける)
        return rows
    for cid, label, rx in EXPECTED:
        n = sum(1 for ln in lines if re.search(rx, ln))
        rows.append({"id": cid, "label": label, "ok": n > 0, "detail": "動作中" if n else "プロセスが見つからない"})
    for name, label in NOIRS_HEALTH:
        f = HERE / "kagaribi_resident" / ("health_%s.json" % name)
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            ok = bool(d.get("ok"))
            when = datetime.fromtimestamp(float(d.get("at") or 0), JST).strftime("%m/%d %H:%M")
            rows.append({"id": "noir-" + name, "label": "ノアズ常駐(%s)" % label, "ok": ok,
                         "detail": ("最後の実行 %s 正常" % when) if ok else ("最後の実行 %s が失敗: %s" % (when, str(d.get("error"))[:60]))})
        except (OSError, ValueError):
            rows.append({"id": "noir-" + name, "label": "ノアズ常駐(%s)" % label, "ok": False, "detail": "状態ファイルが読めない"})
    return rows


def server_ok() -> bool:
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=6) as r:
            return r.status == 200
    except Exception:                                                    # noqa: BLE001
        return False


def write_status(rows: list[dict], problems: list[str], server: bool) -> None:
    now = datetime.now(JST)
    overall = server and not problems and all(r["ok"] for r in rows)
    data = {"at": now.isoformat(timespec="seconds"), "overall_ok": overall, "server_ok": server, "problems": problems, "components": rows}
    (HERE / "kagaribi_status.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    cells = "".join('<tr class="%s"><td>%s</td><td>%s</td><td>%s</td></tr>' % ("ok" if r["ok"] else "ng", "●" if r["ok"] else "▲", html.escape(r["label"]), html.escape(r["detail"])) for r in rows)
    probs = "".join("<li>%s</li>" % html.escape(p) for p in problems) or "<li>なし</li>"
    page = f"""<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="300"><title>篝火の状態</title>
<style>body{{font-family:sans-serif;max-width:640px;margin:16px auto;padding:0 16px}}h1{{font-size:1.2em}}
.big{{padding:12px;border-radius:8px;font-size:1.2em;font-weight:bold;color:#fff;background:{'#1a7f37' if overall else '#b42318'}}}
table{{border-collapse:collapse;width:100%;margin-top:12px}}td{{border-bottom:1px solid #ddd;padding:6px}}tr.ng td{{background:#fde8e8}}td:first-child{{width:1.5em}}</style>
<h1>篝火の状態</h1><div class="big">{'正常に動いています' if overall else '要確認(下を見てください)'}</div>
<p>最終点検: {now.strftime('%Y-%m-%d %H:%M')}(毎時の監査が更新)</p>
<table>{cells}</table><h2 style="font-size:1em">監査の異常</h2><ul>{probs}</ul>"""
    (HERE / "kagaribi_status.html").write_text(page, encoding="utf-8")


def toast(title: str, text: str) -> bool:
    """PCの右下にWindowsのポップアップ(トースト)を即時に出す。LLM不要・無料。失敗しても何も起きない。"""
    def esc(x: str) -> str:
        return html.escape(x, quote=True).replace("'", "''")
    ps = ("[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
          "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] | Out-Null;"
          "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
          "$x.LoadXml('<toast scenario=\"reminder\"><visual><binding template=\"ToastGeneric\"><text>%s</text><text>%s</text></binding></visual></toast>');"
          "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)") % (esc(title)[:60], esc(text)[:200])
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:                                                    # noqa: BLE001
        return False


def popup(title: str, text: str, red: bool = True) -> bool:
    """画面の中央に最前面の小さな窓(お知らせ窓)を出す。Windowsの通知設定に頼らない。
    赤=OKを押すまで残る。赤以外=120秒で自動で閉じる。本体の処理は待たない(切り離して起動)。"""
    def q(x: str) -> str:
        return " ".join(x.replace("'", "''").split())
    flags = (0x30 if red else 0x40) | 0x1000                              # 警告/情報アイコン + 最前面
    ps = "$w=New-Object -ComObject WScript.Shell; $null=$w.Popup('%s',%d,'%s',%d)" % (q(text)[:300], 0 if red else 120, q(title)[:60], flags)
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=0x00000008 | 0x08000000, close_fds=True)   # DETACHED_PROCESS | CREATE_NO_WINDOW
        return True
    except Exception:                                                    # noqa: BLE001
        return False


def external_alert(level: str, text: str, key: str) -> None:
    """篝火の外の受信箱へ。key は同じ期間に1回だけにするため、呼び出し側で時刻などを含める。"""
    try:
        import sys
        sys.path.insert(0, str(HERE))
        import alerts
        alerts.add(level, text, src="kagaribi", key=key)
        if level == "red":                                                # 赤はPCのポップアップも即時に出す(潤の要望 #3108)
            popup("篝火 要確認", text.replace("【篝火】", ""), True)
        elif key.startswith("kagaribi-server-up"):
            popup("篝火 復旧", text.replace("【篝火】", ""), False)
    except Exception:                                                    # noqa: BLE001
        pass
