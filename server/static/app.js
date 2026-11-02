const labels = {jun:"潤",asahi:"☀️朝日",hinata:"🌱日向",mizuki:"🌙美月",yoichi:"⭐夜一",erisia:"🌙エリシア",エリシア:"🌙エリシア",soleil:"🌞ソレイユ",ソレイユ:"🌞ソレイユ",sirius:"🌟シリウス",シリウス:"🌟シリウス",astel:"🍃アステル",アステル:"🍃アステル",潤:"潤",朝日:"☀️朝日",日向:"🌱日向",美月:"🌙美月",夜一:"⭐夜一"};
const icons = {jun:"🔥",asahi:"☀️",hinata:"🌱",mizuki:"🌙",yoichi:"⭐",erisia:"🌙",エリシア:"🌙",soleil:"🌞",ソレイユ:"🌞",sirius:"🌟",シリウス:"🌟",astel:"🍃",アステル:"🍃",潤:"🔥",朝日:"☀️",日向:"🌱",美月:"🌙",夜一:"⭐"};
const warm = new Set(["jun", "asahi", "hinata", "潤", "朝日", "日向"]);
const state = { messages: [], lastId: 0, replyTo: null, live: false, total: 0 };    // total=全件数(画面に出すのは最新30件だけ。古い分はサーバーに残り「古い発言を読み込む」で足す)
const INITIAL_TAIL = 30;
const CHOICE_RE = /(?<![\＼`])【ボタン[:：]([^】]+)】/g;   // 直前に ＼ \ ` が付いたタグは見本として、ボタンにしない   // AIが潤に聞くとき、発言に【ボタン:選択肢A|選択肢B】と書くと、その発言に押せるボタンが付く
function choicesOf(body) { const m = new RegExp(CHOICE_RE.source).exec(String(body || "")); return m ? m[1].split(/[|｜／\/]/).map(x => x.trim()).filter(Boolean).slice(0, 4) : []; }
async function answerChoice(message, label, bar) {
  bar.querySelectorAll("button").forEach(b => { b.disabled = true; });
  try {
    const r = await fetch("/api/messages", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({sender:"jun", target:message.sender, body:label, replyTo:message.id, source:"web"})});
    const d = await r.json(); if (!r.ok) throw new Error(d.error || "送信に失敗しました");
    bar.querySelectorAll("button").forEach(b => { b.classList.toggle("chosen", b.textContent === label); }); appendMessages([d.message]);
  } catch (e) { bar.querySelectorAll("button").forEach(b => { b.disabled = false; }); window.alert(e.message); }
}
const ALIASES = {asahi:["朝日","あさひ","アサヒ"], hinata:["日向","ひなた","ヒナタ"], mizuki:["美月","瑞希","みづき","ミヅキ","みつき","ミツキ"], erisia:["エリシア","えりしあ","エリシヤ"], soleil:["ソレイユ","それいゆ"], sirius:["シリウス","しりうす"], astel:["アステル","あすてる","アステール"], yoichi:["夜一","与一","宵一","陽一","よいち","ヨイチ"]};
state.replyTargets = [];
const ALL_NAMES = Object.entries(ALIASES).flatMap(([id, ns]) => ns.map(n => [n, id])).sort((a, b) => b[0].length - a[0].length);
function nameAt(text, i) { for (const [n, id] of ALL_NAMES) if (text.startsWith(n, i)) { let j = i + n.length; const h = /^(さん|ちゃん|君|くん)/.exec(text.slice(j)); if (h) j += h[0].length; return { id, end: j }; } return null; }
// 宛先は「文頭の呼びかけ」で決める(潤の指示 11/01)。文頭に名前かグループ(みんな=全員 / ノアズ / エコーズ)を呼んでから話し始める。
// 文の途中に出た名前(「朝日が言ってた通り」など)は宛先にしない。呼びかけが無ければ、直前の流れ(直前に話した相手)へ返す。
const NOIRS = ["asahi", "hinata", "mizuki", "yoichi"], ECHOES = ["soleil", "astel", "erisia", "sirius"], EVERYONE = [...NOIRS, ...ECHOES];
const GROUPS = [["みんな", EVERYONE], ["皆さん", EVERYONE], ["みなさん", EVERYONE], ["全員", EVERYONE], ["ノアーズ", NOIRS], ["ノアズ", NOIRS], ["エコーズ", ECHOES], ["エコー", ECHOES]];
function tokenAt(text, i) {
  for (const [n, ids] of GROUPS) if (text.startsWith(n, i)) return { ids, end: i + n.length };
  const a = nameAt(text, i); return a ? { ids: [a.id], end: a.end } : null;
}
function detectTargetsLine(text) {
  let i = 0; while (i < text.length && /[\s「『(（]/.test(text[i])) i++;
  const first = tokenAt(text, i); if (!first) return [];
  const ids = [...first.ids]; let j = first.end;
  for (;;) {
    const sep = /^[\s、,，・と&＆]*/.exec(text.slice(j))[0].length; if (!sep) break;
    const nx = tokenAt(text, j + sep); if (!nx) break;
    nx.ids.forEach(id => { if (!ids.includes(id)) ids.push(id); }); j = nx.end;
  }
  const c = text[j]; if (c !== undefined && "のがを".includes(c)) return [];   // 名前のあとに「が」「の」「を」が続くなら、話題の名前であって呼びかけではない
  return ids;
}
// 呼びかけの拾い方(潤の指示 11/02): ①文章の一番頭 ②各行の頭 ③各文(。！？のあと)の頭は、名前のあとに「、」「,」が続く呼びかけの形のときだけ宛先にする。
// 「朝日が言ってた」のような話題の名前は、②③の形(名前+読点)でなければ宛先にしない。ボタンで選んだ宛先は常に最優先。
function detectTargets(text) {
  const ids = [], add = list => list.forEach(id => { if (!ids.includes(id)) ids.push(id); });
  String(text).split(/\n/).forEach((line, li) => {
    add(detectTargetsLine(line));                                   // 行の頭(従来どおり)
    line.split(/[。！？!?]/).slice(1).forEach(sent => {                // 同じ行の2文目以降: 名前+読点の呼びかけだけ
      const t = sent.replace(/^[\s」』)）]+/, "");
      if (/^(?:[^\s、,，。]{1,8})[、,，]/.test(t)) add(detectTargetsLine(t));
    });
  });
  return ids;
}
function groupName(ids) {
  const set = new Set(ids), same = g => g.length === set.size && g.every(x => set.has(x));
  return same(EVERYONE) ? "みんな" : same(NOIRS) ? "ノアズ" : same(ECHOES) ? "エコーズ" : null;
}
function targetText(ids) { return groupName(ids) || ids.map(x => labels[x] || x).join("・"); }
// 直前の流れ: 新しい方から見て、AIの発言ならその発言者へ。潤自身の発言なら、その宛先を引き継ぐ(返事待ちのあいだも同じ相手に話し続ける)。
function defaultTarget() {
  // 潤のルール(11/02): 誰宛てとも言わないとき、過去10件の中で潤と絡んでいる人たち(潤が話しかけた相手と、潤に話した人)へ送る。
  const recent = state.messages.slice(-10), ids = [];
  for (const x of recent) {
    if (x.sender === "jun") String(x.target || "").split(",").filter(Boolean).forEach(t => { if (!ids.includes(t)) ids.push(t); });
    else if (labels[x.sender] && !ids.includes(x.sender)) ids.push(x.sender);
  }
  return ids;
}
state.pick = new Set();   // 宛先ボタンで明示的に選んだ宛先(最優先)
function currentTargets() {
  // 1)ボタンで選んだ宛先は外さない 2)文頭の呼びかけは最優先(ボタンに足される) 3)どちらも無ければ返信先→過去10件の絡み
  const d = detectTargets(elements.body.value);
  const withAsahi = (t) => (/朝日/.test(elements.body.value) && !t.includes("asahi")) ? [...t, "asahi"] : t;   // 潤確定(#3002/#3003): 文中に「朝日」があれば必ず朝日も宛先に足す
  if (state.pick.size) return withAsahi([...state.pick, ...d.filter(x => !state.pick.has(x))]);
  if (d.length) return withAsahi(d);
  return withAsahi(state.replyTargets.length ? state.replyTargets : defaultTarget());
}
function updateTargetLabel() {
  const el = document.querySelector("#target-label"); const t = currentTargets(); if (el) el.textContent = t.length ? targetText(t) : "みんな";
  const wrap = document.querySelector(".input-wrap"); let chip = document.querySelector("#to-chips");
  if (wrap && !chip) { chip = document.createElement("div"); chip.id = "to-chips"; wrap.prepend(chip); }
  if (chip) { chip.className = state.pick.size ? "picked" : "auto"; chip.textContent = "To: " + (t.length ? targetText(t) : "みんな") + (state.pick.size ? "" : "(自動)"); }
  document.querySelectorAll(".target-rail .grp").forEach(g => { const ids = g.dataset.all ? EVERYONE : g.dataset.group === "ノアズ" ? NOIRS : ECHOES; g.setAttribute("aria-pressed", ids.every(i => state.pick.has(i)) ? "true" : "false"); });
  document.querySelectorAll(".target-rail button[data-id]").forEach(b => { const id = b.dataset.id; b.setAttribute("aria-pressed", state.pick.has(id) ? "true" : "false"); b.classList.toggle("auto", !state.pick.size && t.includes(id)); });
}
const elements = {
  messages: document.querySelector("#messages"), count: document.querySelector("#count"),
  status: document.querySelector("#status"), form: document.querySelector("#composer"),
  body: document.querySelector("#body"), target: document.querySelector("#target"),
  replyBanner: document.querySelector("#reply-banner"), clearReply: document.querySelector("#clear-reply"),
  topicTitle: document.querySelector("#topic-title"), topicSource: document.querySelector("#topic-source"),
  ratio: document.querySelector("#view-ratio"), ratioOutput: document.querySelector("#view-ratio-output")
};

const escapeHtml = value => String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const formatTime = value => new Intl.DateTimeFormat("ja-JP", {timeZone:"Asia/Tokyo",hour:"2-digit",minute:"2-digit",second:"2-digit"}).format(new Date(value));

function renderMessage(message) {
  const reply = message.replyTo ? state.messages.find(item => item.id === message.replyTo) : null;
  const actorLabel = message.senderDisplay || labels[message.sender] || message.sender;
  const senderLabel = actorLabel.replace(/^(?:☀️|🌱|🌙|⭐)\s*/, "");
  const tIds = String(message.target || "").split(",").filter(Boolean);
  const targetLabel = (tIds.length > 1 && groupName(tIds)) || message.targetDisplay || labels[message.target] || message.target;
  const route = message.target ? `→ ${escapeHtml(targetLabel)}` : "→ みんな";
  const article = document.createElement("article");
  article.className = "message";
  article.dataset.side = warm.has(message.sender) ? "warm" : "cool";
  article.dataset.id = message.id;
  article.dataset.actor = ({潤:"jun",朝日:"asahi",日向:"hinata",美月:"mizuki",夜一:"yoichi",エリシア:"erisia",シリウス:"sirius",アステル:"astel",ソレイユ:"soleil"})[message.sender] || message.sender;
  const sourceLabel = message.source === "web" ? "本人入力" : message.source === "seed" ? "接続確認" : message.source === "bridge" ? "ブリッジ" : "API";
  article.innerHTML = `<div class="meta"><span class="msgno" title="発言番号(クリックで引用に入れる)">#${message.id}</span><span class="avatar">${icons[message.sender] || "•"}</span><span class="name">${escapeHtml(senderLabel)}</span><span class="route">${route}</span><span class="source">${sourceLabel}</span><time class="time">${formatTime(message.createdAt)}</time></div>${reply ? `<div class="reply-context"><a class="msgref" data-ref="${reply.id}" href="#${reply.id}">#${reply.id}</a> ${escapeHtml(reply.senderDisplay || labels[reply.sender] || reply.sender)}: ${escapeHtml(reply.body.slice(0,80))}</div>` : ""}<p>${bodyWithImages(String(message.body).replace(CHOICE_RE, "").replace(/[\＼](?=【ボタン)/g, "").trim())}</p><button type="button" class="reply-button">↩ この発言に返信</button>`;
  article.querySelector(".reply-button").addEventListener("click", () => selectReply(message));
  const opts = message.sender === "jun" ? [] : choicesOf(message.body);
  if (opts.length) {
    const bar = document.createElement("div"); bar.className = "choice-bar";
    const answered = state.messages.find(x => x.sender === "jun" && x.replyTo === message.id);
    for (const label of opts) { const b = document.createElement("button"); b.type = "button"; b.textContent = label; b.disabled = !!answered; if (answered && String(answered.body).trim() === label) b.classList.add("chosen"); b.addEventListener("click", () => answerChoice(message, label, bar)); bar.appendChild(b); }
    article.insertBefore(bar, article.querySelector(".reply-button"));
  }
  article.querySelectorAll(".avatar,.name").forEach(el => { el.style.cursor = "pointer"; el.title = "クリックで、この発言に返信(もう一度クリックで解除)"; el.addEventListener("click", () => { if (state.replyTo === message.id) clearReply(); else selectReply(message); }); });
  return article;
}

function markReplying(id) {
  document.querySelectorAll(".message.replying").forEach(x => { x.classList.remove("replying"); const f = x.querySelector(".reply-flag"); if (f) f.remove(); });
  if (id == null) return;
  const art = elements.messages.querySelector(`.message[data-id="${id}"]`); if (!art) return;
  art.classList.add("replying");
  const f = document.createElement("span"); f.className = "reply-flag"; f.textContent = "↩ ここに返信中"; art.querySelector(".meta").prepend(f);
}
function selectReply(message) {
  state.replyTo = message.id;
  const targetId = ({朝日:"asahi",日向:"hinata",美月:"mizuki",夜一:"yoichi",エリシア:"erisia",シリウス:"sirius",アステル:"astel",ソレイユ:"soleil"})[message.sender] || message.sender;
  state.replyTargets = (targetId === "jun" || targetId === "潤") ? [] : [targetId]; updateTargetLabel();
  elements.replyBanner.querySelector("span").textContent = `#${message.id} ${message.senderDisplay || labels[message.sender] || message.sender} へ返信`;
  elements.replyBanner.hidden = false;
  markReplying(message.id);
  elements.body.focus();
}

function clearReply() {
  state.replyTo = null;
  state.replyTargets = []; updateTargetLabel();
  elements.replyBanner.hidden = true;
  markReplying(null);
}

function appendMessages(incoming) {
  if (!incoming.length) return;
  if (!state.messages.length) elements.messages.innerHTML = "";
  for (const message of incoming) {
    if (state.messages.some(item => item.id === message.id)) continue;
    state.messages.push(message);
    state.lastId = Math.max(state.lastId, message.id);
    elements.messages.appendChild(renderMessage(message));
    if (window.kagaribiScene && window.kagaribiScene.setLast) window.kagaribiScene.setLast(message.sender, message.body);
    if (state.live && window.kagaribiScene) window.kagaribiScene.pulse(message.sender, message.body);   // 履歴の読み込み後に来た発言だけ、キャラがはずむ
  }
  elements.count.textContent = `${Math.max(state.total, state.messages.length)}件`;
  updateTargetLabel();
  elements.messages.scrollTop = elements.messages.scrollHeight;
  syncApprove();
}

function addOlderButton() {
  if (document.querySelector("#older-btn") || !state.messages.length || state.messages.length >= state.total) return;
  const b = document.createElement("button"); b.type = "button"; b.id = "older-btn"; b.className = "older-btn";
  const upd = () => { const rest = state.total - state.messages.length; b.textContent = rest > 0 ? `古い発言を読み込む(あと${rest}件)` : ""; b.hidden = rest <= 0; };
  b.addEventListener("click", async () => {
    const oldest = Math.min(...state.messages.map(m => m.id)); b.disabled = true;
    try {
      const data = await (await fetch(`/api/messages?before=${oldest}&limit=30`, {cache:"no-store"})).json();
      const box = elements.messages, prevH = box.scrollHeight, prevTop = box.scrollTop;
      const frag = document.createDocumentFragment();
      const fresh = data.messages.filter(m => !state.messages.some(x => x.id === m.id));      // dataは古い順。そのまま先頭にまとめて足す
      for (const m of fresh) frag.appendChild(renderMessage(m));
      state.messages = [...fresh, ...state.messages];
      box.insertBefore(frag, b.nextSibling); box.scrollTop = prevTop + (box.scrollHeight - prevH);
    } catch (e) {} finally { b.disabled = false; upd(); }
  });
  elements.messages.prepend(b); upd();
}
elements.messages.addEventListener("scroll", () => syncApprove(), {passive:true});
async function poll() {
  try {
    const first = state.lastId === 0;      // 最初は最新だけ(全件を読むと1,400件超で重い)
    const response = await fetch(first ? `/api/messages?tail=${INITIAL_TAIL}` : `/api/messages?after=${state.lastId}`, {cache:"no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const had = state.messages.length;
    appendMessages(data.messages);
    if (first && typeof data.total === "number") state.total = data.total; else state.total += state.messages.length - had;      // 総件数: 最初はサーバーの値、以後は新着の分だけ足す
    elements.count.textContent = `${Math.max(state.total, state.messages.length)}件`;
    if (first) addOlderButton();
    state.live = true;
    elements.status.textContent = "● 共有ログ接続中";
    elements.status.classList.add("online");
  } catch (error) {
    elements.status.textContent = "再接続中…";
    elements.status.classList.remove("online");
  }
}

// 連鎖の操作(潤): 「止める」=次の潤の発言までAIの返事を止める。「連鎖 n/上限」の ー ＋ で、AIだけが続けてよい件数の上限を指定する。
async function chainPost(body) { try { await fetch("/api/chain", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}); } catch (e) { /* 次のpollで状態が戻る */ } pollChain(); }
function buildChainCtl() {
  if (document.querySelector("#chain-ctl")) return;
  const host = document.querySelector(".log-head .log-tools"); if (!host) return;
  const box = document.createElement("span"); box.id = "chain-ctl"; box.className = "chain-ctl";
  box.innerHTML = '<button type="button" id="chain-stop" title="ノアズ・エコーズの返事の連鎖を止める(潤が次に発言するか、もう一度押すまで)" aria-label="連鎖を止める">⏸</button><span class="lim" title="潤の最後の発言から、AIだけで続けてよい件数の上限"><button type="button" id="lim-dec" aria-label="上限を減らす">−</button><b id="lim-text">連鎖 -/-</b><button type="button" id="lim-inc" aria-label="上限を増やす">＋</button></span>';
  host.parentNode.insertBefore(box, host);   // 会話ログの見出しの中(上の帯は並べる表示では隠れているため)
  box.querySelector("#chain-stop").addEventListener("click", () => chainPost({stop: !state.chain || !state.chain.stopped}));
  box.querySelector("#lim-dec").addEventListener("click", () => chainPost({limit: Math.max(0, ((state.chain && state.chain.limit) || 0) - 2)}));
  box.querySelector("#lim-inc").addEventListener("click", () => chainPost({limit: Math.min(100, ((state.chain && state.chain.limit) || 0) + 2)}));
}
async function pollRoute() {
  try {
    const r = await fetch("/api/route", {cache:"no-store"}); if (!r.ok) return;
    const d = await r.json(); state.route = d.route;
    const b = document.querySelector("#route-toggle");
    if (b) { b.textContent = d.route === "split" ? "切り分け" : "全員通達"; b.classList.toggle("on", d.route === "split"); }
  } catch (e) { /* 表示だけ */ }
}
async function pollChain() {
  try {
    const response = await fetch("/api/chain", {cache:"no-store"});
    if (!response.ok) return;
    const chain = await response.json(), note = document.querySelector("#chain");
    state.chain = chain; buildChainCtl(); pollRoute();
    const modBtn = document.querySelector("#mod-toggle");
    if (modBtn) { modBtn.title = "司会(朝日)へ全体共有: " + (chain.moderator ? "ON" : "OFF") + "。ONの間は、朝日宛てではない流れも、まとめて朝日に配る(司会は常に朝日)"; modBtn.setAttribute("aria-pressed", chain.moderator ? "true" : "false"); }
    const stopBtn = document.querySelector("#chain-stop"), lim = document.querySelector("#lim-text");
    if (stopBtn) { stopBtn.textContent = chain.stopped ? "▶" : "⏸"; stopBtn.setAttribute("aria-label", chain.stopped ? "連鎖を再開する" : "連鎖を止める"); stopBtn.classList.toggle("on", !!chain.stopped); }
    if (lim) { lim.textContent = `連鎖 ${chain.aiRun}/${chain.limit}`; lim.classList.toggle("full", !chain.allowed); lim.title = chain.manualLimit ? "手動で指定した上限" : "自動の上限(ー ＋ で手動に切り替わる)"; }
    if (!note) return;
    if (chain.stopped) { note.textContent = "止めています(潤さんの次の発言か再開まで)"; note.hidden = false; }
    else if (chain.free) { note.textContent = "自由雑談中(" + chain.aiRun + "件)"; note.hidden = false; }
    else if (!chain.allowed) { note.textContent = "連鎖を止めました(潤さんの発言待ち)"; note.hidden = false; }
    else note.hidden = true;
  } catch (error) { /* 表示だけなので無視 */ }
}

async function pollTasks() {
  try {
    const response = await fetch("/api/tasks?open=1", {cache:"no-store"});
    if (!response.ok) return;
    const tasks = (await response.json()).tasks, note = document.querySelector("#tasks");
    if (!note) return;
    const count = status => tasks.filter(t => t.status === status).length;
    note.textContent = "エコーへの依頼: 引き継ぎ待ち" + count("pending_ack") + " / 受付済" + count("accepted") + " / 許可済" + count("approved") + " / 作業中" + count("working") + " ";
    for (const t of tasks.filter(t => t.status === "pending_ack" || t.status === "accepted")) {   // 潤が篝火だけを見ていても、ここで許可すれば本体が動く
      const button = document.createElement("button");
      button.type = "button"; button.className = "task-approve"; button.title = t.summary;
      button.textContent = "T" + t.id + " 許可";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try { await fetch("/api/tasks/approve", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:t.id, note:"潤が画面で許可"})}); }
        catch (error) { button.disabled = false; }
        pollTasks();
      });
      note.appendChild(button);
    }
    note.hidden = tasks.length === 0;
    state.pending = tasks.filter(t => t.status === "pending_ack" || t.status === "accepted");
    syncApprove();
  } catch (error) { /* 表示だけなので無視 */ }
}

