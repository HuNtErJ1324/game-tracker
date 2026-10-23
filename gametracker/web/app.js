"use strict";

const $app = document.getElementById("app");
const $tip = document.getElementById("tooltip");
const SERIES = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5"].map(
  (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim());
const OTHER = getComputedStyle(document.documentElement).getPropertyValue("--series-other").trim();
const COMPETITIVE = ["valorant", "league", "overwatch", "cs2"];
const SOURCES = { steam: "Steam", riot: "Riot", epic: "Epic", battlenet: "Battle.net", ea: "EA", ubisoft: "Ubisoft", gog: "GOG", xbox: "Xbox", custom: "Added by you" };

const state = { overview: null, settings: null, view: null, lib: { source: "all", q: "", sort: "recent", hidden: false } };

// ---------------- helpers ----------------

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (init.method !== "GET") {
    init.headers["X-GameTracker"] = "1";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body || {});
  }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^(https?:\/\/|\/radar\/[a-z0-9_]+\.png$)/i.test(u || "") ? u : "");
const gameHref = (id) => `#/game/${encodeURIComponent(id)}`;

function dur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}
function hours(sec) {
  const h = (sec || 0) / 3600;
  if (h >= 100) return `${Math.round(h)} h`;
  if (h >= 1) return `${h.toFixed(1)} h`;
  return `${Math.round((sec || 0) / 60)} min`;
}
function clock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 2 * 86400) return "yesterday";
  if (s < 30 * 86400) return `${Math.floor(s / 86400)} days ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}
function dayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function dayLabel(key) {
  const today = dayKey(Date.now() / 1000), yest = dayKey(Date.now() / 1000 - 86400);
  if (key === today) return "Today";
  if (key === yest) return "Yesterday";
  const [y, m, d] = key.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}
const timeOfDay = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

function art(game) {
  // Server-resolved official art (Steam / Epic / Microsoft Store) or your own override.
  if (game && game.art && (game.art.cover || game.art.hero)) {
    return { cover: safeUrl(game.art.cover), hero: safeUrl(game.art.hero || game.art.cover) };
  }
  if (!game || !String(game.id || game.game_id || "").startsWith("steam:")) return {};
  const appid = String(game.id || game.game_id).split(":")[1];
  const base = `https://cdn.akamai.steamstatic.com/steam/apps/${appid}`;
  return { cover: `${base}/library_600x900.jpg`, hero: `${base}/library_hero.jpg` };
}
function setBg(el, url) {
  if (!el || !url) return;
  const im = new Image();
  im.onload = () => { el.style.backgroundImage = `url("${url}")`; };
  im.src = url;
}
// Missing cover art falls back to the game's name set in type.
document.addEventListener("error", (e) => {
  const img = e.target;
  if (img.tagName === "IMG" && img.dataset.fallback) {
    const div = document.createElement("div");
    div.className = "text-art";
    div.textContent = img.dataset.fallback;
    img.replaceWith(div);
  }
}, true);

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.setAttribute("role", "status");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

// md() comes from markdown.js.

// live session timers
setInterval(() => {
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-start]").forEach((el) => { el.textContent = clock(now - Number(el.dataset.start)); });
}, 1000);

function showTip(html, x, y) {
  $tip.innerHTML = html;
  $tip.hidden = false;
  const r = $tip.getBoundingClientRect();
  $tip.style.left = `${Math.min(window.innerWidth - r.width - 8, Math.max(8, x + 14))}px`;
  $tip.style.top = `${Math.min(window.innerHeight - r.height - 8, Math.max(8, y - r.height - 10))}px`;
}
const hideTip = () => { $tip.hidden = true; };

// ---------------- agent status (every page) ----------------

async function refreshStatus() {
  try {
    state.overview = await api("/api/overview");
  } catch {
    setAgent(null);
    return;
  }
  setAgent(state.overview.agent);
  // Re-render only when something visible changed, so open tooltips/tables aren't reset every poll.
  const o = state.overview, s0 = o.sessions[0];
  const sig = JSON.stringify([o.agent.online, o.now_playing.map((p) => [p.game_id, p.start_ts, p.live]),
    o.sessions.length, s0 && Math.floor(s0.end_ts / 60)]);
  if (state.view === "overview" && sig !== state.sig) renderOverview();
  state.sig = sig;
}
function setAgent(a) {
  const el = document.getElementById("agent");
  el.className = "agent " + (a ? (a.online ? "on" : "off") : "off");
  el.querySelector("span").textContent = !a ? "Server unreachable"
    : a.online ? `Tracking on ${a.host || "your PC"}` : (a.last_seen ? `PC offline, last seen ${ago(a.last_seen)}` : "Waiting for your PC");
}

// ---------------- overview ----------------

async function viewOverview() {
  state.view = "overview";
  if (!state.overview) await refreshStatus();
  if (state.overview) renderOverview();
  else $app.innerHTML = `<p class="empty error">Can't reach the Game Tracker server. Check it's running, then reload.</p>`;
}

function renderOverview() {
  const d = state.overview;
  if (!d) return;
  const lib = Object.fromEntries(d.library.map((g) => [g.id, g]));
  const np = d.now_playing || [];
  let hero;
  if (np.length) {
    const p = np[0], live = p.live;
    hero = `<section class="hero" id="hero" aria-live="polite">
      <div class="state"><span class="rec" aria-hidden="true"></span>Playing now${np.length > 1 ? `, plus ${np.slice(1).map((x) => esc(x.name)).join(", ")}` : ""}</div>
      <div class="hero-row">
        <h1><a href="${gameHref(p.game_id)}">${esc(p.name)}</a></h1>
        <div class="timer" data-start="${p.start_ts}" aria-label="Session length">${clock(Date.now() / 1000 - p.start_ts)}</div>
      </div>
      ${live ? `<div class="live"><strong>${esc(live.headline)}</strong>${(live.details || []).filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("")}</div>` : `<div class="sub">Session started at ${timeOfDay(p.start_ts)}</div>`}
    </section>`;
  } else {
    const last = d.library.find((g) => !g.hidden && g.last_played);
    hero = `<section class="hero idle" id="hero">
      <div class="state">${d.agent.online ? "Nothing running right now" : "Your gaming PC isn't reporting in"}</div>
      ${last ? `<h1><a href="${gameHref(last.id)}">${esc(last.name)}</a></h1>
        <div class="sub">Last played ${ago(last.last_played)}, ${hours(last.total_sec)} total. <a href="${gameHref(last.id)}">Ask the coach where you left off</a></div>`
        : `<h1>Start a game and it shows up here</h1>`}
    </section>`;
  }
  const heroGame = np.length ? np[0].game_id : (d.library.find((g) => !g.hidden && g.last_played) || {}).id;

  const weekAgo = Date.now() / 1000 - 7 * 86400;
  const week = {};
  for (const s of d.sessions) if (s.start_ts >= weekAgo) week[s.game_id] = (week[s.game_id] || 0) + (s.end_ts - s.start_ts);
  const weekRows = Object.entries(week).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const weekMax = weekRows.length ? weekRows[0][1] : 1;
  const weekTotal = Object.values(week).reduce((a, b) => a + b, 0);

  const recent = d.sessions.slice(0, 10);
  const sessRows = recent.map((s) => `<tr class="rowlink" data-href="${gameHref(s.game_id)}" tabindex="0">
      <td>${dayLabel(dayKey(s.start_ts))}</td><td><span class="ic">${esc(s.name || s.game_id)}</span></td>
      <td>${timeOfDay(s.start_ts)} to ${timeOfDay(s.end_ts)}</td><td>${dur(s.end_ts - s.start_ts)}</td></tr>`).join("");

  const jump = d.library.filter((g) => !g.hidden && g.last_played).slice(0, 8);

  $app.innerHTML = `${hero}
    ${jump.length ? `<section class="panel">
      <div class="section-head"><h2>Jump back in</h2><a class="linkish" href="#/library" style="margin:0">Library</a></div>
      <div class="shelf-row">${jump.map(coverHtml).join("")}</div>
    </section>` : ""}
    <section class="grid-2">
      <div class="panel chart" id="chart">
        <div class="section-head"><h2>Last 14 days</h2><span class="sub">${hours(chartTotal(d.sessions))} played</span></div>
        <div id="chart-body"></div>
      </div>
      <div class="panel">
        <div class="section-head"><h2>This week</h2><span class="sub">${hours(weekTotal)}</span></div>
        ${weekRows.length ? `<ol class="ranked">${weekRows.map(([gid, sec]) => `<li><a href="${gameHref(gid)}">
          <span class="top"><span class="name">${esc((lib[gid] || {}).name || gid)}</span><span class="sub">${dur(sec)}</span></span>
          <span class="bar"><i style="width:${Math.max(2, (100 * sec) / weekMax)}%"></i></span></a></li>`).join("")}</ol>`
          : `<p class="muted">No tracked play in the last 7 days.</p>`}
      </div>
    </section>
    <section class="panel">
      <div class="section-head"><h2>Recent sessions</h2><span>${d.sessions.length ? `last ${recent.length}` : ""}</span></div>
      ${recent.length ? `<div class="table-wrap" style="margin-top:0"><table><thead><tr><th>Day</th><th>Game</th><th>Time</th><th>Length</th></tr></thead><tbody>${sessRows}</tbody></table></div>`
        : `<p class="muted" style="margin:0">Sessions appear here once the tracker sees you play something.</p>`}
    </section>`;
  setBg(document.getElementById("hero"), art(lib[heroGame] || { id: heroGame }).hero);
  drawChart(document.getElementById("chart-body"), d.sessions, lib);
}

function chartTotal(sessions) {
  const since = Date.now() / 1000 - 14 * 86400;
  return sessions.filter((s) => s.start_ts >= since).reduce((a, s) => a + s.end_ts - s.start_ts, 0);
}

