"""Страница мобильной версии Ship Tracker (телефон).

Здесь только разметка, стили и сценарий: данные приходят из
`shop_tracker_mob.py` по /api/… — те же расчёты, что в настольной версии.
Вкладка «Ещё» открывает то, что в настольной версии разложено по отдельным
вкладкам: трафик района, ожидаемые, алерты, сервисы, настройки и журнал.
"""

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<meta name="theme-color" content="#0b1622">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Ship Tracker</title>
<link rel="manifest" href="/manifest.webmanifest">
<style>
:root { color-scheme: dark; --bg: #0b1622; --card: #10202e; --line: #1d3144;
  --text: #e8f1fa; --dim: #7d95ad; --accent: #4aa8ff; }
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text); }
body { font: 15px/1.4 -apple-system, "Segoe UI", Roboto, sans-serif;
  padding-bottom: calc(70px + env(safe-area-inset-bottom)); }
header { position: sticky; top: 0; z-index: 8; background: rgba(11,22,34,.96);
  backdrop-filter: blur(8px); padding: calc(8px + env(safe-area-inset-top)) 12px 8px; }
h1 { font-size: 18px; margin: 0 0 2px; font-weight: 650; }
.sub { font-size: 12px; color: var(--dim); display: flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #34c759; flex: none; }
.dot.stale { background: #ff9f0a; } .dot.off { background: #ff453a; }
main { padding: 0 12px 12px; }
input[type=search] { width: 100%; padding: 12px 14px; font-size: 16px; border-radius: 14px;
  border: 1px solid var(--line); background: #12222f; color: var(--text); outline: none; margin-top: 8px; }
input[type=search]::placeholder { color: #6d8299; }
.chips { display: flex; gap: 8px; overflow-x: auto; padding: 10px 0 4px; scrollbar-width: none; }
.chips::-webkit-scrollbar { display: none; }
.chip { flex: 0 0 auto; padding: 7px 13px; border-radius: 999px; border: 1px solid var(--line);
  background: #0f1e2b; color: #a9c2d8; font-size: 13px; }
.chip.on { background: #1e4f7a; border-color: #2f6c9e; color: #fff; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 16px;
  padding: 12px 14px; margin-bottom: 9px; }
.card.pinned { border-color: #3f7fae; }
.card.tap { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
.row { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.name { font-size: 16px; font-weight: 650; }
.star { color: #ffd166; margin-left: 4px; }
.small { font-size: 11px; color: #6f879e; }
.sog { font-size: 15px; font-weight: 600; color: #9fd0ff; white-space: nowrap; }
.meta { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }
.tag { font-size: 12px; padding: 3px 9px; border-radius: 999px; background: #162a3b; color: #a9c2d8; }
.tag.warn { background: #3a2a12; color: #ffcf7d; }
.tag.bad { background: #3a1a1a; color: #ff9c94; }
.tag.ok { background: #12301f; color: #8ee0ab; }
.hint { font-size: 12px; color: var(--dim); margin: 6px 2px 10px; }
.note { font-size: 12px; color: #9db6cc; background: #10202e; border: 1px solid var(--line);
  border-radius: 12px; padding: 9px 11px; margin: 0 0 10px; }
.empty { text-align: center; color: var(--dim); padding: 40px 10px; font-size: 14px; }
.back { display: inline-flex; align-items: center; gap: 6px; background: none; border: 0;
  color: var(--accent); font-size: 14px; padding: 8px 2px; }
.toolbar { display: flex; gap: 8px; align-items: center; margin: 4px 0 10px; }
.toolbar button { flex: 1; padding: 11px; font-size: 14px; border-radius: 12px; border: 1px solid var(--line);
  background: #13263a; color: var(--text); }
.toolbar button.primary { background: #1e4f7a; border-color: #2f6c9e; color: #fff; font-weight: 600; }
.field { margin-bottom: 12px; }
.field label { display: block; font-size: 12px; color: #a9c2d8; margin-bottom: 5px; }
.field input { width: 100%; padding: 11px 13px; font-size: 16px; border-radius: 12px;
  border: 1px solid var(--line); background: #12222f; color: var(--text); outline: none; }
.check { display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid #16283a; }
.check:last-child { border-bottom: 0; }
.check input { width: 20px; height: 20px; accent-color: #4aa8ff; flex: none; }
.check span { font-size: 14px; }
pre.log { white-space: pre-wrap; word-break: break-word; font: 11px/1.45 Consolas, monospace;
  color: #a9c2d8; background: #0a1522; border: 1px solid var(--line); border-radius: 12px;
  padding: 10px; max-height: 65vh; overflow: auto; margin: 0; }
nav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 9; display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  background: rgba(13,26,38,.98); border-top: 1px solid var(--line);
  padding: 6px 2px calc(6px + env(safe-area-inset-bottom)); }
nav button { min-width: 0; background: none; border: 0; color: #7d95ad; font-size: 9px;
  display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 4px 0;
  white-space: nowrap; }
nav button.on { color: var(--accent); }
nav svg { width: 21px; height: 21px; }
.sheet { position: fixed; inset: 0; z-index: 20; display: none; }
.sheet.open { display: block; }
.sheet .backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.55); }
.sheet .panel { position: absolute; left: 0; right: 0; bottom: 0; max-height: 88%;
  overflow-y: auto; background: #0e1c29; border-radius: 18px 18px 0 0;
  padding: 14px 14px calc(18px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); }
.sheet .handle { width: 42px; height: 4px; border-radius: 2px; background: #2b4256;
  margin: 0 auto 10px; }
.kv { display: grid; grid-template-columns: 128px 1fr; gap: 4px 8px; font-size: 14px; margin-top: 10px; }
.kv dt { color: var(--dim); } .kv dd { margin: 0; }
.links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.links a { font-size: 13px; padding: 8px 12px; border-radius: 999px; border: 1px solid var(--line);
  background: #13263a; color: #9fd0ff; text-decoration: none; }
canvas { width: 100%; display: block; border-radius: 16px; background: #0a1a27; }
#map { height: calc(100vh - 370px); min-height: 220px; max-height: 560px; }
.plan { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 8px; }
.plan div { background: #0d1c29; border: 1px solid var(--line); border-radius: 10px;
  padding: 6px 8px; }
.plan b { display: block; font-size: 13px; color: #cfe3f5; }
.plan span { font-size: 10px; color: var(--dim); }
a { color: #7cc4ff; }
</style>
</head>
<body>
<header>
  <h1>Ship Tracker</h1>
  <div class="sub"><span class="dot" id="dot"></span><span id="status">подключаюсь…</span></div>
  <input id="search" type="search" placeholder="Поиск: имя, назначение, MMSI" autocomplete="off">
</header>
<main id="view"></main>
<nav>
  <button data-tab="radar" class="on"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 12 19 5"/></svg>Радар</button>
  <button data-tab="sea"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 16c2 0 2 1.5 4 1.5S9 16 11 16s2 1.5 4 1.5S17 16 19 16s2 1.5 2 1.5"/><path d="M6 12h12l-1.5-4h-9z"/><path d="M12 8V4"/></svg>В море</button>
  <button data-tab="plan"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>4 дня</button>
  <button data-tab="all"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6h16M4 12h16M4 18h16"/></svg>Все суда</button>
  <button data-tab="visits"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 17h18M5 17V9l7-5 7 5v8"/><path d="M9 17v-4h6v4"/></svg>Стоянки</button>
  <button data-tab="link"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="2.5"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/><circle cx="12" cy="12" r="9"/></svg>Связь</button>
  <button data-tab="more"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>Ещё</button>
</nav>
<div class="sheet" id="sheet"><div class="backdrop" data-close></div>
  <div class="panel"><div class="handle"></div><div id="sheetBody"></div></div>
</div>
<script>
const view = document.getElementById('view');
const statusEl = document.getElementById('status');
const dot = document.getElementById('dot');
const search = document.getElementById('search');
const sheet = document.getElementById('sheet');
const sheetBody = document.getElementById('sheetBody');
let tab = 'radar', ships = null, ports = [], seaRows = null, planRows = null,
    allRows = null, visitRows = null, visitHistory = false, linkRows = null,
    trafficRows = null, expectedRows = null, alertsRows = null,
    servicesRows = null, settingsData = null, logRows = null;
let portFilter = '', seaFilter = '', chipFilter = 'all', mapShips = [];
const SUBTABS = ['traffic', 'expected', 'alerts', 'services', 'settings', 'log'];

const esc = v => String(v ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
function ago(m) {
  if (m === null || m === undefined) return 'нет данных';
  if (m < 2) return 'только что';
  if (m < 60) return m + ' мин назад';
  const h = Math.round(m / 60);
  if (h < 24) return h + ' ч назад';
  return Math.round(h / 24) + ' дн назад';
}
async function api(url) {
  const r = await fetch(url, {cache: 'no-store'});
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
async function post(url, data) {
  const r = await fetch(url, {method: 'POST', body: data || ''});
  return r.json();
}
async function loadShips() {
  try {
    const data = await api('/api/ships?limit=2000');
    ships = data; ports = data.ports || [];
    const age = data.db_age_minutes;
    dot.className = 'dot' + (age === null || age > 15 ? ' off' : age > 5 ? ' stale' : '');
    statusEl.textContent = (age === null ? 'база не обновляется' : 'база обновлена ' + ago(age))
      + ' · судов ' + data.total;
    if (tab === 'radar') renderRadar();
  } catch (e) {
    dot.className = 'dot off';
    statusEl.textContent = 'нет связи с приложением';
    view.innerHTML = '<div class="empty">Нет связи с приложением.<br>Перезапустите Ship Tracker и обновите страницу.</div>';
  }
}

function shipCard(s, extra) {
  const stale = s.age_minutes !== null && s.age_minutes > 180;
  const tags = [];
  if (s.destination) tags.push(`<span class="tag">→ ${esc(s.destination)}</span>`);
  if (s.near_port) tags.push(`<span class="tag ok">${esc(s.near_port)}</span>`);
  if (s.type_label) tags.push(`<span class="tag">${esc(s.type_label)}</span>`);
  tags.push(`<span class="tag ${stale ? 'bad' : ''}">${ago(s.age_minutes)}</span>`);
  if (extra) tags.push(extra);
  const sog = s.sog == null ? '—' : Number(s.sog).toFixed(1);
  return `<article class="card ${s.pinned ? 'pinned' : ''}" data-mmsi="${esc(s.mmsi)}">
    <div class="row"><div><div class="name">${esc(s.name || 'Без имени')}${s.pinned ? '<span class="star">★</span>' : ''}</div>
    <div class="small">MMSI ${esc(s.mmsi)}${s.imo ? ' · IMO ' + esc(s.imo) : ''}</div></div>
    <div class="sog">${sog} уз</div></div>
    <div class="meta">${tags.join('')}</div></article>`;
}

/* ------------------------------------------------------------ радар */
function renderRadar() {
  if (!ships) return;
  const shown = (ports || []).filter(p => p.lat && p.lon);
  mapShips = ships.ships.filter(s => s.age_minutes !== null && s.age_minutes <= 720);
  view.innerHTML = `<div class="hint">Порты и суда за последние 12 часов. Нажмите на точку или карточку.</div>
    <canvas id="map"></canvas>
    <div class="meta" style="margin:10px 2px">
      <span class="tag ok">● у порта</span><span class="tag">● в море</span>
      <span class="tag bad">● сигнал старше 3 ч</span></div>
    <div class="chips" id="radarChips">
      <button class="chip on" data-chip="all">Все</button>
      <button class="chip" data-chip="moving">В ходу</button>
      <button class="chip" data-chip="near">У порта</button>
      <button class="chip" data-chip="pinned">★ Закреплённые</button>
    </div>
    <div id="radarList"></div>`;
  const canvas = document.getElementById('map');
  drawMap(canvas, shown, mapShips);
  canvas.addEventListener('click', ev => pickShip(canvas, ev));
  document.querySelectorAll('#radarChips .chip').forEach(chip => chip.addEventListener('click', () => {
    chipFilter = chip.dataset.chip;
    document.querySelectorAll('#radarChips .chip').forEach(c => c.classList.toggle('on', c === chip));
    renderRadarList();
  }));
  renderRadarList();
}

function renderRadarList() {
  const box = document.getElementById('radarList');
  if (!box) return;
  const q = search.value.trim().toLowerCase();
  let list = ships.ships;
  if (chipFilter === 'moving') list = list.filter(s => (s.sog || 0) >= 0.5);
  if (chipFilter === 'near') list = list.filter(s => s.near_port);
  if (chipFilter === 'pinned') list = list.filter(s => s.pinned);
  if (q) list = list.filter(s => (s.name || '').toLowerCase().includes(q)
    || (s.destination || '').toLowerCase().includes(q) || s.mmsi === q);
  box.innerHTML = list.length
    ? list.slice(0, 300).map(s => shipCard(s)).join('')
    : '<div class="empty">Ничего не найдено</div>';
}

function mapProjection(canvas, ports, ships) {
  const pts = ports.map(p => [Number(p.lat), Number(p.lon)])
    .concat(ships.map(s => [s.lat, s.lon]));
  let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
  pts.forEach(([la, lo]) => { minLat = Math.min(minLat, la); maxLat = Math.max(maxLat, la);
    minLon = Math.min(minLon, lo); maxLon = Math.max(maxLon, lo); });
  const padLat = Math.max(0.25, (maxLat - minLat) * 0.12);
  const padLon = Math.max(0.25, (maxLon - minLon) * 0.12);
  minLat -= padLat; maxLat += padLat; minLon -= padLon; maxLon += padLon;
  const w = canvas.width, h = canvas.height;
  return ([la, lo]) => [ (lo - minLon) / (maxLon - minLon) * w,
                         h - (la - minLat) / (maxLat - minLat) * h ];
}
function drawMap(canvas, ports, ships) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 360;
  const height = Math.max(260, Math.round(canvas.clientHeight || 340));
  canvas.width = width * dpr; canvas.height = height * dpr;
  const g = canvas.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, width, height);
  const project = mapProjection({width, height}, ports, ships);
  g.strokeStyle = 'rgba(70,110,145,.18)';
  for (let i = 0; i <= 6; i++) {
    g.beginPath(); g.moveTo(0, i * height / 6); g.lineTo(width, i * height / 6); g.stroke();
    g.beginPath(); g.moveTo(i * width / 6, 0); g.lineTo(i * width / 6, height); g.stroke();
  }
  ships.forEach(s => {
    const [x, y] = project([s.lat, s.lon]);
    const near = !!s.near_port;
    g.beginPath(); g.arc(x, y, near ? 4 : 3, 0, Math.PI * 2);
    g.fillStyle = (s.age_minutes ?? 999) > 180 ? '#ff9c94' : near ? '#8ee0ab' : '#7cc4ff';
    g.globalAlpha = 0.85; g.fill(); g.globalAlpha = 1;
  });
  const boxes = [];
  const labels = ports.slice().sort((a, b) => (b.total || 0) - (a.total || 0));
  ports.forEach(p => {
    const [x, y] = project([Number(p.lat), Number(p.lon)]);
    g.fillStyle = '#ffd166'; g.fillRect(x - 3, y - 3, 6, 6);
  });
  labels.forEach(p => {
    const [x, y] = project([Number(p.lat), Number(p.lon)]);
    g.font = '600 10px -apple-system, Segoe UI, sans-serif';
    const w = g.measureText(p.name).width;
    for (const [dx, dy] of [[7, 4], [7, -12], [7, 20], [7, -28], [7, 36]]) {
      const box = [x + dx - 2, y + dy - 10, x + dx + w + 3, y + dy + 4];
      if (box[0] < 0 || box[2] > width) continue;
      const clash = boxes.some(b => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]));
      if (clash) continue;
      boxes.push(box);
      g.fillStyle = 'rgba(11,22,34,.82)';
      g.fillRect(box[0], box[1], box[2] - box[0], box[3] - box[1]);
      g.fillStyle = '#e8f1fa'; g.fillText(p.name, x + dx, y + dy);
      break;
    }
  });
  canvas._project = project;
}
function pickShip(canvas, ev) {
  if (!canvas._project) return;
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  let best = null, bestD = 22;
  mapShips.forEach(s => {
    const [sx, sy] = canvas._project([s.lat, s.lon]);
    const d = Math.hypot(sx - x, sy - y);
    if (d < bestD) { best = s; bestD = d; }
  });
  if (best) openVessel(best.mmsi);
}

/* ----------------------------------------------------------- в море */
async function renderSea() {
  if (!seaRows) {
    view.innerHTML = '<div class="empty">Считаю суда в море…</div>';
    try { seaRows = await api('/api/sea'); } catch (e) { view.innerHTML = '<div class="empty">Не удалось получить список</div>'; return; }
  }
  if (tab !== 'sea') return;
  const rows = seaRows.rows || [];
  const chips = ['', ...(seaRows.ports || [])];
  view.innerHTML = `<div class="note">${esc(seaRows.note || '')}</div>
    <div class="chips" id="seaChips">${chips.map(p => `<button class="chip ${p === seaFilter ? 'on' : ''}" data-port="${esc(p)}">${esc(p || 'Все порты')}</button>`).join('')}</div>
    <div id="seaList"></div>`;
  document.querySelectorAll('#seaChips .chip').forEach(chip => chip.addEventListener('click', () => {
    seaFilter = chip.dataset.port; renderSea();
  }));
  const list = seaFilter ? rows.filter(r => r.port === seaFilter) : rows;
  document.getElementById('seaList').innerHTML = list.length ? list.map(r => `<article class="card" data-mmsi="${esc(r.mmsi)}">
      <div class="row"><div><div class="name">${esc(r.name || 'Без имени')}</div>
      <div class="small">${esc(r.destination || '—')} · ${esc(r.type_label || '')}</div></div>
      <div class="sog">${r.sog == null ? '—' : Number(r.sog).toFixed(1)} уз</div></div>
      <div class="meta">
        ${r.port ? `<span class="tag ok">${esc(r.port)}</span>` : ''}
        <span class="tag">${esc(r.distance_text)}</span>
        <span class="tag ${r.eta_hours === null ? 'warn' : ''}">${esc(r.eta_text)}</span>
        <span class="tag">${ago(r.age_minutes)}</span>
      </div></article>`).join('') : '<div class="empty">В море сейчас никого из наших судов</div>';
}

/* ------------------------------------------------------ прогноз 4 дня */
async function renderPlan() {
  if (!planRows) {
    view.innerHTML = '<div class="empty">Считаю прогноз…</div>';
    try { planRows = await api('/api/forecast'); } catch (e) { view.innerHTML = '<div class="empty">Не удалось построить прогноз</div>'; return; }
  }
  if (tab !== 'plan') return;
  const rows = planRows.rows || [];
  view.innerHTML = `<div class="note">${esc(planRows.note || '')}</div>`
    + (rows.length ? rows.map(r => `<article class="card" data-mmsi="${esc(r.mmsi)}">
      <div class="row"><div><div class="name">${esc(r.name || 'Без имени')}</div>
      <div class="small">${esc(r.route)}</div></div>
      <div class="sog">${esc(r.speed_text)}</div></div>
      <div class="plan">
        <div><b>${esc(r.travel_text)}</b><span>в пути</span></div>
        <div><b>${esc(r.stay_text)}</b><span>стоянка</span></div>
        <div><b>${esc(r.total_text)}</b><span>всего до выхода</span></div>
      </div>
      <div class="meta"><span class="tag ok">${esc(r.port)}</span>
        <span class="tag">${esc(r.distance_text)}</span>
        <span class="tag">выход ${esc(r.departure_text)}</span></div>
      </article>`).join('') : '<div class="empty">Никто не идёт в наши порты</div>');
}

/* ------------------------------------------------ прогноз всех судов */
async function renderAll() {
  if (!allRows) {
    view.innerHTML = '<div class="empty">Считаю прогноз по всем судам…</div>';
    try { allRows = await api('/api/forecast_all'); } catch (e) { view.innerHTML = '<div class="empty">Не удалось построить прогноз</div>'; return; }
  }
  if (tab !== 'all') return;
  const rows = allRows.rows || [];
  view.innerHTML = `<div class="note">${esc(allRows.note || '')}</div>`
    + (rows.length ? rows.map(r => `<article class="card" data-mmsi="${esc(r.mmsi)}">
      <div class="row"><div><div class="name">${esc(r.name || 'Без имени')}</div>
      <div class="small">${esc(r.type_label || '')} · назначение ${esc(r.destination || '—')}</div></div>
      <div class="sog">${esc(r.speed_text)}</div></div>
      <div class="meta">
        <span class="tag ok">${esc(r.port)}${r.guessed ? ' (по курсу)' : ''}</span>
        <span class="tag">${esc(r.distance_text)}</span>
        <span class="tag">${esc(r.eta_text)}</span>
        <span class="tag">${ago(r.age_minutes)}</span>
      </div>
      <div class="plan">
        <div><b>${esc(r.day1)}</b><span>через день</span></div>
        <div><b>${esc(r.day2)}</b><span>через 2 дня</span></div>
        <div><b>${esc(r.day4)}</b><span>через 4 дня</span></div>
      </div>
      </article>`).join('') : '<div class="empty">Нет судов, идущих в наши порты</div>');
}

/* --------------------------------------------------------- стоянки */
async function renderVisits() {
  if (!visitRows) {
    view.innerHTML = '<div class="empty">Загружаю…</div>';
    try { visitRows = await api('/api/visits?limit=120&history=1'); } catch (e) {
      view.innerHTML = '<div class="empty">Не удалось получить стоянки</div>'; return; }
  }
  if (tab !== 'visits') return;
  const rows = visitRows.rows || [];
  view.innerHTML = `<div class="chips" id="visitChips">
      <button class="chip ${visitHistory ? '' : 'on'}" data-history="0">Стоят сейчас</button>
      <button class="chip ${visitHistory ? 'on' : ''}" data-history="1">История</button>
    </div><div id="visitList"></div>`;
  document.querySelectorAll('#visitChips .chip').forEach(chip => chip.addEventListener('click', () => {
    visitHistory = chip.dataset.history === '1'; renderVisits();
  }));
  const list = visitHistory ? rows : rows.filter(v => v.open);
  document.getElementById('visitList').innerHTML = list.length ? list.map(v => `<article class="card" data-mmsi="${esc(v.mmsi)}">
      <div class="row"><div><div class="name">${esc(v.name || v.mmsi)}</div>
      <div class="small">${esc(v.port)} · ${esc((v.entered_at || '').slice(0, 16).replace('T', ' '))}${v.left_at ? ' → ' + esc((v.left_at || '').slice(0, 16).replace('T', ' ')) : ''}</div></div>
      <div class="sog">${v.hours === null ? '—' : v.hours + ' ч'}</div></div>
      <div class="meta"><span class="tag ${v.open ? 'ok' : ''}">${v.open ? 'стоит сейчас' : 'ушло'}</span>
      <span class="tag">MMSI ${esc(v.mmsi)}</span></div></article>`).join('')
    : '<div class="empty">Записей нет</div>';
}

/* ------------------------------------------------------ подключения */
async function renderLink() {
  if (!linkRows) {
    view.innerHTML = '<div class="empty">Загружаю…</div>';
    try { linkRows = await api('/api/connections'); } catch (e) { view.innerHTML = '<div class="empty">Не удалось получить состояние подключений</div>'; return; }
  }
  if (tab !== 'link') return;
  const rows = linkRows.rows || [];
  view.innerHTML = `<div class="note">${esc(linkRows.note || '')}</div>`
    + rows.map(r => `<article class="card">
      <div class="row"><div><div class="name" style="font-size:15px">${esc(r.title)}</div>
      <div class="small">${esc(r.detail || '')}</div></div>
      <span class="tag ${r.state === 'on' ? 'ok' : r.state === 'off' ? 'bad' : 'warn'}">${esc(r.state_text)}</span></div>
      </article>`).join('')
    + `<div class="toolbar"><button onclick="restartCore(this)">Перезапустить сбор</button></div>`
    + `<div class="hint">База: ${esc(linkRows.database || '')}<br>Версия мобильной: ${esc(linkRows.version || '')} · версия программы на компьютере: ${esc(linkRows.desktop_version || '—')}</div>`;
}

async function restartCore(button) {
  button.disabled = true; button.textContent = 'Перезапускаю…';
  try { await post('/api/restart'); button.textContent = 'Сбор перезапущен'; }
  catch (e) { button.textContent = 'Не удалось перезапустить'; }
  setTimeout(() => { button.disabled = false; button.textContent = 'Перезапустить сбор'; }, 2500);
}

/* ------------------------------------------------------------- ещё */
const MORE = [
  ['traffic', 'Трафик района', 'Всё, что плавает в Дунае и Чёрном море: путь за 24 ч, средний ход, откуда пришло'],
  ['expected', 'Ожидаемые', 'Кто указал наш порт пунктом назначения (страницы AIS-catcher)'],
  ['alerts', 'Алерты', 'Журнал: что отправлено и с каким результатом'],
  ['services', 'Сервисы', 'Источники: включены ли, есть ли ключ, что говорит сбор'],
  ['settings', 'Настройки', 'Порты, фильтры и источники — как вкладка «Фильтры»'],
  ['log', 'Журнал', 'Последние строки журнала приложения'],
];
function backBar(title) {
  return `<button class="back" onclick="setTab('more')">‹ Ещё</button>
    <h2 style="font-size:17px;margin:2px 0 8px">${esc(title)}</h2>`;
}
function renderMore() {
  view.innerHTML = `<div class="note">Всё, что в настольной версии разложено по отдельным вкладкам.</div>`
    + MORE.map(([name, title, detail]) => `<article class="card tap" data-more="${name}">
      <div><div class="name" style="font-size:15px">${esc(title)}</div>
      <div class="small">${esc(detail)}</div></div><div class="sog">›</div></article>`).join('');
}

/* --------------------------------------------------- трафик района */
async function renderTraffic() {
  if (!trafficRows) {
    view.innerHTML = '<div class="empty">Считаю трафик района…</div>';
    try { trafficRows = await api('/api/traffic'); } catch (e) {
      view.innerHTML = backBar('Трафик района') + '<div class="empty">Не удалось получить список</div>'; return; }
  }
  if (tab !== 'traffic') return;
  const rows = trafficRows.rows || [];
  view.innerHTML = backBar('Трафик района')
    + `<div class="note">${esc(trafficRows.note || '')}</div>`
    + `<div class="hint">Судов в районе: ${trafficRows.total ?? rows.length}</div>`
    + (rows.length ? rows.map(r => `<article class="card" data-mmsi="${esc(r.mmsi)}">
      <div class="row"><div><div class="name">${esc(r.name || 'Без имени')}</div>
      <div class="small">MMSI ${esc(r.mmsi)}${r.type_label ? ' · ' + esc(r.type_label) : ''}${r.destination ? ' · → ' + esc(r.destination) : ''}</div></div>
      <div class="sog">${r.sog == null ? '—' : Number(r.sog).toFixed(1)} уз</div></div>
      <div class="meta">
        ${r.near_km != null ? `<span class="tag">${esc(r.near_port)} · ${r.near_km} км</span>` : ''}
        <span class="tag">путь 24 ч: ${r.path_km ?? 0} км</span>
        ${r.avg_knots != null ? `<span class="tag">ср. ход ${r.avg_knots} уз</span>` : ''}
        ${r.origin ? `<span class="tag">откуда ${esc(r.origin)}</span>` : ''}
        <span class="tag ${(r.age_minutes ?? 0) > 180 ? 'bad' : ''}">${ago(r.age_minutes)}</span>
      </div></article>`).join('') : '<div class="empty">В районе нет судов с позицией</div>');
}

/* ------------------------------------------------------ ожидаемые */
async function renderExpected() {
  if (!expectedRows) {
    view.innerHTML = '<div class="empty">Загружаю…</div>';
    try { expectedRows = await api('/api/expected'); } catch (e) {
      view.innerHTML = backBar('Ожидаемые') + '<div class="empty">На телефоне этот экран работает в автономной версии</div>'; return; }
  }
  if (tab !== 'expected') return;
  const rows = expectedRows.rows || [];
  view.innerHTML = backBar('Ожидаемые')
    + `<div class="note">${esc(expectedRows.note || '')}</div>`
    + (expectedRows.stale ? `<div class="note">Страницы сейчас недоступны: показаны прошлые данные${expectedRows.stale_reason ? ' (' + esc(expectedRows.stale_reason) + ')' : ''}.</div>` : '')
    + `<div class="toolbar"><button class="primary" id="expRefresh">${expectedRows.loading ? 'Обновляю…' : 'Обновить сейчас'}</button></div>`
    + `<div class="hint">Обновлено: ${esc((expectedRows.updated || '').slice(0, 16).replace('T', ' ') || 'ещё не было')} · судов ${rows.length}</div>`
    + (rows.length ? rows.map(r => `<article class="card" data-mmsi="${esc(r.mmsi || '')}">
      <div class="row"><div><div class="name" style="font-size:15px">${esc(r.name || 'Без имени')}</div>
      <div class="small">${esc(r.port)}${r.type ? ' · ' + esc(r.type) : ''}</div></div>
      <div class="sog" style="font-size:12px">${esc(r.eta || 'ETA —')}</div></div>
      <div class="meta">
        ${r.last_heard ? `<span class="tag">сигнал ${esc(r.last_heard)}</span>` : ''}
        <span class="tag ${r.stale ? 'warn' : 'ok'}">${r.stale ? 'устарело' : 'ожидается'}</span>
        ${r.mmsi ? `<span class="tag">MMSI ${esc(r.mmsi)}</span>` : ''}
      </div></article>`).join('') : '<div class="empty">Пока никто не заявил наш порт</div>');
  const button = document.getElementById('expRefresh');
  if (button) button.addEventListener('click', async () => {
    button.disabled = true; button.textContent = 'Запрашиваю порты…';
    try {
      await post('/api/expected/refresh');
      setTimeout(async () => {
        expectedRows = null;
        try { expectedRows = await api('/api/expected'); } catch (e) {}
        if (tab === 'expected') renderExpected();
      }, 12000);
    } catch (e) { button.textContent = 'Не удалось'; button.disabled = false; }
  });
}

/* ---------------------------------------------------------- алерты */
async function renderAlerts() {
  if (!alertsRows) {
    view.innerHTML = '<div class="empty">Загружаю…</div>';
    try { alertsRows = await api('/api/alerts'); } catch (e) {
      view.innerHTML = backBar('Алерты') + '<div class="empty">Не удалось получить журнал</div>'; return; }
  }
  if (tab !== 'alerts') return;
  const rows = alertsRows.rows || [];
  view.innerHTML = backBar('Алерты')
    + `<div class="note">${esc(alertsRows.note || '')}</div>`
    + (rows.length ? rows.map(r => `<article class="card">
      <div class="row"><div><div class="name" style="font-size:15px">${esc(r.name || r.mmsi || '—')}</div>
      <div class="small">${esc(r.origin || '—')} → ${esc(r.destination || '—')}</div></div>
      <span class="tag ${r.ok ? 'ok' : 'bad'}">${r.ok ? 'отправлен' : 'ошибка'}</span></div>
      <div class="meta"><span class="tag">${esc((r.sent_at || '').slice(0, 16).replace('T', ' '))}</span>
        ${r.error ? `<span class="tag bad">${esc(r.error)}</span>` : ''}</div>
      </article>`).join('') : '<div class="empty">Журнал пуст</div>');
}

/* --------------------------------------------------------- сервисы */
async function renderServices() {
  if (!servicesRows) {
    view.innerHTML = '<div class="empty">Загружаю…</div>';
    try { servicesRows = await api('/api/services'); } catch (e) {
      view.innerHTML = backBar('Сервисы') + '<div class="empty">На телефоне этот экран работает в автономной версии</div>'; return; }
  }
  if (tab !== 'services') return;
  const rows = servicesRows.rows || [];
  view.innerHTML = backBar('Сервисы')
    + `<div class="note">${esc(servicesRows.note || '')}</div>`
    + `<div class="hint">Сбор данных: ${servicesRows.running ? 'запущен' : 'остановлен'} · версия ${esc(servicesRows.version || '')}</div>`
    + rows.map(r => `<article class="card">
      <div class="row"><div><div class="name" style="font-size:15px">${esc(r.title)}</div>
      <div class="small">${esc(r.detail || '')}</div></div>
      <span class="tag ${r.state === 'on' ? 'ok' : r.state === 'off' ? 'bad' : 'warn'}">${esc(r.state_text)}</span></div>
      </article>`).join('')
    + `<div class="toolbar"><button onclick="restartCore(this)">Перезапустить сбор</button></div>`;
}

/* -------------------------------------------------------- настройки */
async function renderSettings() {
  if (!settingsData) {
    view.innerHTML = '<div class="empty">Загружаю настройки…</div>';
    try { settingsData = await api('/api/settings'); } catch (e) {
      view.innerHTML = backBar('Настройки') + '<div class="empty">На телефоне этот экран работает в автономной версии</div>'; return; }
  }
  if (tab !== 'settings') return;
  const ports = settingsData.ports || [], numbers = settingsData.numbers || [],
        sources = settingsData.sources || [];
  view.innerHTML = backBar('Настройки')
    + `<form id="setForm">
      <div class="card">
        <div class="name" style="font-size:15px;margin-bottom:4px">Отслеживать суда, идущие в порты</div>
        <div class="small" style="margin-bottom:6px">Если не отмечено ни одного — берутся все наши порты.</div>
        ${ports.map(p => `<label class="check"><input type="checkbox" name="port" value="${esc(p.name)}" ${p.on ? 'checked' : ''}><span>${esc(p.label || p.name)}</span></label>`).join('')}
      </div>
      <div class="card">
        <div class="name" style="font-size:15px;margin-bottom:8px">Пороги</div>
        ${numbers.map(n => `<div class="field"><label>${esc(n.label)}</label>
          <input type="number" step="any" name="${esc(n.key)}" value="${esc(n.value)}"></div>`).join('')}
      </div>
      <div class="card">
        <div class="name" style="font-size:15px;margin-bottom:4px">Источники данных</div>
        ${sources.map(s => `<label class="check"><input type="checkbox" name="src" value="${esc(s.flag)}" ${s.on ? 'checked' : ''}><span>${esc(s.title)}</span></label>`).join('')}
      </div>
      <div class="toolbar"><button class="primary" type="submit">Сохранить и перезапустить</button></div>
      <div class="hint" id="setInfo">База: ${esc(settingsData.database || '')}</div>
    </form>`;
  document.getElementById('setForm').addEventListener('submit', async ev => {
    ev.preventDefault();
    const info = document.getElementById('setInfo');
    const body = new URLSearchParams(new FormData(ev.target));
    info.textContent = 'Сохраняю…';
    try {
      const result = await post('/api/settings', body);
      if (!result.ok) throw new Error(result.error || 'не сохранилось');
      info.textContent = 'Сохранено, сбор данных перезапускается.';
      settingsData = null; linkRows = null; servicesRows = null;
    } catch (e) { info.textContent = 'Ошибка: ' + e.message; }
  });
}

/* ----------------------------------------------------------- журнал */
async function renderLog() {
  if (!logRows) {
    view.innerHTML = '<div class="empty">Загружаю журнал…</div>';
    try { logRows = await api('/api/log'); } catch (e) {
      view.innerHTML = backBar('Журнал') + '<div class="empty">На телефоне этот экран работает в автономной версии</div>'; return; }
  }
  if (tab !== 'log') return;
  view.innerHTML = backBar('Журнал')
    + `<div class="toolbar"><button id="logRefresh">Обновить</button></div>`
    + `<div class="hint">${esc(logRows.path || '')}</div>`
    + `<pre class="log">${esc((logRows.rows || []).join('\\n') || 'журнал пуст')}</pre>`;
  const button = document.getElementById('logRefresh');
  if (button) button.addEventListener('click', async () => {
    button.disabled = true;
    try { logRows = await api('/api/log'); } catch (e) {}
    renderLog();
  });
}

/* ------------------------------------------------------------ общее */
function render() {
  if (tab === 'radar') renderRadar();
  else if (tab === 'sea') renderSea();
  else if (tab === 'plan') renderPlan();
  else if (tab === 'all') renderAll();
  else if (tab === 'visits') renderVisits();
  else if (tab === 'link') renderLink();
  else if (tab === 'more') renderMore();
  else if (tab === 'traffic') renderTraffic();
  else if (tab === 'expected') renderExpected();
  else if (tab === 'alerts') renderAlerts();
  else if (tab === 'services') renderServices();
  else if (tab === 'settings') renderSettings();
  else if (tab === 'log') renderLog();
}
function setTab(name) {
  tab = name;
  const hash = SUBTABS.includes(name) ? 'more' : name;
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === hash));
  search.style.display = name === 'radar' ? 'block' : 'none';
  render();
}
document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
search.addEventListener('input', () => { if (tab === 'radar') renderRadarList(); });

async function openVessel(mmsi) {
  sheet.classList.add('open');
  sheetBody.innerHTML = '<div class="empty">Загружаю…</div>';
  try {
    const v = await api('/api/vessel?mmsi=' + encodeURIComponent(mmsi));
    if (!v.mmsi) { sheetBody.innerHTML = '<div class="empty">Судно не найдено</div>'; return; }
    const mapUrl = `https://www.google.com/maps?q=${v.lat},${v.lon}`;
    const hist = (v.history || []).slice().reverse();
    const visits = (v.visits || []).map(p => `<div>${esc(p.port)} · ${esc((p.entered_at || '').slice(0, 16).replace('T', ' '))}${p.left_at ? ' → ' + esc((p.left_at || '').slice(0, 16).replace('T', ' ')) : ' · стоит'}</div>`).join('');
    const name = encodeURIComponent(v.name || '');
    const links = [
      ['MarineTraffic', `https://www.marinetraffic.com/en/ais/details/ships/mmsi:${v.mmsi}`],
      ['VesselFinder', `https://www.vesselfinder.com/vessels?name=${name}`],
      ['ShipFinder', `https://shipfinder.co/`],
      ['MyShipTracking', `https://www.myshiptracking.com/?mmsi=${v.mmsi}`],
      ['Open Waters', `https://openwaters.io/ais/`],
      ['Google Maps', mapUrl],
    ];
    sheetBody.innerHTML = `
      <div class="row"><div><div class="name" style="font-size:19px">${esc(v.name || 'Без имени')}</div>
      <div class="small">MMSI ${esc(v.mmsi)}${v.imo ? ' · IMO ' + esc(v.imo) : ''}${v.callsign ? ' · ' + esc(v.callsign) : ''}</div></div>
      <div class="sog">${v.sog == null ? '—' : Number(v.sog).toFixed(1)} уз</div></div>
      <div class="meta">
        ${v.destination ? `<span class="tag">→ ${esc(v.destination)}</span>` : ''}
        ${v.near_port ? `<span class="tag ok">${esc(v.near_port)}</span>` : ''}
        <span class="tag ${(v.age_minutes ?? 99) > 180 ? 'bad' : ''}">${ago(v.age_minutes)}</span>
      </div>
      <dl class="kv">
        <dt>Координаты</dt><dd><a href="${mapUrl}" target="_blank" rel="noopener">${Number(v.lat).toFixed(5)}, ${Number(v.lon).toFixed(5)}</a></dd>
        <dt>Курс / скорость</dt><dd>${v.cog ?? '—'}° · ${v.sog ?? '—'} уз</dd>
        <dt>Назначение</dt><dd>${esc(v.destination || '—')}</dd>
        <dt>ETA по AIS</dt><dd>${esc((v.eta_declared || '').slice(0, 16).replace('T', ' ') || '—')}</dd>
        <dt>Осадка</dt><dd>${v.draft ?? '—'} м</dd>
        <dt>Размеры</dt><dd>${v.length_m ?? '—'} × ${v.beam_m ?? '—'} м</dd>
        <dt>Тип</dt><dd>${esc(v.type_label || '—')}</dd>
        <dt>До порта</dt><dd>${v.distance_km ?? '—'} км</dd>
      </dl>
      <div class="links">${links.map(([title, url]) => `<a href="${url}" target="_blank" rel="noopener">${esc(title)}</a>`).join('')}</div>
      <div class="hint" style="margin-top:14px">История позиций (${hist.length})</div>
      <canvas id="track" height="110"></canvas>
      <div class="hint" style="margin-top:14px">Стоянки</div>
      ${visits || '<div class="small">нет записей</div>'}`;
    drawTrack(document.getElementById('track'), hist);
  } catch (e) { sheetBody.innerHTML = '<div class="empty">Не удалось загрузить судно</div>'; }
}
function drawTrack(canvas, points) {
  if (!canvas || points.length < 2) return;
  const dpr = window.devicePixelRatio || 1, width = canvas.clientWidth || 320;
  canvas.width = width * dpr; canvas.height = 110 * dpr;
  const g = canvas.getContext('2d'); g.scale(dpr, dpr);
  const lat = points.map(p => p.lat), lon = points.map(p => p.lon);
  const minLat = Math.min(...lat), maxLat = Math.max(...lat);
  const minLon = Math.min(...lon), maxLon = Math.max(...lon);
  const sx = v => 8 + (v - minLon) / (maxLon - minLon || 1) * (width - 16);
  const sy = v => 100 - (v - minLat) / (maxLat - minLat || 1) * 88;
  g.strokeStyle = '#4aa8ff'; g.lineWidth = 2; g.beginPath();
  points.forEach((p, i) => i ? g.lineTo(sx(p.lon), sy(p.lat)) : g.moveTo(sx(p.lon), sy(p.lat)));
  g.stroke();
  const last = points[points.length - 1];
  g.fillStyle = '#ffd166'; g.beginPath(); g.arc(sx(last.lon), sy(last.lat), 4, 0, 7); g.fill();
}
sheet.addEventListener('click', e => { if (e.target.dataset.close !== undefined) sheet.classList.remove('open'); });
view.addEventListener('click', e => {
  const more = e.target.closest('[data-more]');
  if (more) { setTab(more.dataset.more); return; }
  const card = e.target.closest('[data-mmsi]');
  if (card && card.dataset.mmsi) openVessel(card.dataset.mmsi);
});

search.style.display = 'block';
if (location.hash) {
  const name = location.hash.slice(1);
  if (['radar', 'sea', 'plan', 'all', 'visits', 'link', 'more', ...SUBTABS].includes(name)) setTab(name);
}
loadShips();
setInterval(() => { ships = null; seaRows = null; planRows = null; allRows = null; visitRows = null; linkRows = null;
  trafficRows = null; alertsRows = null; servicesRows = null; logRows = null; loadShips(); }, 30000);
</script>
</body>
</html>
"""
