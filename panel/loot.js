// Chests, unboxing reel, bag (items, collection, shop) and their sounds.
(function () {
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Math.round(n).toLocaleString("en-US");
  const post = (data) => window.webkit?.messageHandlers?.panel?.postMessage({ type: "action", data });
  let S = null, tab = "items", confirmBuy = null, busy = false;

  const L = () => S.loot;
  const rarity = (id) => L().config.rarities.find((r) => r.id === id) || { name: "", color: "#fff" };
  const drop = (id) => L().config.drops.find((d) => d.id === id);
  const tierCfg = (id) => L().config.tiers.find((t) => t.id === id);

  // ---------- sounds (all synthesized) ----------
  let ctx;
  const ac = () => (ctx = ctx || new AudioContext());
  function tone(f, t0, dur, type = "square", vol = 0.05, slideTo) {
    const c = ac(), o = c.createOscillator(), g = c.createGain(), t = c.currentTime + t0;
    o.type = type; o.frequency.setValueAtTime(f, t);
    if (slideTo) o.frequency.exponentialRampToValueAtTime(slideTo, t + dur);
    g.gain.setValueAtTime(vol, t); g.gain.exponentialRampToValueAtTime(0.0008, t + dur);
    o.connect(g).connect(c.destination); o.start(t); o.stop(t + dur + 0.02);
  }
  function noise(t0, dur, vol = 0.08, hp = 800) {
    const c = ac(), b = c.createBuffer(1, c.sampleRate * dur, c.sampleRate), d = b.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / d.length);
    const s = c.createBufferSource(), f = c.createBiquadFilter(), g = c.createGain();
    f.type = "highpass"; f.frequency.value = hp; g.gain.value = vol;
    s.buffer = b; s.connect(f).connect(g).connect(c.destination); s.start(c.currentTime + t0);
  }
  const SFX = {
    creak: () => { tone(120, 0, 0.35, "sawtooth", 0.03, 70); tone(95, 0.18, 0.3, "sawtooth", 0.025, 60); },
    burst: () => { noise(0, 0.35, 0.12, 400); tone(523, 0.02, 0.25); tone(784, 0.06, 0.3); tone(1047, 0.1, 0.4); },
    tick: () => tone(1900, 0, 0.025, "square", 0.025),
    coin: (n = 3) => { for (let i = 0; i < n; i++) { tone(1568, i * 0.07, 0.08, "square", 0.03); tone(2093, i * 0.07 + 0.03, 0.1, "square", 0.025); } },
    reveal: (r) => {
      const seqs = { common: [523, 659], rare: [523, 659, 784, 1047], ultra: [392, 523, 659, 784, 1047, 1319],
        extraordinary: [262, 330, 392, 523, 659, 784, 1047, 1319, 1568, 2093] };
      (seqs[r] || seqs.common).forEach((f, i) => tone(f, i * 0.09, 0.28, "square", 0.045));
      if (r === "extraordinary") { [1047, 1319, 1568].forEach((f) => tone(f, 0.95, 1.1, "triangle", 0.05)); noise(0.9, 0.8, 0.05, 3000); }
      if (r === "ultra") noise(0.5, 0.4, 0.04, 3000);
    },
    claim: () => [659, 784, 1047].forEach((f, i) => tone(f, i * 0.1, 0.2, "triangle", 0.06)),
  };

  // ---------- pixel chest ----------
  function chestSVG(tier) {
    const c = tierCfg(tier)?.color || "#a0703c", band = tier === "wooden" ? "#5b3b1c" : "#3a3a44";
    const R = (x, y, w, h, f) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${f}"/>`;
    return `<svg class="chest" viewBox="0 0 32 28" shape-rendering="crispEdges">
      <g class="body">${R(3, 13, 26, 13, c)}${R(3, 13, 26, 2, "rgba(0,0,0,.25)")}${R(3, 24, 26, 2, "rgba(0,0,0,.35)")}
        ${R(7, 13, 2, 13, band)}${R(23, 13, 2, 13, band)}${R(14, 15, 4, 5, "#e8c45a")}${R(15, 17, 2, 2, "#3a2a10")}</g>
      <g class="lid">${R(3, 5, 26, 8, c)}${R(4, 4, 24, 1, c)}${R(3, 5, 26, 2, "rgba(255,255,255,.25)")}
        ${R(7, 4, 2, 9, band)}${R(23, 4, 2, 9, band)}${R(14, 10, 4, 3, "#e8c45a")}</g></svg>`;
  }

  // ---------- top bar: streak, coins, boost, bag ----------
  function bar() {
    const st = S.streak, l = L(), b = l.boost_active;
    const flame = st.days >= 100 ? "f3" : st.days >= 30 ? "f2" : st.days >= 7 ? "f1" : "";
    const mins = b ? Math.ceil((b[1] - Date.now() / 1000) / 60) : 0;  // icon says the size (🧪 ×1.5, ⚗️ ×2+); keeps the bar narrow
    const boost = b ? `<span class="chip boost" title="XP boost ×${b[2]}, ${mins} min left">${b[2] >= 2 ? "⚗️" : "🧪"}${mins}m</span>` : "";
    $("chips").innerHTML = `${boost}
      <span class="chip ${st.today_done ? "" : "warn"}" id="streakChip" title="Streak">
        <span class="flame ${flame}">🔥</span>${st.days}</span>
      <span class="chip" id="coinChip" title="Coins">🪙${fmt(l.gold)}</span>
      <span class="chip btn2" id="bagBtn" title="Bag and shop">🎒</span>`;
    $("bagBtn").onclick = () => openBag();
    $("streakChip").onmouseenter = () => window.showInfoHTML(streakInfo());
    $("streakChip").onmouseleave = () => window.showInfoHTML(null);
  }
  function streakInfo() {
    const st = S.streak;
    return `<div class="name">🔥 ${st.days}-day streak</div>
      <div class="row"><span>Today:</span><span>${st.today_done ? "safe ✓" : fmt(st.need - st.today_xp) + " XP to go"}</span></div>
      <div class="row"><span>Streak Shields:</span><span>${L().items.shield || 0}</span></div>
      <div class="about">A day counts once your skills earn ${fmt(st.need)} XP together. One missed day a week is free; Streak Shields cover more.</div>`;
  }

  // ---------- chest tray ----------
  function tray() {
    const n = L().unopened.length, el = $("tray"), m = L().meter;
    el.style.display = "flex";
    if (!n) {  // no chest waiting: show how close the next one is
      el.className = "idle"; el.onclick = null;
      el.innerHTML = `<span class="mini" style="opacity:.6">${chestSVG("wooden")}</span>
        <span class="trayText">Next chest in <b>${fmt(m.need - m.have)} XP</b>${m.upgrade ? "<br><small>⬆ upgraded: you levelled up</small>" : ""}
        <div class="meter"><i style="width:${(100 * m.have / m.need).toFixed(1)}%"></i></div></span>`;
      return;
    }
    el.className = "";
    const next = L().unopened[0];
    el.innerHTML = `<span class="mini">${chestSVG(next.tier)}</span>
      <span class="trayText"><b>${n} chest${n > 1 ? "s" : ""}</b> to open<br><small>${tierCfg(next.tier).name} · ${next.source}</small></span>
      <span class="openBtn">Open</span>`;
    el.onclick = () => unbox(next);
  }

  // ---------- unboxing ----------
  function card(d, extra = "") {
    const r = rarity(d.rarity);
    return `<div class="card ${extra}" style="--rc:${r.color}"><span class="ci">${d.icon}</span><span class="cn">${d.name.replace(/^(Permission|Voucher|Title): /, "")}</span></div>`;
  }
  function filler(tier) {
    const odds = tierCfg(tier).odds, rs = L().config.rarities;
    let x = Math.random() * odds.reduce((a, b) => a + b, 0), r = rs[0].id;
    for (let i = 0; i < odds.length; i++) { x -= odds[i]; if (x < 0) { r = rs[i].id; break; } }
    const pool = L().config.drops.filter((d) => d.rarity === r);
    return pool[Math.floor(Math.random() * pool.length)];
  }

  function unbox(chest) {
    if (busy) return;
    busy = true;
    const got = L().chest_contents[chest.id], win = got.drop, r = rarity(win.rarity), tier = tierCfg(chest.tier);
    const ov = $("overlay");
    ov.className = "overlay show";
    ov.innerHTML = `<div class="ubox">
      <div class="utitle" style="color:${tier.color}">${tier.name}</div>
      <div class="usrc">${chest.source}</div>
      <div class="stageC" id="stageC">${chestSVG(chest.tier)}<div class="hint">click to open</div></div>
      <div class="reelWrap" id="reelWrap"><div class="marker"></div><div class="reel" id="reel"></div></div>
      <div class="result" id="result"></div></div>`;
    $("stageC").onclick = () => {
      $("stageC").onclick = null;
      post({ action: "open", chest: chest.id });  // saved now, even if you close mid-animation
      const ch = $("stageC").querySelector(".chest");
      $("stageC").querySelector(".hint").remove();
      ch.classList.add("shake"); SFX.creak();
      setTimeout(() => { ch.classList.add("open"); $("stageC").classList.add("burst"); SFX.burst(); }, 700);
      setTimeout(() => spin(win, chest, got, r), 1300);
    };
  }

  function spin(win, chest, got, r) {
    const N = 44, W = 46, at = 37, reel = $("reel");
    const items = Array.from({ length: N }, (_, i) => (i === at ? win : filler(chest.tier)));
    reel.innerHTML = items.map((d) => card(d)).join("");
    $("stageC").classList.add("small");
    $("reelWrap").classList.add("show");
    const wrapW = $("reelWrap").clientWidth, jitter = (Math.random() - 0.5) * (W * 0.6);
    const target = at * W - wrapW / 2 + W / 2 + jitter, dur = 4600, t0 = performance.now();
    let lastIdx = -1;
    const ease = (t) => 1 - Math.pow(1 - t, 4);
    (function frame(now) {
      const t = Math.min(1, (now - t0) / dur), x = target * ease(t);
      reel.style.transform = `translateX(${-x}px)`;
      const idx = Math.floor((x + wrapW / 2) / W);
      if (idx !== lastIdx) { lastIdx = idx; SFX.tick(); }
      if (t < 1) requestAnimationFrame(frame);
      else reveal(win, got, r, reel.children[at]);
    })(t0);
  }

  function reveal(win, got, r, el) {
    el.classList.add("winner", "r-" + win.rarity);
    SFX.reveal(win.rarity);
    const res = $("result"), more = L().unopened.length - 1;
    let what = win.about || "";
    if (win.kind === "gold") what = `+${fmt(win.amount)} coins`;
    if (win.kind === "pet") what = "A new companion next to your panel heading. Equip it in your bag.";
    if (win.kind === "title") what = "Replaces your panel heading. Equip it in your bag.";
    if (win.kind === "voucher" && !win.about) what = "A real-life reward. Claim it in your bag when you take it.";
    res.innerHTML = `<div class="rname r-${win.rarity}" style="color:${r.color}">${r.name}!</div>
      <div class="big">${win.icon} ${win.name}</div><div class="what">${what}</div>
      ${got.bonus_gold ? `<div class="bonus">+${fmt(got.bonus_gold)} bonus coins</div>` : ""}
      <div class="btns"><span class="gb" id="collect">Collect</span>${more > 0 ? `<span class="gb alt" id="nextC">Open next (${more})</span>` : ""}</div>`;
    res.classList.add("show");
    if (win.rarity === "extraordinary" || win.rarity === "ultra") confetti(win.rarity === "extraordinary" ? 90 : 40, r.color);
    if (got.bonus_gold || win.kind === "gold") setTimeout(() => SFX.coin(4), 500);
    const done = () => { busy = false; closeOverlay(); };
    $("collect").onclick = done;
    if ($("nextC")) $("nextC").onclick = () => { busy = false; const nx = L().unopened.find((c) => c.id !== lastOpened); if (nx) unbox(nx); else done(); };
    lastOpened = got && L().unopened[0]?.id;
  }
  let lastOpened = null;

  function confetti(n, color) {
    const ov = $("overlay");
    for (let i = 0; i < n; i++) {
      const p = document.createElement("i");
      p.className = "conf";
      p.style.left = 50 + (Math.random() - 0.5) * 20 + "%";
      p.style.background = Math.random() < 0.5 ? color : ["#ffd84a", "#fff", "#ff5fd2", "#7fe3ff"][i % 4];
      p.style.setProperty("--dx", (Math.random() - 0.5) * 320 + "px");
      p.style.setProperty("--dy", -(80 + Math.random() * 220) + "px");
      p.style.animationDelay = Math.random() * 0.2 + "s";
      ov.appendChild(p);
      setTimeout(() => p.remove(), 2200);
    }
  }

  function closeOverlay() { $("overlay").className = "overlay"; $("overlay").innerHTML = ""; }

  // ---------- bag ----------
  function openBag(which) {
    if (which) tab = which;
    confirmBuy = null;
    $("overlay").className = "overlay show";
    paintBag();
  }
  function paintBag() {
    const l = L(), ov = $("overlay");
    const tabs = [["items", "Items"], ["collection", "Collection"], ["shop", "Shop"]]
      .map(([id, n]) => `<span class="tab ${tab === id ? "on" : ""}" data-tab="${id}">${n}</span>`).join("");
    ov.innerHTML = `<div class="bag"><div class="bagTop">${tabs}<span class="x" id="bagX">✕</span></div>
      <div class="coins">🪙 ${fmt(l.gold)} coins</div><div class="list" id="bagList">${bagList()}</div></div>`;
    ov.querySelectorAll(".tab").forEach((t) => (t.onclick = () => { tab = t.dataset.tab; confirmBuy = null; paintBag(); }));
    $("bagX").onclick = closeOverlay;
    ov.querySelectorAll("[data-act]").forEach((b) => (b.onclick = () => act(JSON.parse(b.dataset.act), b)));
  }
  const btn = (label, a, dis = false) => `<span class="gb sm ${dis ? "dis" : ""}" ${dis ? "" : `data-act='${JSON.stringify(a)}'`}>${label}</span>`;
  const rowH = (d, right, sub = "") => `<div class="it" style="--rc:${rarity(d.rarity).color}"><span class="ii">${d.icon}</span>
      <span class="in">${d.name}<small>${sub}</small></span>${right}</div>`;

  function bagList() {
    const l = L();
    if (tab === "items") {
      const rows = [];
      for (const [id, n] of Object.entries(l.items)) if (n > 0) {
        const d = drop(id), usable = id === "potion" || id === "elixir";
        rows.push(rowH(d, usable ? btn("Use", { action: "use", item: id }, !!l.boost_active) : `<span class="qty">passive</span>`, `×${n} · ${d.about || ""}`));
      }
      for (const [id, n] of Object.entries(l.vouchers)) if (n > 0) {
        const d = drop(id);
        rows.push(rowH(d, btn("Claim", { action: "claim", voucher: id }), `×${n} · ${d.about || "Real-life reward"}`));
      }
      return rows.join("") || `<div class="empty">Nothing yet. Open chests to fill your bag.</div>`;
    }
    if (tab === "collection") {
      return ["pet", "title"].map((slot) => {
        const all = l.config.drops.filter((d) => d.kind === slot);
        return `<div class="sec">${{ pet: "Pets", title: "Titles" }[slot]} · ${all.filter((d) => l.owned.includes(d.id)).length}/${all.length}</div>` +
          all.map((d) => {
            if (!l.owned.includes(d.id)) return `<div class="it locked" style="--rc:${rarity(d.rarity).color}"><span class="ii">❔</span><span class="in">???<small>${rarity(d.rarity).name}</small></span></div>`;
            const on = l.equipped[slot] === d.id;
            return rowH(d, btn(on ? "Unequip" : "Equip", { action: "equip", slot, id: on ? null : d.id }), rarity(d.rarity).name);
          }).join("");
      }).join("");
    }
    const row = (r) => {
      const can = l.gold >= r.cost;
      const right = confirmBuy === r.id
        ? btn(`Buy ${fmt(r.cost)}?`, { action: "redeem", reward: r.id })
        : `<span class="gb sm ${can ? "" : "dis"}" data-ask="${r.id}">🪙${fmt(r.cost)}</span>`;
      const color = r.kind === "chest" ? tierCfg(r.tier).color : "#ffb020";
      const icon = r.kind === "chest" ? `<span class="ii mini2">${chestSVG(r.tier)}</span>` : `<span class="ii">${r.icon}</span>`;
      return `<div class="it" style="--rc:${color}">${icon}<span class="in">${r.name}<small>${r.kind === "chest" ? "Unbox it for a random drop" : "Real-life reward"}</small></span>${right}</div>`;
    };
    return `<div class="sec">Chests</div>` + l.shop.filter((r) => r.kind === "chest").map(row).join("") +
      `<div class="sec">Treats</div>` + l.shop.filter((r) => r.kind === "treat").map(row).join("") +
      `<div class="sec">Real-life rewards</div>` + l.shop.filter((r) => r.kind === "reward").map(row).join("");
  }

  function act(a, el) {
    post(a);
    const l = L();
    // optimistic local update so the click feels instant; the tracker's recount replaces it within a second
    if (a.action === "redeem" && a.reward.startsWith("chest:")) {
      const item = l.shop.find((r) => r.id === a.reward);
      l.gold -= item.cost; SFX.coin(5);
      window.banner(`<div class="big">🎁 ${item.name} bought!</div>It is waiting in your chest tray.`);
      closeOverlay();
      return;
    }
    if (a.action === "claim" || a.action === "redeem") {
      const name = a.action === "claim" ? drop(a.voucher).name : l.shop.find((r) => r.id === a.reward).name;
      SFX.claim();
      window.banner(`<div class="big">Go enjoy it!</div>${name.replace(/^(Permission|Voucher): /, "")}`);
      if (a.action === "claim") l.vouchers[a.voucher]--; else l.gold -= l.shop.find((r) => r.id === a.reward).cost;
      closeOverlay();
      return;
    }
    if (a.action === "use") { l.items[a.item]--; SFX.reveal("rare"); window.banner(`<div class="big">${drop(a.item).icon} Boost active!</div>${drop(a.item).about}`); closeOverlay(); return; }
    if (a.action === "equip") { l.equipped[a.slot] = a.id; SFX.claim(); window.repaintHero(); paintBag(); }
  }
  document.addEventListener("click", (e) => {
    const ask = e.target.closest?.("[data-ask]");
    if (ask && !ask.classList.contains("dis")) { confirmBuy = ask.dataset.ask; paintBag(); }
  });

  window.Loot = {
    render(stats) { S = stats; if (!S.loot) return; bar(); tray(); },
    equipped: () => S?.loot?.equipped || {},
    drop,
    open: openBag,
  };
})();
