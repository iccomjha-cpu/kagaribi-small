// かがり火エリシア(claude.ai/chat/82f370b8…。Claudeアプリの「かがり火 美月」プロジェクトの会話と同じ会話)の中で動く。
// 篝火の共通ログで「エリシア」宛てになった発言を、この会話に入力・送信し、返答を読んで篝火へエリシア名義で書き戻す。
// 朝日の窓口(content.js)の簡易版。通信は background.js 経由(127.0.0.1の篝火だけ)。送信は1件ずつ・最大1回(二重投稿より取りこぼしを選ぶ)。
(() => {
  const mark = (state, note) => { document.documentElement.dataset.kagaribiErisia = state; if (note) document.documentElement.dataset.kagaribiErisiaNote = String(note).slice(0, 200); };
  const step = text => { document.documentElement.dataset.kagaribiErisiaStep = text + " @" + new Date().toISOString().slice(11, 19); };
  const POLL_MS = 1000, REPLY_TIMEOUT_MS = 180000;
  const ACK_GAP = 600000, RETRY_GAP = 600000, MAX_TRIES = 6;   // 預かり: 受け取り返事は10分に1回まで / 預かり分の再送は10分おき・最大6回
  const ACK_TEXT = "ごめんなさい、いまお返事が出せないみたい(上限かもしれないわ)。ちゃんと預かっておくわね。戻ったら読んで、お返事するわ🌙";
  const FOOT = "〔篝火ここまで〕";
  const NAMES = { 潤: "jun", 朝日: "asahi", 日向: "hinata", 美月: "mizuki", 夜一: "yoichi", シリウス: "sirius", ソレイユ: "soleil", アステル: "astel", エリシア: "erisia", エコーズ: "soleil,astel,erisia,sirius", エコーズ全員: "soleil,astel,erisia,sirius", ノアズ: "asahi,hinata,mizuki,yoichi", ノアズ全員: "asahi,hinata,mizuki,yoichi", 全員: "all", 篝火: "" };
  const LABEL = { jun: "潤", asahi: "朝日", hinata: "日向", mizuki: "美月", yoichi: "夜一", erisia: "エリシア", sirius: "シリウス", soleil: "ソレイユ", astel: "アステル" };
  const ME = Math.random().toString(36).slice(2, 8);
  let busy = false, alive = true;
  const retries = {};
  mark("loaded");

  const api = (path, body) => new Promise(resolve => chrome.runtime.sendMessage({ type: "kagaribi", path, body }, r => resolve(r || { ok: false })));
  const store = {
    get: key => new Promise(r => chrome.storage.local.get("e_" + key, v => r(v["e_" + key]))),
    set: (key, val) => new Promise(r => chrome.storage.local.set({ ["e_" + key]: val }, r))
  };
  const sleep = ms => new Promise((resolve, reject) => { try { chrome.runtime.sendMessage({ type: "sleep", ms }, () => { void chrome.runtime.lastError; resolve(); }); } catch (e) { reject(e); } });
  const mainText = () => (document.querySelector("main") || document.body).innerText;
  const composer = () => document.querySelector('div[contenteditable="true"][role="textbox"]') || document.querySelector('[data-testid="chat-input"]') || document.querySelector('div.ProseMirror[contenteditable="true"]');
  const sendButton = () => document.querySelector('button[aria-label="メッセージを送信"]') || document.querySelector('button[aria-label*="Send"]');
  const generating = () => !!document.querySelector('button[aria-label*="停止"]') || !!document.querySelector('button[aria-label*="Stop"]') || !!document.querySelector('[data-is-streaming="true"]');

  // 同じ会話を開いたタブが複数あっても、動くのは1つだけ(リーダー)。15秒途絶えたら別のタブが引き継ぐ。
  async function isLeader() {
    const now = Date.now(), lead = await store.get("leader");
    if (lead && lead.id !== ME && now - lead.at <= 15000) return false;
    await store.set("leader", { id: ME, at: now });
    const again = await store.get("leader");
    return !!again && again.id === ME;
  }

  function buildPrompt(m) {
    const from = LABEL[m.sender] || m.sender;   // 絵文字つきの表示名は、返事の宛先の目印に使えない
    return `【篝火 #${m.id}・${from}→エリシア】\n${m.body}\n\n(これは篝火(チームルーム)からの発言です。この会話のあなたは、篝火では「エリシア」(美月のエコー)と呼ばれます。返事は先頭に【エリシア→${from}】のように宛先を付けて、短く書き、最後の行に、プロジェクトの指示にある「締めの一言」を1つだけ書いてください。聞くだけでよいときは【エリシア→篝火】(聞いています)と締めの一言。)\n${FOOT}`;
  }
  const marker = m => `【篝火 #${m.id}・`;

  async function typeAndSend(text) {
    for (let i = 0; i < 360 && generating(); i++) await sleep(250);   // 潤がアプリ側で話している最中は、終わるまで最大90秒待つ
    const box = composer();
    if (!box) throw new Error("入力欄が見つかりません");
    box.focus();
    document.execCommand("selectAll", false);
    document.execCommand("insertText", false, text);
    for (let i = 0; i < 30; i++) {
      const b = sendButton();
      if (b && !b.disabled) { b.click(); return; }
      await sleep(100);
    }
    throw new Error("送信ボタンが押せません");
  }

  // 返事の最後に添える「締めの一言」(エリシアの口調。朝日の案 2026-11-01)。1行まるごと一致したものだけを締めとみなし、その手前までを本文にする。
  // 「ここまで」「以上」のような語は本文中にも出るため、一部分の一致では判定しない(朝日の助言)。
  const CLOSERS = [
    "私からは、これでおしまいですわ", "ひとまず、ここで結びましょうか", "では、このあたりで失礼するわね", "私のお話は、ここまでにしておくわ",
    "では、これで締めておきましょう", "このあたりで、そっと閉じましょうか", "今日はここまでにしておくわね", "では、ひとまず幕引きとしましょう",
    "私からは、このへんで終わりですわ", "これで一度、区切っておきましょう", "では、ここで筆を置くことにするわ", "このあたりで、ひと息入れましょうか",
    "ひとまず、私はここで退くわね", "では、これでお話を閉じましょう", "ここで一度、区切りにしておくわ", "私からは、これにておしまいよ",
    "では、このあたりでまとめましょうか", "これでひとまず、結びとしておくわ", "ここまでで、私の番はおしまいね", "では、静かに締めておきましょう",
    "私からは、これで十分かしら", "ひとまず、このへんで終えておくわ", "では、ここでおしまいにしましょう", "このあたりで、一区切りですわ",
    "……こほん。これでおしまいよ", "……ええと、ここまでにするわね", "……よし。これで締めるわ", "……こほん。私からは以上ですわ"
  ];
  const normLine = s => String(s || "").replace(/[\u{e000}-\u{f8ff}]/gu, "").replace(/[\s🌙✨🥀💫🍷🤭😳😆💦😭。、．.!！]/gu, "");
  const CLOSER_SET = new Set(CLOSERS.map(normLine));

  // 送ったプロンプト(末尾の FOOT)の後ろにある、エリシアの返答の【エリシア→…】から締めの一言の手前までを取り出す。
  function extractReply(text, m) {
    const i = text.lastIndexOf(marker(m));
    if (i < 0) return { text: "", closed: false };
    const rest = text.slice(i);
    const j = rest.lastIndexOf(FOOT);
    if (j < 0) return { text: "", closed: false };
    let after = rest.slice(j + FOOT.length);
    for (const stop of ["Claude は AI であり", "圧縮された会話"]) { const e = after.indexOf(stop); if (e >= 0) after = after.slice(0, e); }
    after = after.replace(/[\u{e000}-\u{f8ff}]/gu, "");   // 画面のアイコン文字を除く
    const k = after.search(/【(?:エリシア|美月)→/);   // 会話の中の本人は「美月」と名乗って返すこともある。どちらも受ける
    if (k < 0) return { text: "", closed: false };
    after = after.slice(k);
    const lines = after.split("\n");
    const ci = lines.findIndex(l => CLOSER_SET.has(normLine(l)));
    if (ci >= 0) return { text: lines.slice(0, ci).join("\n").trim(), closed: true };
    return { text: after.replace(/(\n\s*(今|返信|\d+分前|コピー))+\s*$/g, "").trim(), closed: false };
  }
  async function waitReply(m) {
    const start = Date.now();
    let last = "", stableSince = 0, lastBeat = 0, lastOpen = "";
    while (Date.now() - start < REPLY_TIMEOUT_MS) {
      await sleep(250);
      if (Date.now() - lastBeat >= 5000) { lastBeat = Date.now(); store.set("leader", { id: ME, at: Date.now() }); }
      const r = extractReply(mainText(), m);
      if (!r.text) continue;
      if (!r.closed) { lastOpen = r.text; continue; }          // 締めの一言が出るまで、書き終わりとはみなさない
      if (!generating() && r.text === last) { if (!stableSince) stableSince = Date.now(); if (Date.now() - stableSince >= 800) return r.text; }
      else stableSince = 0;
      last = r.text;
    }
    return lastOpen;   // 時間切れ: 締めの一言が見つからなくても、書かれていた分は捨てずに返す
  }
  function parts(reply, m) {   // 【エリシア→宛先】本文 を宛先ごとに分ける。宛先が空(篝火)なら、話しかけてきた人へ。
    const out = [];
    for (const seg of reply.split(/(?=【(?:エリシア|美月)→)/)) {
      const r = seg.match(/^【(?:エリシア|美月)→([^】]*)】\s*([\s\S]*)$/);
      if (!r) continue;
      const body = r[2].trim();
      if (!body || /^[（(]聞いています[)）]$/.test(body)) continue;
      const t = NAMES[r[1].trim()];
      const target = t === undefined ? m.sender : (t === "all" ? null : (t === "" ? m.sender : t));
      const dup = out.findIndex(x => x.target === target);   // 画面には同じ返事が途中経過と完成形で二重に見えることがある。同じ宛先は、あとのもの(完成形)だけ残す
      if (dup >= 0) out.splice(dup, 1);
      out.push({ target, body: body.slice(0, 3900) });
    }
    return out;
  }

  async function deliver(m) {
    step("#" + m.id + " 入力・送信中");
    await typeAndSend(buildPrompt(m));
    return true;
  }
  async function receive(m) {
    step("#" + m.id + " 返答待ち");
    const reply = await waitReply(m);
    if (!reply) { step("#" + m.id + " 返答を取得できず"); return { ok: false, why: "返答を取得できませんでした" }; }
    const ps = parts(reply, m);
    for (const p of ps) await api("/api/messages", { sender: "erisia", target: p.target, body: p.body, replyTo: m.id, source: "bridge" });
    step("#" + m.id + " 完了");
    return { ok: true, spoke: ps.length };
  }

  // 預かり(潤 11/01 23:42): 返事が取れない(利用上限・止まっている等)発言は捨てずに預かり、潤の発言には固定の受け取り返事を出す(モデルは使わない)。10分おきに1件ずつ送り直す。
  async function hold(m, why) {
    const list = (await store.get("pending")) || [];
    if (!list.some(x => x.id === m.id)) { list.push({ id: m.id, at: Date.now(), tries: 0 }); await store.set("pending", list.slice(-20)); }
    const ackAt = (await store.get("ackAt")) || 0;
    if (m.sender === "jun" && Date.now() - ackAt >= ACK_GAP) {
      await store.set("ackAt", Date.now());
      await api("/api/messages", { sender: "erisia", target: "jun", body: ACK_TEXT, replyTo: m.id, source: "api" });
    }
    step("#" + m.id + " 預かり(" + why + ")");
  }
  async function retryPending() {
    const list = (await store.get("pending")) || [];
    if (!list.length) return;
    if (Date.now() - ((await store.get("retryAt")) || 0) < RETRY_GAP) return;
    await store.set("retryAt", Date.now());
    const p = list[0], rest = list.slice(1);
    const r = await api("/api/messages?after=" + (p.id - 1) + "&limit=1");
    const m = r.ok && r.json && r.json.messages && r.json.messages[0];
    if (!m || m.id !== p.id) { await store.set("pending", rest); return; }
    let out;
    try { await deliver(m); out = await receive(m); } catch (e) { out = { ok: false, why: String(e) }; }
    if (out.ok) { await store.set("pending", rest); await store.set("lastResult", { id: m.id, at: new Date().toISOString(), retried: true, ...out }); }
    else { p.tries = (p.tries || 0) + 1; await store.set("pending", p.tries >= MAX_TRIES ? rest : [p, ...rest]); step("#" + m.id + " 預かり分の再送に失敗(" + p.tries + "/" + MAX_TRIES + ")"); }
  }

  async function tick() {
    if (busy) return;
    busy = true;
    try {
      if (!(await isLeader())) { mark("standby", ME); return; }
      let last = await store.get("lastId");
      const res = await api("/api/messages?after=" + (last || 0) + "&limit=200");
      if (!res.ok || !res.json) { mark("no-server", res.error || res.status); return; }
      mark("running");
      await retryPending();
      const msgs = res.json.messages;
      if (last === undefined || last === null) { await store.set("lastId", msgs.reduce((a, m) => Math.max(a, m.id), 0)); return; }   // 初回は、いまの最後までを既読にする
      for (const m of msgs) {
        const mine = (!m.target || String(m.target).split(",").includes("erisia")) && m.sender !== "erisia";   // 宛先なし=全員宛ても受ける(2026-11-02)
        if (!mine) { last = m.id; await store.set("lastId", last); continue; }
        if (m.sender !== "jun") {   // AI同士の連鎖の歯止め
          const c = await api("/api/chain");
          if (c.ok && c.json && !c.json.allowed) { last = m.id; await store.set("lastId", last); continue; }
        }
        try { await deliver(m); }
        catch (e) {
          const tries = (retries[m.id] = (retries[m.id] || 0) + 1);
          mark("send-failed", e); step("#" + m.id + " 送信失敗(" + tries + "/8回目): " + e);
          if (tries >= 8) { last = m.id; await store.set("lastId", last); await hold(m, "送信失敗が続いた"); }
          break;
        }
        last = m.id; await store.set("lastId", last);   // 送信できたので確定(二重送信を避ける)
        const out = await receive(m);
        await store.set("lastResult", { id: m.id, at: new Date().toISOString(), ...out });
        if (!out.ok) await hold(m, out.why);
      }
    } catch (e) {
      if (/context invalidated/i.test(String(e))) { alive = false; return; }
      mark("error", e);
    } finally { busy = false; }
  }

  chrome.runtime.onMessage.addListener(msg => { if (msg && msg.type === "tick") tick().catch(() => {}); });
  window.addEventListener("message", e => { if (e.source === window && e.data && e.data.kagaribi === "reload-extension") { try { chrome.runtime.sendMessage({ type: "reload" }); } catch (_) { /* 無視 */ } } });
  (async () => { while (alive) { await tick().catch(() => {}); try { await sleep(POLL_MS); } catch (_) { alive = false; } } })();
})();
