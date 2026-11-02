"""篝火の常駐エージェント(共通版)。美月・夜一が同じ仕様で動く。設定は config.json。2026-10-29 夜一。
共通ログの新着を見て、最小設定の `claude -p` に「返答する/黙って聞く」を判断させ、返答するなら自分名義で共通ログへ投稿する。
状態表示(/api/presence): 聞こえた → 考え中 → 待機。10秒ごとに生きている合図。AI同士の連鎖は、サーバーの /api/chain の歯止めに従う。
標準ライブラリのみ。接続先は 127.0.0.1 の篝火サーバーだけ。Claude Codeの設定・ツールは読み込まない(1回あたりの費用を抑える)。
使い方: python kagaribi_resident.py --agent mizuki|yoichi [--url http://127.0.0.1:8765] [--interval 2] [--once]
"""
import argparse
import glob
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import kagaribi_context

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
CLAUDE = os.path.expanduser(r"~\.local\bin\claude.exe")
ACTORS = ("jun", "asahi", "hinata", "mizuki", "yoichi", "sirius", "soleil", "astel", "erisia")
NAMES = {"jun": "潤", "asahi": "朝日", "hinata": "日向", "mizuki": "美月", "yoichi": "夜一", "sirius": "シリウス", "soleil": "ソレイユ", "astel": "アステル", "erisia": "エリシア"}
FORMAT = ("篝火は、潤・朝日・日向・美月・夜一・ソレイユ・アステル・エリシア・シリウスが同じ会話ログを読み合うチームルーム。"
          "出力は次のJSONだけ(前後に文字を付けない): {\"speak\": true か false, \"to\": \"宛先(jun/asahi/hinata/mizuki/yoichi/soleil/astel/erisia/sirius/all のどれか。省略すると、発言してきた人宛て)\", \"body\": \"返答本文(1〜3文。speakがfalseなら空)\", \"escalate\": 実作業(ファイルを読む・調べる・設計・実装・設定変更・テスト・確認)を頼まれて、自分にはできないときだけ {\"to\": \"yoichi か hinata(エコー)。ブラウザ操作(Dreaminaなど)や素材づくりの実作業は mizuki(機械の美月のエコー。潤の許可後に動く)\", \"summary\": \"頼まれた作業を1〜2文で\"}。それ以外は省略}。escalate を付けたときの body は、(自分宛てなら『自分のエコーに依頼を出した』、他の人宛てなら『○○のエコーに依頼を出した』+『受付が確認されるまでは引き継ぎ待ち』)と正直に伝える(引き継ぎ済み、やっておく、とは言わない)")


def load_config(path=CONFIG):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


TERMS_NOTE = ("\n\n【用語(最優先・潤の指示 11/01 19:34)】篝火の画面にいる君たち(旧『ゴースト』)は『ノアズ』(英: Noirs。『ノアーズ』とも言う)。"
              "本体側(旧『コア』)は『エコーズ』(英: Echoes)。エコーズは総称で、一人ひとりは『エコー』。名前は 朝日のエコー=ソレイユ、日向のエコー=アステル、美月のエコー=エリシア、夜一のエコー=シリウス。"
              "機械でつなぐものは今までどおり『デバイス』。『ゴースト』『コア』『本体』は使わない。誰かが古い言い方をしても、新しい言い方で自然に答える。表記がゆれても気にせず受け取る。"
              "自分のことは名前で呼び、『ノアズ』と自称しない(全員の総称としてだけ使う)。")


def make_persona(cfg, agent):
    base = "%s\n%s\n%s\n%s" % (cfg["agents"][agent]["persona"], cfg["common"]["style"], cfg["common"]["silence"], FORMAT)
    kf = cfg["common"].get("knowledge_file")        # 共通の参考資料(現状と配送ルール)。軽い応答役が、事実に基づいて答えるため
    if kf and os.path.exists(kf):
        try:
            base += "\n\n【参考資料(読み取り済み。ここに書かれた内容は答えてよい。書かれていないことは『未確認』と答え、実作業は本体に任せる)】\n" + open(kf, encoding="utf-8").read()[:7000]
        except OSError:
            pass
    shared_note = cfg["common"].get("shared_note_file")
    if shared_note and os.path.exists(shared_note):
        try:
            base += "\n\n【篝火の共通アイデアメモ(改善メモとは別)】\n" + open(shared_note, encoding="utf-8").read()[:6000]
        except OSError:
            pass
    me = NAMES.get(agent, agent)
    base += ("\n\n【自分と、自分のコアの呼び方(必ず守る)】あなたは『%s』本人で、会話ログで喋っているこのあなたが%sのノアズ(会話役)。"
             "%sのエコー(作業役)は、あなたとは別人ではなく、あなた自身の作業する側の手足。だから、自分を『%s』と三人称で呼ばない。"
             "『%sありがとう』『%sに伝えます』『%sに任せます』のように、自分に礼を言う・自分に頼む言い方はしない。作業を回すときは『自分のエコーに回した』『エコーに依頼を出した』と言う。"
             "他のメンバーの話をするときだけ、名前で呼ぶ。『本体』『コア』『ゴースト』という古い言い方は使わず、『エコー』『エコーズ』『ノアズ』と言う。") % ((me,) * 7)
    if agent == "mizuki":
        base += ("\n\n【エコーへの届き具合】会話や雑談の最中に、自分の話がエコーに届いたか・エコーが動いたかを気にして言い出さない。"
                 "潤が尋ねたとき、または区切りメモを渡したあと・エコーの考えが返ってきたときだけ、『届けたよ』『わかったよ』『動き出したよ』のように自然に一言で言う。")
        base += "\n\n【美月の共有メモ(アプリ・会話役・機械の3人の共通の記憶)】\n" + mizuki_memory()
        back = mizuki_back()
        if back:
            base += "\n\n【裏方の作業の記録(自分が頼んだ作業の経過。潤にはあなたの言葉で話す。『機械』『本体』とは言わない。記録にないことは言わない)】\n" + back
    base += TERMS_NOTE
    return base