function drawChart(el, sessions, lib) {
  const days = [];
  for (let i = 13; i >= 0; i--) days.push(dayKey(Date.now() / 1000 - i * 86400));
  const byDay = Object.fromEntries(days.map((k) => [k, {}]));
  const totals = {};
  for (const s of sessions) {
    const k = dayKey(s.start_ts);
    if (!byDay[k]) continue;
    const sec = s.end_ts - s.start_ts;
    byDay[k][s.game_id] = (byDay[k][s.game_id] || 0) + sec;
    totals[s.game_id] = (totals[s.game_id] || 0) + sec;
  }
  const ranked = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
  const top = ranked.slice(0, 5);
  const series = [...top, ...(ranked.length > 5 ? ["__other"] : [])];
  const color = (g) => (g === "__other" ? OTHER : SERIES[top.indexOf(g)]);
  const nameOf = (g) => (g === "__other" ? "Other games" : (lib[g] || {}).name || g);
  const rows = days.map((k) => {
    const r = {};
    for (const [g, sec] of Object.entries(byDay[k])) { const key = top.includes(g) ? g : "__other"; r[key] = (r[key] || 0) + sec; }
    return r;
  });
  if (!ranked.length) { el.innerHTML = `<p class="muted">No play tracked in the last two weeks yet.</p>`; return; }

  const W = Math.max(280, el.clientWidth), H = 220, L = 34, B = 24, T = 8;
  const maxH = Math.max(...rows.map((r) => Object.values(r).reduce((a, b) => a + b, 0))) / 3600;
  const step = maxH <= 2 ? 0.5 : maxH <= 4 ? 1 : maxH <= 8 ? 2 : maxH <= 16 ? 4 : 6;
  const top_ = Math.max(step, Math.ceil(maxH / step) * step);
  const y = (h) => T + (H - T - B) * (1 - h / top_);
  const colW = (W - L) / days.length, barW = Math.min(36, colW * 0.62);
  let svg = `<g class="grid">`;
  for (let v = 0; v <= top_ + 1e-9; v += step) svg += `<line x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/>`;
  svg += `</g><g class="axis">`;
  for (let v = 0; v <= top_ + 1e-9; v += step) svg += `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v}h</text>`;
  days.forEach((k, i) => {
    if (colW < 30 && i % 2 !== (days.length - 1) % 2) return;
    const [yy, mm, dd] = k.split("-").map(Number);
    const dt = new Date(yy, mm - 1, dd);
    const lbl = colW >= 44 ? `${dt.toLocaleDateString(undefined, { weekday: "short" })} ${dd}` : String(dd);
    svg += `<text x="${L + colW * i + colW / 2}" y="${H - 6}" text-anchor="middle">${esc(lbl)}</text>`;
  });
  svg += `</g><g>`;
  rows.forEach((r, i) => {
    const x = L + colW * i + (colW - barW) / 2;
    let acc = 0;
    const segs = series.filter((g) => r[g]);
    segs.forEach((g, j) => {
      const h0 = acc / 3600, h1 = (acc + r[g]) / 3600;
      acc += r[g];
      const yTop = y(h1), yBot = y(h0) - (j > 0 ? 2 : 0); // 2px surface gap between stacked segments
      const hgt = Math.max(1, yBot - yTop);
      if (j === segs.length - 1) {
        const rad = Math.min(4, hgt, barW / 2);
        svg += `<path fill="${color(g)}" d="M${x},${yBot} V${yTop + rad} Q${x},${yTop} ${x + rad},${yTop} H${x + barW - rad} Q${x + barW},${yTop} ${x + barW},${yTop + rad} V${yBot} Z"/>`;
      } else svg += `<rect fill="${color(g)}" x="${x}" y="${yTop}" width="${barW}" height="${hgt}"/>`;
    });
  });
  svg += `</g><g>`;
  days.forEach((k, i) => { svg += `<rect class="hit" data-i="${i}" x="${L + colW * i}" y="${T}" width="${colW}" height="${H - T - B}"/>`; });
  svg += `</g>`;

  el.innerHTML = `<div class="legend">${series.map((g) => `<span><i style="background:${color(g)}"></i>${esc(nameOf(g))}</span>`).join("")}</div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Hours played per day over the last 14 days">${svg}</svg>
    <details><summary>Show as table</summary><div class="table-wrap"><table><thead><tr><th>Day</th><th>Total</th>${series.map((g) => `<th>${esc(nameOf(g))}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r, i) => `<tr><td>${dayLabel(days[i])}</td><td>${dur(Object.values(r).reduce((a, b) => a + b, 0))}</td>${series.map((g) => `<td>${r[g] ? dur(r[g]) : ""}</td>`).join("")}</tr>`).reverse().join("")}</tbody></table></div></details>`;

  const tip = (i, ev) => {
    const r = rows[i], total = Object.values(r).reduce((a, b) => a + b, 0);
    const lines = series.filter((g) => r[g]).map((g) => `<div class="row"><span><i style="background:${color(g)}"></i>${esc(nameOf(g))}</span><span>${dur(r[g])}</span></div>`).join("");
    showTip(`<b>${dayLabel(days[i])}</b>${lines || '<div class="muted">No play</div>'}${lines ? `<div class="row" style="margin-top:4px"><span>Total</span><span>${dur(total)}</span></div>` : ""}`, ev.clientX, ev.clientY);
  };
  el.querySelectorAll(".hit").forEach((h) => {
    h.addEventListener("pointermove", (ev) => tip(Number(h.dataset.i), ev));
    h.addEventListener("pointerdown", (ev) => tip(Number(h.dataset.i), ev));
    h.addEventListener("pointerleave", hideTip);
  });
}

function coverHtml(g) {
  const a = art(g);
  const playing = (state.overview?.now_playing || []).some((p) => p.game_id === g.id);
  return `<a class="cover${g.hidden ? " hidden-game" : ""}" href="${gameHref(g.id)}">
    <div class="art">${a.cover ? `<img src="${a.cover}" alt="" loading="lazy" data-fallback="${esc(g.name)}">` : `<div class="text-art">${esc(g.name)}</div>`}
      ${playing ? `<span class="playing">Playing</span>` : ""}</div>
    <span class="title">${esc(g.name)}</span>
    <span class="meta">${g.total_sec ? hours(g.total_sec) : "Not played yet"}${g.last_played ? `, ${ago(g.last_played)}` : ""}</span></a>`;
}

// ---------------- library ----------------

async function viewLibrary() {
  state.view = "library";
  $app.innerHTML = `<p class="empty">Loading your library…</p>`;
  const { games } = await api("/api/games");
  const f = state.lib;
  const sources = [...new Set(games.map((g) => g.source))];
  const draw = () => {
    let list = games.filter((g) => (f.hidden || !g.hidden) && (f.source === "all" || g.source === f.source)
      && (!f.q || g.name.toLowerCase().includes(f.q.toLowerCase())));
    if (f.sort === "playtime") list.sort((a, b) => b.total_sec - a.total_sec);
    else if (f.sort === "name") list.sort((a, b) => a.name.localeCompare(b.name));
    else list.sort((a, b) => (b.last_played || 0) - (a.last_played || 0));
    document.getElementById("shelf").innerHTML = list.length ? list.map(coverHtml).join("")
      : `<p class="empty" style="grid-column:1/-1">No games match. Clear the search or pick another launcher.</p>`;
  };
  const hiddenCount = games.filter((g) => g.hidden).length;
  $app.innerHTML = `<div class="toolbar">
      <h2>Library <span class="muted" style="font-size:1rem">${games.filter((g) => !g.hidden).length} games</span></h2>
      <input type="search" id="q" placeholder="Search games" aria-label="Search games" value="${esc(f.q)}">
      <select id="sort" aria-label="Sort by">
        <option value="recent">Recently played</option><option value="playtime">Most played</option><option value="name">Name</option>
      </select>
    </div>
    <div class="toolbar" style="margin-top:-6px">
      <div class="chips" role="group" aria-label="Launcher">
        ${["all", ...sources].map((s) => `<button class="chip" data-src="${s}" aria-pressed="${f.source === s}">${s === "all" ? "All" : esc(SOURCES[s] || s)}</button>`).join("")}
      </div>
      ${hiddenCount ? `<label class="check" style="margin:0 0 0 auto"><input type="checkbox" id="showhidden" ${f.hidden ? "checked" : ""}> Show ${hiddenCount} hidden</label>` : ""}
    </div>
    <div class="shelf" id="shelf"></div>`;
  document.getElementById("sort").value = f.sort;
  document.getElementById("q").addEventListener("input", (e) => { f.q = e.target.value; draw(); });
  document.getElementById("sort").addEventListener("change", (e) => { f.sort = e.target.value; draw(); });
  document.getElementById("showhidden")?.addEventListener("change", (e) => { f.hidden = e.target.checked; draw(); });
  $app.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
    f.source = c.dataset.src;
    $app.querySelectorAll(".chip").forEach((x) => x.setAttribute("aria-pressed", String(x === c)));
    draw();
  }));
  draw();
}

// ---------------- game page ----------------

function cell(c) {
  if (c && typeof c === "object") {
    if ("strip" in c) {  // a match's rounds: filled = won, outlined = lost, gap at the side switch
      return `<span class="strip">${c.strip.map((r, i, a) => `${i && a[i - 1].s !== r.s ? '<i class="half"></i>' : ""}<b class="${r.w ? "w" : "l"}" title="${esc(r.t)}"></b>`).join("")}</span>`;
    }
    if ("ts" in c) return `<span title="${esc(new Date(c.ts * 1000).toLocaleString())}">${ago(c.ts)}</span>`;
    const icon = safeUrl(c.icon) ? `<img src="${esc(c.icon)}" alt="" loading="lazy">` : "";
    const cls = c.tone === "good" ? "good" : c.tone === "bad" ? "bad" : "";
    return `<span class="ic ${cls}">${icon}${esc(c.text)}</span>`;
  }
  return esc(c ?? "");
}

function achList(items, kind) {
  const li = (a) => {
    const secret = kind === "locked" && a.hidden;
    const meta = kind === "unlocked" ? (a.unlock_ts ? `Unlocked ${ago(a.unlock_ts)}` : "Unlocked") : "";
    const pct = a.pct != null ? `${a.pct}% of players` : "";
    return `<li class="${secret ? "secret" : ""}" ${secret ? 'title="Secret achievement. Click to reveal."' : ""}>
      ${safeUrl(a.icon_url) ? `<img src="${esc(a.icon_url)}" alt="" loading="lazy">` : `<span class="ph"></span>`}
      <div><div class="n">${esc(a.name)}</div><div class="d">${esc(a.desc || (a.hidden ? "Secret achievement" : ""))}</div>
      <div class="m">${[meta, pct].filter(Boolean).join(", ")}</div></div></li>`;
  };
  return `<ul class="ach" data-kind="${kind}">${items.slice(0, 6).map(li).join("")}</ul>
    ${items.length > 6 ? `<button class="linkish" data-more="${kind}">Show all ${items.length}</button><ul class="ach" hidden data-rest="${kind}">${items.slice(6).map(li).join("")}</ul>` : ""}`;
}

function panelHtml(p) {
  const s = p.sync;
  const syncLine = s ? `<div class="sync-line ${s.ok ? "" : "err"}">${s.ok ? `Synced ${ago(s.ts)}: ${esc(s.msg)}` : `Last sync ${ago(s.ts)} failed: ${esc(s.msg)}`}</div>` : "";
  const tiles = (p.tiles || []).map((t) => `<div class="tile"><span class="label">${esc(t.label)}</span>
      <span class="value">${safeUrl(t.icon) ? `<img src="${esc(t.icon)}" alt="">` : ""}${esc(t.value)}</span>
      ${t.sub ? `<span class="s">${typeof t.sub === "object" ? cell(t.sub) : esc(t.sub)}</span>` : ""}
      ${t.progress != null ? `<div class="progress" role="progressbar" aria-valuenow="${Math.round(t.progress * 100)}" aria-valuemin="0" aria-valuemax="100"><i style="width:${t.progress * 100}%"></i></div>` : ""}</div>`).join("");
  const tables = (p.tables || []).filter((t) => t.rows.length).map((t) => `<div class="table-wrap"><h3>${esc(t.title)}</h3>
      <table><thead><tr>${t.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${t.rows.map((r, i) => `<tr${t.links ? ` class="rowlink" data-href="${esc(t.links[i])}" tabindex="0" title="${esc(t.link_title || "Open match")}"` : ""}>${r.map((c) => `<td>${cell(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`).join("");
  const ach = p.achievements ? `<div class="ach-cols">
      <div><h3>Recently unlocked</h3>${p.achievements.unlocked.length ? achList(p.achievements.unlocked, "unlocked") : '<p class="muted">None yet.</p>'}</div>
      <div><h3>Most players get next</h3>${p.achievements.locked.length ? achList(p.achievements.locked, "locked") : '<p class="muted">All unlocked. Nice.</p>'}</div></div>` : "";
  const dmap = p.death_map && p.death_map.maps.length ? deathMapHtml(p.death_map) : "";
  const bars = (p.bars || []).filter((b) => b.items.length).map(barsHtml).join("");
  const timeline = p.timeline ? `<div class="timeline" data-timeline='${esc(JSON.stringify(p.timeline))}'></div>` : "";
  return `<div class="panel"><div class="section-head"><h2>${esc(p.title)}</h2></div>${syncLine}
    ${tiles ? `<div class="tiles">${tiles}</div>` : ""}${(p.notes || []).map((n) => `<p class="note">${esc(n)}</p>`).join("")}${bars}${timeline}${ach}${dmap}${tables}</div>`;
}

const modeKey = (gid) => `gt.mode.${gid}`;
function savedMode(gid) { try { return localStorage.getItem(modeKey(gid)); } catch { return null; } }
function saveMode(gid, mode) { try { localStorage.setItem(modeKey(gid), mode); } catch { /* private window */ } }
const gameUrl = (gid, mode) => `/api/games/${encodeURIComponent(gid)}${mode ? `?mode=${encodeURIComponent(mode)}` : ""}`;

function modeBarHtml(d) {
  if ((d.modes || []).length < 2) return "";
  return `<div class="mode-bar" role="group" aria-label="Game mode"><span class="label">Mode</span>
    <div class="chips">${d.modes.map((m) => `<button class="chip" type="button" data-mode="${esc(m.key)}" aria-pressed="${m.key === d.mode}">${esc(m.label)}<span class="muted">${m.count}</span></button>`).join("")}</div></div>`;
}

async function viewGame(gid) {
  state.view = "game";
  const nav = state.nav = (state.nav || 0) + 1;
  $app.innerHTML = `<p class="empty">Loading…</p>`;
  let d;
  try { d = await api(gameUrl(gid, savedMode(gid))); }
  catch (e) { $app.innerHTML = `<p class="empty">${esc(e.message)}. <a href="#/library">Back to the library</a></p>`; return; }
  if (!state.settings) state.settings = await api("/api/settings").catch(() => ({}));
  const g = d.game;
  const competitive = d.panels.some((p) => COMPETITIVE.includes(p.key));
  const presets = competitive
    ? [["improve", "How can I improve?"], ["session", "Recap my recent sessions"], ["story", "What should I work towards?"]]
    : [["story", "Where am I, and what's next?"], ["session", "Recap where I left off"]];
  const spoiler = state.settings.spoiler_level || "light";
  const current = d.coach.filter((c) => (c.gen || 0) === (d.coach_gen || 0)).reverse();  // oldest first
  const earlier = d.coach.filter((c) => (c.gen || 0) !== (d.coach_gen || 0));

  $app.innerHTML = `<section class="banner" id="banner">
      <div class="sub">${esc(SOURCES[g.source] || g.source)}${g.installed ? "" : ", not installed"}${d.playing ? ", playing now" : ""}</div>
      <h1>${esc(g.name)}</h1>
      <div class="facts">
        <div><b>${hours(g.total_sec)}</b><span>Total played</span></div>
        <div><b>${hours(g.tracked_sec)}</b><span>Tracked here</span></div>
        <div><b>${g.session_count}</b><span>Sessions</span></div>
        <div><b>${g.last_played ? ago(g.last_played) : "Never"}</b><span>Last played</span></div>
      </div>
      <div class="actions">
        ${d.panels.length ? `<button class="btn primary" id="sync">Sync stats now</button>` : ""}
        <button class="btn" id="hide">${g.hidden ? "Show in library" : "Hide from library"}</button>
        <button class="btn" id="track">${g.tracked ? "Stop tracking" : "Track this game"}</button>
        <button class="btn" id="set-cover">Set cover image</button>
        ${g.custom ? `<button class="btn" id="remove">Remove game</button>` : ""}
      </div>
    </section>
    <div class="game-grid">
      <div class="stack">
        ${modeBarHtml(d)}
        <div class="stack" id="panels">${d.panels.length ? d.panels.map(panelHtml).join("") : `<div class="panel"><p class="muted" style="margin:0">No stat source for this game yet: playtime and sessions are still tracked, and the coach can work from your notes.</p></div>`}</div>
        <div id="improve-panel">${improvePanelHtml(gid, d.improve)}</div>
        <div id="guides-panel">${guidesPanelHtml(gid, d.kb)}</div>
        <div class="panel">
          <div class="section-head"><h2>Sessions</h2><span>${d.sessions.length ? `${d.sessions.length} tracked` : ""}</span></div>
          ${d.sessions.length ? `<div class="table-wrap" style="margin-top:0"><table><thead><tr><th>Day</th><th>Time</th><th>Length</th></tr></thead><tbody>
            ${d.sessions.slice(0, 25).map((s) => `<tr><td>${dayLabel(dayKey(s.start_ts))}</td><td>${timeOfDay(s.start_ts)} to ${timeOfDay(s.end_ts)}</td><td>${dur(s.end_ts - s.start_ts)}</td></tr>`).join("")}
            </tbody></table></div>` : `<p class="muted" style="margin:0">Play it once and sessions start showing up here.</p>`}
        </div>
      </div>
      <aside class="coach-dock">
        <div class="panel coach">
          <div class="coach-head">
            <div class="section-head" style="margin:0"><h2>Coach</h2><span class="coach-links">
              <a class="linkish" style="margin:0" href="${current.length ? chatHref(gid, d.coach_gen || 0) : `#/chat/new/${encodeURIComponent(gid)}`}">Open in chat</a>
              ${current.length ? `<button class="linkish" id="new-convo" style="margin:0">New conversation</button>` : ""}</span></div>
            <p class="sub">An agent that reads all your data for this game and remembers what it learns about you.</p>
          </div>
          <div class="coach-scroll" id="coach-scroll">
            ${earlier.length ? `<details class="history"><summary>Earlier conversations (${earlier.length} answers)</summary>${earlier.map(turnHtml).join("")}</details>` : ""}
            <div class="thread" id="thread">${current.map(turnHtml).join("")}</div>
            ${current.length ? "" : `<p class="muted coach-empty">Pick a starter below or ask your own question.</p>`}
            <div id="answer"></div>
          </div>
          <div class="coach-compose">
            <div class="presets">${presets.map(([k, l]) => `<button class="btn" data-preset="${k}">${esc(l)}</button>`).join("")}</div>
            <form id="ask">
              <div class="row"><input type="text" id="question" placeholder="${current.length ? "Ask a follow-up" : `Ask anything about ${esc(g.name)}`}" aria-label="Question for the coach">
                <select id="spoiler" aria-label="Spoilers">
                  <option value="none">No spoilers</option><option value="light">Light spoilers</option><option value="full">Spoilers OK</option>
                </select>
                <button class="btn primary" type="submit">Ask</button></div>
            </form>
            <details class="notes"><summary>Your notes${g.notes ? "" : ": tell the coach where you are"}</summary>
              <textarea id="notes" placeholder="e.g. Just finished the heist, siding with Panam, level 22 netrunner build">${esc(g.notes)}</textarea>
              <button class="btn" id="save-notes" style="margin-top:8px">Save notes</button>
            </details>
          </div>
        </div>
      </aside>
    </div>`;
  setBg(document.getElementById("banner"), art(g).hero);
  document.getElementById("spoiler").value = spoiler;
  const coachScroll = document.getElementById("coach-scroll");
  const showTurn = (el) => { if (el) coachScroll.scrollTop = el.offsetTop - 8; };
  showTurn(document.getElementById("thread").lastElementChild);
  wireAchievements();
  wireGuidesPanel(gid);
  let mode = d.mode;
  $app.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.mode === mode) return;
    mode = b.dataset.mode;
    saveMode(gid, mode);
    $app.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    const box = document.getElementById("panels");
    box.classList.add("loading");
    try {
      const fresh = await api(gameUrl(gid, mode));
      if (state.view !== "game" || mode !== fresh.mode) return;
      d.panels = fresh.panels;
      box.innerHTML = fresh.panels.map(panelHtml).join("");
      wireAchievements();
    } catch (e) { toast(e.message); }
    finally { box.classList.remove("loading"); }
  }));

  const ask = async (preset) => {
    const q = document.getElementById("question").value.trim();
    const out = document.getElementById("answer");
    const buttons = $app.querySelectorAll(".coach .btn");
    buttons.forEach((b) => (b.disabled = true));
    out.innerHTML = `<div class="thinking"><span class="spinner" aria-hidden="true"></span>The coach is going through your data. This can take a minute or two.</div>`;
    coachScroll.scrollTop = coachScroll.scrollHeight;
    try {
      const r = await api(`/api/games/${encodeURIComponent(gid)}/coach`, {
        method: "POST", body: { preset: preset || (q ? "custom" : "auto"), question: q, spoiler: document.getElementById("spoiler").value } });
      if (state.nav !== nav) return;  // left the page: the answer is saved and shows when you come back
      out.innerHTML = "";
      document.getElementById("thread").insertAdjacentHTML("beforeend", turnHtml({ question: r.question, content: r.content, ts: Date.now() / 1000, model: r.model }));
      document.getElementById("question").value = "";
      document.getElementById("question").placeholder = "Ask a follow-up";
      document.querySelector(".coach-empty")?.remove();
      showTurn(document.getElementById("thread").lastElementChild);
    } catch (e) {
      if (state.nav === nav) out.innerHTML = errorHtml(e.message);
    } finally {
      if (state.nav === nav) buttons.forEach((b) => (b.disabled = false));
    }
  };
  if (d.coach_status && d.coach_status.error) document.getElementById("answer").innerHTML = errorHtml(d.coach_status.error);
  else if (d.coach_status) {  // asked on an earlier visit and still being written
    const buttons = $app.querySelectorAll(".coach .btn");
    buttons.forEach((b) => (b.disabled = true));
    document.getElementById("answer").innerHTML = coachThinkingHtml(d.coach_status.question, "The coach is going through your data. This can take a minute or two.");
    coachScroll.scrollTop = coachScroll.scrollHeight;
    waitForCoach(gid, d.coach_gen || 0, nav, (t) => {
      document.getElementById("thread").innerHTML = t.turns.map(turnHtml).join("");
      document.getElementById("answer").innerHTML = t.status && t.status.error ? errorHtml(t.status.error) : "";
      document.querySelector(".coach-empty")?.remove();
      buttons.forEach((b) => (b.disabled = false));
      showTurn(document.getElementById("thread").lastElementChild);
    });
  }
  $app.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => { document.getElementById("question").value = ""; ask(b.dataset.preset); }));
  document.getElementById("ask").addEventListener("submit", (e) => { e.preventDefault(); ask(); });
  document.getElementById("new-convo")?.addEventListener("click", async () => {
    await api(`/api/games/${encodeURIComponent(gid)}/coach/new`, { method: "POST" });
    toast("New conversation started. The coach keeps what it learned about you.");
    viewGame(gid);
  });

  document.getElementById("save-notes").addEventListener("click", async () => {
    await api(`/api/games/${encodeURIComponent(gid)}`, { method: "POST", body: { notes: document.getElementById("notes").value } });
    toast("Notes saved");
  });
  document.getElementById("hide").addEventListener("click", async () => {
    await api(`/api/games/${encodeURIComponent(gid)}`, { method: "POST", body: { hidden: g.hidden ? 0 : 1 } });
    toast(g.hidden ? "Back in your library" : "Hidden from your library");
    viewGame(gid);
  });
  document.getElementById("track").addEventListener("click", async () => {
    await api(`/api/games/${encodeURIComponent(gid)}`, { method: "POST", body: { tracked: g.tracked ? 0 : 1 } });
    toast(g.tracked ? "Tracking stopped for this game" : "Tracking this game");
    viewGame(gid);
  });
  document.getElementById("set-cover").addEventListener("click", async () => {
    const cur = (g.art_override || {}).cover || "";
    const url = prompt("Paste a link to a cover image (portrait works best). Leave it empty to go back to the automatic art.", cur);
    if (url === null) return;
    try {
      await api(`/api/games/${encodeURIComponent(gid)}`, { method: "POST", body: { cover_url: url.trim() } });
      toast(url.trim() ? "Cover updated" : "Back to automatic art");
      viewGame(gid);
    } catch (e) { toast(e.message); }
  });
  document.getElementById("remove")?.addEventListener("click", async () => {
    if (!confirm(`Remove ${g.name}? Its play history stays in the database.`)) return;
    await api(`/api/games/${encodeURIComponent(gid)}`, { method: "DELETE" });
    location.hash = "#/library";
  });
  document.getElementById("sync")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "Syncing…";
    const before = JSON.stringify(d.panels.map((p) => p.sync));
    const r = await api(`/api/games/${encodeURIComponent(gid)}/sync`, { method: "POST" });
    if (!r.agent_online) { toast("Your gaming PC is offline. Sync runs when it reconnects."); btn.disabled = false; btn.textContent = "Sync stats now"; return; }
    for (let i = 0; i < 20; i++) {
      await new Promise((res) => setTimeout(res, 3000));
      if (state.view !== "game") return;
      const fresh = await api(gameUrl(gid, mode));
      if (JSON.stringify(fresh.panels.map((p) => p.sync)) !== before) {
        document.getElementById("panels").innerHTML = fresh.panels.map(panelHtml).join("");
        wireAchievements();
        break;
      }
    }
    btn.disabled = false;
    btn.textContent = "Sync stats now";
  });
}

function turnHtml(c) {
  return `<div class="turn"><div class="q"><span>${esc(c.question)}</span><time>${ago(c.ts)}</time></div>
    <div class="answer">${md(c.content)}</div>${c.model ? `<div class="via">${esc(c.model)}</div>` : ""}</div>`;
}

// ---------------- coach answers that outlive the page that asked ----------------
// An answer takes a minute or two and is saved by the server even if you leave. A page that finds one still being
// written shows "thinking" and checks back until it's saved. `nav` is the page visit (state.nav) that is waiting.

const errorHtml = (msg) => `<p class="error">${esc(msg)}${/key|set up/i.test(msg) ? ` <a href="#/settings">Open Settings</a>` : ""}</p>`;
const coachThinkingHtml = (q, text) => `${q ? `<div class="turn pending"><div class="q"><span>${esc(q)}</span></div></div>` : ""}
  <div class="thinking"><span class="spinner" aria-hidden="true"></span>${esc(text)}</div>`;

async function waitForCoach(gid, gen, nav, done) {
  while (state.nav === nav) {
    await new Promise((res) => setTimeout(res, 4000));
    if (state.nav !== nav) return;
    let t;
    try { t = await api(`/api/coach/conversations/${encodeURIComponent(gid)}/${gen}`); } catch { continue; }
    if (state.nav !== nav) return;
    if (!t.status || t.status.error) { done(t); return; }
  }
}

function matchCoachHtml(sub, placeholder = "Or ask about this match") {
  return `<section class="panel coach" id="match-coach" data-placeholder="${esc(placeholder)}">
      <div class="section-head" style="margin:0"><h2>Coach</h2><span class="coach-links" id="mc-links"></span></div>
      <p class="sub" style="margin:6px 0 0">${esc(sub)} Each match gets its own conversation, which you can also continue in Chat.</p>
      <div class="thread" id="mc-thread"></div>
      <div id="m-answer"></div>
      <div class="presets" id="mc-start"><button class="btn primary" data-q="">Break down this match</button></div>
      <form id="ask-match"><div class="row"><input type="text" id="mq" placeholder="${esc(placeholder)}" aria-label="Question about this match">
        <button class="btn" type="submit">Ask</button></div></form>
    </section>`;
}

function wireMatchCoach(gid, mid, mc, thinking) {
  const nav = state.nav;
  const $ = (id) => document.getElementById(id);
  let gen = mc ? mc.gen : null, turns = mc ? mc.turns : [];
  const render = () => {
    $("mc-thread").innerHTML = turns.map(turnHtml).join("");
    $("mc-start").hidden = turns.length > 0;
    $("mq").placeholder = turns.length ? "Ask a follow-up about this match" : $("match-coach").dataset.placeholder;
    $("mc-links").innerHTML = gen != null && turns.length ? `<a class="linkish" style="margin:0" href="${chatHref(gid, gen)}">Open in chat</a>` : "";
  };
  const setBusy = (on) => $("match-coach").querySelectorAll(".btn").forEach((b) => (b.disabled = on));
  const wait = (q) => {  // asked on an earlier visit and still being written
    $("m-answer").innerHTML = coachThinkingHtml(q, thinking);
    setBusy(true);
    waitForCoach(gid, gen, nav, (t) => {
      turns = t.turns;
      render();
      setBusy(false);
      $("m-answer").innerHTML = t.status && t.status.error ? errorHtml(t.status.error) : "";
    });
  };
  const ask = async (q) => {
    $("m-answer").innerHTML = coachThinkingHtml(q || "Break down this match", thinking);
    setBusy(true);
    try {
      const r = await api(`/api/games/${encodeURIComponent(gid)}/coach`, { method: "POST", body: { preset: "match", question: q, match_id: mid } });
      if (state.nav !== nav) return;  // left the page: the answer is saved and shows when you come back
      gen = r.gen;
      turns = [...turns, { question: r.question, content: r.content, ts: r.ts, model: r.model }];
      $("m-answer").innerHTML = "";
      $("mq").value = "";
      render();
    } catch (e) {
      if (state.nav === nav) $("m-answer").innerHTML = errorHtml(e.message);
    } finally {
      if (state.nav === nav) setBusy(false);
    }
  };
  render();
  if (mc && mc.status) {
    if (mc.status.error) $("m-answer").innerHTML = errorHtml(mc.status.error);
    else wait(mc.status.question);
  }
  $("match-coach").querySelector("[data-q]").addEventListener("click", () => ask(""));
  $("ask-match").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("mq").value.trim();
    if (q || !turns.length) ask(q);
  });
}

// Overwatch roles keep the same colour everywhere; other groups take the next slot in order.
const ROLE_COLORS = { tank: SERIES[0], damage: SERIES[1], support: SERIES[2] };
function groupColor(group, groups) {
  if (!group) return "#ffffff";
  return ROLE_COLORS[group] || SERIES[groups.indexOf(group) % SERIES.length];
}

function orderGroups(list) {  // roles always in the same order: tank, damage, support, then anything else
  const uniq = [...new Set(list.filter(Boolean))];
  const known = Object.keys(ROLE_COLORS);
  return [...known.filter((k) => uniq.includes(k)), ...uniq.filter((g) => !known.includes(g))];
}

function barsHtml(b) {
  const max = Math.max(1, ...b.items.map((i) => i.value || 0));
  const groups = orderGroups(b.items.map((i) => i.group));
  const legend = groups.length > 1 ? `<div class="legend">${groups.map((g) => `<span><i style="background:${groupColor(g, groups)}"></i>${esc(g[0].toUpperCase() + g.slice(1))}</span>`).join("")}</div>` : "";
  return `<div class="hbars"><h3>${esc(b.title)}</h3>${legend}${b.items.map((i) => `<div class="hbar">
      <span class="hb-label">${safeUrl(i.icon) ? `<img src="${esc(i.icon)}" alt="" loading="lazy">` : ""}${/^#\/[\w%/:-]+$/.test(i.href || "") ? `<a href="${esc(i.href)}">${esc(i.label)}</a>` : esc(i.label)}</span>
      <span class="hb-track"><i style="width:${Math.max(1.5, (100 * (i.value || 0)) / max)}%;background:${groupColor(i.group, groups)}"></i></span>
      <span class="hb-val">${esc(i.display ?? i.value)}</span>
      ${i.sub ? `<span class="hb-sub">${esc(i.sub)}</span>` : ""}</div>`).join("")}</div>`;
}

function drawTimelines() {
  $app.querySelectorAll(".timeline[data-timeline]").forEach((el) => {
    const t = JSON.parse(el.dataset.timeline);
    const pts = t.series.flatMap((s) => s.points);
    const groups = orderGroups(t.series.map((s) => s.group));
    t.series.sort((a, b) => groups.indexOf(a.group) - groups.indexOf(b.group));
    const W = Math.max(300, el.clientWidth), H = 220, L = 74, R = 12, T = 10, B = 26;
    const t0 = Math.min(...pts.map((p) => p.ts)), t1 = Math.max(t.line ? 0 : Date.now() / 1000, ...pts.map((p) => p.ts));
    const vals = [...pts.map((p) => p.v), ...(t.target != null ? [t.target] : [])];
    const lo = Math.max(0, Math.min(...vals) - 3), hi = Math.max(...vals) + 3;
    const x = (ts) => L + (W - L - R) * ((ts - t0) / Math.max(1, t1 - t0));
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / Math.max(1, hi - lo));
    let svg = `<g class="grid">`;
    t.ticks.filter((k) => k.v >= lo && k.v <= hi).forEach((k) => { svg += `<line x1="${L}" x2="${W - R}" y1="${y(k.v)}" y2="${y(k.v)}"/>`; });
    svg += `</g><g class="axis">`;
    t.ticks.filter((k) => k.v >= lo && k.v <= hi).forEach((k) => { svg += `<text x="${L - 8}" y="${y(k.v) + 4}" text-anchor="end">${esc(k.label)}</text>`; });
    const dateFmt = t1 - t0 > 300 * 86400 ? { month: "short", year: "numeric" } : { month: "short", day: "numeric" };  // years apart: show the year
    [t0, (t0 + t1) / 2, t1].forEach((ts, i) => {
      svg += `<text x="${x(ts)}" y="${H - 6}" text-anchor="${["start", "middle", "end"][i]}">${new Date(ts * 1000).toLocaleDateString(undefined, dateFmt)}</text>`;
    });
    svg += `</g>`;
    if (t.target != null) svg += `<line x1="${L}" x2="${W - R}" y1="${y(t.target)}" y2="${y(t.target)}" class="tl-target"/><text x="${W - R}" y="${y(t.target) - 6}" text-anchor="end" class="tl-target-label">target</text>`;
    t.series.forEach((s) => {
      const c = groupColor(s.group, groups);
      let d = "";
      s.points.forEach((p, i) => { d += i ? (t.line ? ` L${x(p.ts)},${y(p.v)}` : ` H${x(p.ts)} V${y(p.v)}`) : `M${x(p.ts)},${y(p.v)}`; });
      if (!t.line) d += ` H${x(t1)}`;
      svg += `<path d="${d}" fill="none" stroke="${c}" stroke-width="2"/>`;
      s.points.forEach((p) => { svg += `<circle cx="${x(p.ts)}" cy="${y(p.v)}" r="5" fill="${c}" stroke="var(--panel)" stroke-width="2" class="tl-pt" data-tip="${esc(`${s.name}: ${p.label}, ${new Date(p.ts * 1000).toLocaleDateString()}`)}"/>`; });
    });
    el.innerHTML = `<h3>${esc(t.title)}</h3><div class="legend">${t.series.map((s) => `<span><i style="background:${groupColor(s.group, groups)}"></i>${esc(s.name)}</span>`).join("")}</div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(t.title)}">${svg}</svg>`;
    el.querySelectorAll(".tl-pt").forEach((c) => {
      const tip = (ev) => showTip(`<b>${esc(c.dataset.tip)}</b>`, ev.clientX, ev.clientY);
      c.addEventListener("pointermove", tip);
      c.addEventListener("pointerdown", tip);
      c.addEventListener("pointerleave", hideTip);
    });
  });
}

function wireAchievements() {
  $app.querySelectorAll("[data-more]").forEach((b) => b.addEventListener("click", () => {
    const rest = $app.querySelector(`[data-rest="${b.dataset.more}"]`);
    rest.hidden = !rest.hidden;
    b.textContent = rest.hidden ? b.textContent.replace("Hide", "Show all") : "Hide the rest";
  }));
  $app.querySelectorAll(".ach .secret").forEach((li) => li.addEventListener("click", () => li.classList.remove("secret")));
  wireDeathMaps();
  drawTimelines();
}

// ---------------- VALORANT death / kill map ----------------

const DMAPS = {};
let dmapSeq = 0;

function deathMapHtml(dm) {
  const id = `dmap-${++dmapSeq}`;
  DMAPS[id] = { maps: dm.maps, map: 0, kind: "death", side: "all", mode: "dots" };
  const hasUtil = dm.maps.some((m) => m.points.some((p) => p.kind === "utility"));
  const sides = dm.sides || [["attack", "Attack"], ["defense", "Defense"]];
  const chips = (role, opts, cur) => `<div class="chips" role="group" data-role="${role}">${opts.map(([v, l]) =>
    `<button class="chip" data-v="${v}" aria-pressed="${v === cur}">${l}</button>`).join("")}</div>`;
  return `<div class="dmap" id="${id}"><h3>Where it happens</h3>
    <div class="dmap-controls">
      ${chips("map", dm.maps.map((m, i) => [String(i), `${esc(m.name)} <span class="muted">${m.points.length}</span>`]), "0")}
      ${chips("kind", [["death", "Deaths"], ["kill", "Kills"], ["both", "Both"], ...(hasUtil ? [["utility", "Utility"]] : [])], "death")}
      ${chips("side", [["all", "Both sides"], ...sides], "all")}
      ${dm.heat ? chips("mode", [["dots", "Dots"], ["heat", "Heat"]], "dots") : ""}
    </div><div class="dmap-stage"></div></div>`;
}

function drawDeathMap(id) {
  const st = DMAPS[id], el = document.getElementById(id);
  if (!st || !el) return;
  const m = st.maps[st.map];
  const pts = m.points.filter((p) => (st.kind === "both" ? p.kind !== "utility" : p.kind === st.kind) && (st.side === "all" || p.side === st.side));
  const color = (p) => (p.kind === "death" ? SERIES[1] : p.kind === "utility" ? SERIES[2] : SERIES[0]);
  const heat = st.mode === "heat";
  const marks = pts.map((p, i) => {
    const x = p.x * 1000, y = p.y * 1000;
    if (heat) return `<circle class="heat" cx="${x}" cy="${y}" r="46" fill="url(#heat-${p.kind})"/>`;
    const lower = p.lower ? ' stroke-dasharray="4 3"' : "";
    return p.kind === "death" && p.first
      ? `<rect class="pt" data-i="${i}" x="${x - 10}" y="${y - 10}" width="20" height="20" transform="rotate(45 ${x} ${y})" fill="${color(p)}"/>`
      : `<circle class="pt${p.lower ? " lower" : ""}" data-i="${i}" cx="${x}" cy="${y}" r="11" fill="${color(p)}"${lower}/>`;
  }).join("");
  const grads = heat ? `<defs>${["death", "kill", "utility"].map((k) => `<radialGradient id="heat-${k}"><stop offset="0" stop-color="${color({ kind: k })}" stop-opacity=".7"/><stop offset=".45" stop-color="${color({ kind: k })}" stop-opacity=".35"/><stop offset="1" stop-color="${color({ kind: k })}" stop-opacity="0"/></radialGradient>`).join("")}</defs>` : "";
  const legend = [
    ["death", "both"].includes(st.kind) ? `<span><i style="background:${SERIES[1]}"></i>Death</span>${heat ? "" : `<span><i class="diamond" style="background:${SERIES[1]}"></i>First death of the round</span>`}` : "",
    ["kill", "both"].includes(st.kind) ? `<span><i style="background:${SERIES[0]}"></i>Kill (where you stood)</span>` : "",
    st.kind === "utility" ? `<span><i style="background:${SERIES[2]}"></i>Where your grenade landed</span>` : "",
    heat ? `<span class="muted">Brighter = more often</span>` : "",
    !heat && pts.some((p) => p.lower) ? `<span class="muted">Dashed outline = lower level</span>` : "",
  ].join("");
  el.querySelector(".dmap-stage").innerHTML = `<svg viewBox="0 0 1000 1000" role="img" aria-label="${esc(m.name)} minimap with ${pts.length} marks">
      ${grads}<image href="${esc(safeUrl(m.icon))}" x="0" y="0" width="1000" height="1000"/>${marks}</svg>
    <div class="legend">${legend}<span class="muted">${pts.length} shown</span></div>`;
  el.querySelectorAll(".pt").forEach((c) => {
    const p = pts[Number(c.dataset.i)];
    const tip = (ev) => showTip(`<b>${{ death: "Death", kill: "Kill", utility: "Utility" }[p.kind]}, ${ago(p.ts)}</b>${esc(p.label)}${p.lower ? " (lower level)" : ""}`, ev.clientX, ev.clientY);
    c.addEventListener("pointermove", tip);
    c.addEventListener("pointerdown", tip);
    c.addEventListener("pointerleave", hideTip);
  });
}

function wireDeathMaps() {
  for (const id of Object.keys(DMAPS)) {
    const el = document.getElementById(id);
    if (!el) { delete DMAPS[id]; continue; }
    if (el.dataset.wired) continue;
    el.dataset.wired = "1";
    el.querySelectorAll(".dmap-controls .chips").forEach((g) => g.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
      const st = DMAPS[id], role = g.dataset.role;
      st[role] = role === "map" ? Number(b.dataset.v) : b.dataset.v;
      g.querySelectorAll(".chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      drawDeathMap(id);
    })));
    drawDeathMap(id);
  }
}