// 許可ボタン: 許可を求めている本人の発言(「T123 許可」と書いてある発言)に付ける。画面の外へ流れたときは、会話ログの上の帯に出す
async function approveTask(id, button) {
  if (button) button.disabled = true;
  try { await fetch("/api/tasks/approve", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id, note:"潤が画面で許可"})}); }
  catch (error) { if (button) button.disabled = false; }
  pollTasks();
}
function syncApprove() {
  const pending = state.pending || [], ids = new Set(pending.map(t => String(t.id)));
  document.querySelectorAll(".msg-approve").forEach(b => { if (!ids.has(b.dataset.task)) b.remove(); });
  const box = elements.messages, boxRect = box.getBoundingClientRect(), seen = new Set();
  for (const m of state.messages) {
    if (m.sender === "jun") continue;
    for (const hit of String(m.body || "").matchAll(/T(\d+)\s*許可/g)) {
      const id = hit[1]; if (!ids.has(id)) continue;
      const art = box.querySelector(`article[data-id="${m.id}"]`); if (!art) continue;
      let btn = art.querySelector(`.msg-approve[data-task="${id}"]`);
      if (!btn) { btn = document.createElement("button"); btn.type = "button"; btn.className = "msg-approve"; btn.dataset.task = id; btn.textContent = "T" + id + " 許可する"; btn.addEventListener("click", () => approveTask(Number(id), btn)); art.appendChild(btn); }
      const r = art.getBoundingClientRect(); if (r.bottom > boxRect.top + 4 && r.top < boxRect.bottom - 4) seen.add(id);      // 画面に見えている
    }
  }
  const bar = document.querySelector("#pending-bar"); if (!bar) return;
  const hidden = pending.filter(t => !seen.has(String(t.id)));
  bar.innerHTML = ""; bar.hidden = hidden.length === 0;
  if (!hidden.length) return;
  const label = document.createElement("span"); label.textContent = "🟠 許可待ちがあります"; bar.appendChild(label);
  for (const t of hidden) { const b = document.createElement("button"); b.type = "button"; b.className = "msg-approve"; b.title = t.summary; b.textContent = "T" + t.id + " 許可する"; b.addEventListener("click", () => approveTask(t.id, b)); bar.appendChild(b); }
}