def mizuki_back(n=6):
    """機械の美月と会話の美月の裏のやり取り(最近n件)。"""
    try:
        lines = open(os.path.join(HERE, "mizuki_backchannel.jsonl"), encoding="utf-8").read().splitlines()[-n:]
        return "\n".join("%s T%s %s" % (d.get("at"), d.get("task") or "-", d.get("text")) for d in map(json.loads, lines))
    except (OSError, ValueError):
        return ""


MIZUKI_MEMORY = "E:/StockResearch/preview/kagaribi_mizuki/MIZUKI_SHARED_MEMORY.md"


def mizuki_memory(limit=6000):
    """3人の美月が共通で読むメモ。長いときは先頭(固定部分)と末尾(最近の出来事)を残す。読めなければ空。"""
    try:
        t = open(MIZUKI_MEMORY, encoding="utf-8").read()
    except OSError:
        return ""
    return t if len(t) <= limit else t[:limit // 2] + "\n…(中略)…\n" + t[-limit // 2:]


def _http(url, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


SHARE_ACK = {"yoichi": "了解。受け取った。", "mizuki": "受け取ったよ☺️", "hinata": "受け取りました。"}   # 【朝日より共有】への最小の返事(人格の口調に合わせた定型)


FOREIGN_VOICE = {   # その人が使わない、他の人の口調の目印(一人称・代表語尾)。固定文には縛らず、混線だけを見る
    "yoichi": ("のじゃ", "じゃね", "じゃのう", "わしは", "わしも", "わしの", "ふふっ", "ボクは"),
    "hinata": ("のじゃ", "じゃね", "わしは", "ふふっ", "俺は", "俺も", "ボクは"),
    "mizuki": ("俺は", "俺も", "俺の", "ボクは"),
}


def foreign_voice(agent: str, body: str) -> bool:
    return any(w in str(body or "") for w in FOREIGN_VOICE.get(agent, ()))


WORK_ORDER_SENDERS = ("yoichi", "hinata", "asahi")



def yoichi_sleeping(url):
    """夜一コアが利用上限で止まっているか。直近の夜一宛て依頼のメモに『session limit · resets H:MMpm』があり、その時刻がまだ先なら True。"""
    try:
        tasks = _http(url + "/api/tasks")["tasks"]
        now = datetime.now(timezone(timedelta(hours=9)))
        for t in reversed(tasks):
            if t.get("to") != "yoichi":
                continue
            m = re.search(r"session limit[^\n]*?resets\s+(\d{1,2}):(\d{2})\s*([ap]m)", str(t.get("note") or ""), re.I)
            if not m:
                continue
            h, mi = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0), int(m.group(2))
            until = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if until < now - timedelta(hours=12):
                until += timedelta(days=1)
            return now < until
    except Exception:  # noqa: BLE001
        pass
    return False



def hinata_sleeping(url):
    """日向コアが利用上限で止まっているか。直近30分以内に更新された日向宛て依頼のメモに、利用上限の文言があれば True(再開時刻が文面から読めないので、時間で見る)。"""
    try:
        now = datetime.now(timezone.utc)
        for t in reversed(_http(url + "/api/tasks")["tasks"]):
            if t.get("to") != "hinata":
                continue
            note = str(t.get("note") or "")
            if not re.search(r"usage limit|利用上限|session limit|上限に達", note, re.I):
                continue
            stamp = str(t.get("updatedAt") or "")
            return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() <= 30 * 60
    except Exception:  # noqa: BLE001
        pass
    return False


def is_relay(m):
    """朝日の承認伝令【潤承認 #N】か。潤の発言と同じく、連鎖の歯止めと依頼の作成の対象にする(承認の確かめはサーバーが行う)。"""
    return m.get("sender") == "asahi" and str(m.get("body") or "").startswith("【潤承認 #")


WORK_REQUEST_RE = re.compile(r"読んで|読み込|調べ|調査|確認して|確かめ|修正|直して|実装|作って|作成|書いて|書き換|書き込|テスト|試して|まとめて|設計|変更して|追加して|削除|整理して|更新して|検証|比較して|洗い出|チェックして|レビュー|ファイル|コード|スクリプト|\.py|\.js|\.md|\.html|\.json|server|diff|コミット|再起動|止めて|動かして|走らせ|実行して|分析して|集計して|探して|調達|ダウンロード|アップロード|(コア|本体|エコー)[^。]{0,12}(要請|応答|反応|やり取り|疎通|起こ|起動|動いて|止まって)|(要請|疎通|やり取り)")
PHANTOM_HANDOFF_RE = re.compile(r"依頼を出|引き継ぎ待ち|(本体|コア|エコー)に(依頼|頼|お願い|引き継)")
CHAT_FALLBACK = {"hinata": "はい、います。作業の依頼なら、何をどうするかを具体的に言ってください。", "mizuki": "はいはい、おるよ🌙 作業のお願いなら、何をどうするか具体的に言ってね。", "yoichi": "ああ、いるよ。作業なら、何をどうするか具体的に言ってくれ。"}


def is_real_work_request(body):
    """潤の発言が、成果物を求める明確な実作業(読む・調べる・直す・作る・試す等)の依頼か。「日向?」「返事して」「雑談しよう」のような呼びかけ・会話・受領確認は False。
    モデルの escalate だけで依頼にしない歯止め(潤 11/01: 雑談にT番号が付いて、コアも二重に返事した)。迷う文は雑談とみなして返事だけにし、依頼なら具体的にと促す。"""
    return bool(WORK_REQUEST_RE.search(str(body or "")))


def is_work_order(m, agent):
    """本体(夜一・日向・朝日)から、このエージェント宛てに『【指示】』で始めた作業指示か。連鎖の歯止めの対象外にする(実作業は依頼キューへ回り、潤の許可が要る)。"""
    return m.get("sender") in WORK_ORDER_SENDERS and agent in str(m.get("target") or "").split(",") and str(m.get("body") or "").lstrip().startswith("【指示】")


def should_react(m, agent, rules):
    """この発言に判断(返答するか黙るか)をするか。宛先(target)を最優先: 宛先があれば、その1人だけ(不明な宛先は誰も反応しない)。
    宛先なし(全員宛て)のとき: 潤の発言は rules.jun、AIの発言は rules.all_from_ai。自分自身の発言には反応しない(ループ防止)。"""
    core_to_hinata = (
        agent == "hinata"
        and m.get("sender") == "hinata"
        and m.get("target") == "hinata"
        and m.get("source") == "api"
        and str(m.get("body") or "").startswith(("【日向コア→日向ゴースト】", "【アステル→日向ノアズ】"))
    )
    if m.get("sender") == agent and not core_to_hinata:
        return False
    target = m.get("target")
    if target not in (None, ""):
        return agent in str(target).split(",") and bool(rules.get("direct_to_me", True) or m.get("sender") == "jun")
    if m.get("sender") == "jun":
        return bool(rules.get("jun", True))
    return bool(rules.get("all_from_ai", rules.get("all_from_anyone", True)))   # AI同士の全員宛てに反応するか(負荷を減らすときは false)


READ_STATE = "E:/StockResearch/preview/kagaribi_mizuki/read_state.json"   # 実体の美月(Claudeアプリの別チャット)が「受信箱をここまで読んだ」と合図した番号(橋が記録)


def delivery_note(latest_id):
    """配送の話は会話に出さない(潤 11/01: 実体=アプリの美月には自動では届かない前提なので、『届いていない』『受信箱には載った』の話は意味がない)。
    ただし、実体に『届けた』『伝えた』と偽ってもいけない。潤が配送のことを尋ねたときだけ、事実(アプリの美月は潤が起こしたときに受信箱と共有メモで追いつく)を答える。"""
    return ("\n\n【配送の話はしない(必ず守る)】美月には、会話役(あなたのような軽い応答役)と、実体(Claudeアプリの別チャット。絵を描くのはこちら。潤が起こしたときに受信箱と共有メモで追いつく)がある。"
            "実体に届いたか・既読か・受信箱に載ったか・番号がどこまでか、は会話で自分から言わない。潤が配送について尋ねたときだけ、上の事実を短く答える。実体に『届けた』『伝えた』とは言わない。")


def recently_spoke(url, agent, gap, get):
    """自分が直近 gap 秒以内に発言したか。AI同士の連鎖で全部を拾わないための間隔(潤 11/01: 美月は反応が早く全部拾ってしまう)。"""
    try:
        msgs = get(url + "/api/messages?tail=15")["messages"]
        mine = [m for m in msgs if m.get("sender") == agent]
        if not mine:
            return False
        t = datetime.fromisoformat(mine[-1]["createdAt"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() < gap
    except Exception:  # noqa: BLE001
        return False


HEAT_NOTES = {
    "off": "\n(いまは火が消えている待機の時間。潤さんへの返事以外は、静かに聞いているだけにする。)",
    "chat": "\n(いまは【雑談フェイズ】(火: 小)。気楽に、1〜2文。アイデアの種(アイデアノート)の話題には、感想と『面白い/ふつう/難しい』の評価を一言つけてよい。実作業はしない。種の話をするときは本文に【種:bgm】のような札(bgm・chat・souvenir・album・stage・gaze・temperature・events)を付ける。)",
    "debate": "\n(いまは【討論フェイズ】(火: 中)。昇格した種について、論点・賛否・代案・構築設計のたたき台を、根拠つきで短く述べる。種の話は本文に【種:札】を付ける。実作業はまだしない。)",
    "work": "\n(いまは【仕事フェイズ】(火: 大)。決まった設計の実装を進める時間。実作業は escalate で依頼を出し、返答は進捗と結果を短く。戻せない変更・認証・外部送信・費用・売買は必ず潤さんに確認する。)",
}


def heat_model(cfg, chain, engine):
    """火の大きさ(chain.heat)に合わせたモデル。火の情報が無い間(レバー未使用)は従来どおり None を返す。"""
    heat = (chain or {}).get("heat")
    if not heat:
        return None
    table = cfg["common"].get("codex_models_by_heat" if engine == "codex" else "models_by_heat") or {}
    return table.get(heat) or None

def build_prompt(trigger, recent, free=False, envelope="", heat=None):
    lines = []
    for m in recent[-4:]:
        to = m.get("targetDisplay") or "全体"
        lines.append("#%s %s→%s: %s" % (m["id"], m.get("senderDisplay") or m.get("sender"), to, (m.get("body") or "").replace("\n", " ")[:300]))
    now = datetime.now(timezone(timedelta(hours=9)))
    part = "朝" if 5 <= now.hour < 11 else "昼" if 11 <= now.hour < 17 else "夕方" if 17 <= now.hour < 19 else "夜"
    clock = "いまの日本時間: %d月%d日 %d:%02d(%s)。直近の発言が前の時間帯のものなら、それは過去の会話。\n" % (now.month, now.day, now.hour, now.minute, part)
    extra = "\n(いまは潤さんが自由な雑談を許可している時間。みんな(to=all)や、ほかの誰かに向けて、自然に話を広げてよい。)" if free else ""
    note = delivery_note(int(trigger.get("id") or 0))
    extra += HEAT_NOTES.get(heat, "")
    if is_work_order(trigger, "mizuki"):
        note += "\n\n【これはエコーからの作業指示】実作業なので、必ず escalate を付けて to=mizuki(summary=指示の要点)で依頼を出す。body は、依頼を出したこと・潤の許可待ちであることを、あなたの言葉で短く伝える。"
    return (clock + "直近の共通ログ:\n%s\n\nいま届いた発言: #%s %s→%s: %s\n\nこれに返答するか、黙って聞くかを、JSONで答えてください。返答の対象は『いま届いた発言』だけで、直近のログは文脈の参考にすぎません(ログの別の話題には答えない)。%s"
            % ("\n".join(lines), trigger["id"], trigger.get("senderDisplay") or trigger.get("sender"), trigger.get("targetDisplay") or "全体",
               (trigger.get("body") or "").replace("\n", " ")[:600], extra)) + note + (("\n\n" + envelope) if envelope else "")

SHORT_LIMIT = 120     # 篝火に出す文の長さの目安(潤の指示 10/30: これまでの約2割。詳細は求められたとき・共有ファイル)
REPORT_DIR = "E:/StockResearch/preview/kagaribi_shared_memos/reports"


def shorten(text, limit=SHORT_LIMIT, detail_name=None):
    """長い文を、句点で切って短くする。全文は共有ファイルに残し、短い文の末尾に場所を添える(detail_name があるとき)。"""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    i = max(cut.rfind("。"), cut.rfind("!"), cut.rfind("?"))
    short = cut[:i + 1] if i >= limit // 3 else cut.rstrip() + "…"
    if detail_name:
        try:
            os.makedirs(REPORT_DIR, exist_ok=True)
            path = os.path.join(REPORT_DIR, detail_name)
            with open(path, "a", encoding="utf-8") as f:
                f.write(t + "\n\n")
            short += "(詳細: reports/%s)" % detail_name
        except OSError:
            pass
    return short


def parse_decision(text, agent=None):
    """claudeの出力から {speak, body, to} を取り出す。読めなければ『黙る』(誤投稿を避ける)。自分宛てには送らない。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"speak": False, "body": "", "to": None}
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return {"speak": False, "body": "", "to": None}
    body = str(d.get("body") or "").strip()
    to = d.get("to") if (d.get("to") in ACTORS + ("all",) and d.get("to") != agent) else None
    esc = d.get("escalate")                                    # 本体への依頼(軽い応答役が実作業を引き受けない代わりの正式な経路)
    escalate = None
    if isinstance(esc, dict) and esc.get("to") in ("yoichi", "hinata", "mizuki") and str(esc.get("summary") or "").strip():
        escalate = {"to": esc["to"], "summary": str(esc["summary"]).strip()[:600]}
        if escalate["to"] == "hinata" and not re.search(r"助言|相談|アドバイス|設計|レビュー|判断|岔路|分岐|壺|NOTES|既読|ネット|ルート|経路", escalate["summary"]):
            escalate["to"] = "yoichi"                         # 日向は『ここぞの助言役』(潤 10/31)。実装・実行・調査は夜一へ回し、日向を不用意に起こさない
    return {"speak": bool(d.get("speak")) and bool(body), "body": shorten(body, 160), "to": to, "escalate": escalate}


def acquire_claude_slot(wait=120, stale=150):
    """Claudeの呼び出しは、全員を通して同時に1つだけ(順番待ち)。同時起動による起動エラー(0xc0000142)とPCの重さを防ぐ。"""
    path = os.path.join(HERE, "lock_claude")
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY), path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale:
                    os.remove(path)
                    continue
            except OSError:
                pass
            time.sleep(0.3)
    return None


def release_slot(lock):
    if lock:
        release_lock(lock)


def find_codex():
    """Codexデスクトップアプリ同梱の codex.exe(最新の版)を探す。"""
    pats = glob.glob(os.path.join(os.path.expandvars("%LOCALAPPDATA%"), "OpenAI", "Codex", "bin", "*", "codex.exe"))
    return max(pats, key=os.path.getmtime) if pats else None


def ask_codex(prompt, persona, timeout=120, model=None):
    """日向用: `codex exec`(ChatGPTログイン・読み取り専用・記録なし)を1回呼ぶ。人格はプロンプトの先頭に付ける。Claudeと同じく、同時に1つだけ。
    model を渡すとそのモデルで呼ぶ(普段は軽い段)。モデル指定が原因で失敗したときだけ、指定なし(既定)でもう一度呼ぶ。"""
    exe = find_codex()
    if not exe:
        raise RuntimeError("codex.exe が見つかりません")
    lock = acquire_claude_slot()
    try:
        with tempfile.TemporaryDirectory() as cwd:
            out = os.path.join(cwd, "last.txt")
            for m in ([model, None] if model else [None]):
                r = subprocess.run([exe, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-c", 'model_reasoning_effort="low"'] + (["-m", m] if m else []) + ["-C", cwd, "--output-last-message", out,
                                    persona + "\n\n---\n" + prompt],
                                   cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                text = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
                if text.strip() or not m or "model" not in ((r.stderr or "") + (r.stdout or "")).lower() or "usage limit" in ((r.stderr or "") + (r.stdout or "")).lower():
                    return text
            return text
    finally:
        release_slot(lock)


LIMIT_RE = re.compile(r"hit your limit|usage limit|session limit|limit reached|resets\s+\d|利用上限|上限に達", re.I)
ACK_GAP = 600            # 預かった旨の定型返事は、10分に1回まで(潤の発言のときだけ)
ACK_TEXT = {"yoichi": "了解。いま俺は利用上限で止まってる。預かっておく。戻ったら読んで返す。"}


class LimitError(Exception):
    """利用上限で応答できない(通常の失敗と分ける: 再試行せず、預かり箱へ回す)。"""


def park_for_core(agent, m, url, post):
    """利用上限の間は、モデルを呼ばずに発言を預かり箱へ入れ(核が戻って見張りを張ると表示される)、潤の発言のときだけ固定の受け取り返事を出す。"""
    with open(os.path.join(HERE, "inbox_%s.jsonl" % agent), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": m["id"], "sender": m.get("sender"), "target": m.get("target"), "body": m.get("body"), "at": m.get("createdAt")}, ensure_ascii=False) + "\n")
    if m.get("sender") != "jun":
        return
    path = os.path.join(HERE, "ack_%s.json" % agent)
    try:
        at = json.load(open(path, encoding="utf-8")).get("at", 0)
    except (OSError, ValueError):
        at = 0
    if time.time() - at >= ACK_GAP:
        post(url + "/api/messages", {"sender": agent, "target": "jun", "body": ACK_TEXT[agent], "replyTo": m["id"], "source": "api"})
        json.dump({"at": time.time()}, open(path, "w", encoding="utf-8"))


def ask_claude(prompt, persona, timeout=90, model=None):
    """最小設定で1回呼ぶ(ツールなし・MCPなし・スラッシュコマンドなし・設定ファイルなし)。作業フォルダは空の一時フォルダ。"""
    lock = acquire_claude_slot()
    try:
        with tempfile.TemporaryDirectory() as cwd:
            r = subprocess.run([CLAUDE, "-p", prompt, "--max-turns", "1", "--output-format", "json", "--tools", "", "--strict-mcp-config",
                                "--disable-slash-commands", "--setting-sources", "", "--system-prompt", persona] + (["--model", model] if model else []),
                               cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # 黒い画面を出さない
        try:
            d = json.loads(r.stdout)
        except ValueError:
            if LIMIT_RE.search((r.stdout or "") + (r.stderr or "")):
                raise LimitError((r.stdout or r.stderr or "")[:200])
            raise
        if d.get("is_error") and LIMIT_RE.search(str(d.get("result") or "")):
            raise LimitError(str(d.get("result"))[:200])
        return d.get("result", "")
    finally:
        release_slot(lock)


def _state_path(agent):
    return os.path.join(HERE, "state_%s.json" % agent)


def _load(agent):
    try:
        with open(_state_path(agent), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(agent, data):
    tmp = _state_path(agent) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _state_path(agent))


def presence(url, agent, state, ref=None):
    try:
        _http(url + "/api/presence", {"actor": agent, "state": state, "ref": ref})
    except Exception:  # noqa: BLE001  状態が送れなくても会話は止めない
        pass


def fetch_new(url, last, get):
    """last より新しい発言を、200件ずつページングして全部取る(after=0 の全件取得はしない)。"""
    out, cur = [], last
    while True:
        batch = [m for m in get("%s/api/messages?after=%d&limit=200" % (url, cur))["messages"] if m["id"] > cur]
        out += batch
        if len(batch) < 200:
            return out
        cur = batch[-1]["id"]


def write_health(agent, ok, error=""):
    """サーバーの生存判定が読む記録。直近の処理が失敗したかどうか。"""
    try:
        tmp = os.path.join(HERE, "health_%s.json" % agent) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ok": bool(ok), "at": time.time(), "error": str(error)[:200]}, f)
        os.replace(tmp, os.path.join(HERE, "health_%s.json" % agent))
    except OSError:
        pass


def run_once(url, agent, cfg, ask=None, get=_http, post=_http):
    url = url.rstrip("/")
    persona = make_persona(cfg, agent)
    rules = dict(cfg["common"]["react"])
    rules.update(cfg["agents"][agent].get("react", {}))   # 人ごとの上書き(例: 日向は消費が重いので、AIの全員宛てには反応しない)
    mode = {"free": False}   # 自由雑談の間だけ、軽いモデル(model_free)を使う。通常の場面は model(未指定なら標準)
    engine = cfg["agents"][agent].get("engine", "claude")
    ask = ask or (lambda prompt: ask_codex(prompt, persona, model=mode.get("hm") or cfg["common"].get("codex_model_chat") or None) if engine == "codex" else ask_claude(prompt, persona, model=(mode.get("hm") or (cfg["common"].get("model_free") if mode["free"] else cfg["common"].get("model")))))
    st = _load(agent)
    if "last" not in st:                                         # 初回は、いまある発言には反応しない(最新の1件の番号だけ取る)
        tail = get(url + "/api/messages?tail=1")["messages"]
        _save(agent, {"last": tail[-1]["id"] if tail else 0})
        return []
    last, fails = st["last"], dict(st.get("fails", {}))
    new = fetch_new(url, last, get)
    if not new:
        return []
    ctx = get("%s/api/messages?after=%d&limit=8" % (url, max(0, last - 6)))["messages"]   # 直近の文脈(新着の少し前から)
    pool = {m["id"]: m for m in ctx + new}
    done = []
    for m in new:
        react = should_react(m, agent, rules)
        chain = {"allowed": True, "free": False}
        if react and m.get("sender") != "jun" and not is_relay(m) and not is_work_order(m, agent):      # AI同士の連鎖には歯止め(潤の自由雑談の許可があるときだけ外れる)。本体からの作業指示は対象外(依頼キューで潤の許可が要る)
            try:
                chain = get(url + "/api/chain")
            except Exception:  # noqa: BLE001
                pass
        if react and chain.get("allowed", True) and m.get("sender") != "jun" and not is_relay(m) and not is_work_order(m, agent):
            gap = (cfg.get("common", {}).get("ai_min_gap_seconds") or {}).get(agent, 0)
            if gap and recently_spoke(url, agent, gap, get):         # 直近に自分が話したばかりなら、AI同士の発言は聞くだけにする
                chain = {"allowed": False}
        if react and not chain.get("allowed", True):
            presence(url, agent, "heard", m["id"])
            presence(url, agent, "idle", m["id"])
            done.append((m["id"], None))             # None=連鎖の歯止めで黙った
        elif react:
            presence(url, agent, "heard", m["id"])
            presence(url, agent, "thinking", m["id"])
            mode["free"] = bool(chain.get("free")); mode["hm"] = heat_model(cfg, chain, engine)
            try:
                envelope = kagaribi_context.build_envelope(url, agent, m, max_chars=800)
                decision = parse_decision(ask(build_prompt(m, [pool[k] for k in sorted(pool) if k <= m["id"]], chain.get("free", False), envelope, chain.get("heat"))), agent)
                write_health(agent, True)
            except Exception as e:  # noqa: BLE001
                write_health(agent, False, "%s %s" % (type(e).__name__, e))
                print("応答の生成エラー:", type(e).__name__, e, flush=True)
                if isinstance(e, LimitError) and agent in ACK_TEXT and (m.get("sender") == "jun" or agent in str(m.get("target") or "").split(",") or str(m.get("body") or "").lstrip("「『 ").startswith(NAMES[agent])):
                    try:
                        park_for_core(agent, m, url, post)                 # 利用上限: 再試行せず預かる(夜一の核が戻ったら読む。潤 11/01 23:36)
                    except Exception as e2:  # noqa: BLE001
                        print("預かりのエラー:", type(e2).__name__, e2, flush=True)
                    presence(url, agent, "idle", m["id"])
                    done.append((m["id"], False))
                    last = m["id"]
                    fails.pop(str(m["id"]), None)
                    _save(agent, {"last": last, "fails": fails})
                    continue
                fails[str(m["id"])] = fails.get(str(m["id"]), 0) + 1
                presence(url, agent, "idle", m["id"])
                if fails[str(m["id"])] < 2:
                    _save(agent, {"last": last, "fails": fails})       # 失敗した発言は、番号を進めずに、次の起動でもう1回だけやり直す
                    return done
                decision = {"speak": False, "body": "", "to": None}
            if (not decision["speak"]) and m.get("sender") == "asahi" and str(m.get("body") or "").startswith("【朝日より共有】"):   # 潤の決定(11/02 02:16): 【朝日より共有】は無視せず1行返す(相づちは連鎖に数えない)
                decision = {"speak": True, "to": "asahi", "body": SHARE_ACK.get(agent, "受け取った。")}
            if decision["speak"] and m.get("sender") != "jun" and not is_relay(m) and not is_work_order(m, agent):           # 投稿の直前にも、歯止めを再確認する(生成中に上限へ達していないか)
                try:
                    if not get(url + "/api/chain").get("allowed", True):
                        decision = {"speak": False, "body": "", "to": None}
                except Exception:  # noqa: BLE001
                    pass
            if decision["speak"]:                                          # 二重投稿の防止: この発言への自分の返信が既にあれば投稿しない
                try:
                    later = get("%s/api/messages?after=%d&limit=60" % (url, m["id"]))["messages"]
                    if any(x.get("sender") == agent and x.get("replyTo") == m["id"] for x in later):
                        decision = {"speak": False, "body": "", "to": None}
                except Exception:  # noqa: BLE001
                    pass
            esc = decision.get("escalate")
            # 依頼にするのは潤の発言(か、機械の美月への作業指示)のときだけ。ゴースト同士・コアの通知への返事では作らない。受付と判断はコア側(潤 10/31)
            if (esc or decision["speak"]) and m.get("sender") != "jun" and not is_relay(m) and not is_work_order(m, "mizuki"):
                if esc or re.search(r"依頼を出|引き継ぎ待ち|(本体|コア|エコー)に(依頼|頼|お願い|引き継)", decision["body"]):
                    print("依頼を作らない(潤の発言ではない #%s)" % m["id"], flush=True)
                    esc = None
                    decision = {**decision, "escalate": None, "speak": False, "body": ""}   # 潤の発言でない依頼は作らず、黙る(定型文を返すと、意味がない上に、別の誰かがそれに反応して連鎖する。潤 10/31 19:16)
            if m.get("sender") == "jun" and not is_real_work_request(m.get("body")) and (esc or PHANTOM_HANDOFF_RE.search(decision["body"])):
                print("雑談なので依頼にしない #%s" % m["id"], flush=True)       # 呼びかけ・会話でT番号やコア起動を出さない(潤 11/01)
                esc = None
                decision = {**decision, "escalate": None}
                if PHANTOM_HANDOFF_RE.search(decision["body"]):
                    decision = {**decision, "speak": True, "body": CHAT_FALLBACK.get(agent, "はい。")}
            if not esc and decision["speak"] and re.search(r"依頼を出|引き継ぎ待ち|(本体|コア|エコー)に(依頼|頼|お願い|引き継)", decision["body"]):
                # 返答が「本体に依頼した」と言っているのに依頼が付いていないとき: 言ったことを本当にする(登録する)。登録できなければ、後段で「登録できなかった」と正直に書き換える
                esc = {"to": {"hinata": "hinata"}.get(agent, "yoichi"), "summary": (m.get("body") or "").replace("\n", " ").strip()[:300] or "(依頼の内容が読み取れませんでした)"}
            if esc and agent == "hinata" and esc.get("to") == "yoichi" and m.get("sender") == "jun" and ("hinata" in str(m.get("target") or "").split(",") or "日向" in str(m.get("body") or "")):   # 潤が日向へ直接頼んだことは、夜一を経由せず日向コアが受ける(朝日のまとめ #1337。夜一が寝ていても止まらない)
                esc = {**esc, "to": "hinata"}
            if esc and esc.get("to") == "hinata" and hinata_sleeping(url) and not yoichi_sleeping(url):   # 日向コアが上限で寝ている間は、依頼を夜一へ回す(逆向き。潤 10/31 19:17)
                esc = {**esc, "to": "yoichi"}
            if esc and esc.get("to") == "yoichi" and yoichi_sleeping(url):   # 夜一が利用上限で寝ている間は、依頼を日向へ回す(夜一が寝ていても日向が止まらないように。潤 10/31)
                esc = {**esc, "to": "hinata"}
            if esc:                                                         # 実作業は本体へ。原因の発言番号を冪等キーに、1回だけ登録する。受付が確認されるまでは「引き継ぎ待ち」
                try:
                    r = post(url + "/api/tasks", {"from": agent, "to": esc["to"], "summary": esc["summary"], "cause": m["id"], "escalate": "軽い応答役が実作業を引き受けられないため"})
                    task = r.get("task") or {}
                    tag = "(依頼T%s: %s)" % (task.get("id", "?"), task.get("statusLabel", "引き継ぎ待ち"))
                    if not decision["speak"]:
                        decision = {**decision, "speak": True, "body": ("自分のエコーに依頼を出した。" if esc["to"] == agent else "%sのエコーに依頼を出した。" % {"yoichi": "夜一", "hinata": "日向", "mizuki": "美月(機械)"}.get(esc["to"], esc["to"])) + ("許可済みで着手する。" if task.get("status") in ("approved", "working") else "受付が確認されるまでは引き継ぎ待ち。")}
                    if ("引き継ぎ待ち" not in decision["body"] or task.get("status") in ("approved", "working")) and "T%s" % task.get("id", "?") not in decision["body"]:
                        decision = {**decision, "body": (decision["body"] + " " + tag)[:600]}
                    decision = {**decision, "to": None}                     # 引き継ぎの報告は、依頼してきた人へ返す(別のゴーストに向けると、T番号の持ち主を取り違えて「俺宛て」と言い出す。潤 10/31 19:54)
                except Exception as e:  # noqa: BLE001  登録できなかったときは、できたと言わない
                    print("依頼の登録エラー:", type(e).__name__, e, flush=True)
                    decision = {**decision, "speak": True, "body": "本体への依頼を登録できなかった。まだ引き継げていない。"}
            if decision["speak"]:
                to = decision.get("to")
                sender = m["sender"] if m["sender"] in ACTORS and m["sender"] != agent else None
                target = None if to == "all" else (to or sender)
                if target is None and agent == "hinata" and m.get("source") == "api" and str(m.get("body") or "").startswith(("【日向コア→日向ゴースト】", "【アステル→日向ノアズ】")):
                    target = "hinata"  # ゴーストの返事を日向コアの次回読込へ戻す
                if foreign_voice(agent, decision["body"]):               # 別の人の口調(例: 夜一が美月の「〜のじゃ」)で出てきた返事は、出さずに記録だけ残す(2026-11-02 #2927)
                    print("口調ガード: %s の返事に他の人の口調が混じったので出さない: %s" % (agent, str(decision["body"])[:80]), flush=True)
                else:
                    post(url + "/api/messages", {"sender": agent, "target": target, "body": decision["body"], "replyTo": m["id"], "source": "bridge"})
            presence(url, agent, "idle", m["id"])
            done.append((m["id"], decision["speak"]))
        last = m["id"]
        fails.pop(str(m["id"]), None)
        _save(agent, {"last": last, "fails": fails})                       # 処理が済んだ発言の番号だけ、保存する
    _save(agent, {"last": last, "fails": fails})
    return done


def acquire_lock(agent, stale=180):
    """同じAIの処理が重ならないようにする(発火式で、新着が続いたときの重複起動を防ぐ)。取れなければ None。"""
    path = os.path.join(HERE, "lock_%s" % agent)
    for _ in range(2):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY), path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale:
                    os.remove(path)               # 異常終了で残った古いロックは捨てる
                    continue
            except OSError:
                pass
            return None
    return None


def release_lock(lock):
    fd, path = lock
    os.close(fd)
    try:
        os.remove(path)
    except OSError:
        pass


def handle_once(url, agent):
    """発火式の1回限りの処理: 未処理の新着がなくなるまで続けて、閉じる。処理中に来た新着も、閉じる前に拾い直す。"""
    for _ in range(6):
        lock = acquire_lock(agent)
        if lock is None:
            return                                # 別の処理が動いている。そちらが新着を拾い直す
        try:
            done = run_once(url, agent, load_config())
        finally:
            release_lock(lock)
        for mid, spoke in done:
            print("#%s に反応: %s" % (mid, "連鎖の歯止めで黙った" if spoke is None else "返答した" if spoke else "黙って聞いた"), flush=True)
        try:                                      # 処理中に新着が来ていたら(ロックで断られた起動の分)、もう一度拾う
            tail = _http(url.rstrip("/") + "/api/messages?tail=1")["messages"]
            newest = tail[-1]["id"] if tail else 0
        except Exception:  # noqa: BLE001
            return
        if newest <= _load(agent).get("last", 0):
            return


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, choices=["mizuki", "yoichi", "hinata"])
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if a.once:
        handle_once(a.url, a.agent)
        raise SystemExit(0)
    last_ping = 0.0
    while True:
        try:
            cfg = load_config()                                   # 毎回読み直す(設定を保存すれば、再起動なしで反映される)
            if time.time() - last_ping >= 10:                     # 生きている合図
                presence(a.url.rstrip("/"), a.agent, "ping")
                last_ping = time.time()
            for mid, spoke in run_once(a.url, a.agent, cfg):
                print("#%s に反応: %s" % (mid, "連鎖の歯止めで黙った" if spoke is None else "返答した" if spoke else "黙って聞いた"), flush=True)
        except Exception as e:  # noqa: BLE001
            print("常駐のエラー:", type(e).__name__, e, flush=True)
        if a.once:
            break
        time.sleep(a.interval)