// ---------------- VALORANT match page ----------------

const mmss = (t) => `${Math.floor((t || 0) / 60)}:${String(Math.floor((t || 0) % 60)).padStart(2, "0")}`;
const BUY = { pistol: "Pistol", eco: "Eco", half: "Half buy", full: "Full buy" };
const credits = (v) => (v == null ? "?" : `${Number(v).toLocaleString()}`);

// A game's sub-page built by its integration, e.g. one Overwatch hero.
async function viewDetail(gid, key) {
  state.view = "detail";
  $app.innerHTML = `<p class="empty">Loading…</p>`;
  let d;
  try { d = await api(`/api/games/${encodeURIComponent(gid)}/detail/${encodeURIComponent(key)}`); }
  catch (e) { $app.innerHTML = `<p class="empty">${esc(e.message)}. <a href="${gameHref(gid)}">Back to the game</a></p>`; return; }
  const fact = (f) => `<div><b class="${f.tone === "good" ? "good" : f.tone === "bad" ? "bad" : ""}">${esc(f.v)}</b><span>${esc(f.l)}</span></div>`;
  $app.innerHTML = `<section class="banner" id="banner">
      <div class="sub"><a href="${gameHref(gid)}">${esc(d.game.name)}</a>${d.sub ? `, ${esc(d.sub)}` : ""}</div>
      <h1 class="with-icon">${safeUrl(d.icon) ? `<img src="${esc(d.icon)}" alt="">` : ""}${esc(d.title)}</h1>
      ${(d.facts || []).length ? `<div class="facts">${d.facts.map(fact).join("")}</div>` : ""}
    </section>
    <div class="stack">${(d.panels || []).map(panelHtml).join("")}</div>`;
  drawTimelines();
}

