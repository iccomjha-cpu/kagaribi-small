// 篝火の野営地シーン v3(レイヤー別・差し替え可能)。キャラクターシート docs/kagaribi_character_sheet.md と、美月の実装仕様表 KAGARIBI_v0.1_SPEC.md を基準とする。2026-10-29 夜一。
// 方針: 見た目の基準画像は朝日側が作る。ここは「受け取って載せる」構造。重いCanvas/WebGL・常時AI生成・動画背景は使わない。
//  - レイヤー: 空・遠景(sky) / 舞台(stage) / 4人(characters) / 篝火(fire) / 光(glow はCSS)。画像は static/assets/ に置き、manifest.json に名前を書くと既定の絵の代わりに使われる。
//      assets/manifest.json 例: {"sky":"sky_night.png","stage":"stage.png","characters":{"asahi":"asahi.png","hinata":"hinata.png","mizuki":"mizuki.png","yoichi":"yoichi.png"},"fire":"fire_sheet.png","fireFrames":3}
//      (旧キー "background" も sky として読む。炎が fire_01.png〜fire_04.png の別ファイルなら "fireFiles":["fire_01.png",...] も可)
//  - 既定の絵は、起動時に1回だけ小さなキャンバスで描いて静止画(data URL)にする。毎フレームの描画はしない。動き(揺れ・炎・星・火の粉)はCSSだけ。
//  - 名前・時刻・本文などの文字は画像に焼き込まない(HTMLで描く)。
(() => {
  const wrap = document.querySelector(".scene-wrap");
  if (!wrap) return;
  const W = 192, H = 108, GROUND = 78;
  const px = (c, x, y, w, h, col) => { c.fillStyle = col; c.fillRect(Math.round(x), Math.round(y), w, h); };
  const mk = (w, h) => { const cv = document.createElement("canvas"); cv.width = w; cv.height = h; const c = cv.getContext("2d"); c.imageSmoothingEnabled = false; return [cv, c]; };

  // --- キャラクターシートの色(◇暫定案) ---
  const CAST = {
    asahi:  { hair: "#fbefc0", hair2: "#e8d48a", outfit: "#f2a03c", outfit2: "#c97a1f", skin: "#ffd9b8", kind: "child" },
    hinata: { hair: "#7a4b2a", hair2: "#5a3319", outfit: "#c58a5b", outfit2: "#6fa86b", skin: "#ffd0a8", kind: "glasses" },
    mizuki: { hair: "#cfc8e8", hair2: "#a79fc9", outfit: "#8e9bda", outfit2: "#5f6bb3", skin: "#ffe0cc", kind: "long" },
    yoichi: { hair: "#1b2140", hair2: "#0f1430", outfit: "#2f4c8f", outfit2: "#1f3468", skin: "#f5cfae", kind: "tall" }
  };
  const SEATS = [42, 69, 123, 150];   // 4人が座る位置(0〜192の座標)
  const PALETTES = {
    night: { sky: ["#070a1f", "#0f1538", "#1a1f55", "#2b2766", "#43336f"], hill: "#171640", hill2: "#10112e", tree: "#080a1c", ground: "#1b2a2b", ground2: "#142021", star: "#fff6d8", moon: "#f4efd0", town: "#0c0e26", water: "#2a3a78", water2: "#5f77c4" },
    dawn:  { sky: ["#161a44", "#2c2c6a", "#5b3f7e", "#a8527f", "#e58a6a"], hill: "#2a2352", hill2: "#1c1a3c", tree: "#0c0e24", ground: "#223030", ground2: "#192525", star: "#ffe9c8", moon: "#fbe6c0", town: "#14153a", water: "#43467e", water2: "#e0a890" },
    dusk:  { sky: ["#1b2158", "#3a3274", "#7a4a82", "#d1704f", "#f2a45a"], hill: "#34285a", hill2: "#231d42", tree: "#0e1026", ground: "#27332d", ground2: "#1d2823", star: "#ffe9c8", moon: "#fbe6c0", town: "#1a1638", water: "#4a4284", water2: "#f2b07a" }
  };
  const BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]];
  const jstHour = () => (new Date().getUTCHours() + 9) % 24;
  const timeBand = h => h >= 4 && h < 6 ? "predawn" : h < 10 && h >= 6 ? "morning" : h >= 10 && h < 16 ? "noon" : h >= 16 && h < 19 ? "dusk" : h >= 19 && h < 23 ? "night" : "midnight";   // 日本時間の6区分
  const AUTO_TIME = false;   // 既定は常に夜(v0.1は夜だけ完成していればよい)。trueで実際の日本時間により夜明け・夕暮れへ自動で変わる(手動切替は付けない)
  const paletteName = h => !AUTO_TIME || (h >= 19 || h < 4) ? "night" : (h < 7 ? "dawn" : "dusk");

  // --- レイヤー1: 空・遠景(空・月・星・遠い山・街・橋・水面・木) ---
  function defaultSky(name) {
    const P = PALETTES[name]; const [cv, b] = mk(W, H);
    const bands = P.sky.length;
    for (let y = 0; y < GROUND + 6; y++) for (let x = 0; x < W; x++) {
      const t = (y / (GROUND + 6)) * (bands - 1), i = Math.floor(t), f = t - i;
      px(b, x, y, 1, 1, P.sky[f * 16 > BAYER[y % 4][x % 4] ? Math.min(i + 1, bands - 1) : i]);
    }
    let seed = 7; const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
    for (let i = 0; i < (name === "dusk" ? 6 : 40); i++) px(b, Math.floor(rnd() * W), Math.floor(rnd() * 40), 1, 1, P.star);
    const mx = 160, my = 16;
    for (let dy = -7; dy <= 7; dy++) for (let dx = -7; dx <= 7; dx++) { const d = dx * dx + dy * dy; if (d <= 49) px(b, mx + dx, my + dy, 1, 1, d > 40 ? "#d9d2a8" : P.moon); }
    [[-2, -2], [2, 1], [-1, 3], [3, -3]].forEach(([dx, dy]) => px(b, mx + dx, my + dy, 1, 1, "#cfc79a"));
    const ridge = [40, 38, 36, 35, 33, 34, 36, 38, 41, 43, 45, 44, 42, 40, 38, 37, 39, 42, 45, 47, 46, 44, 42, 41, 43, 46, 48, 47, 45, 42, 40, 39];
    for (let x = 0; x < W; x++) { const top = ridge[Math.floor(x / 6) % ridge.length] + 14; px(b, x, top, 1, GROUND - top + 6, P.hill); px(b, x, top + 10, 1, GROUND - top - 4, P.hill2); }
    // 遠景の街(窓の明かり)・橋・水面
    const bld = [[58, 10], [66, 14], [74, 8], [80, 16], [90, 11], [99, 18], [108, 9], [116, 13], [124, 7]];
    bld.forEach(([x, h], k) => { px(b, x, 66 - h, 7, h + 4, P.town); if (k % 2 === 0) px(b, x + 2, 66 - h + 3, 1, 1, "#ffd88a"); px(b, x + 4, 66 - h + 6, 1, 1, "#ffd88a"); if (k % 3 === 1) px(b, x + 1, 66 - h + 9, 1, 1, "#ffd88a"); });
    for (let x = 52; x < 134; x++) { const a = Math.sin(((x - 52) / 82) * Math.PI); px(b, x, 66 - Math.round(a * 3), 1, 2, P.town); }   // 橋
    for (let y = 68; y < 76; y++) for (let x = 40; x < 150; x++) { px(b, x, y, 1, 1, P.water); if ((x + y * 3) % 11 === 0 && BAYER[y % 4][x % 4] < 7) px(b, x, y, 1, 1, P.water2); }   // 水面の光
    const pine = (cx, base, h) => { for (let i = 0; i < h; i++) { const w = Math.max(1, Math.round((i / h) * 7)); px(b, cx - w, base - h + i, w * 2 + 1, 1, P.tree); } px(b, cx, base, 1, 4, P.tree); };
    [[26, 78, 18], [34, 79, 24], [160, 78, 16], [168, 79, 22]].forEach(a => pine(...a));
    return cv.toDataURL();
  }

  // --- レイヤー2: 舞台(地面・石の柱・ランタン・座る場所・箱・小物。キャラクターと篝火は含めない。背景は透過) ---
  function defaultStage(name) {
    const P = PALETTES[name]; const [cv, b] = mk(W, H);
    px(b, 0, GROUND, W, H - GROUND, P.ground);
    for (let y = GROUND; y < H; y++) for (let x = 0; x < W; x++) if ((x + y) % 7 === 0 && BAYER[y % 4][x % 4] < 6) px(b, x, y, 1, 1, P.ground2);
    // 木の床(中央の広場)
    for (let x = 56; x < 136; x += 8) { px(b, x, 92, 7, 12, "#3a2a24"); px(b, x, 92, 7, 1, "#4d3a30"); px(b, x + 7, 92, 1, 12, "#241a16"); }
    // 石の柱(左右)とランタン
    [[6, 70], [178, 70]].forEach(([x, y]) => {
      px(b, x, y, 8, 34, "#3b3e5c"); px(b, x, y, 8, 3, "#555a82"); px(b, x + 1, y + 6, 1, 8, "#2b2e48"); px(b, x + 5, y + 16, 1, 6, "#2b2e48"); px(b, x - 1, y + 33, 10, 2, "#2b2e48");
    });
    [[16, 72], [168, 72]].forEach(([x, y]) => {
      px(b, x, y, 1, 22, "#241a16"); px(b, x - 2, y - 7, 5, 6, "#5a3a22"); px(b, x - 1, y - 6, 3, 4, "#ffd88a"); px(b, x, y - 5, 1, 2, "#fff3b8");
      for (let r = 12; r > 3; r -= 3) { b.fillStyle = `rgba(255,190,90,${((1 - r / 13) * 0.22).toFixed(3)})`; for (let yy = -r; yy <= r; yy++) for (let xx = -r; xx <= r; xx++) if (xx * xx + yy * yy <= r * r && BAYER[(y + yy) & 3][(x + xx) & 3] < 8) b.fillRect(x + xx, y - 4 + yy, 1, 1); }
    });
    // 座る場所(丸太のベンチ)・箱・小物
    SEATS.forEach(sx => { px(b, sx - 11, 94, 22, 4, "#6a4528"); px(b, sx - 11, 94, 22, 1, "#8a5a36"); px(b, sx - 9, 98, 2, 3, "#3d2615"); px(b, sx + 7, 98, 2, 3, "#3d2615"); });
    [[30, 88], [162, 88]].forEach(([x, y]) => { px(b, x, y, 10, 9, "#7a5230"); px(b, x, y, 10, 1, "#9a6c42"); px(b, x + 4, y, 2, 9, "#5a3a22"); px(b, x, y + 4, 10, 1, "#5a3a22"); });
    [[48, 102], [140, 103], [96, 104]].forEach(([x, y]) => { px(b, x, y, 3, 2, "#6b6a78"); px(b, x + 1, y - 1, 2, 1, "#8a8aa0"); });
    for (let i = 0; i < 9; i++) { const a = (i / 9) * 6.283; px(b, 96 + Math.cos(a) * 9, 96 + Math.sin(a) * 3, 3, 2, i % 2 ? "#6b6a78" : "#4d4c5c"); }   // 篝火の石(火そのものは別レイヤー)
    return cv.toDataURL();
  }

  // --- レイヤー3: キャラクター(24x28、足元が下端。背景は透過) ---
  function defaultSprite(who) {
    const d = CAST[who]; const [cv, c] = mk(24, 28); const x = 12, sit = 27;
    const tall = d.kind === "tall", child = d.kind === "child";
    const bodyH = tall ? 11 : child ? 7 : 9, headS = child ? 10 : 9, by = sit - bodyH - 2, hy = by - headS + 1;
    px(c, x - 6, sit - 1, 12, 2, "#0b0d1b");
    px(c, x - 5, by, 10, bodyH, d.outfit); px(c, x - 5, by, 10, 2, d.outfit2);
    px(c, x - 4, by + bodyH, 3, 2, "#2b2438"); px(c, x + 1, by + bodyH, 3, 2, "#2b2438");
    if (d.kind === "glasses") px(c, x - 1, by + 3, 3, 2, d.outfit2);
    if (tall) { px(c, x - 6, by + 1, 1, 6, d.outfit2); px(c, x + 5, by + 1, 1, 6, d.outfit2); }
    px(c, x - 4, hy, 9, headS, d.skin);
    px(c, x - 5, hy - 1, 11, 4, d.hair); px(c, x - 5, hy + 2, 2, 4, d.hair); px(c, x + 4, hy + 2, 2, 4, d.hair);
    if (d.kind === "long") { px(c, x - 6, hy + 3, 2, 9, d.hair); px(c, x + 5, hy + 3, 2, 9, d.hair); px(c, x - 5, hy + 9, 2, 3, d.hair2); px(c, x + 4, hy + 9, 2, 3, d.hair2); px(c, x + 3, hy - 1, 2, 2, "#fff3b8"); }
    if (child) { px(c, x, hy - 4, 1, 3, d.hair); px(c, x + 1, hy - 5, 1, 2, d.hair); }
    if (tall) { px(c, x + 3, hy, 2, 2, "#ffe9a0"); px(c, x + 3, hy + 1, 1, 1, "#fff6d8"); }
    const ey = hy + Math.floor(headS / 2);
    px(c, x - 2, ey, 1, 2, "#1a1426"); px(c, x + 2, ey, 1, 2, "#1a1426");
    if (d.kind === "glasses") { [[-3, -1], [-3, 2], [1, -1], [1, 2]].forEach(([dx, dy]) => px(c, x + dx, ey + dy, 3, 1, "#2b2438")); px(c, x, ey, 1, 1, "#2b2438"); }
    px(c, x - 1, ey + 3, 3, 1, child ? "#d9645a" : "#b87b6a");
    px(c, x - 3, ey + 2, 1, 1, child ? "#ff9d8a" : d.skin); px(c, x + 3, ey + 2, 1, 1, child ? "#ff9d8a" : d.skin);
    return cv.toDataURL();
  }

  // --- レイヤー4: 篝火(32x32 を3コマ横に並べたシート。炎は低め・小さめ) ---
  function defaultFireSheet() {
    const [cv, c] = mk(96, 32);
    const frames = [
      [[0, 0, 7], [-1, -3, 5], [1, -5, 2]],
      [[0, 0, 7], [1, -3, 5], [-1, -5, 2]],
      [[0, 0, 7], [0, -4, 6], [0, -6, 2]]
    ];
    frames.forEach((shapes, f) => {
      const fx = 16 + f * 32, fy = 26;
      px(c, fx - 8, fy + 1, 16, 3, "#5b3a22"); px(c, fx - 6, fy - 1, 12, 3, "#74492a"); px(c, fx - 8, fy + 1, 2, 3, "#3d2615"); px(c, fx + 6, fy + 1, 2, 3, "#3d2615");
      shapes.forEach(([ox, oy, w]) => { px(c, fx + ox - w / 2, fy + oy - 3, w, 3, "#e8872c"); if (w > 2) px(c, fx + ox - (w - 2) / 2, fy + oy - 3, w - 2, 3, "#f5b43a"); });
      shapes.slice(0, 2).forEach(([ox, oy, w]) => px(c, fx + ox - (w - 4) / 2, fy + oy - 2, Math.max(1, w - 4), 2, "#ffe27a"));
      px(c, fx - 1, fy - 3, 2, 2, "#fff6c8");
    });
    return cv.toDataURL();
  }

  // --- 背景の動き: 横長の背景をゆっくり左右に動かし、一定時間ごとに次の背景へフェード ---
  const skyRun = { token: 0 };
  async function startSky(slides) {
    const run = ++skyRun.token;
    for (const old of skyEl.querySelectorAll(".sl")) old.remove();
    if (!Array.isArray(slides) || !slides.length) return;
    const list = [];
    for (const s of slides) { const u = "/assets/" + s.file; if (await ok(u)) list.push({ url: u, sec: Math.max(20, s.seconds || 90) }); }
    if (!list.length || run !== skyRun.token) return;
    const still = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;   // 動きを減らす設定なら、動かさず切り替えだけ
    const layers = [0, 1].map(() => { const d = document.createElement("div"); d.className = "sl"; skyEl.appendChild(d); return d; });
    let i = 0, front = 0;
    const show = (layer, item, forward) => {
      layer.style.transition = "none"; layer.style.backgroundImage = `url("${item.url}")`; layer.style.backgroundPosition = (forward ? "0%" : "100%") + " 0";
      void layer.offsetWidth;
      layer.style.transition = still ? "opacity 4s ease" : `background-position ${item.sec}s linear, opacity 4s ease`;
      if (!still) layer.style.backgroundPosition = (forward ? "100%" : "0%") + " 0";
    };
    show(layers[0], list[0], true); layers[0].style.opacity = 1;
    const step = () => {
      if (run !== skyRun.token) return;
      const cur = list[i % list.length], next = list[(i + 1) % list.length], back = layers[1 - front], fwd = (i + 1) % 2 === 0;
      setTimeout(() => {
        if (run !== skyRun.token) return;
        back.style.opacity = 0; show(back, next, fwd);
        void back.offsetWidth; back.style.opacity = 1; layers[front].style.opacity = 0;
        front = 1 - front; i++; step();
      }, Math.max(8, cur.sec - 4) * 1000);
    };
    if (list.length > 1) step();
  }
  // --- 差し替え(assets/)の確認と反映 ---
  const ok = url => new Promise(res => { const i = new Image(); i.onload = () => res(true); i.onerror = () => res(false); i.src = url; });
  const css = (el, prop, url) => { el.style.setProperty(prop, `url("${url}")`); };
  const skyEl = wrap.querySelector(".sky"), stageEl = wrap.querySelector(".stage"), fireEl = wrap.querySelector(".fire .sheet");
  let manifest = {};
  async function pick(file, fallback) { const url = file ? "/assets/" + file : null; return url && await ok(url) ? url : fallback(); }
  async function applyAssets() {
    try { const r = await fetch("/assets/manifest.json", { cache: "no-store" }); if (r.ok) manifest = await r.json(); } catch (e) { manifest = {}; }
    let name = paletteName(jstHour());
    const band = manifest.skyByTime ? timeBand(jstHour()) : null;   // 時間帯別の背景(夜明け前・朝・昼・夕暮れ・夜・深夜)があれば、日本時間で選ぶ
    if (band) name = band;
    wrap.classList.toggle("smooth", !!manifest.smooth);              // 高解像度の画像(なめらか表示)
    css(skyEl, "--sky-image", await pick(band ? manifest.skyByTime[band] : (manifest.sky || manifest.background), () => defaultSky(paletteName(jstHour()))));
    css(stageEl, "--stage-image", await pick(manifest.stage, () => defaultStage(name)));
    for (const who of Object.keys(CAST)) {
      const el = wrap.querySelector(`.actor[data-actor="${who}"] .sprite`);
      css(el, "--sprite-image", await pick(manifest.characters && manifest.characters[who], () => defaultSprite(who)));
      for (const [v, k] of [["talk", "--sprite-talk"], ["think", "--sprite-think"], ["sad", "--sprite-sad"], ["anger", "--sprite-anger"]]) {   // 表情の差分(話す=楽・考え中・停止=哀)。無ければ基本の絵のまま
        const f = manifest.expressions && manifest.expressions[who] && manifest.expressions[who][v]; const u = f ? "/assets/" + f : null;
        if (u && await ok(u)) css(el, k, u); else el.style.removeProperty(k);
      }
    }
    startSky(manifest.skySlides);   // 背景がゆっくり左右に動き、時間が経つと次の背景へフェードする(manifest.skySlides があるとき)
    for (const k of ["anger", "sad", "think", "joy"]) {   // 気分の記号の絵(差し替え用)。manifest.marks に {anger:"mark_anger.png", sad:..., think:..., joy:...} を書くと、絵文字の代わりに使う
      const f = manifest.marks && manifest.marks[k]; const u = f ? "/assets/" + f : null; const on = !!(u && await ok(u));
      if (on) css(wrap, "--mark-" + k, u); else wrap.style.removeProperty("--mark-" + k);
      let any = on;
      for (const a of wrap.querySelectorAll(".actor")) {   // キャラ別の絵(manifest.marksBy[キャラ][種類])があれば、そのキャラだけ差し替える
        const who = ["asahi", "hinata", "mizuki", "yoichi"].find(w => a.classList.contains(w));
        const g = who && manifest.marksBy && manifest.marksBy[who] && manifest.marksBy[who][k]; const gu = g ? "/assets/" + g : null;
        if (gu && await ok(gu)) { css(a, "--mark-" + k, gu); any = true; } else a.style.removeProperty("--mark-" + k);
      }
      wrap.classList.toggle("mark-img-" + k, any);
    }
    css(fireEl, "--fire-image", await pick(manifest.fire, defaultFireSheet));
    if (manifest.fireFrames) fireEl.style.setProperty("--fire-frames", String(manifest.fireFrames));
    wrap.dataset.palette = name;
  }
  applyAssets();
  setInterval(() => { if (manifest.skyByTime ? timeBand(jstHour()) !== wrap.dataset.palette : (!manifest.sky && !manifest.background && paletteName(jstHour()) !== wrap.dataset.palette)) applyAssets(); }, 5 * 60 * 1000);

  // --- 寝姿: 全員が idle のまま manifest.sleepAfterSec(既定600秒)たつと、manifest.sleepScene の絵をゆっくり重ねる。誰かが動く・発言すると消える ---
  const sleepEl = document.createElement("div"); sleepEl.className = "sleep-scene"; wrap.appendChild(sleepEl);
  let lastActive = Date.now();
  const wake = () => { lastActive = Date.now(); sleepEl.classList.remove("on"); };
  setInterval(() => {
    if (!manifest.sleepScene || Date.now() - lastActive < (manifest.sleepAfterSec || 600) * 1000) return;
    if (!sleepEl.style.backgroundImage) sleepEl.style.backgroundImage = `url("/assets/${manifest.sleepScene}")`;
    sleepEl.classList.add("on");
  }, 5000);
  window.kagaribiSleepNow = () => { lastActive = 0; };   // 確認用: 即座に寝姿へ

  // --- 影と火の照り返し: 各キャラの足元に影を置き、火と反対側へ伸ばす。揺らぎ(--lit)は炎のコマ送りと同じ周期(CSS側) ---
  const fireBox = wrap.querySelector(".fire");
  const actors = [...wrap.querySelectorAll(".actor")];
  for (const a of actors) if (!a.querySelector(".shadow")) { const sh = document.createElement("i"); sh.className = "shadow"; a.insertBefore(sh, a.querySelector(".sprite")); }
  function aimShadows() {
    const f = fireBox.getBoundingClientRect(), fx = f.left + f.width / 2;
    for (const a of actors) {
      const r = a.getBoundingClientRect(), d = (r.left + r.width / 2) - fx;
      a.style.setProperty("--dir", Math.max(-1, Math.min(1, d / (wrap.getBoundingClientRect().width * 0.25))).toFixed(2));   // 火から遠いほど長く、反対側へ
    }
  }
  aimShadows(); window.addEventListener("resize", aimShadows); setTimeout(aimShadows, 1500);

  // --- 画像なしの演出(2026-10-31 夜一): 火の粉・星のまたたき(と流れ星)・火明かり(画面の隅を暗く、火のまわりを暖かく) ---
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const glow = document.createElement("div"); glow.className = "fx-glow"; wrap.appendChild(glow);
  if (!reduce) {
    const cv = document.createElement("canvas"); cv.className = "fx-canvas"; wrap.appendChild(cv);
    const g = cv.getContext("2d");
    const cvb = document.createElement("canvas"); cvb.className = "fx-canvas fx-back"; skyEl.after(cvb);   // 星・流れ星は舞台の後ろ(空の上)に描く
    const gb = cvb.getContext("2d");
    let W = 0, H = 0, stars = [], sparks = [], shoot = null;
    const size = () => {
      const r = cv.getBoundingClientRect(); W = cv.width = cvb.width = Math.max(1, Math.round(r.width)); H = cv.height = cvb.height = Math.max(1, Math.round(r.height));
      stars = Array.from({ length: 34 }, () => ({ x: Math.random() * W, y: Math.random() * H * 0.30, ph: Math.random() * 6.28, sp: 0.6 + Math.random() * 1.6, big: Math.random() < 0.18 }));
    };
    size(); window.addEventListener("resize", size);
    const busy = () => wrap.querySelectorAll('.actor[data-state="working"],.actor[data-state="thinking"]').length;
    setInterval(() => {
      const t = performance.now() / 1000, px = Math.max(2, Math.round(W / 190));   // ドットの大きさ(画面幅に比例)
      g.clearRect(0, 0, W, H); gb.clearRect(0, 0, W, H);
      for (const s of stars) {                                   // 星: ゆっくりまたたく(明るさを段階で変える)
        const a = 0.18 + 0.82 * Math.max(0, Math.sin(t * s.sp + s.ph)) ** 2;
        gb.fillStyle = `rgba(255,246,214,${(a * 0.85).toFixed(2)})`; const k = s.big ? px * 1.6 : px;
        gb.fillRect(Math.round(s.x / px) * px, Math.round(s.y / px) * px, k, k);
      }
      if (!shoot && Math.random() < 0.004) shoot = { x: Math.random() * W * 0.7, y: Math.random() * H * 0.12, life: 0 };   // 流れ星(ときどき)
      if (shoot) {
        shoot.life += 1; const L = shoot.life;
        for (let i = 0; i < 7; i++) { gb.fillStyle = `rgba(255,244,210,${(0.9 - i * 0.12).toFixed(2)})`; gb.fillRect(Math.round((shoot.x + L * px * 1.6 - i * px) / px) * px, Math.round((shoot.y + L * px * 0.7 - i * px * 0.45) / px) * px, px, px); }
        if (L > 22) shoot = null;
      }
      const f = wrap.querySelector(".fire").getBoundingClientRect(), w = cv.getBoundingClientRect();
      if (Math.abs(w.width - W) > 1 || Math.abs(w.height - H) > 1) size();      // 描画面の大きさがずれたら測り直す(ずれたまま描くと火の粉が左に寄る)
      const fx = f.left - w.left + f.width / 2, fy = f.top - w.top + f.height * 0.45;
      const hk = ({ off: 0.45, chat: 0.65, debate: 0.85, work: 1 })[wrap.dataset.heat] || 0.65;       // 火が小さいほど、火の粉も小さく・少なく
      const rate = (0.55 + Math.min(3, busy()) * 0.5) * (0.5 + hk * 0.5);               // 作業中・考え中が多いほど火の粉が増える
      while (Math.random() < rate / (1 + sparks.length / 14)) sparks.push({ x: fx + (Math.random() - 0.5) * f.width * 0.7, y: fy, vx: (Math.random() - 0.5) * 0.7, vy: -(0.8 + Math.random() * 1.5) * (0.6 + hk * 0.4), life: 0, max: 40 + Math.random() * 50, sz: hk });
      sparks = sparks.filter(p => p.life < p.max);
      for (const p of sparks) {                                    // 火の粉: 上へ昇り、ゆらぎながら赤→橙→黄→消える
        p.life++; p.x += p.vx + Math.sin(p.life * 0.2 + p.max) * 0.35; p.y += p.vy;
        const k = p.life / p.max, col = k < 0.35 ? "255,214,110" : k < 0.7 ? "255,150,50" : "220,80,40";
        g.fillStyle = `rgba(${col},${(1 - k).toFixed(2)})`; const q = Math.max(1, Math.round(px * (p.sz || 1))); g.fillRect(Math.round(p.x / px) * px, Math.round(p.y / px) * px, q, q);
      }
    }, 66);
  }

  // --- 風にそよぐ蔦と、ぼんやり明滅するランタン(2026-10-31 潤の指示): 舞台の絵の一部を複製して、ゆっくり揺らす/光を重ねる ---
  if (!reduce && stageEl) {
    const LEAVES = []; for (let ty = 0; ty < 600; ty += 100) for (let tx = 0; tx < 900; tx += 110) LEAVES.push([tx, ty, 150, 140]);   // 緑の層(stage_green.png)を細かい区画に分けて、区画ごとに少しずつ違う揺れ方をさせる
    LEAVES.forEach(([x, y, w, h], i) => {
      const fx = x / 900, fy = y / 600, fw = w / 900, fh = h / 600, d = document.createElement("i"); d.className = "leaf";
      d.style.cssText = `left:${fx * 100}%;top:${fy * 100}%;width:${fw * 100}%;height:${fh * 100}%;background-size:${100 / fw}% ${100 / fh}%;background-position:${fw >= 1 ? 0 : fx / (1 - fw) * 100}% ${fh >= 1 ? 0 : fy / (1 - fh) * 100}%;--sp:${(4 + (i * 1.7) % 4).toFixed(1)}s;--dl:${(-(i * 2.3) % 9).toFixed(1)}s`;
      stageEl.appendChild(d);
    });
    [[255, 88, 0], [832, 88, -2.7], [62, 478, -4.1]].forEach(([x, y, dl], i) => {         // 吊りランタン左右2つと、机の上のランタン
      const d = document.createElement("i"); d.className = "lamp"; d.style.cssText = `left:${x / 9}%;top:${y / 6}%;--sp:${(6.5 + i * 1.6).toFixed(1)}s;--dl:${dl}s`; stageEl.appendChild(d);
    });
  }

  // --- 焚き火の段階絵(2026-10-31 潤の指示): 舞台の焚き火のまわりを編集した絵を、火の強さに合わせて、なめらかに重ねて切り替える ---
  if (stageEl) {
    const box = document.createElement("div"); box.className = "fireedit"; stageEl.appendChild(box);   // 舞台の絵(1800x1200)の (690,620) 480x480 の場所
    const names = ["0", "1", "2", "3", "4", "4b"], layers = {};
    names.forEach(n => {
      const im = new Image(); im.alt = ""; im.className = "fl fl-" + n; im.onload = () => { layers[n] = im; box.appendChild(im); upd(); }; im.src = "/assets/fire/layer_" + n + ".png";
    });
    const STAGE_OF = { off: "0", chat: "2", debate: "3", work: "4" };                              // 火の強さ → 段階(3は届き次第。ないときは近い方を使う)
    const order = ["0", "1", "2", "3", "4"];
    let cur = "";
    function pickStage(want) { for (let i = order.indexOf(want); i >= 0; i--) if (layers[order[i]]) return order[i]; return order.find(n => layers[n]) || ""; }
    function upd() {
      const want = pickStage(STAGE_OF[wrap.dataset.heat || "chat"] || "2"); if (!want || want === cur) return; cur = want;
      for (const n of Object.keys(layers)) layers[n].classList.toggle("on", n === want || (want === "4" && n === "4b"));
      if (layers["4b"]) layers["4b"].classList.toggle("sh", want === "4");                           // 最大のときは、2枚をゆっくり入れ替えて、燃え盛らせる
      box.dataset.stage = want;
    }
    setInterval(upd, 500);
  }

  // --- 吹き出し: 状態・感情に合わせた短い定型の台詞だけを出す(assets/lines.json。同じ台詞が続かないよう、ランダムに選ぶ) ---
  const NAMES = { asahi: "朝日", hinata: "日向", mizuki: "美月", yoichi: "夜一" }, ICONS = { asahi: "☀️", hinata: "🌱", mizuki: "🌙", yoichi: "⭐" };
  let LINES = {}; const lastLine = {}, bubbleT = {};
  fetch("/assets/lines.json", { cache: "no-store" }).then(r => r.ok ? r.json() : {}).then(j => { LINES = j.lines || {}; sayIdleSoon(); }).catch(() => {});
  function say(id, kind) {
    const el = wrap.querySelector(`.actor[data-actor="${id}"] .bubble`); const list = (LINES[id] || {})[kind];
    if (!el || !list || !list.length) return;
    let t, n = 0; do { t = list[Math.floor(Math.random() * list.length)]; } while (t === lastLine[id] && list.length > 1 && ++n < 6);
    lastLine[id] = t; el.dataset.name = NAMES[id]; el.dataset.icon = ICONS[id]; el.dataset.kind = kind; el.textContent = t;
    el.classList.add("show"); clearTimeout(bubbleT[id]); bubbleT[id] = setTimeout(() => el.classList.remove("show"), kind === "idle" ? 4800 : 6500);   // 時間が来たら、ふわっと消える
  }
  function sayIdleSoon() { for (const id of Object.keys(NAMES)) say(id, "idle"); }
  setInterval(() => {                                    // ときどき、待機中のひとことに入れ替わる
    const ids = Object.keys(NAMES).filter(id => { const a = wrap.querySelector(`.actor[data-actor="${id}"]`); return a && (a.dataset.state || "idle") === "idle" && !a.classList.contains("talking"); });
    if (ids.length) say(ids[Math.floor(Math.random() * ids.length)], "idle");
  }, 20000);

  // --- 舞台のギミック: ランタンのレバー(見守り↔おまかせ)と、火の大きさ(消す/小/中/大)。状態は /api/mode に保存 ---
  (function () {
    const HEAT = ["off", "chat", "debate", "work"], NAME = { off: "火を消す(待機)", chat: "小(雑談まで)", debate: "中(討論まで)", work: "大(実装まで)" };
    let st = { mode: "watch", heat: "chat" };
    const lever = document.createElement("button"); lever.type = "button"; lever.className = "lever"; lever.setAttribute("aria-label", "ランタンのレバー(見守り/おまかせ)");
    lever.innerHTML = '<span class="chain"></span><span class="knob"></span><span class="lbl"></span>'; wrap.appendChild(lever);
    const tip = document.createElement("div"); tip.className = "gimmick-tip"; wrap.appendChild(tip);
    let tipTimer = 0; const say2 = t => { tip.textContent = t; tip.classList.add("on"); clearTimeout(tipTimer); tipTimer = setTimeout(() => tip.classList.remove("on"), 2600); };
    function paint() {
      wrap.dataset.mode = st.mode; wrap.dataset.heat = st.heat || "chat";
      lever.querySelector(".lbl").textContent = st.mode === "auto" ? "おまかせ" : "見守り";
    }
    async function load() { try { const j = await (await fetch("/api/mode", { cache: "no-store" })).json(); st = j.heat ? j : { mode: j.mode || "watch", heat: "chat" }; paint(); } catch (e) {} }
    async function save(p) { try { const j = await (await fetch("/api/mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...p, by: "jun" }) })).json(); if (j.mode) { st = j; paint(); return true; } } catch (e) {} return false; }
    lever.addEventListener("click", async e => { e.stopPropagation(); const next = st.mode === "auto" ? "watch" : "auto"; if (await save({ mode: next })) say2(next === "auto" ? "レバーを引いた: おまかせ(みんなで自由に)" : "レバーを戻した: 見守り(火で調整)"); });
    const fireB = wrap.querySelector(".fire");
    if (fireB) fireB.addEventListener("click", async () => {
      if (st.mode === "auto") { say2("おまかせ中は、火は自動で変わるよ"); return; }
      const next = HEAT[(HEAT.indexOf(st.heat || "chat") + 1) % HEAT.length]; if (await save({ heat: next })) say2("火: " + NAME[next]);
    });
    window.kagaribiStepHeat = async d => {                                                          // 額縁の左右スイッチ: 火を1段ずつ強く/弱く(端で一周する)。表示と保存だけで、AIへの指示は走らない
      if (st.mode === "auto") { say2("おまかせ中は、火は自動で変わるよ"); return; }
      const next = HEAT[(HEAT.indexOf(st.heat || "chat") + d + HEAT.length) % HEAT.length]; if (await save({ heat: next })) say2("火: " + NAME[next]);
    };
    setInterval(load, 15000); load();
  })();

  // --- 額縁: 火の強さごとに別の額縁(assets/frame/frame.json)。左右の炎のスイッチで1段ずつ切り替え(端で一周)。9分割で、画面の大きさに合わせて伸ばす ---
  (function () {
    const FRAME_OF = { off: "watch", chat: "chat", debate: "debate", work: "work" };
    const SYM = { off: "消灯", chat: "雑談", debate: "討論", work: "作業" };
    let man = {}, cur = "";
    // 好みの設定(額縁・フィルター・炎のスイッチの表示)。この端末のブラウザだけに保存(スマホとPCで別々に決められる)。「自動」=火の強さに合わせる
    const KEY = "kagaribi_look"; let look = { frame: "auto", filter: "auto", sw: true, props: true };
    try { look = { ...look, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch (e) {}
    const saveLook = () => { try { localStorage.setItem(KEY, JSON.stringify(look)); } catch (e) {} };
    const host0 = wrap.parentElement; if (getComputedStyle(host0).position === "static") host0.style.position = "relative";
    const fr = document.createElement("div"); fr.className = "frame"; fr.setAttribute("aria-hidden", "true"); host0.appendChild(fr);   // 額縁は舞台の外側に回す(舞台の絵を隠さない)
    const flame = (cls, d, label) => {
      const b = document.createElement("button"); b.type = "button"; b.className = "frame-sw " + cls; b.setAttribute("aria-label", label);
      b.innerHTML = '<svg viewBox="0 0 16 20" shape-rendering="crispEdges"><path d="M8 0h2v4h2v4h2v6h-2v4H4v-4H2V8h2V6h2V2h2z" fill="#ff7a1a"/><path d="M8 8h2v4h2v4H6v-4h2z" fill="#ffd24a"/></svg><i></i>';
      b.addEventListener("click", e => { e.stopPropagation(); if (window.kagaribiStepHeat) window.kagaribiStepHeat(d); });
      host0.appendChild(b); return b;
    };
    const swL = flame("left", -1, "火を1段弱くする(最小の次は最大)"); const swR = flame("right", 1, "火を1段強くする(最大の次は最小)");
    // 額縁を含めた全体が、画面の余白いっぱいに収まるよう、舞台の大きさを決める(舞台は16:9のまま。額縁の厚みは舞台の高さに比例させ、ドットの比率を保つ)
    function size() {
      const m = man[cur]; const none = !m || cur === "none";
      const AW = host0.clientWidth - 4, AH = host0.clientHeight - 4;
      if (none) { wrap.style.width = ""; fr.hidden = true; } else {
        const inH = m.h - m.top - m.bottom;
        const ar = (getComputedStyle(wrap).aspectRatio || "16 / 9").split("/").map(Number), a = (ar[0] / (ar[1] || 1)) || 16 / 9;   // 舞台の縦横比は絵で変わる(3:2など)。16:9決め打ちだと幅が足りず額縁がはみ出す
        const u = Math.max(9, Math.min(AW / (a + (m.left + m.right) / inH), AH / (m.h / inH))); wrap.style.width = Math.floor(a * u) + "px"; fr.hidden = false;
      }
      requestAnimationFrame(() => {
        const ox = wrap.offsetLeft, oy = wrap.offsetTop, ow = wrap.offsetWidth, oh = wrap.offsetHeight;
        const s = none ? 0 : oh / (m.h - m.top - m.bottom), T = none ? 0 : m.top * s, R = none ? 0 : m.right * s, B = none ? 0 : m.bottom * s, L = none ? 0 : m.left * s;
        if (!none) {
          Object.assign(fr.style, { left: (ox - L) + "px", top: (oy - T) + "px", width: (ow + L + R) + "px", height: (oh + T + B) + "px", borderWidth: [T, R, B, L].map(v => Math.round(v) + "px").join(" "), borderImageSlice: [m.top, m.right, m.bottom, m.left].join(" ") });
        }
        const bs = Math.max(22, Math.min(40, none ? 30 : Math.min(L, R) * 0.9));
        for (const [b, cx] of [[swL, ox - L / 2 + (none ? 14 : 0)], [swR, ox + ow + R / 2 - (none ? 14 : 0)]]) Object.assign(b.style, { left: (cx - bs / 2) + "px", top: (oy + oh / 2 - bs / 2) + "px", width: bs + "px", height: bs + "px" });
      });
    }
    function upd() {
      const heat = wrap.dataset.heat || "chat"; const pv = new URLSearchParams(location.search).get("frame");   // ?frame=名前 で、候補の額縁を試しに出せる
      const want = pv && man[pv] ? pv : look.frame === "none" ? "none" : man[look.frame] ? look.frame : man[FRAME_OF[heat]] ? FRAME_OF[heat] : (man.chat ? "chat" : Object.keys(man)[0]);   // 好みの指定(look.frame)が「自動」以外なら、それを優先
      if (!want || want === cur) return; cur = want;
      if (man[want]) fr.style.borderImageSource = 'url("/assets/frame/' + man[want].file + '")'; fr.dataset.frame = want; size();
    }
    fetch("/assets/frame/frame.json", { cache: "no-store" }).then(r => r.ok ? r.json() : {}).then(j => { man = j || {}; upd(); }).catch(() => {});
    let lastW = 0; if (window.ResizeObserver) new ResizeObserver(() => { if (host0.clientWidth !== lastW) { lastW = host0.clientWidth; size(); } }).observe(host0); window.addEventListener("resize", size);
    setInterval(upd, 500);

    // --- 画面の外の「筆とインク壺」: 押すと、額縁・フィルター・スイッチの設定の羊皮紙が開く ---
    const FNAME = { chat: "雑談(可愛い)", debate: "討論(豪華)", work: "作業(シック)", watch: "見守り(古木)", yoichi: "夜一(夜猫)", mizuki: "美月(月)", hinata: "日向(木の芽)", asahi: "朝日(黄金)" };
    const FILTERS = [["auto", "自動"], ["none", "なし"], ["sepia", "セピア"], ["blue", "青"], ["sparkle", "きらめき"], ["dark", "暗め"]];
    const props = document.createElement("img"); props.className = "desk-props"; props.alt = ""; props.src = "/assets/props/desk_layer.png?v=1";   // 書きかけの手紙・インク壺・羽根ペン(舞台の机の上。位置は舞台の絵の座標)
    props.addEventListener("click", e => { e.stopPropagation(); window.open("/glossary", "_blank"); });   // 押すと用語集(別タブ)
    if (stageEl) stageEl.appendChild(props);
    const book = document.createElement("img"); book.className = "album-book"; book.alt = "思い出アルバム"; book.title = "思い出アルバムを開く(別タブ)"; book.src = "/assets/album/album_book_6.png?v=1";   // 机の上のアルバム。押すと /album が別タブで開く
    book.addEventListener("click", e => { e.stopPropagation(); window.open("/album", "_blank"); });
    if (stageEl) stageEl.appendChild(book);
    const host = wrap.parentElement; if (getComputedStyle(host).position === "static") host.style.position = "relative";
    const pen = document.createElement("button"); pen.type = "button"; pen.className = "look-pen"; pen.setAttribute("aria-label", "額縁・フィルターの設定を開く");
    pen.innerHTML = '<svg viewBox="0 0 24 24" shape-rendering="crispEdges"><path d="M15 1h3v2h2v3h2v2h-2v2h-2v2h-2v-2h-2V8h-2V6h2V4h2z" fill="#e8e0cc"/><path d="M11 12h2v2h2v-2h2v2h-2v2h-2v2h-2v2H9v2H7v-2H5v-2h2v-2h2v-2h2z" fill="#b8a888"/><path d="M3 20h4v2H3z" fill="#1b1a40"/><path d="M13 15h9v8h-9z" fill="#1d1a38"/><path d="M14 16h7v1h-7z" fill="#4a4a8a"/><path d="M16 13h3v2h-3z" fill="#2a2850"/></svg>';
    const pan = document.createElement("div"); pan.className = "look-panel"; pan.hidden = true;
    wrap.appendChild(pen); host.appendChild(pan);
    // --- 壺(潤の思いつきメモ): 押すと中身(NOTES.md)が読め、下の欄に書くと追記される(仮の絵) ---
    const pot = document.createElement("button"); pot.type = "button"; pot.className = "tsubo-btn"; pot.setAttribute("aria-label", "壺(思いつきメモ)を開く");
    pot.innerHTML = '<svg viewBox="0 0 24 24" shape-rendering="crispEdges"><path d="M8 3h8v2H8z" fill="#8a5a34"/><path d="M9 5h6v2H9z" fill="#b87a44"/><path d="M6 7h12v2H6z" fill="#8a5a34"/><path d="M5 9h14v8H5z" fill="#b87a44"/><path d="M6 17h12v3H6z" fill="#8a5a34"/><path d="M8 20h8v1H8z" fill="#5a3a20"/><path d="M7 10h2v5H7z" fill="#d89a5c"/><path d="M10 11h5v2h-5z" fill="#e8c88a"/></svg>';
    const tp = document.createElement("div"); tp.className = "tsubo-panel"; tp.hidden = true;
    tp.innerHTML = '<div class="lp-title">壺(思いつきメモ)</div><pre class="tsubo-log"></pre><textarea class="tsubo-in" rows="3" placeholder="思いついたことを書く(Ctrl+Enterで入れる)"></textarea><div class="tsubo-row"><button type="button" class="tsubo-send">壺に入れる</button><span class="tsubo-msg"></span></div><div class="lp-note">追記だけ。消えません。夜一と日向が区切りで読みます。</div>';
    wrap.appendChild(pot); host.appendChild(tp);
    const tLog = tp.querySelector(".tsubo-log"), tIn = tp.querySelector(".tsubo-in"), tMsg = tp.querySelector(".tsubo-msg");
    function tsuboLoad() {
      fetch("/api/tsubo", { cache: "no-store" }).then(r => r.json()).then(d => { tLog.textContent = d.text || d.error || ""; tLog.scrollTop = tLog.scrollHeight; }).catch(() => { tLog.textContent = "読めませんでした"; });
    }
    function tsuboSend() {
      const t = tIn.value.trim(); if (!t) return;
      tMsg.textContent = "…";
      fetch("/api/tsubo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t }) })
        .then(r => r.json().then(j => ({ ok: r.ok, j }))).then(({ ok, j }) => { if (ok) { tIn.value = ""; tMsg.textContent = "入れました"; tsuboLoad(); } else tMsg.textContent = j.error || "失敗"; })
        .catch(() => { tMsg.textContent = "失敗"; });
    }
    pot.addEventListener("click", e => { e.stopPropagation(); tp.hidden = !tp.hidden; if (!tp.hidden) { pan.hidden = true; tMsg.textContent = ""; tsuboLoad(); } });
    tp.addEventListener("click", e => e.stopPropagation());
    tp.querySelector(".tsubo-send").addEventListener("click", tsuboSend);
    tIn.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); tsuboSend(); } });
    document.addEventListener("click", e => { if (!tp.hidden && !tp.contains(e.target) && e.target !== pot && !pot.contains(e.target)) tp.hidden = true; });
    function paintLook() {
      wrap.dataset.filter = look.filter; wrap.dataset.sw = look.sw ? "1" : "0"; host0.classList.toggle("sw-off", !look.sw); wrap.dataset.props = look.props ? "1" : "0"; saveLook(); upd();
      pan.querySelectorAll("[data-k]").forEach(b => b.classList.toggle("on", look[b.dataset.k] === ((b.dataset.k === "sw" || b.dataset.k === "props") ? b.dataset.v === "1" : b.dataset.v)));
    }
    function build() {
      const row = (k, title, items) => '<div class="lp-row"><b>' + title + '</b>' + items.map(([v, t]) => '<button type="button" data-k="' + k + '" data-v="' + v + '">' + t + "</button>").join("") + "</div>";
      pan.innerHTML = '<div class="lp-title">額縁と色味の設定</div>' +
        row("frame", "額縁", [["auto", "自動(火に合わせる)"], ["none", "額縁なし"], ...Object.keys(man).map(k => [k, FNAME[k] || k])]) +
        row("filter", "フィルター", FILTERS) + row("sw", "炎のスイッチ", [["1", "出す"], ["0", "隠す"]]) + row("props", "机の小物(手紙・インク・羽根ペン)", [["1", "置く"], ["0", "置かない"]]) +
        '<div class="lp-note">この端末だけに保存。火の強さの切り替えは、左右の炎のスイッチか、焚き火を押す。</div>';
      pan.querySelectorAll("[data-k]").forEach(b => b.addEventListener("click", e => { e.stopPropagation(); look[b.dataset.k] = (b.dataset.k === "sw" || b.dataset.k === "props") ? b.dataset.v === "1" : b.dataset.v; paintLook(); }));
      paintLook();
    }
    pen.addEventListener("click", e => { e.stopPropagation(); pan.hidden = !pan.hidden; if (!pan.hidden) build(); });
    document.addEventListener("click", e => { if (!pan.hidden && !pan.contains(e.target) && e.target !== pen) pan.hidden = true; });
    wrap.dataset.filter = look.filter; wrap.dataset.sw = look.sw ? "1" : "0"; host0.classList.toggle("sw-off", !look.sw); wrap.dataset.props = look.props ? "1" : "0";
  })();

  // --- 発言したキャラがはずみ、短い吹き出しを出す(CSSのクラスを付けるだけ。吹き出しは最新発言の頭だけ・短時間) ---
  const timers = {};
  window.kagaribiScene = {
    // 吹き出しに、そのキャラの直近の発言を常時出す(色つきの名札つき。文字はHTML/CSSで描く)
    setLast(id, text) {
      const NAME = { asahi: "朝日", hinata: "日向", mizuki: "美月", yoichi: "夜一" };
      const el = wrap.querySelector(`.actor[data-actor="${id}"] .bubble`);
      if (!el || !NAME[id]) return;
      el.dataset.name = NAME[id]; el.dataset.icon = ICONS[id];   // 発言の生の文は出さない(定型の台詞は say() が出す)
    },
    pulse(sender, text) {
      wake();
      const id = ({ 朝日: "asahi", 日向: "hinata", 美月: "mizuki", 夜一: "yoichi" })[sender] || sender;
      const el = wrap.querySelector(`.actor[data-actor="${id}"]`);
      if (!el) return;
      const bubble = el.querySelector(".bubble");
      el.classList.add("talking");
      // 表情の気分: 発言の中身で決める(怒=失敗・エラー系 / 哀=詫び・困りごと系 / 喜=成功・感謝系)。8秒で基本の表情に戻る
      const mood = /失敗|エラー|できなかった|却下|NG|壊れ|止まっ/.test(text || "") ? "anger" : /残念|ごめん|申し訳|すまぬ|すまない|困|未完/.test(text || "") ? "sad" : /完了|できた|成功|ありがと|やった|検証済/.test(text || "") ? "joy" : "";
      say(id, mood || (el.dataset.state === "working" ? "working" : "heard"));
      if (mood) { el.dataset.mood = mood; clearTimeout(timers[id + "m"]); timers[id + "m"] = setTimeout(() => { delete el.dataset.mood; }, 3200); }
      clearTimeout(timers[id]);
      timers[id] = setTimeout(() => el.classList.remove("talking"), 3600);
    },
    // 状態表示(篝火常駐): idle / heard(うなずく) / thinking(…と考える) / working(作業中の枠)。サーバーの /api/presence の値をそのまま反映する
    setPresence(map) {
      for (const [id, entry] of Object.entries(map || {})) {
        const el = wrap.querySelector(`.actor[data-actor="${id}"]`);
        if (!el) continue;
        if (entry.state && entry.state !== "idle") wake();
        if (el.dataset.state !== entry.state) { el.dataset.state = entry.state; const k = ({ thinking: "think", working: "working", heard: "heard", idle: "idle" })[entry.state]; if (k) say(id, k); }
        const alive = entry.alive ? "true" : "false";
        if (el.dataset.alive !== alive) el.dataset.alive = alive;
        const evidence = entry.evidence || "";   // 発火式の根拠: ok_recent / ok_old / never / failed / missing(待機を「確認済み」と見せない)
        if (el.dataset.evidence !== evidence) el.dataset.evidence = evidence;
      }
    },
    paletteName: () => wrap.dataset.palette || ""
  };
})();
