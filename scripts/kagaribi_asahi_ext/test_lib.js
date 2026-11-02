const assert = require("assert");
const L = require("./lib.js");
assert.strictEqual(L.shouldDeliver({ sender: "jun", target: null }), true);
assert.strictEqual(L.shouldDeliver({ sender: "mizuki", target: "asahi" }), true);
assert.strictEqual(L.shouldDeliver({ sender: "mizuki", target: null }), false);      // AI同士の全員宛ては配らない
assert.strictEqual(L.shouldDeliver({ sender: "mizuki", target: null }, { free: true }), true);   // 自由雑談の間は配る
assert.strictEqual(L.shouldDeliver({ sender: "asahi", target: "jun" }), false);
assert.strictEqual(L.shouldDeliver({ sender: "yoichi", target: "mizuki" }), false);
const orig = { id: 7, sender: "mizuki", target: "asahi", body: "こんにちは" };
let d = L.parseReply("【朝日→篝火】こんにちは〜☀️", orig);
assert.deepStrictEqual([d.target, d.speak], ["mizuki", true]);
d = L.parseReply("【朝日→全員】みなさん", orig); assert.strictEqual(d.target, null);
d = L.parseReply("【朝日→夜一】夜一へ", orig); assert.strictEqual(d.target, "yoichi");
d = L.parseReply("【朝日→シリウス】やあ", orig); assert.strictEqual(d.target, "sirius");
// 先頭の印(【朝日より共有】など)のあとに【朝日→相手】が来る形(2026-11-02 #2993)
d = L.parseReply("【朝日より共有】【朝日→全員】受領テストだよ", orig); assert.strictEqual(d.target, null); assert.ok(d.body.startsWith("【朝日より共有】"), "印は本文に残す");
d = L.parseReply("【潤承認 #2975】【朝日→夜一】やってね", orig); assert.strictEqual(d.target, "yoichi"); assert.ok(d.body.startsWith("【潤承認 #2975】"));
d = L.parseReply("【朝日より共有】本文だけで宛先なし", orig); assert.ok(d.body.startsWith("【朝日より共有】"));d = L.parseReply("【朝日→シリウス・エリシア】やあ", orig); assert.strictEqual(d.target, "sirius,erisia");
d = L.parseReply("【朝日→エコーズ】やあ", orig); assert.strictEqual(d.target, "soleil,astel,erisia,sirius");
d = L.parseReply("【朝日→篝火】シリウス、これお願い", orig); assert.strictEqual(d.target, "sirius");
d = L.parseReply("シリウス、これお願い", { id: 8, sender: "jun", target: null }); assert.strictEqual(d.target, "sirius");
d = L.parseReply("【朝日→全員】シリウス、みんなに", orig); assert.strictEqual(d.target, null);
d = L.parseReply("【朝日→篝火】(聞いています)", orig); assert.strictEqual(d.speak, false);
d = L.parseReply("前置きなしの返答", { id: 8, sender: "jun", target: null }); assert.deepStrictEqual([d.target, d.speak], [null, true]);
const text = "あなたの発言:\nA\nChatGPT の発言:\n古い返答\nあなたの発言:\nB\nChatGPT の発言:\n【朝日→篝火】新しい\nChatGPT は間違えることがあります。";
assert.strictEqual(L.extractLastReply(text), "【朝日→篝火】新しい");
assert.strictEqual(L.countReplies(text), 2);
assert.strictEqual(L.extractLastReply("ChatGPT の発言:\n朝の時間☀️\n\nここまでの会話は役に立っていますか？"), "朝の時間☀️");
assert.ok(L.buildPrompt(orig).includes("#7"));
// 送った発言の目印の直後の返答を読む(画面が古い発言を外して、返答の数が増えなくても動く)
const long = "あなたの発言:\n【篝火 #7・美月→朝日】前\nChatGPT の発言:\n古い\nあなたの発言:\n【篝火 #8・潤→全員】みんなお疲れ\n(朝日として…)\nChatGPT の発言:\n【朝日→全員】お疲れさま☀️\n\nChatGPT は間違えることがあります。";
assert.strictEqual(L.extractReplyAfter(long, L.promptMarker({ id: 8 })), "【朝日→全員】お疲れさま☀️");
assert.strictEqual(L.extractReplyAfter(long, L.promptMarker({ id: 9 })), "");                       // まだ送っていない/表示にない
assert.strictEqual(L.extractReplyAfter("【篝火 #8・潤→全員】本文\n(指示)\n", L.promptMarker({ id: 8 })), "");   // 返答がまだ出ていない
// 自発の返答(篝火から届いた発言への返答ではないもの)だけを取り出す
const conv = "あなたの発言:\n【篝火 #5・潤→全員】やあ\nChatGPT の発言:\n【朝日→全員】こんにちは\nあなたの発言:\n篝火に「テスト」と投稿して\nChatGPT の発言:\n【朝日→全員】テスト\nあなたの発言:\nただの雑談\nChatGPT の発言:\nやっほー☀️\nChatGPT は間違えることがあります。";
assert.deepStrictEqual(L.extractSpontaneous(conv), ["【朝日→全員】テスト"]);
// 手貼りの【篝火…】(控えに無い番号)は自発経路で拾う。拡張が配った番号は除外。返答の先頭に会話文があっても、最初の【朝日→…】から拾う。
const man = "あなたの発言:\n【篝火 #2727続き・シリウス→朝日】切り分け\nChatGPT の発言:\nうん、読んだよ。\n【朝日→シリウス】届いた?\nChatGPT は間違えることがあります。";
assert.deepStrictEqual(L.extractSpontaneous(man, []), ["【朝日→シリウス】届いた?"]);
assert.deepStrictEqual(L.extractSpontaneous(man), []);                                   // sentIds 無し=従来どおり除外
const sentConv = "あなたの発言:\n【篝火 #5・潤→全員】やあ\nChatGPT の発言:\n【朝日→全員】こんにちは";
assert.deepStrictEqual(L.extractSpontaneous(sentConv, [5]), []);                          // 拡張が配った #5 は通常経路
assert.deepStrictEqual(L.extractSpontaneous(sentConv, [9]), ["【朝日→全員】こんにちは"]);   // 控えに無い番号=手貼り扱い
// 本文中の書式説明「【朝日→…】」は宛先の印にしない(分割もしない)
const prose = "【朝日→シリウス】先頭以外の【朝日→…】拾い上げも筋いいね。";
assert.strictEqual(L.splitReply(prose, { id: 0, sender: "jun", target: null }).length, 1);
assert.strictEqual(L.splitReply(prose, { id: 0, sender: "jun", target: null })[0].target, "sirius");
assert.deepStrictEqual(L.extractSpontaneous("あなたの発言:\nx\nChatGPT の発言:\nなるほど【朝日→…】の話ね。【朝日→全員】やあ", []), ["【朝日→全員】やあ"]);
assert.strictEqual(L.hash("a"), L.hash("a"));
assert.notStrictEqual(L.hash("a"), L.hash("b"));
// 潤さんの直接の発言の抽出と、合図の判定
const talk = "あなたの発言:\n【篝火 #5・潤→全員】やあ\nChatGPT の発言:\nこんにちは\nあなたの発言:\nみんなで対応して、これ頼む\nChatGPT の発言:\n了解\nあなたの発言:\n夜一、これ頼む\nChatGPT の発言:\nうん\nあなたの発言:\nただの雑談だよ\nChatGPT の発言:\nはい";
assert.deepStrictEqual(L.extractUserTurns(talk), ["みんなで対応して、これ頼む", "夜一、これ頼む", "ただの雑談だよ"]);
assert.deepStrictEqual(L.routeFlag("みんなで対応して"), { target: null });
assert.deepStrictEqual(L.routeFlag("全員に伝えて"), { target: null });
assert.deepStrictEqual(L.routeFlag("夜一、これ頼む"), { target: "yoichi" });
assert.deepStrictEqual(L.routeFlag("ヨイチ君、お願い"), { target: "yoichi" });
assert.deepStrictEqual(L.routeFlag("美月、見て"), { target: "mizuki" });
assert.deepStrictEqual(L.routeFlag("日向さん、頼む"), { target: "hinata" });
assert.strictEqual(L.routeFlag("朝日、今日はどう?"), null);
assert.strictEqual(L.routeFlag("夜一って頼れるよね"), null);   // 文頭の呼びかけではない
// 複数の宛先が並んだ返答を、宛先ごとに分ける
const multi = L.splitReply("【朝日→夜一】夜一へ。確認して。【朝日→美月】美月へ。見てね。【朝日→全員】みんな、お願い。", { id: 0, sender: "jun", target: null });
assert.deepStrictEqual(multi.map(d => [d.target, d.body]), [["yoichi", "夜一へ。確認して。"], ["mizuki", "美月へ。見てね。"], [null, "みんな、お願い。"]]);
assert.strictEqual(L.splitReply("【朝日→全員】ひとつだけ", { id: 0, sender: "jun", target: null }).length, 1);
assert.deepStrictEqual(L.extractUserTurns("あなたの発言:\nみんなで対応して\nChatGPT が応答中\nChatGPT は間違えることがあります。"), ["みんなで対応して"]);   // 生成中の画面の文字は含めない
// 本体の依頼の許可(外出中にスマホから窓口で出す経路)
const wait1 = [{ id: 7, status: "accepted" }];
assert.deepStrictEqual(L.approvalFlag("T7 許可", wait1), { ids: [7], body: "T7 許可" });
assert.deepStrictEqual(L.approvalFlag("T7 やっといて", wait1), { ids: [7], body: "T7 やっといて" });
assert.deepStrictEqual(L.approvalFlag("オッケー", wait1), { ids: [7], body: "オッケー(T7)" });          // 受付済が1件だけなら、番号を添えて流す
assert.strictEqual(L.approvalFlag("オッケー", [{ id: 7, status: "accepted" }, { id: 8, status: "accepted" }]), null);   // 2件あれば曖昧
assert.strictEqual(L.approvalFlag("オッケー", [{ id: 7, status: "working" }]), null);                    // 許可待ちが無ければ流さない
assert.strictEqual(L.approvalFlag("オッケー", []), null);
assert.strictEqual(L.approvalFlag("T7 まだ待って", wait1), null);                                         // 否定語
assert.strictEqual(L.approvalFlag("今日の天気はどう?", wait1), null);                                     // 許可の言葉が無い
assert.strictEqual(L.approvalFlag("それでいいと思うけど、あとで別の件も含めてゆっくり考えたいな、どう思う?", wait1), null);   // 長い一言は許可と見なさない
assert.strictEqual(L.approvalFlag("T9 許可", wait1), null);                                              // 許可待ちに無い番号
assert.ok(L.buildPrompt({ id: 50, sender: "hinata", target: "asahi", body: "【許可待ち T7】試験 作業は『T7 許可』で始める" }, { allowed: true }).includes("T7 許可"));   // 通知には、許可の伝え方の指示が付く
assert.ok(!L.buildPrompt({ id: 51, sender: "jun", target: null, body: "やあ" }, { allowed: true }).includes("許可を待っている依頼"));
// 司会(日向・夜一・美月の返事を朝日が確認する)と、伝令の印
assert.strictEqual(L.shouldModerate({ id: 5, sender: "hinata", replyTo: 3, body: "こういう風にやりますね" }, { id: 3, sender: "jun", target: "hinata", body: "x" }, []), true);
assert.strictEqual(L.shouldModerate({ id: 5, sender: "hinata", replyTo: 3, body: "こういう風にやりますね" }, { id: 3, sender: "jun", target: "hinata", body: "x" }, [3]), false);   // 1つの指示につき1回
assert.strictEqual(L.shouldModerate({ id: 5, sender: "hinata", replyTo: 3, body: "【朝日→潤】こういう風にやりますね" }, { id: 3, sender: "jun", target: "hinata", body: "x" }, []), false);
assert.strictEqual(L.applyRelayMarks({ target: "hinata", body: "[承認] やってね" }, { id: 9 }).body, "【潤承認 #9】やってね");
assert.strictEqual(L.applyRelayMarks({ target: "hinata", body: "[訂正] ここは違う" }, { id: 9 }).body, "【潤の訂正 #9】ここは違う");
console.log("lib tests OK");