async function viewMatch(mid) {
  state.view = "match";
  state.nav = (state.nav || 0) + 1;
  $app.innerHTML = `<p class="empty">Loading match…</p>`;
  let res;
  try { res = await api(`/api/matches/${encodeURIComponent(mid)}`); }
  catch (e) { $app.innerHTML = `<p class="empty">${esc(e.message)}. <a href="#/library">Back to the library</a></p>`; return; }
  if (res.game.id === "steam:730") return viewCs2Match(res, mid);
  const m = res.match.data, gid = res.game.id;
  const rounds = m.rounds || [];
  const rb = m.round_based !== false && rounds.length > 0;
  const me = (m.scoreboard || []).find((p) => p.me);
  const verdict = !rb ? `#${m.placement} of ${m.players}` : m.won ? "Victory" : m.won === false ? "Defeat" : "Draw";
  const tone = !rb ? (m.placement === 1 ? "good" : "") : m.won ? "good" : m.won === false ? "bad" : "";
  const fk = rounds.filter((r) => r.fk).length, fd = rounds.filter((r) => r.fd).length;
  const fact = (v, l, cls = "") => `<div><b class="${cls}">${v}</b><span>${l}</span></div>`;
  const hasEconomy = rounds.some((r) => r.loadout != null);

  $app.innerHTML = `<section class="banner" id="banner">
      <div class="sub"><a href="${gameHref(gid)}">${esc(res.game.name || "VALORANT")}</a>, ${esc(m.queue)}, ${new Date(m.start * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</div>
      <h1><span class="${tone}">${verdict}</span>${rb && m.rounds_won != null ? ` ${m.rounds_won}–${m.rounds_lost}` : ""} on ${esc(m.map)}</h1>
      <div class="facts">
        ${fact(`<span class="ic">${safeUrl(m.agent_icon) ? `<img src="${esc(m.agent_icon)}" alt="">` : ""}${esc(m.agent)}</span>`, "Agent")}
        ${fact(`${m.kills}/${m.deaths}/${m.assists}`, "K / D / A")}
        ${m.acs != null && rb ? fact(m.acs, "Combat score") : ""}
        ${m.adr != null && rb ? fact(m.adr, "Damage / round") : ""}
        ${m.hs_pct != null ? fact(`${m.hs_pct}%`, "Headshots") : ""}
        ${rb ? fact(`${fk} / ${fd}`, "First kills / deaths") : ""}
        ${m.rr_change != null ? fact(`${m.rr_change > 0 ? "+" : ""}${m.rr_change}`, "RR", m.rr_change > 0 ? "good" : m.rr_change < 0 ? "bad" : "") : ""}
        ${m.rank ? fact(esc(m.rank), "Rank after") : ""}
        ${fact(`${Math.round((m.length_sec || 0) / 60)} min`, "Length")}
      </div>
    </section>
    ${rb ? `<section class="match-grid">
      <div class="panel"><div class="section-head"><h2>Round map</h2><span class="sub" id="mm-caption"></span></div>
        <div class="chips" role="group" id="mm-kind">
          <button class="chip" data-v="death" aria-pressed="true">Deaths</button>
          <button class="chip" data-v="kill" aria-pressed="false">Kills</button>
          <button class="chip" data-v="both" aria-pressed="false">Both</button>
        </div>
        <div id="mm-stage" class="dmap-stage"></div>
      </div>
      <div class="panel"><div class="section-head"><h2>Rounds</h2><button class="linkish" id="mm-all" hidden>Show all rounds</button></div>
        <ol class="rounds" id="mm-rounds">${roundsListHtml(rounds, { reviews: res.reviews, frames: res.frames, abilities: m.abilities })}</ol>
      </div>
    </section>` : ""}
    ${rb && m.abilities ? abilitiesPanelHtml(m.abilities) : ""}
    ${rb && hasEconomy ? `<section class="panel chart"><div class="section-head"><h2>Economy</h2><span class="sub">Average loadout value at the start of each round</span></div><div id="econ"></div></section>` : ""}
    ${m.scoreboard ? `<section class="panel"><div class="section-head"><h2>Scoreboard</h2></div>${scoreboardHtml(m.scoreboard, rb)}</section>`
      : `<section class="panel"><p class="muted" style="margin:0">The full scoreboard, kill feed and team economy appear once the tracker re-downloads this match. That happens on its own the next time VALORANT or the Riot client is open.</p></section>`}
    ${matchCoachHtml("Reads every round of this match.", "Or ask about this match, e.g. why did we lose the second half?")}`;
  setBg(document.getElementById("banner"), safeUrl(m.map_splash));

  if (rb) {
    wireRoundMap(m, rounds);
    if (hasEconomy) drawEconomy(document.getElementById("econ"), rounds);
  }

  wireMatchCoach(gid, mid, res.coach, "The coach is going through the rounds. This can take a minute or two.");
}

function viewCs2DemoMatch(res, mid) {
  const m = res.match.data, gid = res.game.id, rounds = m.rounds || [];
  const verdict = m.won ? "Victory" : m.won === false ? "Defeat" : "Draw";
  const tone = m.won ? "good" : m.won === false ? "bad" : "";
  const fact = (v, l, cls = "") => `<div><b class="${cls}">${v}</b><span>${l}</span></div>`;
  const fk = rounds.filter((r) => r.fk).length, fd = rounds.filter((r) => r.fd).length;
  $app.innerHTML = `<section class="banner" id="banner">
      <div class="sub"><a href="${gameHref(gid)}">${esc(res.game.name || "Counter-Strike 2")}</a>, ${esc(m.queue)}, ${new Date(m.start * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}, from demo</div>
      <h1><span class="${tone}">${verdict}</span> ${m.rounds_won}–${m.rounds_lost} on ${esc(m.map)}</h1>
      <div class="facts">
        ${fact(`${m.kills}/${m.deaths}/${m.assists}`, "K / D / A")}${fact(m.adr, "Damage / round")}
        ${m.hs_pct != null ? fact(`${m.hs_pct}%`, "Headshot kills") : ""}${fact(`${fk} / ${fd}`, "First kills / deaths")}
        ${fact(m.mvps || 0, "MVPs")}${fact(rounds.length, "Rounds")}${fact(`${Math.round((m.length_sec || 0) / 60)} min`, "Length")}
      </div>
    </section>
    <section class="match-grid">
      <div class="panel"><div class="section-head"><h2>Round map</h2><span class="sub" id="mm-caption"></span></div>
        <div class="chips" role="group" id="mm-kind">
          <button class="chip" data-v="death" aria-pressed="true">Deaths</button>
          <button class="chip" data-v="kill" aria-pressed="false">Kills</button>
          <button class="chip" data-v="utility" aria-pressed="false">Utility</button>
          <button class="chip" data-v="both" aria-pressed="false">Both</button>
        </div>
        <div id="mm-stage" class="dmap-stage"></div>
      </div>
      <div class="panel"><div class="section-head"><h2>Rounds</h2><button class="linkish" id="mm-all" hidden>Show all rounds</button></div>
        <ol class="rounds" id="mm-rounds">${roundsListHtml(rounds, { cs2: true, reviews: res.reviews, frames: res.frames })}</ol>
      </div>
    </section>
    <section class="panel chart"><div class="section-head"><h2>Economy</h2><span>Average gear value at the start of each round</span></div><div id="econ"></div></section>
    <section class="panel"><div class="section-head"><h2>Scoreboard</h2></div>${scoreboardHtml(m.scoreboard || [], true, { cs2: true })}</section>
    ${matchCoachHtml("Reads every round of this demo: positions, utility and the full scoreboard.")}`;
  setBg(document.getElementById("banner"), safeUrl(m.map_icon));
  wireRoundMap(m, rounds);
  drawEconomy(document.getElementById("econ"), rounds);
  wireMatchCoach(gid, mid, res.coach, "The coach is going through the demo. This can take a minute or two.");
}

function viewCs2Match(res, mid) {
  if (res.match.data.source === "demo") return viewCs2DemoMatch(res, mid);
  const m = res.match.data, gid = res.game.id, rounds = m.rounds || [];
  const verdict = m.won ? "Victory" : m.won === false ? "Defeat" : m.partial ? "Unfinished" : "Draw";
  const tone = m.won ? "good" : m.won === false ? "bad" : "";
  const fact = (v, l, cls = "") => `<div><b class="${cls}">${v}</b><span>${l}</span></div>`;
  const BUYS = { pistol: "Pistol", eco: "Eco", half: "Half buy", full: "Full buy" };
  const side = (s) => (s === "ct" ? "CT" : s === "t" ? "T" : "?");
  const frames = res.frames || {};
  let prev = null;
  const list = rounds.map((r) => {
    const sep = prev && r.side !== prev ? `<li class="half-sep">Sides switch</li>` : "";
    prev = r.side;
    const badges = [r.kills >= 3 ? `<span class="badge good">${r.kills}K</span>` : "", r.hs ? `<span class="badge">${r.hs} headshot${r.hs > 1 ? "s" : ""}</span>` : "",
      r.bomb ? `<span class="badge">Bomb ${esc(r.bomb)}</span>` : ""].join("");
    return `${sep}<li class="round ${r.won ? "won" : "lost"}">
      <div class="rhead"><span class="rnum">${r.n + 1}</span><span class="res ${r.won ? "good" : "bad"}">${r.won ? "Won" : "Lost"}</span>
        <span class="muted">${side(r.side)}${r.end ? `, ${esc(r.end)}` : ""}</span>
        <span class="buy">${esc(BUYS[r.buy] || "")}${r.loadout != null ? ` · $${Number(r.loadout).toLocaleString()} gear` : ""}</span></div>
      <div class="rbody"><div>${r.kills} kill${r.kills === 1 ? "" : "s"}, ${r.damage} damage, ${r.survived ? `<span class="good">survived</span>` : "died"}${r.money != null ? `, $${Number(r.money).toLocaleString()} to spend` : ""}</div>
        ${badges ? `<div class="badges">${badges}</div>` : ""}
        ${!r.survived ? framesHtml(frames[r.n], `Round ${r.n + 1}`) : ""}</div></li>`;
  }).join("");
  $app.innerHTML = `<section class="banner" id="banner">
      <div class="sub"><a href="${gameHref(gid)}">${esc(res.game.name || "Counter-Strike 2")}</a>, ${esc(m.queue)}, ${new Date(m.start * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</div>
      <h1><span class="${tone}">${verdict}</span>${m.rounds_won != null ? ` ${m.rounds_won}–${m.rounds_lost}` : ""} on ${esc(m.map)}</h1>
      <div class="facts">
        ${fact(`${m.kills}/${m.deaths}/${m.assists}`, "K / D / A")}${fact(m.adr, "Damage / round")}
        ${m.hs_pct != null ? fact(`${m.hs_pct}%`, "Headshot kills") : ""}${fact(m.mvps || 0, "MVPs")}
        ${m.score != null ? fact(m.score, "Score") : ""}${fact(rounds.length, "Rounds")}${fact(`${Math.round((m.length_sec || 0) / 60)} min`, "Length")}
      </div>
    </section>
    <section class="panel chart"><div class="section-head"><h2>Economy</h2><span>Your gear and money at the start of each round</span></div><div id="econ"></div></section>
    <section class="panel"><div class="section-head"><h2>Rounds</h2><span>${rounds.filter((r) => r.won).length} won of ${rounds.length}</span></div>
      <ol class="rounds cs2-rounds">${list}</ol>
      <p class="muted" style="margin:12px 0 0;font-size:.8rem">CS2 only shares positions with spectators, so there's no death map for CS2.</p></section>
    ${matchCoachHtml("Reads every round of this match.")}`;
  drawEconomy(document.getElementById("econ"), rounds, [["loadout", "Your gear", SERIES[0]], ["money", "Money to spend", SERIES[1]]]);
  wireMatchCoach(gid, mid, res.coach, "The coach is going through the rounds. This can take a minute or two.");
}

function wireRoundMap(m, rounds) {
  state.mm = { m, round: null, kind: "death" };
  drawMatchMap();
  document.querySelectorAll("#mm-kind .chip").forEach((b) => b.addEventListener("click", () => {
    state.mm.kind = b.dataset.v;
    document.querySelectorAll("#mm-kind .chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    drawMatchMap();
  }));
  const pick = (li) => {
    const n = Number(li.dataset.n);
    state.mm.round = state.mm.round === n ? null : n;
    if (state.mm.round != null && state.mm.kind === "death" && !rounds.find((r) => r.n === n).death) state.mm.kind = "both";
    document.querySelectorAll("#mm-kind .chip").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.v === state.mm.kind)));
    document.querySelectorAll("#mm-rounds .round").forEach((x) => x.classList.toggle("selected", Number(x.dataset.n) === state.mm.round));
    document.getElementById("mm-all").hidden = state.mm.round == null;
    drawMatchMap();
    if (window.innerWidth < 960) document.getElementById("mm-stage").scrollIntoView({ behavior: "smooth", block: "center" });
  };
  document.querySelectorAll("#mm-rounds .round").forEach((li) => {
    li.addEventListener("click", () => pick(li));
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(li); } });
  });
  document.getElementById("mm-all").addEventListener("click", () => {
    state.mm.round = null;
    document.querySelectorAll("#mm-rounds .round").forEach((x) => x.classList.remove("selected"));
    document.getElementById("mm-all").hidden = true;
    drawMatchMap();
  });
}

const KEY_LABEL = { c: "C", q: "Q", e: "E", x: "X" };
const ABILITY_STATUS = { exact: ["good", "matches Riot"], close: ["", "close to Riot"], off: ["bad", "doesn't match Riot"], unknown: ["", "no Riot count"] };

function abilitiesPanelHtml(a) {
  const keys = ["c", "q", "e", "x"].filter((k) => a.check[k]);
  if (!keys.length) return "";
  const per = a.per_slot || {};
  const tile = (v, l) => `<div class="tile"><span class="label">${l}</span><span class="value">${v}</span></div>`;
  return `<section class="panel"><div class="section-head"><h2>Abilities</h2><span class="${ABILITY_STATUS[a.status][0]}">Timing ${ABILITY_STATUS[a.status][1]}</span></div>
    <div class="tiles">${tile(a.first_s != null ? mmss(a.first_s) : "–", "First ability, typical")}${tile(`${a.deaths_no_util} / ${a.deaths}`, "Deaths before any ability")}
      ${tile(a.buy_phase, "Cast in buy phase")}${tile(a.ult_rounds.length ? a.ult_rounds.map((n) => `R${n}`).join(" ") : "None", "Ultimate")}</div>
    <div class="table-wrap"><table><thead><tr><th>Ability</th><th>Riot casts</th><th>Timed</th><th>Check</th><th>Usually at</th><th>Rounds used</th></tr></thead><tbody>
    ${keys.map((k) => { const c = a.check[k], st = ABILITY_STATUS[c.status]; return `<tr><td><span class="kbd">${KEY_LABEL[k]}</span> ${esc(a.names[k] || "")}</td><td>${c.riot ?? "–"}</td><td>${c.timed}</td>
      <td><span class="${st[0]}">${st[1]}</span></td><td>${(per[k] || {}).median_t != null ? mmss(per[k].median_t) : "–"}</td><td>${(per[k] || {}).rounds ?? 0}</td></tr>`; }).join("")}</tbody></table></div>
    <p class="note">From your C, Q, E and X presses, lined up with the match and checked against Riot's count of casts. Times are after the barriers drop.${a.status === "off" ? " Some don't match Riot's count (rebound keys or cancelled casts?), so treat those times as rough." : ""}</p></section>`;
}