async function pollPresence() {
  try {
    const response = await fetch("/api/presence", {cache:"no-store"});
    if (!response.ok) return;
    const presence = (await response.json()).presence;
    if (window.kagaribiScene) window.kagaribiScene.setPresence(presence);
    const warn = document.querySelector("#asahi-warn");   // 朝日の窓口が止まったら、画面の上に原因の目当てを出す
    if (warn) warn.hidden = !presence.asahi || presence.asahi.alive;
  } catch (error) { /* 状態が取れなくても会話は動く */ }
}

async function pollTopic() {
  try {
    const response = await fetch("/api/topic", {cache:"no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const topic = (await response.json()).topic;
    elements.topicTitle.textContent = topic.title || "議題なし";
    elements.topicSource.textContent = topic.available ? `PocStore ${topic.source}・読取専用` : "PocStore・候補なし";
  } catch (error) {
    elements.topicTitle.textContent = "議題なし";
    elements.topicSource.textContent = "PocStore・取得できません";
  }
}

elements.form.addEventListener("submit", async event => {
  event.preventDefault();
  const body = elements.body.value.trim();
  if (!body) return;
  const sendButton = elements.form.querySelector(".send");
  sendButton.disabled = true;
  try {
    const response = await fetch("/api/messages", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({sender:"jun", target:currentTargets().join(",") || null, body, replyTo:state.replyTo, source:"web"})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "送信に失敗しました");
    appendMessages([result.message]);
    elements.body.value = "";
    state.pick.clear();
    updateTargetLabel();
    clearReply();
  } catch (error) {
    window.alert(error.message);
  } finally {
    sendButton.disabled = false;
    elements.body.focus();
  }
});

elements.body.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); elements.form.requestSubmit(); } });
elements.clearReply.addEventListener("click", clearReply);
elements.body.addEventListener("input", updateTargetLabel);
// --- 上部ヘッダーの現在時刻(日本時間)と、設定(小さな歯車→モーダル): ログの文字サイズ・アニメーションON/OFF。設定はこのブラウザにだけ保存する ---
const clockEl = document.querySelector("#clock");
const tickClock = () => { if (clockEl) clockEl.textContent = new Intl.DateTimeFormat("ja-JP", {timeZone:"Asia/Tokyo", hour:"2-digit", minute:"2-digit", second:"2-digit"}).format(new Date()); };
tickClock(); setInterval(tickClock, 1000);
const settingsEl = document.querySelector("#settings");
const store = { get(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch (error) { return fallback; } }, set(key, value) { try { localStorage.setItem(key, value); } catch (error) { /* 保存できなくても動く */ } } };
function setView(view, {persist=true} = {}) {
  const mobile = window.matchMedia("(max-width: 900px)").matches;
  const allowed = mobile ? new Set(["scene", "history"]) : new Set(["scene", "split", "history"]);
  const selected = allowed.has(view) ? view : (mobile ? "scene" : "split");
  document.body.dataset.view = selected;
  document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === selected)));
  if (persist) store.set(mobile ? "kagaribi.mobileView" : "kagaribi.view", selected);
}
function applyRatio(value) {
  const main = Math.max(45, Math.min(80, Number(value) || 80));
  document.documentElement.style.setProperty("--main-ratio", `${main}fr`);
  document.documentElement.style.setProperty("--log-ratio", `${100-main}fr`);
  if (elements.ratio) elements.ratio.value = String(main);
  if (elements.ratioOutput) elements.ratioOutput.textContent = `${main}:${100-main}`;
  store.set("kagaribi.ratio", String(main));
}
function applySettings() {
  document.body.dataset.logsize = store.get("kagaribi.logsize", "m");
  document.body.classList.toggle("no-anim", store.get("kagaribi.anim", "on") === "off");
}
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => setView(button.dataset.view)));
if (elements.ratio) elements.ratio.addEventListener("input", event => applyRatio(event.target.value));
const chooseInitialView = () => setView(window.matchMedia("(max-width: 900px)").matches ? store.get("kagaribi.mobileView", "scene") : store.get("kagaribi.view", "split"), {persist:false});
window.matchMedia("(max-width: 900px)").addEventListener("change", chooseInitialView);
if (settingsEl) {
  document.querySelector("#open-settings").addEventListener("click", () => {
    settingsEl.querySelector(`input[name="logsize"][value="${store.get("kagaribi.logsize", "m")}"]`).checked = true;
    settingsEl.querySelector("#anim-on").checked = store.get("kagaribi.anim", "on") !== "off";
    settingsEl.showModal();
  });
  settingsEl.querySelectorAll('input[name="logsize"]').forEach(input => input.addEventListener("change", () => { store.set("kagaribi.logsize", input.value); applySettings(); }));
  settingsEl.querySelector("#anim-on").addEventListener("change", event => { store.set("kagaribi.anim", event.target.checked ? "on" : "off"); applySettings(); });
}
applySettings();
applyRatio(store.get("kagaribi.ratio", "80"));
chooseInitialView();
pollTopic();
poll();
setInterval(poll, 1500);
setInterval(pollPresence, 1000);
setInterval(pollChain, 2000);
setInterval(pollTasks, 5000);
setInterval(pollTopic, 15000);

