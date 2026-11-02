// 篝火(127.0.0.1:8765)への通信の代行。ページ(chatgpt.com)からは直接つなげないので、拡張のバックグラウンドが行う。
const BASE = "http://127.0.0.1:8765";
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "reload") {   // 夜一が拡張を書き換えたあと、潤さんの手作業なしで新しい版に入れ替えるための合図
    sendResponse({ ok: true });
    setTimeout(() => chrome.runtime.reload(), 100);
    return;
  }
  if (msg && msg.type === "sleep") {   // 画面が裏に回るとページのタイマーは1分に1回まで間引かれる。待ち時間はバックグラウンド(間引かれない)に任せる
    setTimeout(() => sendResponse({}), Math.min(Number(msg.ms) || 0, 20000));
    return true;
  }
  if (msg && msg.type === "kagaribi") {
    if (typeof msg.path !== "string" || !msg.path.startsWith("/api/")) { sendResponse({ ok: false, error: "bad path" }); return; }
    fetch(BASE + msg.path, {
      method: msg.body ? "POST" : "GET",
      headers: msg.body ? { "Content-Type": "application/json; charset=utf-8" } : undefined,
      body: msg.body ? JSON.stringify(msg.body) : undefined
    }).then(async r => sendResponse({ ok: r.ok, status: r.status, json: await r.json().catch(() => null) }))
      .catch(e => sendResponse({ ok: false, error: String(e) }));
    return true; // 非同期で返す
  }
});
// 保険: ページが長く隠れていてタイマーが間引かれても、1分ごとにページへ「見に行け」と知らせる
chrome.alarms.create("kagaribi-tick", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(async () => {
  const tabs = await chrome.tabs.query({ url: "https://chatgpt.com/c/6ae3402d-49a0-83ee-9876-e0013df6cadd*" });
  for (const t of tabs) chrome.tabs.sendMessage(t.id, { type: "tick" }).catch(() => {});
  const etabs = await chrome.tabs.query({ url: ERISIA_URL + "*" });   // かがり火エリシア(claude.ai)
  for (const t of etabs) chrome.tabs.sendMessage(t.id, { type: "tick" }).catch(() => {});
  selfHeal().catch(() => {});
  selfHealErisia().catch(() => {});
});

// 自己回復(潤の指示 11/01): 窓口のページの取り込みが止まったら、タブを自動で再読み込みする(タブが無ければ開く)。
// 目印は、ページ側が動いているあいだ数秒ごとに書く storage の leader.at。それが HEAL_STALE_MS を超えて古ければ「止まっている」とみなす。
// 夜一の点検で、タブを開き直すと直ることを確認した(止まっていたのに画面上は「生きている」に見えていた)。
// 連続の再読み込みで暴走しないよう、間隔(HEAL_COOLDOWN_MS)と回数(HEAL_MAX。ページが動き出せば0に戻る)を制限する。上限に達したら何もしない(日向の見張りの合図が最後の手段)。
const HEAL_URL = "https://chatgpt.com/c/6ae3402d-49a0-83ee-9876-e0013df6cadd";
const HEAL_STALE_MS = 180000, HEAL_COOLDOWN_MS = 300000, HEAL_MAX = 3, HEAL_GRACE_MS = 120000;
const bgStartedAt = Date.now();
const lget = key => new Promise(r => chrome.storage.local.get(key, v => r(v[key])));
const lset = (key, val) => new Promise(r => chrome.storage.local.set({ [key]: val }, r));
async function selfHeal() {
  const now = Date.now();
  const tabs = await chrome.tabs.query({ url: HEAL_URL + "*" });
  const lead = await lget("leader");
  if (!lead && now - bgStartedAt < HEAL_GRACE_MS) return;            // 起動直後は、ページ側の最初の合図を待つ
  const age = lead && lead.at ? now - lead.at : Infinity;
  const heal = (await lget("heal")) || { count: 0, at: 0 };
  if (tabs.length && age <= HEAL_STALE_MS) { if (heal.count) await lset("heal", { ...heal, count: 0, recoveredAt: now }); return; }   // 動いている
  if (now - heal.at < HEAL_COOLDOWN_MS || heal.count >= HEAL_MAX) return;
  let action;
  if (tabs.length) { await chrome.tabs.reload(tabs[0].id); action = "再読み込み"; }
  else { await chrome.tabs.create({ url: HEAL_URL, active: false }); action = "タブを開いた"; }
  await lset("heal", { count: heal.count + 1, at: now, action, staleSec: age === Infinity ? null : Math.round(age / 1000) });
}

// かがり火エリシアの窓口(claude.ai)も同じ考え方で自己回復する。目印は storage の e_leader.at、回復の記録は e_heal。
const ERISIA_URL = "https://claude.ai/chat/82f370b8-2458-4091-aae5-f5653476c8a0";
async function selfHealErisia() {
  const now = Date.now();
  const tabs = await chrome.tabs.query({ url: ERISIA_URL + "*" });
  const lead = await lget("e_leader");
  if (!lead && now - bgStartedAt < HEAL_GRACE_MS) return;
  const age = lead && lead.at ? now - lead.at : Infinity;
  const heal = (await lget("e_heal")) || { count: 0, at: 0 };
  if (tabs.length && age <= HEAL_STALE_MS) { if (heal.count) await lset("e_heal", { ...heal, count: 0, recoveredAt: now }); return; }
  if (now - heal.at < HEAL_COOLDOWN_MS || heal.count >= HEAL_MAX) return;
  let action;
  if (tabs.length) { await chrome.tabs.reload(tabs[0].id); action = "再読み込み"; }
  else { await chrome.tabs.create({ url: ERISIA_URL, active: false }); action = "タブを開いた"; }
  await lset("e_heal", { count: heal.count + 1, at: now, action, staleSec: age === Infinity ? null : Math.round(age / 1000) });
}