function roundsListHtml(rounds, opts = {}) {
  let prevSide = null;
  return rounds.map((r) => {
    const half = prevSide && r.side !== prevSide ? `<li class="half-sep">Sides switch</li>` : "";
    prevSide = r.side;
    const d = r.death;
    const kills = r.kills.map((k) => `${esc(k.weapon)} on ${esc(k.victim)} <span class="muted">${mmss(k.t)}${k.area ? `, ${esc(k.area)}` : ""}</span>`);
    const badges = [r.fk ? `<span class="badge good">First kill</span>` : "", r.fd ? `<span class="badge bad">First death</span>` : "",
      r.traded ? `<span class="badge">Traded</span>` : "", r.plant ? `<span class="badge">Spike ${esc(r.plant.site)} ${mmss(r.plant.t)}</span>` : ""].join("");
    const feed = (r.feed || []).map((f) => `<li><span class="${f.ks}">${esc(f.killer)}</span> <span class="muted">${esc(f.weapon)}</span> <span class="${f.vs}">${esc(f.victim)}</span> <span class="muted">${mmss(f.t)}</span></li>`).join("");
    return `${half}<li class="round ${r.won ? "won" : "lost"}" data-n="${r.n}" tabindex="0" role="button" aria-label="Round ${r.n + 1}">
      <div class="rhead"><span class="rnum">${r.n + 1}</span>
        <span class="res ${r.won ? "good" : "bad"}">${r.won ? "Won" : "Lost"}</span>
        <span class="muted">${esc(["ct", "t"].includes(r.side) ? r.side.toUpperCase() : r.side || "")}${r.end ? `, ${esc(r.end.toLowerCase())}` : ""}</span>
        <span class="buy">${esc(BUY[r.buy] || r.buy || "")}${r.enemy_buy ? ` vs ${esc((BUY[r.enemy_buy] || r.enemy_buy).toLowerCase())}` : ""}</span></div>
      <div class="rbody">
        <div>${r.weapon ? `${esc(r.weapon)}, ` : ""}${opts.cs2 ? `$${credits(r.loadout)} gear` : `${credits(r.loadout)} credits`}${kills.length ? `: ${kills.join("; ")}` : ", no kills"}</div>
        ${Array.isArray(r.abilities) && r.abilities.length ? `<div class="muted">Abilities: ${r.abilities.map((a) => `<span class="kbd">${KEY_LABEL[a.k] || "?"}</span> ${esc(((opts.abilities || {}).names || {})[a.k] || "")} ${a.t < 0 ? "in buy phase" : mmss(a.t)}`).join(", ")}</div>` : ""}
        ${(r.utility || []).length ? `<div class="muted">Utility: ${Object.entries((r.utility || []).reduce((a, u) => ({ ...a, [u.type]: (a[u.type] || 0) + 1 }), {})).map(([t, c]) => `${c} ${t === "he" ? "HE" : esc(t)}`).join(", ")}</div>` : ""}
        <div>${d ? `Died ${mmss(d.t)} to ${esc(d.by)} (${esc(d.weapon)})${d.area ? ` at ${esc(d.area)}` : ""}` : `<span class="good">Survived</span>`}</div>
        ${d && opts.reviews && opts.reviews[r.n] ? `<div class="rv"><b>Review:</b> ${esc(opts.reviews[r.n].verdict)} <span class="muted">Fix: ${esc(opts.reviews[r.n].fix)}</span>${opts.reviews[r.n].video ? `<br><span class="muted">Video: ${esc(opts.reviews[r.n].video)}</span>` : ""}</div>` : ""}
        ${d && opts.frames ? framesHtml(opts.frames[r.n], `Round ${r.n + 1}`) : ""}
        ${badges ? `<div class="badges">${badges}</div>` : ""}
        ${feed ? `<ol class="feed">${feed}</ol>` : ""}
      </div></li>`;
  }).join("");
}

function drawMatchMap() {
  const st = state.mm, m = st.m, el = document.getElementById("mm-stage");
  if (!el) return;
  const rounds = m.rounds.filter((r) => st.round == null || r.n === st.round);
  const D = SERIES[1], K = SERIES[0];
  let lines = "", marks = "", n = 0;
  const pts = [];
  const num = (x, y, label, fill, first) => (first
    ? `<rect x="${x - 21}" y="${y - 21}" width="42" height="42" transform="rotate(45 ${x} ${y})" fill="${fill}" class="pt"/>`
    : `<circle cx="${x}" cy="${y}" r="25" fill="${fill}" class="pt"/>`)
    + `<text x="${x}" y="${y + 9}" text-anchor="middle" class="ptn big">${label}</text>`;
  const showD = ["death", "both"].includes(st.kind), showK = ["kill", "both"].includes(st.kind), showU = st.kind === "utility";
  const U = SERIES[2], NADE = { smoke: "S", flash: "F", he: "H", molotov: "M" };
  for (const r of rounds) {
    const d = r.death;
    if (showU) {
      for (const u of r.utility || []) {
        if (u.x == null) continue;
        const x = u.x * 1000, y = u.y * 1000;
        marks += `<g data-p="${pts.length}"><rect x="${x - 16}" y="${y - 16}" width="32" height="32" fill="${U}" class="pt"/><text x="${x}" y="${y + 7}" text-anchor="middle" class="ptn">${NADE[u.type] || "?"}</text></g>`;
        pts.push(`<b>Round ${r.n + 1}: ${esc(u.type)}</b>landed at ${mmss(u.t)}${u.lower ? " (lower level)" : ""}`);
        n++;
      }
    }
    if (showD && d && d.x != null) {
      const x = d.x * 1000, y = d.y * 1000;
      if (d.kx != null) {
        lines += `<line x1="${d.kx * 1000}" y1="${d.ky * 1000}" x2="${x}" y2="${y}" class="kline" stroke="${D}"/>`
          + `<circle cx="${d.kx * 1000}" cy="${d.ky * 1000}" r="13" class="kpos" stroke="${D}"/>`;
      }
      marks += `<g data-p="${pts.length}">${num(x, y, r.n + 1, D, r.fd)}</g>`;
      pts.push(`<b>Round ${r.n + 1}: died ${mmss(d.t)}</b>${esc(d.by)} with ${esc(d.weapon)}${d.area ? `, ${esc(d.area)}` : ""}${r.fd ? ", first death" : ""}${r.traded ? ", traded" : ""}${d.kx != null ? "<br>Ring and line: where they shot from" : ""}`);
      n++;
    }
    if (showK) {
      for (const k of r.kills || []) {
        if (k.x == null) continue;
        const x = k.x * 1000, y = k.y * 1000;
        if (k.vx != null) {
          lines += `<line x1="${x}" y1="${y}" x2="${k.vx * 1000}" y2="${k.vy * 1000}" class="kline" stroke="${K}"/>`
            + `<circle cx="${k.vx * 1000}" cy="${k.vy * 1000}" r="13" class="kpos" stroke="${K}"/>`;
        }
        marks += `<g data-p="${pts.length}">${num(x, y, r.n + 1, K, false)}</g>`;
        pts.push(`<b>Round ${r.n + 1}: kill ${mmss(k.t)}</b>${esc(k.weapon)} on ${esc(k.victim)}${k.area ? `, from ${esc(k.area)}` : ""}${k.vx != null ? "<br>Ring and line: where they were" : ""}`);
        n++;
      }
    }
    if (st.round != null && r.plant && r.plant.x != null) {
      const x = r.plant.x * 1000, y = r.plant.y * 1000;
      marks += `<g data-p="${pts.length}"><rect x="${x - 12}" y="${y - 12}" width="24" height="24" rx="4" class="plant"/><text x="${x}" y="${y + 6}" text-anchor="middle" class="ptn dark">S</text></g>`;
      pts.push(`<b>Spike planted on ${esc(r.plant.site)}</b>at ${mmss(r.plant.t)}`);
    }
  }
  const legend = [
    showD ? `<span><i style="background:${D}"></i>Your death (round number)</span><span><i class="diamond" style="background:${D}"></i>First death of the round</span>` : "",
    showD && m.rounds.some((r) => r.death && r.death.kx != null) ? `<span><i class="ring" style="border-color:${D}"></i>Where your killer was</span>` : "",
    showK ? `<span><i style="background:${K}"></i>Your kill (where you stood)</span>` : "",
    showK && m.rounds.some((r) => (r.kills || []).some((k) => k.vx != null)) ? `<span><i class="ring" style="border-color:${K}"></i>Where your victim was</span>` : "",
    showU ? `<span><i style="background:${U}"></i>Your utility: S smoke, F flash, H HE, M molotov</span>` : "",
  ].join("");
  el.innerHTML = `<svg viewBox="0 0 1000 1000" role="img" aria-label="${esc(m.map)} minimap">
      ${safeUrl(m.map_icon) ? `<image href="${esc(m.map_icon)}" x="0" y="0" width="1000" height="1000"/>` : ""}${lines}${marks}</svg>
    <div class="legend">${legend}</div>`;
  document.getElementById("mm-caption").textContent = st.round == null ? `All ${m.rounds.length} rounds` : `Round ${st.round + 1}`;
  el.querySelectorAll("g[data-p]").forEach((g) => {
    const tip = (ev) => showTip(pts[Number(g.dataset.p)], ev.clientX, ev.clientY);
    g.addEventListener("pointermove", tip);
    g.addEventListener("pointerdown", tip);
    g.addEventListener("pointerleave", hideTip);
  });
  if (!n && st.round != null) el.insertAdjacentHTML("afterbegin", `<p class="muted" style="text-align:center">Nothing to plot for this round.</p>`);
}

function drawEconomy(el, rounds, seriesOverride) {
  const team = rounds.some((r) => r.team_loadout != null);
  const series = seriesOverride || (team ? [["team_loadout", "Your team", SERIES[0]], ["enemy_loadout", "Enemy team", SERIES[1]]] : [["loadout", "Your loadout", SERIES[0]]]);
  const W = Math.max(300, el.clientWidth), H = 200, L = 40, B = 30, T = 8;
  const max = Math.max(1000, ...rounds.flatMap((r) => series.map(([k]) => r[k] || 0)));
  const top = Math.ceil(max / 1000) * 1000;
  const y = (v) => T + (H - T - B) * (1 - v / top);
  const colW = (W - L) / rounds.length, gap = 2, barW = Math.min(14, (colW * 0.8 - gap) / series.length);
  let svg = `<g class="grid">`;
  for (let v = 0; v <= top; v += 1000) svg += `<line x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/>`;
  svg += `</g><g class="axis">`;
  for (let v = 0; v <= top; v += 1000) svg += `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v / 1000}k</text>`;
  rounds.forEach((r, i) => {
    if (colW < 18 && i % 2) return;
    svg += `<text x="${L + colW * i + colW / 2}" y="${H - 16}" text-anchor="middle">${r.n + 1}</text>`;
  });
  svg += `</g><g>`;
  rounds.forEach((r, i) => {
    const x0 = L + colW * i + (colW - (barW * series.length + gap * (series.length - 1))) / 2;
    series.forEach(([k, , c], j) => {
      const v = r[k] || 0, x = x0 + j * (barW + gap), yt = y(v), h = Math.max(1, y(0) - yt), rad = Math.min(3, h, barW / 2);
      svg += `<path fill="${c}" d="M${x},${y(0)} V${yt + rad} Q${x},${yt} ${x + rad},${yt} H${x + barW - rad} Q${x + barW},${yt} ${x + barW},${yt + rad} V${y(0)} Z"/>`;
    });
    const cx = L + colW * i + colW / 2;
    svg += r.won ? `<rect x="${cx - 4}" y="${H - 10}" width="8" height="8" rx="2" fill="var(--good)"/>`
      : `<rect x="${cx - 3.5}" y="${H - 9.5}" width="7" height="7" rx="2" fill="none" stroke="var(--muted)" stroke-width="1.5"/>`;
  });
  svg += `</g><g>`;
  rounds.forEach((r, i) => { svg += `<rect class="hit" data-i="${i}" x="${L + colW * i}" y="${T}" width="${colW}" height="${H - T}"/>`; });
  svg += `</g>`;
  el.innerHTML = `<div class="legend">${series.map(([, l, c]) => `<span><i style="background:${c}"></i>${l}</span>`).join("")}<span><i style="background:var(--good)"></i>Round won</span><span><i style="border:1.5px solid var(--muted)"></i>Round lost</span></div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Loadout value per round">${svg}</svg>`;
  el.querySelectorAll(".hit").forEach((h) => {
    const r = rounds[Number(h.dataset.i)];
    const tip = (ev) => showTip(`<b>Round ${r.n + 1}, ${r.won ? "won" : "lost"}</b>${series.map(([k, l, c]) => `<div class="row"><span><i style="background:${c}"></i>${l}</span><span>${credits(r[k])}</span></div>`).join("")}
      <div class="row"><span>Buys</span><span>${esc(BUY[r.buy] || "?")} vs ${esc(BUY[r.enemy_buy] || "?")}</span></div>
      <div class="row"><span>You</span><span>${esc(r.weapon || "no gun")}, ${credits(r.loadout)}</span></div>`, ev.clientX, ev.clientY);
    h.addEventListener("pointermove", tip);
    h.addEventListener("pointerdown", tip);
    h.addEventListener("pointerleave", hideTip);
  });
}

function scoreboardHtml(sb, rb, opts = {}) {
  const table = (rows, title) => `<div class="table-wrap"><h3>${title}</h3><table class="scoreboard"><thead><tr>
      <th>Player</th>${opts.cs2 ? "" : "<th>Rank</th>"}<th>K</th><th>D</th><th>A</th>${rb ? `${opts.cs2 ? "" : "<th>ACS</th>"}<th>ADR</th>` : ""}<th>HS%</th>${rb ? "<th>First kills</th>" : ""}${opts.cs2 ? "<th>MVPs</th>" : ""}</tr></thead>
    <tbody>${rows.map((p) => `<tr class="${p.me ? "me" : ""}">
      <td><span class="ic">${safeUrl(p.agent_icon) ? `<img src="${esc(p.agent_icon)}" alt="${esc(p.agent)}" title="${esc(p.agent)}">` : ""}${p.name && p.name !== "?#" ? esc(p.name) : `<span class="muted">Hidden name</span>`}${p.me ? ` <span class="badge">You</span>` : ""}${p.party ? ` <span class="badge">Party</span>` : ""}</span></td>
      ${opts.cs2 ? "" : `<td><span class="ic">${safeUrl(p.rank_icon) ? `<img src="${esc(p.rank_icon)}" alt="">` : ""}${esc(p.rank || "")}</span></td>`}
      <td>${p.kills}</td><td>${p.deaths}</td><td>${p.assists}</td>${rb ? `${opts.cs2 ? "" : `<td>${p.acs ?? ""}</td>`}<td>${p.adr ?? ""}</td>` : ""}
      <td>${p.hs_pct != null ? `${p.hs_pct}%` : "–"}</td>${rb ? `<td>${p.fk}</td>` : ""}${opts.cs2 ? `<td>${p.mvps ?? 0}</td>` : ""}</tr>`).join("")}</tbody></table></div>`;
  if (!rb) return table(sb, "All players");
  return table(sb.filter((p) => p.team === "ally"), "Your team") + table(sb.filter((p) => p.team === "enemy"), "Enemy team");
}

// ---------------- settings ----------------

// ---------------- Improvement loop ----------------

const improveHref = (gid) => `#/improve/${encodeURIComponent(gid)}`;
const pct = (v) => (v == null ? "–" : `${Math.round(v)}%`);

// Video frames from the seconds before a death: a strip of thumbnails that opens a larger view.
function framesHtml(fr, label) {
  if (!fr || !(fr.urls || []).length) return "";
  const before = (i) => (fr.before[i] != null ? `${fr.before[i]} s before` : "");
  return `<div class="frames" data-frames="${esc(JSON.stringify(fr))}" data-label="${esc(label)}" role="group" aria-label="${esc(label)}: video frames before the death">
    ${fr.urls.map((u, i) => `<button type="button" data-i="${i}" aria-label="Open frame ${i + 1}, ${esc(before(i))}"><img src="${esc(u)}" alt="" loading="lazy">${fr.before[i] != null ? `<span>-${esc(fr.before[i])}s</span>` : ""}</button>`).join("")}</div>`;
}

