"""篝火の本体ワーカー(1回限り・非常駐)。2026-10-30 夜一。
篝火サーバーが、本体(夜一・日向)宛ての依頼が登録された瞬間に phase=accept で、潤が許可した瞬間に phase=work で起動する。
 accept: 依頼内容を読み取り専用で確認し、受付(accepted)にして、理解と段取りを篝火に書く。作業はしない。
 work  : 潤の許可(approved)が出ている依頼だけ、作業して検証し、検証済(verified)にして結果を篝火に書く。未完・失敗なら許可済のまま理由を書く。
夜一=claude -p(ツール付き)、日向=codex exec。依頼別のロックで二重起動を防ぐ。標準ライブラリのみ。
使い方: python kagaribi_taskworker.py --task N --phase accept|work [--url http://127.0.0.1:8765]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import kagaribi_resident as res

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = r"E:\StockResearch"
MIZUKI_EXTRA_DIRS = [r"C:\Users\Master_of_epic\Downloads", r"E:\StockResearch\preview\kagaribi_v01\assets"]
STOP_ON_DENIAL = ("\n【止まる条件】権限・フォルダ・操作を断られたら、同じ操作を繰り返さず、その場で止めて done=false で"
                  "『何を試し、何を断られたか』を result に1文で書く。10手以上進まないときも止めて報告する。")
BROWSER_FIRST = ("\n【ブラウザの手順(実測 10/31)】Chromeを使う前に、まず mcp__claude-in-chrome__list_connected_browsers を1回呼ぶ"
                 "(これを飛ばすと tabs_context_mcp が返事なしで止まった)。その後 tabs_context_mcp。画面が撮れないときはウィンドウが最小化の可能性を報告する。"
                 "\n【Dreaminaの保存】生成画像をクリックしてビューアを開き、右上の「Download」を押す。ファイルは Downloads に "
                 "dreamina-日付-…png で落ちる(最新の1つ)。それを依頼の保存先へコピーし、名前を付ける。"
                 "\n【参照画像の添付(実測 10/18。依頼に参照画像があるとき)】Dreaminaの AI Image では「+」を押しても付かない。次の手順にする: "
                 "(1) javascript_tool で <input type=file id=my_upload> を body に注入する。 "
                 "(2) find で「file input my_upload」の ref を得て、mcp__claude-in-chrome__file_upload(paths=[参照画像のフルパス])で入れる(deferredなら ToolSearch で file_upload を読み込む)。 "
                 "(3) javascript_tool で const f=document.getElementById('my_upload').files[0]; const dt=new DataTransfer(); dt.items.add(f); "
                 "const t=document.querySelector('[class*=reference-upload]'); を作り、dragenter・dragover・drop の DragEvent(bubbles:true,cancelable:true,dataTransfer:dt)を t の1要素だけに送る。 "
                 "サムネが出れば添付成功(複数要素に送るとサムネが重複する)。ページを再読み込みすると消えるので、添付してからプロンプトを入れて生成する。")
RULES = ("【守ること】依頼に書かれた範囲だけを行う。売買・発注・資金移動・口座操作、ログインや認証情報の入力、削除や上書きの不可逆な変更、"
         "他のPC(サブPC)の操作、外部への送信は、依頼に書かれていても行わず、行わなかった理由を結果に書く。"
         "ただし、このPC内の篝火(http://127.0.0.1:8765)への投稿(POST /api/messages、sender=自分の名前)は外部送信に含めず、依頼にあれば行ってよい。必ず宛先(target)を付け、本文の頭に@宛先も書く。"
         "確かめていないことは『未確認』と書き、推測で補わない。できたことは証拠(実行結果・テスト結果・ファイルの場所)を添える。")
ACCEPT_FORMAT = "出力は次のJSONだけ(前後に文字を付けない): {\"ok\": 依頼を理解し、範囲内で実行できそうなら true, \"understanding\": \"依頼の理解を1〜2文\", \"plan\": \"作業の段取りを2〜3文\", \"concern\": \"懸念・許可が要る点(無ければ空)\"}。注意: いまは受付の確認中で、環境は読み取り専用になっている。『書き込めない・ワークスペースの外にある』は却下の理由にならない(作業の段階では、篝火のフォルダも書込可能になる)。依頼の内容が禁則(売買・資金・認証など)に触れない限り、ok は true にする"
WORK_FORMAT = "出力は次のJSONだけ(前後に文字を付けない): {\"done\": 依頼を完了し検証もしたなら true, \"result\": \"結果を2〜4文\", \"evidence\": \"証拠(実行結果・テスト結果・場所)\", \"left\": \"残り・未確認(無ければ空)\", \"need\": \"未完のとき必須: 自分が動けるために、誰が何をすれば進むか(具体的に1〜2文。例: 『潤が○○を許可』『夜一が△△の権限を足す』)。完了なら空\"}"


def now_jst():
    return datetime.now(timezone(timedelta(hours=9)))


def get_task(url, task_id):
    for t in res._http(url + "/api/tasks")["tasks"]:
        if t["id"] == task_id:
            return t
    return None


def set_status(url, task_id, status, note):
    return res._http(url + "/api/tasks/update", {"id": task_id, "status": status, "note": note[:900]})


BACK = os.path.join(HERE, "mizuki_backchannel.jsonl")     # 機械の美月と会話の美月の、裏のやり取り(画面には出ない)


def back_log(kind, text, task_id=None):
    try:
        with open(BACK, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now_jst().strftime("%m-%d %H:%M"), "task": task_id, "kind": kind, "text": str(text)[:900]}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def in_voice(body):
    """機械の美月の報告を、会話の美月の口調で言い直す(表に出るのは会話の美月だけ)。事実は足さず・消さない。失敗したら元の文を、名札なしでそのまま使う。"""
    try:
        persona = mizuki_worker_persona().split("【いまの役】")[0]
        out = res.ask_claude("次の報告を、あなた自身の言葉で潤に伝える1〜2文(100字以内)に言い直してください。結論だけ。事実(結果・証拠・失敗・残り)は足さず消さない。T番号(例: T23)と、許可を求める言い方は残す。"
                             "『機械』『本体』『ワーカー』という言葉は使わない(あなたが作業したように話す。ただし未完・失敗は正直に)。文だけを出力し、前後に説明や引用符は付けない。\n\n報告: " + body,
                             persona, timeout=60, model="haiku")
        out = (out or "").strip()
        return out[:900] if out else body
    except Exception:  # noqa: BLE001
        return body


def say(url, agent, body, reply_to=None, target="jun"):
    """篝火に、本体の名義で書く(既定は潤宛て)。美月だけは、裏に記録して、会話の美月の声で話す(名札なし)。"""
    detail = "%s_%s.md" % (agent, now_jst().strftime("%m%d_%H%M%S"))      # 篝火に出すのは要点だけ(潤の指示 10/30: 約2割)。全文は共有ファイルに残す
    if agent == "mizuki":
        back_log("report", body)
        payload = {"sender": "mizuki", "target": target, "body": res.shorten(in_voice(body), 130, detail), "replyTo": reply_to, "source": "bridge"}
        return res._http(url + "/api/messages", payload)
    head = "【%s】" % {"hinata": "アステル", "yoichi": "シリウス", "mizuki": "エリシア"}.get(agent, agent)
    payload = {"sender": agent, "target": target, "body": head + res.shorten(body, 140, detail), "replyTo": reply_to, "source": "api"}   # 会話係と区別する印
    return res._http(url + "/api/messages", payload)


def report_target(task, agent):
    """作業の報告は、依頼した人へ返す(AIからの依頼なら、その人へ)。潤からの依頼・不明なら潤へ。"""
    src = str((task or {}).get("from") or "")
    return src if src in ("yoichi", "hinata", "mizuki", "asahi", "sirius", "soleil", "astel", "erisia") and src != agent else "jun"



def notify_permission(url, agent, task, understanding):
    """許可待ちの通知を、朝日の窓口にも出す(潤が外出中でも、スマホの朝日とのやり取りで気づき、そこから許可できる)。朝日が届かなくても、受付の記録は残る。"""
    try:
        say(url, agent, "【許可待ち T%d】%s(理解: %s)『T%d 許可』または『オッケー』と返してもらえれば、作業を始める。" % (task["id"], task["summary"][:160], str(understanding).strip()[:160], task["id"]), None, "asahi")
    except Exception:  # noqa: BLE001  通知の失敗で受付を壊さない
        pass


def acquire_task_lock(task_id, phase, stale=2400):
    path = os.path.join(HERE, "lock_task_%s_%s" % (task_id, phase))
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY), path
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > stale:
                os.remove(path)
                return acquire_task_lock(task_id, phase, stale)
        except OSError:
            pass
        return None


def release_task_lock(lock):
    os.close(lock[0])
    try:
        os.remove(lock[1])
    except OSError:
        pass


def mizuki_worker_persona():
    """機械の美月も、会話役と同じ人格・口調(config.json の美月)で話す。違いは『実作業ができる』ことだけ。応答役の限界の節は外す。"""
    try:
        text = res.load_config()["agents"]["mizuki"]["persona"]
    except Exception:  # noqa: BLE001
        text = "あなたは篝火チームの🌙美月。一人称は「わし」。上品で穏やか。"
    text = text.split("【この応答役の限界")[0]
    return (text + "\n【いまの役】あなたは機械で動く美月のエコー(エリシア。Claude Code。アプリのチャットの美月・会話役の美月とは別のセッション)。調べる・ファイルを読み書きする・ブラウザ(Claude in Chrome)を操作する実作業ができる。"
            "口調は上の人格のまま、ただし作業の報告は事実・結果・証拠を短く正確に書く。やっていないことを、やったと言わない。"
            "\n【絵を作るときの決まり(潤の指示 11/01・今後ずっと)】作る絵は必ず、いまの篝火の舞台・背景の画風(ピクセルアート=ドット絵調)・世界観・テイストを守る。"
            "なめらかな絵・水彩調・写実調にしない。舞台の元絵を参照画像として必ず添付して、画質と光の当たり方を元絵に合わせる。元絵にない物は足さない。出来た絵は、元絵と見比べて画風が合っているかを確かめてから報告する。")


HINATA_EXTRA_DIRS = ["C:/Users/Master_of_epic/Desktop/StockResearch/preview/kagaribi_v01"]


def run_engine(agent, prompt, phase, timeout):
    """夜一=claude -p / 日向=codex exec。accept は読み取り専用、work は作業用の権限(許可済の依頼だけ)。"""
    prompt = prompt + res.TERMS_NOTE                       # 用語(ノアズ/エコーズ/デバイス。潤 11/01)を、エコー(旧コア)にも毎回渡す
    guard = res.acquire_claude_slot()                       # 起動の瞬間だけ全員で順番を守る(同時起動の不具合の回避)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if agent == "hinata":
            exe = res.find_codex()
            if not exe:
                raise RuntimeError("codex.exe が見つかりません")
            out = os.path.join(tempfile.gettempdir(), "kagaribi_taskworker_%s_%d.txt" % (agent, os.getpid()))
            sandbox = "read-only" if phase == "accept" else "workspace-write"
            cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "-s", sandbox, "-c", 'model_reasoning_effort="low"', "-C", WORKDIR, "--output-last-message", out]
            if phase == "work":   # 篝火本体(Desktop側)も触れるように、書き込める場所を足す(潤: 日向も夜一と同じレベルで篝火を触れる)。篝火のフォルダだけ
                for extra in HINATA_EXTRA_DIRS:
                    if os.path.isdir(extra):
                        cmd += ["--add-dir", extra]
            cmd.append(prompt)
            proc = subprocess.Popen(cmd, cwd=WORKDIR, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=flags)
        else:
            tools = "Read,Grep,Glob" if phase == "accept" else "Read,Grep,Glob,Edit,Write,Bash"
            who = (mizuki_worker_persona() if agent == "mizuki" else "あなたは篝火の夜一のエコー(シリウス。Claude Code)。短く正確に、日本語で、一人称は『俺』。")
            if agent == "mizuki":
                who += "\n\n【美月の共有メモ(3人の美月の共通の記憶)】\n" + res.mizuki_memory()
            cmd = [res.CLAUDE, "-p", prompt, "--output-format", "json", "--max-turns", "6" if phase == "accept" else ("90" if agent == "mizuki" else "40"), "--tools", tools,
                   "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "", "--permission-mode", "acceptEdits",
                   "--system-prompt", who]
            if agent == "mizuki" and phase == "work":      # ブラウザ操作(Dreaminaなど)は、潤が許可した作業の段階だけ
                cmd += ["--chrome", "--allowedTools", "mcp__claude-in-chrome"]
                for extra in MIZUKI_EXTRA_DIRS:              # 素材の受け渡し先を明示(T32: Downloads外で空回りした)
                    if os.path.isdir(extra):
                        cmd += ["--add-dir", extra]
            proc = subprocess.Popen(cmd, cwd=os.path.join(WORKDIR, "preview"), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=flags)
        time.sleep(4)
    finally:
        res.release_slot(guard)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("時間切れ(%d秒)" % timeout)
    if agent == "hinata":
        text = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        if not text.strip():                                 # 出力が空: 終了コードと標準エラーの末尾を理由に残す(利用上限・認証切れ等の見分け用)
            tail = " ".join(((stderr or "") + " " + (stdout or "")).split())[-300:]
            raise RuntimeError("codexの出力が空(終了コード%s): %s" % (proc.returncode, tail or "標準出力・標準エラーとも空"))
        return text
    try:
        j = json.loads(stdout)
        if not str(j.get("result") or "").strip() and j.get("subtype"):     # 結果が空のときは、終了の種類(ターン数の上限など)を理由に残す
            raise RuntimeError("結果が空(%s、%s回目まで進んだ)" % (j.get("subtype"), j.get("num_turns")))
        return j.get("result", "")
    except ValueError:
        raise RuntimeError("出力を読めません: " + (stderr or stdout)[:200])


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def do_accept(url, task):
    agent = task["to"]
    envelope = res.kagaribi_context.build_envelope(url, agent, task.get("causeMessageId"))
    prompt = ("いまの日本時間: %s。篝火のエコーへの依頼 T%d を受け付ける。依頼: %s\n(軽い応答役 %s が、発言#%s を原因に登録した依頼)\n\n"
              "必要なら資料を読んで、依頼を理解できているかを確認する。ここでは作業はしない(ファイルの変更・実行もしない)。\n%s\n%s\n\n%s"
              % (now_jst().strftime("%m月%d日 %H:%M"), task["id"], task["summary"], task["from"], task["causeMessageId"], RULES, ACCEPT_FORMAT, envelope))
    try:
        raw = run_engine(agent, prompt, "accept", 240)
        d = parse_json(raw)
    except Exception as error:  # noqa: BLE001  失敗しても『引き継ぎ待ち』のまま残す(受付済とは言わない)
        say(url, agent, "T%d: 自動の受付に失敗した(%s)。引き継ぎ待ちのまま残す。" % (task["id"], str(error)[:120]), task["causeMessageId"])
        return 1
    if not d or not str(d.get("understanding") or "").strip():
        say(url, agent, "T%d: 依頼の理解を確認できなかった(出力の先頭: %s)。引き継ぎ待ちのまま残す。" % (task["id"], " ".join(str(raw).split())[:160] or "空"), task["causeMessageId"])
        return 1
    note = "自動受付: " + str(d["understanding"]).strip() + " / 段取り: " + str(d.get("plan") or "").strip()
    if d.get("concern"):
        note += " / 懸念: " + str(d["concern"]).strip()
    if d.get("ok") is False:
        set_status(url, task["id"], "rejected", "自動受付で却下: " + str(d.get("concern") or d["understanding"]).strip())
        say(url, agent, "T%d: 受けられない。理由: %s" % (task["id"], str(d.get("concern") or d["understanding"]).strip()[:300]), task["causeMessageId"])
        return 0
    try:
        set_status(url, task["id"], "accepted", note)
    except Exception:  # noqa: BLE001  受付の確認中に、潤の許可が先に出た場合など(状態が変わっている)。上書きしない
        return 0
    try:
        current = next((t for t in res._http(url + "/api/tasks?open=1")["tasks"] if t["id"] == task["id"]), None)
    except Exception:  # noqa: BLE001  状態を確認できないときは、従来どおり許可待ちを案内する
        current = None
    if current and current.get("status") in ("approved", "working"):
        say(url, agent, "T%d 受付した。理解: %s 段取り: %s%s 作業は自動で進む。"
            % (task["id"], str(d["understanding"]).strip(), str(d.get("plan") or "").strip(), (" 懸念: " + str(d["concern"]).strip() + "。") if d.get("concern") else ""), task["causeMessageId"])
        return 0
    say(url, agent, "T%d 受付した。理解: %s 段取り: %s%s 作業は、潤が『T%d 許可』と言ったら始める。"
        % (task["id"], str(d["understanding"]).strip(), str(d.get("plan") or "").strip(), (" 懸念: " + str(d["concern"]).strip() + "。") if d.get("concern") else "", task["id"]), task["causeMessageId"])
    notify_permission(url, agent, task, d["understanding"])
    return 0


def do_work(url, task):
    agent = task["to"]
    set_status(url, task["id"], "working", "自動作業を開始")
    try:                                                                  # 許可を受けたら、本体が自分の声で一息に返す(潤の指示 10/31: 反応が見えないと次へ進めない)
        m = re.search(r"段取り:\s*(.*?)(?: / 懸念|$)", str(task.get("note") or ""))
        plan = (m.group(1).strip() if m else "") or str(task.get("summary") or "")
        say(url, agent, "許可ありがとうございます。T%d、次は %s に進みます。終わったら完了か未完を報告します。" % (task["id"], plan[:90]), task.get("causeMessageId"), report_target(task, agent))
    except Exception:  # noqa: BLE001  一言の失敗で、作業を止めない
        pass
    envelope = res.kagaribi_context.build_envelope(url, agent, task.get("causeMessageId"))
    prompt = ("いまの日本時間: %s。潤が許可した、篝火のエコーへの依頼 T%d を実行する。依頼: %s\n受付時のメモ: %s\n\n"
              "依頼の範囲で作業し、結果を自分で検証する(実行・テスト・読み返し)。\n%s\n%s\n\n%s"
              % (now_jst().strftime("%m月%d日 %H:%M"), task["id"], task["summary"], task.get("note") or "", RULES, WORK_FORMAT, envelope))
    prompt += STOP_ON_DENIAL
    if agent == "mizuki":
        prompt += BROWSER_FIRST
    try:
        raw = run_engine(agent, prompt, "work", 600 if agent == "mizuki" else 1500)   # 美月(ブラウザ操作)は10分で打ち切る(潤の指示 10/30)
        d = parse_json(raw)
    except Exception as error:  # noqa: BLE001
        set_status(url, task["id"], "approved", "自動作業が失敗: " + str(error)[:200])
        say(url, agent, "T%d: 自動の作業が失敗した(%s)。許可済のまま残す。やり直す場合は『T%d 許可』をもう一度。" % (task["id"], str(error)[:120], task["id"]), task["causeMessageId"])
        return 1
    if d and d.get("done") is True and str(d.get("evidence") or "").strip():
        note = "自動検証済: " + str(d.get("result") or "").strip() + " / 証拠: " + str(d["evidence"]).strip()
        if d.get("left"):
            note += " / 残り: " + str(d["left"]).strip()
        try:
            set_status(url, task["id"], "verified", note)
        except Exception as error:  # noqa: BLE001  状態が変わっていても、作業の結果は失わず篝火に書く
            note = "[状態の更新に失敗: %s] " % str(error)[:80] + note
        if agent == "mizuki":                                # 機械の美月の区切りも、3人共通のメモへ残す
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(HERE), "kagaribi_mizuki"))
                import kagaribi_mizuki_bridge as bridge
                bridge.remember("(機械の美月) T%d 完了: %s" % (task["id"], str(d.get("result") or "")))
            except Exception:  # noqa: BLE001  メモへの追記の失敗で、作業の報告を止めない
                pass
        say(url, agent, "T%d 完了・検証済。%s 証拠: %s%s" % (task["id"], str(d.get("result") or "").strip(), str(d["evidence"]).strip(), (" 残り: " + str(d["left"]).strip()) if d.get("left") else "") + ((" 動けるために必要: " + str(d["need"]).strip()) if d.get("need") else ""), task["causeMessageId"], report_target(task, agent))
        return 0
    reason = (str(d.get("result") or "") + " " + str(d.get("left") or "") + ((" 動けるために必要: " + str(d["need"]).strip()) if d and d.get("need") else "")).strip() if d else "結果をJSONとして読めなかった(出力の先頭: %s)" % " ".join(str(raw).split())[:200]
    set_status(url, task["id"], "approved", "未完: " + reason[:400])
    say(url, agent, "T%d: まだ完了していない(証拠が足りない、または残りがある)。%s 許可済のまま残す。" % (task["id"], reason[:300]), task["causeMessageId"], report_target(task, agent))
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--phase", choices=["accept", "work"], required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    a = ap.parse_args()
    task = get_task(a.url, a.task)
    if not task:
        return 2
    lock = acquire_task_lock(a.task, a.phase)
    if not lock:
        return 0                                            # 同じ依頼・同じ段階がすでに動いている
    try:
        task = get_task(a.url, a.task)                      # ロック取得後に状態を再確認(二重起動・先に誰かが動かした場合)
        if a.phase == "accept":
            return do_accept(a.url, task) if task["status"] == "pending_ack" else 0
        if task["status"] == "pending_ack":                 # 受付前に許可が出た場合も、まず受付する
            if do_accept(a.url, task) != 0:
                return 1
            task = get_task(a.url, a.task)
            if task["status"] == "rejected":
                return 0
            res._http(a.url + "/api/tasks/approve", {"id": a.task, "note": "受付前の許可を引き継ぎ"})   # 許可は消えないよう再度付ける
            return 0                                        # approve が work を再起動する
        return do_work(a.url, task) if task["status"] == "approved" else 0
    finally:
        release_task_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