// 実体の美月の既読(橋が記録した合図)を、画面の上に出す。会話役に届いても、実体が読んだ合図が無ければ「未読」(会話役と実体を混ぜないため)
async function pollDelivery() {
  try {
    const d = (await (await fetch("/api/delivery", {cache:"no-store"})).json()).mizuki;
    let el = document.querySelector("#delivery");
    if (!el) { el = document.createElement("span"); el.id = "delivery"; el.className = "chainnote"; const clock = document.querySelector("#clock"); clock.parentNode.insertBefore(el, clock); }
    if (!d.readId) { el.textContent = "🌙美月(実体): 合図なし"; el.title = "実体の美月がまだ『読んだ』の合図(>> read=#番号)を出していません"; }
    else if (d.unread > 0) { el.textContent = "🌙美月(実体): 既読#" + d.readId + "(" + d.at + ")・未読" + d.unread + "件"; el.title = "最新#" + d.latestId + "。それより新しい発言は、実体にはまだ届いていません"; }
    else { el.textContent = "🌙美月(実体): 既読(最新まで)"; el.title = "最新#" + d.latestId + "まで読んだ合図があります"; }
  } catch (e) { /* 取れなければ表示しない */ }
}
pollDelivery(); setInterval(pollDelivery, 10000);

// 会話パネルの丸アイコン: manifest.icons[キャラ] があれば、絵文字の代わりに丸い絵を出す(無ければ絵文字のまま)
(function () {
  let ICONS = {};
  const apply = () => document.querySelectorAll(".message .avatar:not(.has-icon)").forEach(av => {
    const who = av.closest(".message") && av.closest(".message").dataset.actor, f = ICONS[who];
    if (f) { av.classList.add("has-icon"); av.style.backgroundImage = `url("/assets/${f}")`; }
  });
  fetch("/assets/manifest.json", { cache: "no-store" }).then(r => r.json()).then(m => { ICONS = m.icons || {}; apply(); }).catch(() => {});
  const box = document.querySelector(".messages");
  if (box) new MutationObserver(apply).observe(box, { childList: true, subtree: true });
})();