const frameBox = { fr: null, i: 0, label: "" };
function showFrame(i) {
  const box = document.getElementById("frame-box"), fr = frameBox.fr;
  frameBox.i = Math.max(0, Math.min(fr.urls.length - 1, i));
  box.querySelector("img").src = fr.urls[frameBox.i];
  box.querySelector("img").alt = `${frameBox.label}, frame ${frameBox.i + 1}`;
  box.querySelector(".fbx-cap").textContent = `${frameBox.label} · ${fr.before[frameBox.i] != null ? `${fr.before[frameBox.i]} s before the death` : ""} · ${frameBox.i + 1}/${fr.urls.length}`;
  box.querySelector("[data-step='-1']").disabled = frameBox.i === 0;
  box.querySelector("[data-step='1']").disabled = frameBox.i === fr.urls.length - 1;
}
function openFrames(fr, i, label) {
  let box = document.getElementById("frame-box");
  if (!box) {
    box = document.createElement("dialog");
    box.id = "frame-box";
    box.className = "frame-box";
    box.innerHTML = `<img alt=""><div class="fbx-bar"><span class="fbx-cap"></span><div>
      <button class="btn" type="button" data-step="-1">Earlier</button><button class="btn" type="button" data-step="1">Later</button>
      <button class="btn" type="button" data-close>Close</button></div></div>`;
    document.body.appendChild(box);
    box.addEventListener("click", (e) => {
      if (e.target === box || e.target.closest("[data-close]")) return box.close();
      const step = e.target.closest("[data-step]");
      if (step) showFrame(frameBox.i + Number(step.dataset.step));
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") { e.preventDefault(); showFrame(frameBox.i - 1); }
      if (e.key === "ArrowRight") { e.preventDefault(); showFrame(frameBox.i + 1); }
    });
  }
  Object.assign(frameBox, { fr, label });
  showFrame(i);
  if (!box.open) box.showModal();
}
// Capture phase, so a frame inside a clickable round opens the frame instead of selecting the round.
document.addEventListener("click", (e) => {
  const b = e.target.closest(".frames button");
  if (!b) return;
  e.stopPropagation();
  const strip = b.closest(".frames");
  openFrames(JSON.parse(strip.dataset.frames), Number(b.dataset.i), strip.dataset.label);
}, true);
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.closest?.(".frames button")) e.stopPropagation();
}, true);

function factChips(f) {
  const out = [];
  if (f.facing != null) out.push([f.facing > 60 ? "bad" : "", `facing ${f.facing}° off your killer`]);
  if (f.ally_m != null) out.push([f.ally_m > 15 ? "bad" : "", `nearest teammate ${f.ally_m} m`]);
  if (f.allies_alive != null) out.push(["", `${f.allies_alive + 1}v${f.enemies_alive ?? "?"}`]);
  if (f.killer_m != null) out.push(["", `killer ${f.killer_m} m away`]);
  if (f.shot_2s === false) out.push(["bad", "didn't fire"]);
  if (f.moving_last_shot === true) out.push(["bad", "last shot moving"]);
  if (f.moving_last_shot === false) out.push(["", "last shot stopped"]);
  if (f.after_kill_s != null && f.after_kill_s <= 5) out.push(["bad", `${f.after_kill_s}s after your kill`]);
  if (f.util_before === 0) out.push(["bad", "no ability used yet"]);
  else if (f.util_before != null) out.push(["", `${f.util_before} ${f.util_before === 1 ? "ability" : "abilities"} used${f.last_util_s != null ? `, last ${f.last_util_s}s before` : ""}`]);
  return out.map(([c, t]) => `<span class="fchip ${c}">${esc(t)}</span>`).join("");
}

function improvePanelHtml(gid, s) {
  if (!s) return "";
  const body = s.focus.length
    ? `<ul class="focus-mini">${s.focus.map((f) => `<li><span>${esc(f.label)}</span><b>${pct(f.recent)}</b><span class="muted">target ${pct(f.target)}</span></li>`).join("")}</ul>`
    : s.matches
      ? `<p class="sub">${s.matches} match${s.matches === 1 ? "" : "es"} analysed. Focus habits are picked once there's enough data, or choose one yourself.</p>`
      : `<p class="sub">Play a match with the tracker running. Once it syncs, each death gets reviewed and habit stats start building.</p>`;
  return `<div class="panel"><div class="section-head"><h2>Improve</h2><a class="linkish" style="margin:0" href="${improveHref(gid)}">Open</a></div>
    ${s.running ? `<div class="sync-line"><span class="spinner" aria-hidden="true"></span> Reviewing deaths…</div>` : ""}${body}</div>`;
}

async function viewImprove(gid) {
  state.view = "improve";
  $app.innerHTML = `<p class="empty">Loading…</p>`;
  let s, g;
  try { [s, g] = await Promise.all([api(`/api/games/${encodeURIComponent(gid)}/improve`), api(`/api/games/${encodeURIComponent(gid)}`)]); }
  catch (e) { $app.innerHTML = `<p class="empty">${esc(e.message)}. <a href="${gameHref(gid)}">Back to the game</a></p>`; return; }
  if (state.view !== "improve") return;
  const focusIds = new Set(s.focus.map((f) => f.habit));
  const tags = [...new Set(s.reviews.flatMap((r) => r.tags))];
  const label = Object.fromEntries(s.catalog.map((h) => [h.id, h.label]));
  const tagName = (t) => label[t] || t.replace(/_/g, " ");
  let filter = "";

  $app.innerHTML = `<section class="banner" id="banner">
      <div class="sub"><a href="${gameHref(gid)}">${esc(g.game.name)}</a>, improvement</div>
      <h1>Improve</h1>
      <div class="facts">
        <div><b>${s.matches}</b><span>Matches analysed</span></div>
        <div><b>${s.inputs}</b><span>With shooting data</span></div>
        <div><b>${s.reviews.length}</b><span>Deaths reviewed</span></div>
        ${s.video ? `<div><b>${s.video}</b><span>With video</span></div>` : ""}
        <div><b>${s.done.length}</b><span>Habits fixed</span></div>
      </div>
      <div class="actions"><button class="btn primary" id="imp-review" ${s.running || !s.ready ? "disabled" : ""}>Review new matches</button></div>
    </section>
    <div id="imp-status">${s.running ? `<div class="sync-line"><span class="spinner" aria-hidden="true"></span> Reviewing deaths…</div>` : ""}${s.ready ? "" : `<p class="note">Add your Claude API key in <a href="#/settings">Settings</a> for death reviews.</p>`}</div>
    <section class="panel"><div class="section-head"><h2>Focus</h2><span>${s.focus.length ? "Working on these now" : ""}</span></div>
      ${s.focus.length ? `<div class="focus-grid">${s.focus.map((f) => `<article class="focus-card">
          <h3>${esc(f.label)}</h3>
          <p class="muted">${esc(f.means)}</p>
          <div class="focus-nums"><div><b>${pct(f.recent)}</b><span>Last 5 matches</span></div><div><b>${pct(f.baseline)}</b><span>When you started</span></div><div><b>${pct(f.target)}</b><span>Target</span></div></div>
          ${f.trend.length >= 2 ? `<div class="timeline" data-timeline='${esc(JSON.stringify({ title: "Each match", line: true, target: f.target, series: [{ name: f.short, group: f.habit, points: f.trend }], ticks: [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100].map((v) => ({ v, label: `${v}%` })) }))}'></div>` : ""}
          <p class="focus-fix"><b>Next match:</b> ${esc(f.fix)}</p>
          <button class="linkish" data-drop="${esc(f.habit)}">Stop focusing on this</button>
        </article>`).join("")}</div>`
        : `<p class="sub">${s.matches ? "Nothing stands out yet, or there isn't enough data. Pick a habit below to work on." : "Focus habits appear once a few matches are analysed."}</p>`}
      ${s.done.length ? `<p class="sub">Fixed: ${s.done.map((d) => esc(d.label)).join(", ")}</p>` : ""}
    </section>
    ${s.habits.length ? `<section class="panel"><div class="section-head"><h2>All habits</h2><span>Share of deaths or fights</span></div>
      <div class="table-wrap" style="margin-top:0"><table><thead><tr><th>Habit</th><th>Last 5</th><th>Before</th><th>Target</th><th>Matches</th><th></th></tr></thead><tbody>
      ${s.habits.map((h) => `<tr><td class="hab"><b>${esc(h.label)}</b><span>${esc(h.means)}</span></td>
        <td><span class="${h.recent != null && h.recent > h.target ? "bad" : "good"}">${pct(h.recent)}</span></td><td>${pct(h.previous)}</td><td>${pct(h.target)}</td><td>${h.matches}</td>
        <td>${focusIds.has(h.id) ? `<span class="muted">Focus</span>` : `<button class="btn" data-focus="${esc(h.id)}">Focus</button>`}</td></tr>`).join("")}
      </tbody></table></div></section>` : ""}
    <section class="panel"><div class="section-head"><h2>Death reviews</h2><span>${s.reviews.length ? "Newest first" : ""}</span></div>
      ${tags.length ? `<div class="chips" role="group" aria-label="Filter by habit">${[["", "All"], ...tags.map((t) => [t, tagName(t)])].map(([t, l]) => `<button class="chip" type="button" data-tag="${esc(t)}" aria-pressed="${t === ""}">${esc(l)}</button>`).join("")}</div>` : ""}
      <div id="imp-reviews" class="review-list"></div>
    </section>`;
  setBg(document.getElementById("banner"), art(g.game).hero);
  drawTimelines();

  const renderReviews = () => {
    const list = s.reviews.filter((r) => !filter || r.tags.includes(filter));
    document.getElementById("imp-reviews").innerHTML = list.length ? list.map((r) => `<article class="review">
        <div class="review-head"><a href="#/match/${encodeURIComponent(r.match_id)}">${esc(r.map || "Match")}, round ${r.round + 1}</a>
          <span class="muted">${r.start ? ago(r.start) : ""}${r.side ? `, ${esc(["ct", "t"].includes(r.side) ? r.side.toUpperCase() : r.side)}` : ""}, ${r.won ? "round won" : "round lost"}</span></div>
        <p class="review-verdict">${esc(r.verdict)}</p>
        <p class="review-fix"><b>Instead:</b> ${esc(r.fix)}</p>
        ${r.video ? `<p class="review-video"><b>In the video:</b> ${esc(r.video)}</p>` : ""}
        ${framesHtml(r.frames, `${r.map || "Match"}, round ${r.round + 1}`)}
        <div class="review-facts"><span class="fchip">died ${mmss(r.death.t)} to ${esc(r.death.by)}, ${esc(r.death.weapon)}</span>${factChips(r.facts)}</div>
        <div class="review-foot"><span>${r.tags.map((t) => `<span class="rtag ${focusIds.has(t) ? "focus" : ""}">${esc(tagName(t))}</span>`).join("")}</span>
          <span class="fb" role="group" aria-label="Was this fair?"><button class="linkish" data-fb="1" data-m="${esc(r.match_id)}" data-r="${r.round}" aria-pressed="${r.feedback === 1}">Fair</button><button class="linkish" data-fb="-1" data-m="${esc(r.match_id)}" data-r="${r.round}" aria-pressed="${r.feedback === -1}">Not fair</button></span></div>
      </article>`).join("") : `<p class="muted" style="margin:0">${s.reviews.length ? "No deaths with that habit." : "No reviews yet. They appear after your next VALORANT or CS2 match syncs."}</p>`;
    document.querySelectorAll("[data-fb]").forEach((b) => b.addEventListener("click", async () => {
      const r = s.reviews.find((x) => x.match_id === b.dataset.m && String(x.round) === b.dataset.r);
      const value = r.feedback === Number(b.dataset.fb) ? 0 : Number(b.dataset.fb);
      try { await api(`/api/games/${encodeURIComponent(gid)}/improve/feedback`, { method: "POST", body: { match_id: r.match_id, round: r.round, value } }); }
      catch (e) { toast(e.message); return; }
      r.feedback = value || null;
      renderReviews();
    }));
  };
  renderReviews();

  document.querySelectorAll("[data-tag]").forEach((c) => c.addEventListener("click", () => {
    filter = c.dataset.tag;
    document.querySelectorAll("[data-tag]").forEach((x) => x.setAttribute("aria-pressed", String(x === c)));
    renderReviews();
  }));
  const setFocus = async (habit, drop) => {
    try { await api(`/api/games/${encodeURIComponent(gid)}/improve/focus`, { method: "POST", body: { habit, drop } }); viewImprove(gid); }
    catch (e) { toast(e.message); }
  };
  document.querySelectorAll("[data-focus]").forEach((b) => b.addEventListener("click", () => setFocus(b.dataset.focus, false)));
  document.querySelectorAll("[data-drop]").forEach((b) => b.addEventListener("click", () => setFocus(b.dataset.drop, true)));
  const follow = async () => {
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 4000));
      const fresh = await api(`/api/games/${encodeURIComponent(gid)}/improve`).catch(() => null);
      if (fresh && !fresh.running) { if (state.view === "improve" && location.hash === improveHref(gid)) viewImprove(gid); return; }
    }
  };
  if (s.running) follow();
  document.getElementById("imp-review").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api(`/api/games/${encodeURIComponent(gid)}/improve/review`, { method: "POST" }); }
    catch (err) { toast(err.message); e.target.disabled = false; return; }
    document.getElementById("imp-status").innerHTML = `<div class="sync-line"><span class="spinner" aria-hidden="true"></span> Reviewing deaths…</div>`;
    follow();
  });
}

// ---------------- Guides knowledge base ----------------

const guidesHref = (gid) => `#/guides/${encodeURIComponent(gid)}`;
const ytLink = (vid, t) => `https://youtu.be/${encodeURIComponent(vid)}${t ? `?t=${Math.floor(t)}` : ""}`;

function kbStatus(k) {
  if (k.running) return `<div class="sync-line"><span class="spinner" aria-hidden="true"></span> Finding and reading guides. This takes a minute or two.</div>`;
  const r = k.run;
  if (!r || !r.ts) return "";
  return `<div class="sync-line ${r.ok ? "" : "err"}">${r.ok ? `Updated ${ago(r.ts)}: ` : `Last update ${ago(r.ts)} failed: `}${esc(r.msg)}</div>`;
}

function kbSetupHint(k) {
  if (!k.ready) return `Add your Claude API key in <a href="#/settings">Settings</a> to build guide notes.`;
  if (!k.search) return `Add a YouTube API key in <a href="#/settings">Settings</a> and the tracker will find guides for this game every week. You can also paste a video link on the knowledge base page.`;
  return "";
}

function guidesPanelHtml(gid, k) {
  if (!k) return "";
  const hint = kbSetupHint(k);
  return `<div class="panel">
    <div class="section-head"><h2>Guides</h2><a class="linkish" style="margin:0" href="${guidesHref(gid)}">Open knowledge base</a></div>
    ${kbStatus(k)}
    ${k.notes ? `<p class="sub">${k.notes} notes on ${k.topics} topics from ${k.videos} guide${k.videos === 1 ? "" : "s"}. Latest:</p>
      <ul class="kb-recent">${k.recent.map((n) => `<li><a href="${guidesHref(gid)}"><span class="kb-topic-tag">${esc(n.topic)}</span>${esc(n.title)}</a></li>`).join("")}</ul>`
      : `<p class="sub">No guide notes yet.${hint ? "" : " Find some now, or they'll come with the weekly update."}</p>`}
    ${hint ? `<p class="note">${hint}</p>` : ""}
    ${k.ready && k.search ? `<button class="btn" type="button" data-kb-refresh ${k.running ? "disabled" : ""}>Find new guides</button>` : ""}
  </div>`;
}

async function startGuides(gid, urls) {
  await api(`/api/games/${encodeURIComponent(gid)}/kb/refresh`, { method: "POST", body: urls ? { urls } : {} });
}

async function waitForGuides(gid, onUpdate) {  // poll until the run finishes, then hand back the fresh data
  for (let i = 0; i < 150; i++) {
    await new Promise((r) => setTimeout(r, 4000));
    const k = await api(`/api/games/${encodeURIComponent(gid)}/kb`).catch(() => null);
    if (!k) continue;
    if (!k.summary.running) { onUpdate(k); return k; }
  }
  return null;
}

function wireGuidesPanel(gid) {
  const box = document.getElementById("guides-panel");
  const btn = box?.querySelector("[data-kb-refresh]");
  const follow = () => waitForGuides(gid, (k) => {
    if (state.view === "game" && document.getElementById("guides-panel") === box) { box.innerHTML = guidesPanelHtml(gid, k.summary); wireGuidesPanel(gid); }
  });
  if (btn?.disabled) follow();
  btn?.addEventListener("click", async () => {
    btn.disabled = true;
    try { await startGuides(gid); } catch (e) { toast(e.message); btn.disabled = false; return; }
    const k = await api(`/api/games/${encodeURIComponent(gid)}/kb`);
    box.innerHTML = guidesPanelHtml(gid, k.summary);
    wireGuidesPanel(gid);
  });
}

const kbState = { q: "", topic: "" };

async function viewGuides(gid) {
  state.view = "guides";
  $app.innerHTML = `<p class="empty">Loading…</p>`;
  let k;
  try { k = await api(`/api/games/${encodeURIComponent(gid)}/kb`); }
  catch (e) { $app.innerHTML = `<p class="empty">${esc(e.message)}. <a href="#/library">Back to the library</a></p>`; return; }
  if (state.view !== "guides") return;
  const s = k.summary, vids = Object.fromEntries(k.videos.map((v) => [v.video_id, v]));
  const topics = Object.entries(k.notes.reduce((a, n) => ({ ...a, [n.topic]: (a[n.topic] || 0) + 1 }), {})).sort((a, b) => b[1] - a[1]);
  if (kbState.gid !== gid) Object.assign(kbState, { gid, q: "", topic: "" });
  const hint = kbSetupHint(s);
  const used = k.videos.filter((v) => v.status === "used").length;

  $app.innerHTML = `<section class="banner" id="banner">
      <div class="sub"><a href="${gameHref(gid)}">${esc(k.game.name)}</a>, guides knowledge base</div>
      <h1>Guides</h1>
      <div class="facts">
        <div><b>${k.notes.length}</b><span>Notes</span></div>
        <div><b>${topics.length}</b><span>Topics</span></div>
        <div><b>${used}</b><span>Guides read</span></div>
        <div><b>${k.videos.length - used}</b><span>Skipped</span></div>
      </div>
      <div class="actions">
        ${s.ready && s.search ? `<button class="btn primary" id="kb-refresh" ${s.running ? "disabled" : ""}>Find new guides</button>` : ""}
        <form id="kb-add" class="kb-add"><input type="text" id="kb-url" placeholder="Paste a YouTube guide link" aria-label="YouTube link to add">
          <button class="btn" type="submit" ${s.ready ? "" : "disabled"}>Add guide</button></form>
      </div>
    </section>
    <div id="kb-status">${kbStatus(s)}${hint ? `<p class="note">${hint}</p>` : ""}</div>
    ${k.notes.length ? `<div class="kb-controls">
      <input type="search" id="kb-q" placeholder="Search notes" aria-label="Search notes" value="${esc(kbState.q)}">
      <div class="chips" role="group" aria-label="Topic">${[["", `All <span class="muted">${k.notes.length}</span>`], ...topics.map(([t, n]) => [t, `${esc(t)} <span class="muted">${n}</span>`])]
        .map(([t, l]) => `<button class="chip" type="button" data-topic="${esc(t)}" aria-pressed="${t === kbState.topic}">${l}</button>`).join("")}</div>
    </div>` : ""}
    <div id="kb-notes" class="stack"></div>
    ${k.videos.length ? `<section class="panel"><div class="section-head"><h2>Guides looked at</h2></div>
      <div class="table-wrap" style="margin-top:0"><table><thead><tr><th>When</th><th>Video</th><th>Channel</th><th>Length</th><th>Result</th></tr></thead><tbody>
      ${k.videos.map((v) => {
        const n = k.notes.filter((x) => x.video_id === v.video_id).length;
        return `<tr><td>${ago(v.ts)}</td><td class="kb-vid"><a href="${ytLink(v.video_id)}" target="_blank" rel="noopener noreferrer">${esc(v.title || v.video_id)}</a></td>
          <td>${esc(v.channel || "")}</td><td>${v.duration ? mmss(v.duration) : "–"}</td>
          <td class="kb-why"><span class="${v.status === "used" ? "good" : "muted"}">${v.status === "used" ? `${n} note${n === 1 ? "" : "s"}` : "Skipped"}</span>${v.reason ? `: ${esc(v.reason)}` : ""}</td></tr>`;
      }).join("")}</tbody></table></div></section>` : ""}`;
  setBg(document.getElementById("banner"), safeUrl(k.game.hero));

  const hostOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; } };
  const noteHtml = (n) => {
    const v = vids[n.video_id] || {};
    const src = n.video_id
      ? `<a href="${ytLink(n.video_id, n.t)}" target="_blank" rel="noopener noreferrer">Watch at ${mmss(n.t)}</a>
        <span title="${esc(v.title || "")}">${esc(v.channel || "")}${v.title ? `, ${esc(v.title)}` : ""}</span>`
      : `<span>Saved by your coach${safeUrl(n.source) ? `, from <a href="${esc(n.source)}" target="_blank" rel="noopener noreferrer">${esc(hostOf(n.source))}</a>` : ""}</span>`;
    return `<article class="kb-note"><h3>${esc(n.title)}</h3>
      <div class="answer">${md(n.points.map((p) => `- ${p}`).join("\n"))}</div>
      <div class="kb-src">${src}
        <button class="linkish" type="button" data-del="${n.id}">Remove</button></div></article>`;
  };
  const renderNotes = () => {
    const q = kbState.q.trim().toLowerCase();
    const shown = k.notes.filter((n) => (!kbState.topic || n.topic === kbState.topic)
      && (!q || `${n.topic} ${n.title} ${n.points.join(" ")}`.toLowerCase().includes(q)));
    const groups = {};
    shown.forEach((n) => (groups[n.topic] = groups[n.topic] || []).push(n));
    document.getElementById("kb-notes").innerHTML = shown.length
      ? Object.entries(groups).sort((a, b) => b[1].length - a[1].length).map(([t, ns]) => `<section class="panel kb-group">
          <div class="section-head"><h2>${esc(t)}</h2><span>${ns.length} note${ns.length === 1 ? "" : "s"}</span></div>
          <div class="kb-grid">${ns.map(noteHtml).join("")}</div></section>`).join("")
      : `<div class="panel"><p class="muted" style="margin:0">${k.notes.length ? "No notes match." : "No notes yet. Find guides above, or paste a link to a guide you like."}</p></div>`;
    document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/games/${encodeURIComponent(gid)}/kb/notes/${b.dataset.del}`, { method: "DELETE" }); }
      catch (e) { toast(e.message); return; }
      k.notes = k.notes.filter((n) => String(n.id) !== b.dataset.del);
      renderNotes();
    }));
  };
  renderNotes();

  document.getElementById("kb-q")?.addEventListener("input", (e) => { kbState.q = e.target.value; renderNotes(); });
  document.querySelectorAll("[data-topic]").forEach((c) => c.addEventListener("click", () => {
    kbState.topic = c.dataset.topic;
    document.querySelectorAll("[data-topic]").forEach((x) => x.setAttribute("aria-pressed", String(x === c)));
    renderNotes();
  }));
  const follow = () => {
    document.getElementById("kb-status").innerHTML = kbStatus({ ...s, running: true });
    waitForGuides(gid, () => { if (state.view === "guides" && location.hash === guidesHref(gid)) viewGuides(gid); });
  };
  if (s.running) follow();
  document.getElementById("kb-refresh")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await startGuides(gid); follow(); } catch (err) { toast(err.message); e.target.disabled = false; }
  });
  document.getElementById("kb-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = document.getElementById("kb-url").value.trim();
    if (!url) return;
    try { await startGuides(gid, [url]); document.getElementById("kb-url").value = ""; follow(); }
    catch (err) { toast(err.message); }
  });
}

// ---------------- Coach chat ----------------

const chat = { list: [], games: [], pending: {}, drafts: {}, spoiler: null, viewKey: null, timer: null };
const chatKey = (gid, gen) => `${gid}#${gen}`;
const chatHref = (gid, gen) => `#/chat/${encodeURIComponent(gid)}/${gen}`;
const CHAT_STARTERS = {
  all: [["next", "What should I play next?"], ["week", "Recap my week in games"]],
  competitive: [["improve", "How can I improve?"], ["session", "Recap my recent sessions"]],
  story: [["story", "Where am I, and what's next?"], ["session", "Recap where I left off"]],
};

// A match's conversation is named after the match until you rename it.
function matchTitle(m) {
  const day = m.start ? new Date(m.start * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
  return [[m.map, m.score, m.result].filter(Boolean).join(" "), day].filter(Boolean).join(", ");
}
const titled = (c) => (c.match && !c.renamed ? { ...c, title: matchTitle(c.match) } : c);

function chatGroup(ts) {
  const now = new Date(), day = 86400;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (ts >= today) return "Today";
  if (ts >= today - day) return "Yesterday";
  if (ts >= today - 7 * day) return "Previous 7 days";
  if (ts >= today - 30 * day) return "Previous 30 days";
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

const chatThumb = (c) => (safeUrl(c.cover) ? `<img src="${esc(c.cover)}" alt="" loading="lazy">`
  : `<span class="ph">${c.game_id === "*" ? "All" : esc((c.game_name || "?").slice(0, 1))}</span>`);

function fitChat() {  // the chat fills the window below the top bar; the top bar wraps on phones
  const bar = document.querySelector(".topbar");
  if (bar) document.documentElement.style.setProperty("--topbar-h", `${bar.offsetHeight}px`);
}

function chatTick() {  // elapsed time on "thinking" lines
  clearInterval(chat.timer);
  chat.timer = setInterval(() => {
    const els = document.querySelectorAll("[data-since]");
    if (!els.length && !Object.values(chat.pending).some((p) => !p.error)) { clearInterval(chat.timer); return; }
    els.forEach((el) => { el.textContent = mmss((Date.now() - Number(el.dataset.since)) / 1000); });
  }, 1000);
}

async function viewChat(arg) {
  state.view = "chat";
  const nav = state.nav = (state.nav || 0) + 1;
  const parts = arg.split("/").filter(Boolean);
  const isNew = !parts.length || parts[0] === "new";
  let scope = isNew ? (parts[1] ? decodeURIComponent(parts[1]) : "*") : decodeURIComponent(parts[0]);
  const gen = isNew ? null : Number(parts[1]);
  const keyNow = () => (isNew ? `new:${scope}` : chatKey(scope, gen));
  chat.viewKey = keyNow();
  if (!chat.spoiler) {
    if (!state.settings) state.settings = await api("/api/settings").catch(() => ({}));
    chat.spoiler = state.settings.spoiler_level || "light";
  }
  const [lst, thread] = await Promise.all([
    api("/api/coach/conversations"),
    isNew ? Promise.resolve({ turns: [] }) : api(`/api/coach/conversations/${encodeURIComponent(scope)}/${gen}`),
  ]);
  if (state.view !== "chat" || chat.viewKey !== keyNow() || state.nav !== nav) return;  // navigated away while loading
  chat.list = lst.conversations.map(titled);
  chat.games = lst.games;
  const conv = isNew ? null : chat.list.find((c) => c.game_id === scope && c.gen === gen);
  if (!isNew && !conv && !chat.pending[keyNow()]) { location.replace("#/chat"); return; }
  const gameOf = (id) => chat.games.find((g) => g.id === id);
  const nameOf = (id) => (id === "*" ? "All games" : gameOf(id)?.name || conv?.game_name || id);
  const turns = thread.turns;
  const spoilers = [["none", "None"], ["light", "Light"], ["full", "OK"]];

  $app.innerHTML = `<div class="chat" id="chat">
    <aside class="chat-side" aria-label="Chats">
      <div class="chat-side-top">
        <a class="btn primary chat-new" href="#/chat/new">New chat</a>
        <input type="search" id="chat-q" placeholder="Search chats" aria-label="Search chats">
      </div>
      <nav class="chat-list" id="chat-list"></nav>
    </aside>
    <section class="chat-main" id="chat-main">
      <header class="chat-head">
        <button class="btn chat-toggle" id="chat-toggle" type="button" aria-expanded="false" aria-controls="chat-list">Chats</button>
        <div class="chat-title">
          <h2 id="chat-title">${esc(conv ? conv.title : "New chat")}</h2>
          <span class="sub">${scope === "*" || isNew ? `<span id="chat-scope-name">${esc(nameOf(scope))}</span>` : `<a href="${gameHref(scope)}">${esc(nameOf(scope))}</a>`}${conv && conv.turns ? `, ${conv.turns} ${conv.turns === 1 ? "question" : "questions"}` : ""}</span>
        </div>
        ${conv ? `<div class="chat-actions">${conv.match ? `<a class="linkish" href="#/match/${encodeURIComponent(conv.match.id)}">Open match</a>` : ""}<button class="linkish" id="chat-rename" type="button">Rename</button><button class="linkish" id="chat-delete" type="button">Delete</button></div>` : ""}
      </header>
      <div class="chat-scroll" id="chat-scroll"><div class="chat-thread" id="chat-thread"></div></div>
      <form class="chat-compose" id="chat-form">
        <div class="chat-scope">
          ${isNew ? `<label>About<select id="chat-game">${[{ id: "*", name: "All games" }, ...chat.games].map((g) => `<option value="${esc(g.id)}"${g.id === scope ? " selected" : ""}>${esc(g.name)}</option>`).join("")}</select></label>`
            : `<span>About <b>${esc(nameOf(scope))}</b></span>`}
          <label>Spoilers<select id="chat-spoiler">${spoilers.map(([v, l]) => `<option value="${v}"${v === chat.spoiler ? " selected" : ""}>${l}</option>`).join("")}</select></label>
        </div>
        <div class="chat-input">
          <textarea id="chat-text" rows="1" placeholder="Message the coach" aria-label="Message the coach"></textarea>
          <button class="btn primary" id="chat-send" type="submit">Send</button>
        </div>
        <p class="chat-hint">Enter to send, Shift+Enter for a new line. Answers take a minute or two while the coach reads your data.</p>
      </form>
    </section>
  </div>`;
  fitChat();

  const $ = (id) => document.getElementById(id);
  const text = $("chat-text"), scroller = $("chat-scroll");
  const grow = () => { text.style.height = "auto"; text.style.height = `${Math.min(220, text.scrollHeight + 2)}px`; };
  // an answer this page asked for (chat.pending), or one asked elsewhere, like a match page, that's still being written
  const remote = () => {
    const c = !isNew && !chat.pending[keyNow()] && chat.list.find((x) => x.game_id === scope && x.gen === gen);
    return c && c.writing ? { q: c.writing.question, t0: c.writing.since * 1000 } : null;
  };
  const busy = () => (chat.pending[keyNow()] && !chat.pending[keyNow()].error) || remote();
  const syncSend = () => { $("chat-send").disabled = !!busy() || !text.value.trim(); };
  text.value = chat.drafts[keyNow()] || "";
  grow();

  const renderList = () => {
    const q = ($("chat-q").value || "").trim().toLowerCase();
    const items = chat.list.filter((c) => !q || c.title.toLowerCase().includes(q) || c.game_name.toLowerCase().includes(q));
    const fresh = Object.entries(chat.pending).filter(([k, p]) => k.startsWith("new:") && !p.error);
    let html = q ? "" : fresh.map(([k, p]) => `<a class="chat-item${isNew && k === keyNow() ? " active" : ""}" href="#/chat/new/${encodeURIComponent(k.slice(4))}">
        ${chatThumb({ game_id: k.slice(4), game_name: nameOf(k.slice(4)), cover: gameOf(k.slice(4))?.cover })}
        <span class="txt"><span class="t">${esc(p.q)}</span><span class="g">${esc(nameOf(k.slice(4)))}, thinking…</span></span></a>`).join("");
    let last = null;
    for (const c of items) {
      const g = chatGroup(c.last_ts);
      if (g !== last) { html += `<h4>${esc(g)}</h4>`; last = g; }
      const active = !isNew && c.game_id === scope && c.gen === gen;
      const p = chat.pending[chatKey(c.game_id, c.gen)];
      html += `<a class="chat-item${active ? " active" : ""}" href="${chatHref(c.game_id, c.gen)}"${active ? ' aria-current="page"' : ""}>
        ${chatThumb(c)}<span class="txt"><span class="t">${esc(c.title)}</span><span class="g">${esc(c.game_name)}${(p && !p.error) || c.writing ? ", thinking…" : ""}</span></span></a>`;
    }
    $("chat-list").innerHTML = html || `<p class="muted chat-none">${q ? "No chats match that." : "No chats yet. Ask the coach something to start one."}</p>`;
  };

  const msgHtml = (t, i) => `<div class="msg user"><div class="bubble">${esc(t.question)}</div></div>
    <div class="msg bot"><div class="answer">${md(t.content)}</div>
      <div class="via"><span title="${esc(new Date(t.ts * 1000).toLocaleString())}">${ago(t.ts)}</span>${t.model ? `<span>${esc(t.model)}</span>` : ""}<button class="linkish" type="button" data-copy="${i}">Copy</button></div></div>`;
  const renderThread = () => {
    const p = chat.pending[keyNow()] || remote();
    if (!turns.length && !p) {
      const kind = scope === "*" ? "all" : gameOf(scope)?.competitive ? "competitive" : "story";
      $("chat-thread").innerHTML = `<div class="chat-empty">
        <h3>${scope === "*" ? "Ask about any of your games" : `Ask about ${esc(nameOf(scope))}`}</h3>
        <p>The coach reads everything the tracker knows${scope === "*" ? " across your library" : " about this game"}, and remembers what it learns about you between chats.</p>
        <div class="chat-starters">${CHAT_STARTERS[kind].map(([k, l]) => `<button class="btn" type="button" data-starter="${k}">${esc(l)}</button>`).join("")}</div></div>`;
      $("chat-thread").querySelectorAll("[data-starter]").forEach((b) => b.addEventListener("click", () => send("", b.dataset.starter, b.textContent)));
      return;
    }
    $("chat-thread").innerHTML = turns.map(msgHtml).join("") + (p ? `<div class="msg user"><div class="bubble">${esc(p.q)}</div></div>
      <div class="msg bot">${p.error ? `<p class="error">${esc(p.error)}${/key|set up/i.test(p.error) ? ` <a href="#/settings">Open Settings</a>` : ""}</p>`
        : `<div class="thinking"><span class="spinner" aria-hidden="true"></span>Reading your data and thinking <span data-since="${p.t0}">${mmss((Date.now() - p.t0) / 1000)}</span></div>`}</div>` : "");
    $("chat-thread").querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => {
      navigator.clipboard.writeText(turns[Number(b.dataset.copy)].content).then(() => toast("Copied"), () => toast("Couldn't copy"));
    }));
  };
  const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; };

  async function send(question, preset = "", label = "") {
    const q = question.trim();
    if (busy() || (!q && !preset)) return;
    const key = keyNow(), target = scope;
    const body = { game_id: target, question: q, preset: preset || "auto", spoiler: $("chat-spoiler").value };
    if (isNew) body.new = true; else body.gen = gen;
    chat.pending[key] = { q: q || label, t0: Date.now() };
    chat.drafts[key] = "";
    text.value = "";
    grow(); syncSend(); renderThread(); renderList(); toBottom(); chatTick();
    try {
      const r = await api("/api/coach/ask", { method: "POST", body });
      delete chat.pending[key];
      if (state.view !== "chat") return;
      if (chat.viewKey === key) {
        if (isNew) location.hash = chatHref(target, r.gen);
        else route();
      } else if (location.hash.startsWith("#/chat")) {
        route();  // refresh the sidebar wherever they are in the chat page
      }
    } catch (e) {
      chat.pending[key] = { ...chat.pending[key], error: e.message };
      if (!chat.drafts[key]) chat.drafts[key] = q;
      if (state.view === "chat" && chat.viewKey === key) { renderThread(); renderList(); text.value = chat.drafts[key]; grow(); syncSend(); }
    }
  }

  renderList();
  renderThread();
  toBottom();
  syncSend();
  if (busy()) chatTick();
  // answers being written by another page: check back until they're saved
  const elsewhere = () => chat.list.some((c) => c.writing && !chat.pending[chatKey(c.game_id, c.gen)]);
  (async () => {
    while (elsewhere() && state.nav === nav) {
      await new Promise((res) => setTimeout(res, 4000));
      if (state.nav !== nav) return;
      let fresh;
      try { fresh = await api("/api/coach/conversations"); } catch { continue; }
      if (state.nav !== nav) return;
      const waiting = !!remote();
      chat.list = fresh.conversations.map(titled);
      if (waiting && !remote()) { route(); return; }  // the one on screen is done
      renderList();
    }
  })();

  $("chat-q").addEventListener("input", renderList);
  text.addEventListener("input", () => { chat.drafts[keyNow()] = text.value; grow(); syncSend(); });
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(text.value); }
  });
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); send(text.value); });
  $("chat-spoiler").addEventListener("change", (e) => { chat.spoiler = e.target.value; });
  $("chat-game")?.addEventListener("change", (e) => {  // switch the new chat's game without losing the draft
    const draft = text.value;
    scope = e.target.value;
    chat.viewKey = keyNow();
    chat.drafts[keyNow()] = draft;
    history.replaceState(null, "", scope === "*" ? "#/chat/new" : `#/chat/new/${encodeURIComponent(scope)}`);
    $("chat-scope-name").textContent = nameOf(scope);
    renderThread(); renderList(); syncSend();
  });
  const side = $("chat");
  const setSide = (open) => { side.classList.toggle("side-open", open); $("chat-toggle").setAttribute("aria-expanded", String(open)); };
  $("chat-toggle").addEventListener("click", () => setSide(!side.classList.contains("side-open")));
  $("chat-main").addEventListener("click", (e) => { if (side.classList.contains("side-open") && !e.target.closest("#chat-toggle")) setSide(false); });
  side.addEventListener("keydown", (e) => { if (e.key === "Escape") setSide(false); });

  $("chat-rename")?.addEventListener("click", () => {
    const h = $("chat-title"), input = document.createElement("input");
    input.type = "text"; input.className = "chat-rename"; input.value = conv.title; input.maxLength = 120;
    input.setAttribute("aria-label", "Chat name");
    h.replaceWith(input); input.focus(); input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const title = input.value.trim();
      if (save && title !== conv.title) {
        try { await api(`/api/coach/conversations/${encodeURIComponent(scope)}/${gen}`, { method: "POST", body: { title } }); }
        catch (err) { toast(err.message); }
      }
      route();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
    input.addEventListener("blur", () => finish(true));
  });
  $("chat-delete")?.addEventListener("click", async () => {
    if (!confirm(`Delete "${conv.title}"? This can't be undone. What the coach learned about you is kept.`)) return;
    try { await api(`/api/coach/conversations/${encodeURIComponent(scope)}/${gen}`, { method: "DELETE" }); }
    catch (err) { toast(err.message); return; }
    location.hash = "#/chat";
  });
  if (!isNew || !turns.length) text.focus({ preventScroll: true });
}