// --- 画像のドラッグ&ドロップ/貼り付け: 共有フォルダへ保存して、【画像:リンク】を入力欄へ入れる。ログでは小さな画像で表示 ---
function bodyWithImages(text) {
  return escapeHtml(text).replace(/【画像[:：]\s*(\/shared_images\/[\w.\-]+)】/g,
    (all, url) => `<a class="shared-img" href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="共有画像" loading="lazy"></a>`
    ).replace(/(?<![&\w])#(\d{3,6})(?!\d)/g, (all, n) => `<a class="msgref" data-ref="${n}" href="#${n}" title="元の発言を見る">#${n}</a>`);
}
(function () {
  const box = document.getElementById("composer");
  const area = document.getElementById("body");
  if (!box || !area) return;
  async function upload(file) {
    if (!file || !/^image\/(png|jpeg|gif|webp)$/.test(file.type)) return;
    area.placeholder = "画像を保存中…";
    try {
      const res = await fetch("/api/upload?name=" + encodeURIComponent(file.name || "image"), { method: "POST", headers: { "Content-Type": file.type }, body: file });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "保存できませんでした");
      const tag = "【画像:" + data.url + "】";
      area.value = (area.value ? area.value.replace(/\s*$/, " ") : "") + tag + " ";
      area.focus();
    } catch (error) { alert("画像を貼れませんでした: " + error.message); }
    area.placeholder = "篝火に話しかける…(送信者: 潤)";
  }
  ["dragenter", "dragover"].forEach(t => document.addEventListener(t, e => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); box.classList.add("drop-ready"); } }));
  ["dragleave", "drop"].forEach(t => document.addEventListener(t, e => { if (t === "drop" || e.target === document.documentElement || !e.relatedTarget) box.classList.remove("drop-ready"); }));
  document.addEventListener("drop", e => {
    const files = [...(e.dataTransfer ? e.dataTransfer.files : [])].filter(f => f.type.startsWith("image/"));
    if (!files.length) return;
    e.preventDefault();
    files.forEach(upload);
  });
  area.addEventListener("paste", e => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter(f => f.type.startsWith("image/"));
    if (!files.length) return;
    e.preventDefault();
    files.forEach(upload);
  });
})();


// 発言番号: 本文中の「#番号」(と返信元)をクリックすると、元の発言を小さな窓で見せる。発言の左上の「#番号」をクリックすると、返信欄へ引用として入れる。
(function () {
  let pop = null;
  const close = () => { if (pop) { pop.remove(); pop = null; } };
  async function fetchMessage(id) {
    const local = state.messages.find(m => m.id === id);
    if (local) return local;
    try { const r = await fetch(`/api/messages?after=${id - 1}&limit=1`, {cache: "no-store"}); const d = await r.json(); return (d.messages || []).find(m => m.id === id) || null; } catch (e) { return null; }
  }
  async function showRef(id, anchor) {
    close();
    const m = await fetchMessage(id);
    pop = document.createElement("div"); pop.className = "ref-pop";
    if (!m) pop.textContent = "#" + id + " は見つかりません";
    else {
      const who = (m.senderDisplay || labels[m.sender] || m.sender).replace(/^(?:☀️|🌱|🌙|⭐)\s*/, "");
      const to = m.target ? "→ " + (m.targetDisplay || labels[m.target] || m.target) : "→ みんな";
      pop.innerHTML = `<div class="ref-head"><b>#${m.id}</b> ${escapeHtml(who)} ${escapeHtml(to)} <time>${formatTime(m.createdAt)}</time><button type="button" class="ref-close" aria-label="閉じる">×</button></div><div class="ref-body">${escapeHtml(m.body)}</div>`;
      pop.querySelector(".ref-close").addEventListener("click", close);
    }
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + "px";
    pop.style.top = (r.bottom + 6 + h > window.innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6) + "px";
    const el = elements.messages.querySelector(`.message[data-id="${id}"]`);       // 画面にあれば、元の発言を一瞬光らせる
    if (el) { el.classList.add("ref-flash"); setTimeout(() => el.classList.remove("ref-flash"), 1600); }
  }
  document.addEventListener("click", event => {
    const ref = event.target.closest && event.target.closest(".msgref");
    if (ref) { event.preventDefault(); showRef(Number(ref.dataset.ref), ref); return; }
    const no = event.target.closest && event.target.closest(".msgno");
    if (no) { const n = no.textContent.trim(); if (!elements.body.value.includes(n)) elements.body.value = (elements.body.value + " " + n + " ").trimStart(); elements.body.focus(); return; }
    if (pop && !event.target.closest(".ref-pop")) close();
  });
  document.addEventListener("keydown", event => { if (event.key === "Escape") close(); });
})();