async function viewSettings() {
  state.view = "settings";
  const s = await api("/api/settings");
  state.settings = s;
  const field = (key, label, help, type = "text", extra = "") => `<div class="field"><label for="f-${key}">${label}</label>
    <input id="f-${key}" name="${key}" type="${type}" value="${esc(Array.isArray(s[key]) ? s[key].join(", ") : s[key])}" ${extra}>
    ${help ? `<span class="help">${help}</span>` : ""}</div>`;
  $app.innerHTML = `<div class="settings">
    <h2 style="margin-top:4px">Settings</h2>
    <form id="settings-form" class="stack">
      <div class="panel">
        <h3>Claude coach</h3>
        ${field("anthropic_api_key", "Claude API key", "Create one at console.anthropic.com. Stored on this server only; leave blank to use the ANTHROPIC_API_KEY environment variable.", "password", 'autocomplete="off" placeholder="sk-ant-…"')}
        <div class="field"><label for="f-coach_backend">Coach engine</label>
          <select id="f-coach_backend" name="coach_backend"><option value="prime-agent">prime-agent, using your Claude API key</option><option value="anthropic">Claude API directly</option></select>
          <span class="help">Both use the key above.</span></div>
        ${field("claude_model", "Model", "Anthropic model ID, e.g. claude-sonnet-5-5 or claude-opus-5-5.")}
        <div class="field"><label for="f-spoiler_level">Default spoiler level</label>
          <select id="f-spoiler_level" name="spoiler_level"><option value="none">No spoilers</option><option value="light">Light spoilers</option><option value="full">Spoilers OK</option></select></div>
        <label class="check"><input type="checkbox" name="coach_web_search" ${s.coach_web_search ? "checked" : ""}> Let the coach search the web for guides and patch notes</label>
      </div>
      <div class="panel">
        <h3>Stat sources</h3>
        ${field("overwatch_battletag", "Overwatch BattleTag", "Like Name#1234, case-sensitive. Your career profile must be public: Options, Social, Career Profile Visibility.", "text", 'placeholder="Name#1234"')}
        ${field("steam_api_key", "Steam Web API key (optional)", "Only needed for games whose achievements aren't cached locally. Get one at steamcommunity.com/dev/apikey.", "password", 'autocomplete="off"')}
        <p class="help muted" style="margin:14px 0 0">VALORANT and League stats come from the Riot client on your PC: open it while playing and they sync on their own.</p>
      </div>
      <div class="panel">
        <h3>Improvement reviews</h3>
        <p class="sub" style="margin-top:0">For VALORANT and CS2: Claude reviews each death from the match data, and the tracker builds habit stats to work on one or two at a time.</p>
        <label class="check"><input type="checkbox" name="input_reading" ${s.input_reading ? "checked" : ""}> Read my movement and shooting while VALORANT or CS2 is in front (game keys and the mouse only, so nothing you type can be read back; stays on the PC)</label>
        <label class="check"><input type="checkbox" name="death_reviews" ${s.death_reviews ? "checked" : ""}> Have Claude review my deaths after each match</label>
        <label class="check"><input type="checkbox" name="video_evidence" ${s.video_evidence ? "checked" : ""}> Add video evidence: the NVIDIA App records each VALORANT match and saves a CS2 Instant Replay when you die. Your PC keeps six small frames per death (crosshair area, minimap, kill feed) for the review and deletes the video</label>
        <div id="cap-status"></div>
        ${field("review_model", "Model for death reviews", "Anthropic model ID. claude-haiku-5-5 costs a fraction of a cent per match, with or without video frames.")}
      </div>
      <div class="panel">
        <h3>Guides knowledge base</h3>
        <p class="sub" style="margin-top:0">Every week, for each game you've been playing, the tracker finds YouTube guides that fit you, reads their captions, and keeps short notes with links to the exact moment. The coach uses them.</p>
        ${field("youtube_api_key", "YouTube API key", "From console.cloud.google.com: enable the YouTube Data API v3, then create an API key under Credentials and restrict it to that API. Free; no billing needed.", "password", 'autocomplete="off"')}
        <label class="check"><input type="checkbox" name="kb_enabled" ${s.kb_enabled ? "checked" : ""}> Find new guides automatically</label>
        ${field("kb_interval_days", "Look for new guides every (days)", "", "number", 'min="1" max="60"')}
        ${field("kb_videos_per_run", "Guides to read per game each time", "Each guide costs a fraction of a cent with Claude Haiku.", "number", 'min="1" max="10"')}
        ${field("kb_active_days", "Only for games played in the last (days)", "", "number", 'min="1" max="365"')}
        ${field("kb_model", "Model for reading guides", "Anthropic model ID. claude-haiku-5-5 is fast and cheap.")}
      </div>
      <div class="panel">
        <h3>Tracking</h3>
        ${field("poll_interval_sec", "Check for running games every (seconds)", "", "number", 'min="2" max="60"')}
        ${field("min_session_sec", "Ignore sessions shorter than (seconds)", "Filters out launcher blips and quick crashes.", "number", 'min="0"')}
        ${field("auto_sync_minutes", "While playing, sync stats every (minutes)", "", "number", 'min="2"')}
      </div>
      <div><button class="btn primary" type="submit">Save settings</button></div>
    </form>
    <div class="panel">
      <h3>Library</h3>
      <p class="sub">The library rescans every time the tracker starts. Rescan now if you just installed something.</p>
      <button class="btn" id="rescan">Rescan library</button>
      <form id="add-game" style="margin-top:18px">
        <h3>Add a game the scan missed</h3>
        <div class="field"><label for="ag-name">Name</label><input id="ag-name" required></div>
        <div class="field"><label for="ag-path">Game folder or .exe on your gaming PC</label><input id="ag-path" required placeholder="D:\\Games\\Elden Ring\\Game\\eldenring.exe"></div>
        <button class="btn" type="submit" style="margin-top:12px">Add game</button>
      </form>
    </div>
    <div class="panel">
      <h3>Run the server elsewhere</h3>
      <p class="sub">To keep the dashboard on a home server (reachable over Tailscale), run the server there, then point your gaming PC's tracker at it with this server's agent token:</p>
      <button class="btn" id="show-token">Show agent command</button>
      <pre class="cmd" id="token-cmd" hidden></pre>
      ${field("allowed_hosts", "Extra hostnames", "Comma-separated. localhost, IP addresses, Tailscale *.ts.net names and bare machine names already work.")}
    </div>
  </div>`;
  document.getElementById("f-spoiler_level").value = s.spoiler_level;
  document.getElementById("f-coach_backend").value = s.coach_backend || "prime-agent";
  const form = document.getElementById("settings-form");
  const allowed = document.getElementById("f-allowed_hosts");
  const collect = () => {
    const out = {};
    for (const el of [...form.elements, allowed]) {
      if (!el.name) continue;
      const v = el.type === "checkbox" ? el.checked : el.value;
      const orig = Array.isArray(s[el.name]) ? s[el.name].join(", ") : s[el.name];
      if (String(v) !== String(orig)) out[el.name] = v;
    }
    return out;
  };
  const save = async (e) => {
    e?.preventDefault();
    const changes = collect();
    if (!Object.keys(changes).length) { toast("Nothing changed"); return; }
    try {
      state.settings = await api("/api/settings", { method: "POST", body: changes });
      toast("Settings saved");
      viewSettings();
    } catch (err) { toast(err.message); }
  };
  form.addEventListener("submit", save);
  allowed.addEventListener("change", save);
  document.getElementById("rescan").addEventListener("click", async () => {
    const r = await api("/api/scan", { method: "POST" });
    toast(r.agent_online ? "Rescanning. New games show up in a few seconds." : "Your gaming PC is offline. It rescans when it reconnects.");
  });
  document.getElementById("add-game").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/games", { method: "POST", body: { name: document.getElementById("ag-name").value, path: document.getElementById("ag-path").value } });
      toast("Game added");
      location.hash = gameHref(r.id);
    } catch (err) { toast(err.message); }
  });
  document.getElementById("show-token").addEventListener("click", async () => {
    const { token } = await api("/api/settings/agent-token");
    const pre = document.getElementById("token-cmd");
    pre.textContent = `start.bat agent --server ${location.origin} --token ${token}`;
    pre.hidden = false;
  });
  if (s.video_evidence) captureStatus(0, (state.capRun = (state.capRun || 0) + 1));
}

// What the PC agent last saw of the NVIDIA App, refreshed while Settings is open.
async function captureStatus(round, run) {
  const el = document.getElementById("cap-status");
  if (!el || state.view !== "settings" || run !== state.capRun) return;
  let c;
  try { c = await api("/api/capture"); } catch { return; }
  const st = c.status, lines = [];
  const btn = (id, label) => `<button class="btn" type="button" id="${id}" style="margin-left:8px">${label}</button>`;
  if (!c.online) lines.push("Your gaming PC is offline, so the NVIDIA App can't be checked.");
  else if (!st || !st.enabled) lines.push("Waiting for the tracker on your PC to pick up the setting…");
  else {
    if (st.problem) lines.push(esc(st.problem));
    else {
      lines.push(`NVIDIA App connected${st.recording_match ? ", recording this VALORANT match" : ""}.`);
      if (st.instant_replay === false) lines.push(`Instant Replay is off. CS2 deaths need it; VALORANT doesn't.${btn("cap-ir", "Turn on Instant Replay")}`);
      if (st.replay_seconds > 60) lines.push(`Instant Replay keeps ${Math.round(st.replay_seconds / 60)} minutes, so each CS2 death writes a large file before the tracker trims and deletes it. 20 to 30 seconds is plenty: set it in the NVIDIA overlay (Alt+Z).`);
    }
    if (st.ffmpeg === false) lines.push("ffmpeg isn't installed on the PC yet. Restart the tracker to install it.");
  }
  const events = (st && st.events) || [];
  el.innerHTML = `<div class="note">${lines.map((l) => `<div>${l}</div>`).join("")}
    ${events.length ? `<ul class="cap-log">${events.slice(-4).reverse().map((e) => `<li><span class="muted">${esc(ago(e.ts))}</span> ${esc(e.text)}</li>`).join("")}</ul>` : ""}</div>`;
  document.getElementById("cap-ir")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/capture/instant-replay", { method: "POST" });
      toast(r.agent_online ? "Asking the NVIDIA App to turn on Instant Replay" : "Your gaming PC is offline");
    } catch (err) { toast(err.message); }
  });
  if (round < 40) setTimeout(() => captureStatus(round + 1, run), 6000);
}

// ---------------- router ----------------

async function route() {
  state.nav = (state.nav || 0) + 1;
  hideTip();
  fitChat();  // keeps --topbar-h current for sticky bars
  const h = location.hash || "#/";
  const name = h.startsWith("#/library") || h.startsWith("#/game/") || h.startsWith("#/match/") || h.startsWith("#/hero/") || h.startsWith("#/guides/") || h.startsWith("#/improve/") ? "library"
    : h.startsWith("#/chat") ? "chat" : h.startsWith("#/settings") ? "settings" : "overview";
  document.body.classList.toggle("chat-mode", name === "chat");
  document.querySelectorAll("[data-nav]").forEach((a) => (a.dataset.nav === name ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  try {
    if (h.startsWith("#/game/")) await viewGame(decodeURIComponent(h.slice(7)));
    else if (h.startsWith("#/match/")) await viewMatch(decodeURIComponent(h.slice(8)));
    else if (h === "#/chat" || h.startsWith("#/chat/")) await viewChat(h.slice(7));
    else if (h.startsWith("#/guides/")) await viewGuides(decodeURIComponent(h.slice(9)));
    else if (h.startsWith("#/improve/")) await viewImprove(decodeURIComponent(h.slice(10)));
    else if (h.startsWith("#/hero/")) {
      const [g, k] = h.slice(7).split("/");
      await viewDetail(decodeURIComponent(g), decodeURIComponent(k || ""));
    }
    else if (h === "#/library") await viewLibrary();
    else if (h === "#/settings") await viewSettings();
    else await viewOverview();
  } catch (e) {
    $app.innerHTML = `<p class="empty error">Couldn't load this page: ${esc(e.message)}</p>`;
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", route);
// Table rows that link somewhere (e.g. a match): click or Enter opens them.
document.addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-href]");
  if (tr && !e.target.closest("a, button")) location.hash = tr.dataset.href;
});
document.addEventListener("keydown", (e) => {
  const tr = e.target.closest && e.target.closest("tr[data-href]");
  if (tr && e.key === "Enter") location.hash = tr.dataset.href;
});
let resizeT;
window.addEventListener("resize", () => {
  clearTimeout(resizeT);
  resizeT = setTimeout(() => { if (state.view === "overview") renderOverview(); }, 200);
  if (state.view === "chat") fitChat();
});
refreshStatus().then(route);
setInterval(refreshStatus, 10000);