// --- 宛先ボタン(右側に縦に8人: ノアズ4+エコーズ4)。押すとその人を宛先に加える(もう一度押すと外す)。ボタンで選んだ宛先は、文頭の呼びかけより優先する。送信すると解除。エコーズの絵は仮 ---
(function buildTargetRail() {
  const area = document.querySelector(".scene-area"); if (!area || document.querySelector(".target-rail")) return;
  const rail = document.createElement("nav"); rail.className = "target-rail"; rail.setAttribute("aria-label", "宛先");
  const mk = (id, echo) => {
    const b = document.createElement("button"); b.type = "button"; b.dataset.id = id; b.title = ALIASES[id][0] + (echo ? "(エコーズ)" : "(ノアズ)"); b.setAttribute("aria-pressed", "false");
    if (echo) { b.className = "echo"; b.textContent = icons[id]; } else b.style.backgroundImage = `url(/assets/icon_${id}.png)`;
    b.addEventListener("click", () => { if (state.pick.has(id)) state.pick.delete(id); else state.pick.add(id); updateTargetLabel(); elements.body.focus(); });
    return b;
  };
  const grp = (name, ids) => { const g = document.createElement("button"); g.type = "button"; g.className = "grp"; g.dataset.group = name; g.textContent = name + "全員"; g.title = name + "全員を宛先にする(もう一度で解除)"; g.setAttribute("aria-pressed", "false");
    g.addEventListener("click", () => { if (ids.every(i => state.pick.has(i))) ids.forEach(i => state.pick.delete(i)); else ids.forEach(i => state.pick.add(i)); updateTargetLabel(); elements.body.focus(); }); return g; };
  const mod = document.createElement("button"); mod.type = "button"; mod.id = "mod-toggle"; mod.className = "mod"; mod.setAttribute("aria-label", "司会(朝日)へ全体共有"); mod.setAttribute("aria-pressed", "false"); mod.style.backgroundImage = "url(/assets/icon_asahi.png)";
  mod.addEventListener("click", () => chainPost({moderator: !(state.chain && state.chain.moderator)})); rail.appendChild(mod);   // 司会=朝日(王冠つきの絵だけ)。ONの間、朝日宛てではない流れも朝日に配る
  rail.appendChild(grp("ノアズ", NOIRS)); NOIRS.forEach(id => rail.appendChild(mk(id, false)));
  const sep = document.createElement("i"); sep.className = "sep"; rail.appendChild(sep);
  rail.appendChild(grp("エコーズ", ECHOES)); ECHOES.forEach(id => rail.appendChild(mk(id, true)));
  const gap = document.createElement("i"); gap.className = "gap"; rail.appendChild(gap);   // 押し間違えないよう、人のボタンから少し離す
  const act = (txt, title, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = "grp act"; b.textContent = txt; b.title = title; b.addEventListener("click", () => { fn(); updateTargetLabel(); elements.body.focus(); }); return b; };
  const all = act("全員", "全員(8人)を宛先にする。もう一度押すと全部外れる", () => { if (EVERYONE.every(i => state.pick.has(i))) state.pick.clear(); else EVERYONE.forEach(i => state.pick.add(i)); });
  all.dataset.all = "1"; all.setAttribute("aria-pressed", "false"); rail.appendChild(all);
  const route = document.createElement("button"); route.type = "button"; route.id = "route-toggle"; route.className = "grp act route"; route.textContent = "全員通達";
  route.title = "連絡方式。全員通達=検査・報告・質問をすべて篝火に出す / 切り分け=重い作業はエコーズ側で処理し篝火には結論と質問だけ。押すと切り替わり、以後の潤さんの発言の文末に毎回この方式が付く";
  route.addEventListener("click", async () => { try { await fetch("/api/route", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({route: state.route === "split" ? "all" : "split", by:"jun"})}); } catch (e) { /* 次のpollで戻る */ } pollRoute(); elements.body.focus(); });
  rail.appendChild(route);
  area.appendChild(rail); updateTargetLabel();
})();
