"""Страница мобильной версии Ship Tracker (телефон).

Здесь только разметка, стили и сценарий: данные приходят из
`mobile_server.py` по /api/… — те же расчёты, что в настольной версии.
Фильтры радара повторяют настольные: «только цели», «в порту / ждёт
лоцмана», «в ходу», «закреплённые», трек и прогноз на 3 дня.
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
:root { color-scheme: dark; --bg: #08111b; --bg2: #0b1622; --card: #10202e;
  --card2: #0d1b27; --line: #1d3144; --text: #e8f1fa; --dim: #7d95ad;
  --accent: #4aa8ff; --ok: #8ee0ab; --warn: #ffcf7d; --bad: #ff9c94; }
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; height: 100%; }
body { background: radial-gradient(1200px 600px at 50% -200px, #12283c 0%, var(--bg) 62%);
  color: var(--text); font: 15px/1.42 -apple-system, "Segoe UI", Roboto, sans-serif;
  padding-bottom: calc(74px + env(safe-area-inset-bottom)); }
header { position: sticky; top: 0; z-index: 8; background: rgba(9,17,26,.92);
  backdrop-filter: blur(12px); border-bottom: 1px solid rgba(29,49,68,.65);
  padding: calc(9px + env(safe-area-inset-top)) 12px 9px; }
.hrow { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
h1 { font-size: 18px; margin: 0; font-weight: 700; letter-spacing: .2px; }
.sub { font-size: 12px; color: var(--dim); display: flex; align-items: center; gap: 6px; margin-top: 2px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #34c759; flex: none;
  box-shadow: 0 0 0 3px rgba(52,199,89,.14); }
.dot.stale { background: #ff9f0a; box-shadow: 0 0 0 3px rgba(255,159,10,.14); }
.dot.off { background: #ff453a; box-shadow: 0 0 0 3px rgba(255,69,58,.14); }
main { padding: 10px 12px 14px; }
input[type=search] { width: 100%; padding: 11px 14px; font-size: 16px; border-radius: 13px;
  border: 1px solid var(--line); background: #0e1d2a; color: var(--text); outline: none; margin-top: 9px; }
input[type=search]::placeholder { color: #6d8299; }
.chips { display: flex; gap: 7px; overflow-x: auto; padding: 2px 0 6px; scrollbar-width: none; }
.chips::-webkit-scrollbar { display: none; }
.chip { flex: 0 0 auto; padding: 7px 13px; border-radius: 999px; border: 1px solid var(--line);
  background: #0f1e2b; color: #a9c2d8; font-size: 13px; transition: background .15s, border-color .15s; }
.chip.on { background: linear-gradient(180deg, #2564a3, #1b4d7a); border-color: #3a7dbb; color: #fff;
  box-shadow: 0 2px 8px rgba(37,100,163,.35); }
.chip.toggle.on { background: linear-gradient(180deg, #1d5c46, #154f3a); border-color: #2c7d5f; }
.card { background: linear-gradient(180deg, var(--card), var(--card2));
  border: 1px solid var(--line); border-radius: 15px; padding: 12px 14px; margin-bottom: 9px;
  box-shadow: 0 3px 12px rgba(0,0,0,.22); transition: transform .12s ease, border-color .15s; }
.card:active { transform: scale(.99); }
.card.pinned { border-color: #3f7fae; }
.card.tap { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
.row { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.name { font-size: 16px; font-weight: 650; }
.star { color: #ffd166; margin-left: 4px; }
.small { font-size: 11.5px; color: #7590a8; }
.sog { font-size: 15px; font-weight: 650; color: #9fd0ff; white-space: nowrap; }
.meta { margin-top: 7px; display: flex; flex-wrap: wrap; gap: 6px; }
.tag { font-size: 11.5px; padding: 3px 9px; border-radius: 999px; background: #162a3b;
  color: #a9c2d8; border: 1px solid rgba(45,74,101,.55); }
.tag.warn { background: #2f2511; color: var(--warn); border-color: #4a3a17; }
.tag.bad { background: #31191a; color: var(--bad); border-color: #4d2626; }
.tag.ok { background: #112a1d; color: var(--ok); border-color: #1d4532; }
.hint { font-size: 12px; color: var(--dim); margin: 6px 2px 10px; }
.note { font-size: 12px; color: #9db6cc; background: #0e1d2a; border: 1px solid var(--line);
  border-radius: 12px; padding: 10px 12px; margin: 0 0 10px; line-height: 1.45; }
.empty { text-align: center; color: var(--dim); padding: 42px 12px; font-size: 14px; }
.bar { display: flex; gap: 8px; align-items: center; margin: 2px 0 10px; flex-wrap: wrap; }
.bar button { flex: 1 1 auto; padding: 10px 12px; font-size: 13.5px; border-radius: 12px;
  border: 1px solid var(--line); background: linear-gradient(180deg, #13263a, #0f1e2c);
  color: var(--text); font-weight: 550; }
.bar button:active { background: #1a3550; }
.bar button.primary { background: linear-gradient(180deg, #2a6ba8, #1d517d);
  border-color: #3a7dbb; color: #fff; }
.bar button:disabled { opacity: .6; }
.titlebar { display: flex; align-items: center; gap: 6px; margin: 2px 0 8px; }
.titlebar h2 { font-size: 17px; margin: 0; flex: 1; }
.iconbtn { width: 38px; height: 38px; border-radius: 12px; border: 1px solid var(--line);
  background: #10202e; color: var(--text); font-size: 16px; display: grid; place-items: center; }
.back { background: none; border: 0; color: var(--accent); font-size: 14px; padding: 8px 4px;
  white-space: nowrap; flex: none; }
.field { margin-bottom: 11px; }
.field label { display: block; font-size: 12px; color: #a9c2d8; margin-bottom: 5px; }
.field input { width: 100%; padding: 11px 13px; font-size: 16px; border-radius: 12px;
  border: 1px solid var(--line); background: #0e1d2a; color: var(--text); outline: none; }
.check { display: flex; align-items: center; gap: 10px; padding: 10px 0;
  border-bottom: 1px solid rgba(29,49,68,.55); }
.check:last-child { border-bottom: 0; }
.check input { width: 20px; height: 20px; accent-color: #4aa8ff; flex: none; }
.check span { font-size: 14px; }
pre.log { white-space: pre-wrap; word-break: break-word; font: 11px/1.5 Consolas, monospace;
  color: #a9c2d8; background: #08131d; border: 1px solid var(--line); border-radius: 12px;
  padding: 10px; max-height: 62vh; overflow: auto; margin: 0; }
nav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 9; display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr)); background: rgba(10,20,30,.97);
  border-top: 1px solid rgba(29,49,68,.8); backdrop-filter: blur(12px);
  padding: 7px 2px calc(7px + env(safe-area-inset-bottom)); }
nav button { min-width: 0; background: none; border: 0; color: #7790a7; font-size: 9px;
  display: flex; flex-direction: column; align-items: center; gap: 3px; padding: 3px 0;
  white-space: nowrap; transition: color .15s; }
nav button.on { color: #6fbdff; }
nav button.on svg { filter: drop-shadow(0 0 6px rgba(74,168,255,.45)); }
nav svg { width: 21px; height: 21px; }
.sheet { position: fixed; inset: 0; z-index: 20; display: none; }
.sheet.open { display: block; }
.sheet .backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.55); }
.sheet .panel { position: absolute; left: 0; right: 0; bottom: 0; max-height: 88%;
  overflow-y: auto; background: #0c1926; border-radius: 20px 20px 0 0;
  padding: 14px 14px calc(18px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); }
.sheet .handle { width: 42px; height: 4px; border-radius: 2px; background: #2b4256;
  margin: 0 auto 10px; }
.kv { display: grid; grid-template-columns: 122px 1fr; gap: 5px 10px; font-size: 14px; margin-top: 10px; }
.kv dt { color: var(--dim); } .kv dd { margin: 0; }
.links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.links a { font-size: 13px; padding: 8px 12px; border-radius: 999px; border: 1px solid var(--line);
  background: #13263a; color: #9fd0ff; text-decoration: none; }
canvas { width: 100%; display: block; border-radius: 15px; background: #08131e; }
.maplayer { position: relative; margin-top: 4px; }
.maplayer #map { height: calc(100vh - 420px); min-height: 300px; max-height: 620px; }
.zoom { position: absolute; right: 8px; bottom: 8px; display: flex; flex-direction: column; gap: 6px; }
.zoom button { width: 38px; height: 38px; border-radius: 11px; border: 1px solid #26425c;
  background: rgba(13,27,39,.92); color: #dbe9f6; font-size: 17px; line-height: 1; }
.legend { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 2px; }
.plan { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 8px; }
.plan div { background: #0c1a26; border: 1px solid var(--line); border-radius: 10px; padding: 6px 8px; }
.plan b { display: block; font-size: 13px; color: #cfe3f5; }
.plan span { font-size: 10px; color: var(--dim); }
a { color: #7cc4ff; }
</style>
</head>
<body>
<header>
  <div class="hrow"><h1>Ship Tracker</h1>
    <button class="iconbtn" id="refreshAll" title="Обновить">⟳</button></div>
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
const refreshAll = document.getElementById('refreshAll');
let tab = 'radar', ships = null, ports = [], seaRows = null, planRows = null,
    allRows = null, visitRows = null, visitHistory = false, linkRows = null,
    trafficRows = null, expectedRows = null, alertsRows = null,
    servicesRows = null, settingsData = null, logRows = null, sourcesRows = null,
    tracks = null, forecastPts = null;
let trafficSort = 'age', expectedFilter = 'all', alertFilter = 'all';
let seaFilter = '', mapShips = [];
let showTracks = true, showForecast = true, showSchematic = false;
let targetsOnly = false, portOnly = false, fitMode = 'ports', radarSelected = null;
let mapView = {scale: 1, dx: 0, dy: 0};
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
function clearCaches() {
  ships = seaRows = planRows = allRows = visitRows = linkRows = trafficRows =
    alertsRows = servicesRows = logRows = sourcesRows = null;
}
/* Треки и прогноз для карты — тяжёлые: не сбрасываем их каждые 30 секунд,
   только по кнопке «Обновить». */
function clearMapLayers() { tracks = null; forecastPts = null; }
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
async function busy(button, work, label) {
  const text = button.textContent;
  button.disabled = true;
  if (label) button.textContent = label;
  try { await work(); } finally { button.disabled = false; button.textContent = text; }
}
function head(title, extra) {
  return `<div class="titlebar"><button class="back" data-back="1">‹ Ещё</button>
    <h2>${esc(title)}</h2>${extra || ''}</div>`;
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
/* Радар повторяет настольный: карта с сеткой координат, порты с радиусом,
   суда с линией курса и подписями без наложений, приглушённые старые
   позиции, «только цели», «в порту / ждёт лоцмана», трек и прогноз
   выбранного судна, схематический вид, «Наши суда» и легенда со счётчиками. */
function radarSets() {
  const all = ((ships && ships.ships) || []).filter(s => s.age_minutes !== null);
  // Своя обстановка — как port_focus_vessels на ПК: цель, порт, рейд
  // или судно у порта. Остальное — чужой трафик мелкой точкой.
  const ours = all.filter(s => s.focus || s.target);
  return { all, ours, traffic: all.filter(s => !(s.focus || s.target)) };
}
function staleLimit(s) {
  const parked = !!s.near_port && Number(s.sog || 0) < 0.5;
  if (parked) return 540;            // в порту и на рейде — 9 часов, как berth_stale_hours
  const setting = settingsData && (settingsData.numbers || [])
    .find(n => n.key === 'stale_minutes');
  const minutes = setting ? Number(setting.value) : 15;
  return minutes > 0 ? minutes : 99999;
}
function shipIsStale(s) {
  return (s.age_minutes ?? 99999) > staleLimit(s);
}
function radarFiltered(s) {
  // выбранное судно остаётся на карте при любом фильтре — как на ПК
  if (String(s.mmsi) === String(radarSelected)) return true;
  if ((s.age_minutes ?? 99999) > 720) return false;
  // «только цели» и «в порту / ждёт лоцмана» — те же отборы, что на ПК
  if (targetsOnly && !s.target) return false;
  if (portOnly && !(s.at_port || s.near_port)) return false;
  return true;
}

function radarShown() {
  const sets = radarSets();
  const shown = sets.ours.filter(radarFiltered);
  const selected = sets.all.find(s => String(s.mmsi) === String(radarSelected));
  if (selected && !shown.includes(selected)) shown.push(selected);
  return shown;
}
function freeSpace(box, boxes) {
  return !boxes.some(b => !(box[0] + box[2] < b[0] || box[0] > b[0] + b[2]
    || box[1] + box[3] < b[1] || box[1] > b[1] + b[3]));
}
function staleLabelText() {
  const setting = settingsData && (settingsData.numbers || [])
    .find(n => n.key === 'stale_minutes');
  const minutes = setting ? Number(setting.value) : 15;
  return (minutes > 0 ? minutes + ' мин' : '—') + ' / 9 ч в порту';
}

function renderRadar() {
  if (!ships) return;
  mapShips = radarShown();
  view.innerHTML = `
    <div class="bar">
      <button data-act="refresh">⟳ Обновить</button>
      <button data-act="fit">Наши суда</button>
    </div>
    <div class="chips" id="radarChecks">
      <button class="chip toggle ${targetsOnly ? 'on' : ''}" data-check="targets">только цели</button>
      <button class="chip toggle ${portOnly ? 'on' : ''}" data-check="port">в порту / ждёт лоцмана</button>
      <button class="chip toggle ${showTracks ? 'on' : ''}" data-check="tracks">трек</button>
      <button class="chip toggle ${showForecast ? 'on' : ''}" data-check="forecast">прогноз 3 дня</button>
      <button class="chip toggle ${showSchematic ? 'on' : ''}" data-check="schematic">схематический вид</button>
    </div>
    <div class="maplayer"><canvas id="map"></canvas>
      <div class="zoom"><button data-zoom="in">+</button><button data-zoom="out">−</button><button data-zoom="reset">⤢</button></div>
    </div>
    <div class="legend">
      <span class="tag bad">● цель</span><span class="tag">· трафик района</span>
      <span class="tag ok">▣ порт</span><span class="tag warn">— прогноз выбранного</span>
    </div>
    <div class="hint">Кнопки и перетаскивание — масштаб и сдвиг, нажатие по точке — карточка судна.</div>
    <div class="hint" id="radarCount"></div>
    <div id="radarList"></div>`;
  drawRadar();
  bindMap();
  const redraw = () => { renderRadar(); };
  document.querySelectorAll('#radarChecks .chip').forEach(chip => chip.addEventListener('click', async () => {
    const key = chip.dataset.check;
    if (key === 'targets') targetsOnly = !targetsOnly;
    else if (key === 'port') portOnly = !portOnly;
    else if (key === 'schematic') showSchematic = !showSchematic;
    else if (key === 'tracks') {
      showTracks = !showTracks;
      if (showTracks && !tracks) {
        const text = chip.textContent;
        chip.textContent = 'трек…';
        try { tracks = await api('/api/tracks?hours=24'); } catch (e) { tracks = {}; }
        chip.textContent = text;
      }
    } else if (key === 'forecast') {
      showForecast = !showForecast;
      if (showForecast && !forecastPts) {
        const text = chip.textContent;
        chip.textContent = 'прогноз…';
        try { forecastPts = await api('/api/forecast_all'); } catch (e) { forecastPts = {rows: []}; }
        chip.textContent = text;
      }
    }
    // на ПК включение отбора сразу переводит кадр на наши суда
    if (key === 'targets' || key === 'port') {
      fitMode = 'vessels';
      mapView = {scale: 1, dx: 0, dy: 0};
    }
    redraw();
  }));
  renderRadarList();
}

function renderRadarList() {
  const box = document.getElementById('radarList');
  if (!box) return;
  const q = search.value.trim().toLowerCase();
  let list = mapShips;
  if (q) list = list.filter(s => (s.name || '').toLowerCase().includes(q)
    || (s.destination || '').toLowerCase().includes(q) || s.mmsi === q);
  const sets = radarSets();
  const counter = document.getElementById('radarCount');
  if (counter) counter.textContent = `На карте: ${mapShips.length} из ${sets.ours.length} наших`
    + ` · трафик района: ${sets.traffic.length} · в списке: ${list.length}`
    + (radarSelected ? ` · выбрано: ${radarSelected}` : '');
  box.innerHTML = list.length
    ? list.slice(0, 300).map(s => shipCard(s)).join('')
    : '<div class="empty">Под этот фильтр судов нет</div>';
}

function drawRadar() {
  const canvas = document.getElementById('map');
  if (canvas) drawRadarMap(canvas);
}

/* Сдвиг и масштаб: как колесо и перетаскивание в настольной версии. */
function bindMap() {
  const canvas = document.getElementById('map');
  if (!canvas) return;
  let drag = null;
  canvas.addEventListener('pointerdown', ev => {
    drag = {x: ev.clientX, y: ev.clientY, dx: mapView.dx, dy: mapView.dy, moved: false};
    canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener('pointermove', ev => {
    if (!drag) return;
    const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (Math.hypot(dx, dy) > 5) drag.moved = true;
    mapView.dx = drag.dx + dx; mapView.dy = drag.dy + dy;
    drawRadar();
  });
  canvas.addEventListener('pointerup', ev => {
    const wasDrag = drag && drag.moved;
    drag = null;
    if (!wasDrag) pickShip(canvas, ev);
  });
  view.querySelectorAll('[data-zoom]').forEach(button => button.addEventListener('click', () => {
    const act = button.dataset.zoom;
    if (act === 'reset') mapView = {scale: 1, dx: 0, dy: 0};
    else mapView.scale = Math.min(9, Math.max(0.6,
      mapView.scale * (act === 'in' ? 1.35 : 1 / 1.35)));
    drawRadar();
  }));
}

/* Первый кадр — по портам; кнопка «Наши суда» — по всем нашим судам,
   включая те, что идут к нам через всё Чёрное и Средиземное море. */
function radarBase(width, height) {
  const allPorts = (ports || []).filter(p => p.lat && p.lon);
  const sets = radarSets();
  const pts = [];
  if (fitMode === 'vessels') sets.ours.forEach(s => pts.push([Number(s.lat), Number(s.lon)]));
  if (!pts.length) allPorts.forEach(p => pts.push([Number(p.lat), Number(p.lon)]));
  if (!pts.length) sets.ours.forEach(s => pts.push([Number(s.lat), Number(s.lon)]));
  if (!pts.length) return null;
  let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
  pts.forEach(([la, lo]) => {
    minLat = Math.min(minLat, la); maxLat = Math.max(maxLat, la);
    minLon = Math.min(minLon, lo); maxLon = Math.max(maxLon, lo);
  });
  const padLat = Math.max(0.15, (maxLat - minLat) * 0.18);
  const padLon = Math.max(0.15, (maxLon - minLon) * 0.18);
  minLat -= padLat; maxLat += padLat; minLon -= padLon; maxLon += padLon;
  return {
    minLat, maxLat, minLon, maxLon,
    centerLat: (minLat + maxLat) / 2, centerLon: (minLon + maxLon) / 2,
    project: ([la, lo]) => [(lo - minLon) / (maxLon - minLon) * width,
                            height - (la - minLat) / (maxLat - minLat) * height],
  };
}

function forecastPoint(row, day) {
  const parts = String(row[day] || '').split(',');
  if (parts.length !== 2) return null;
  const lat = Number(parts[0]), lon = Number(parts[1]);
  return Number.isFinite(lat) && Number.isFinite(lon) ? [lat, lon] : null;
}

function drawRadarMap(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 360;
  const height = Math.max(280, Math.round(canvas.clientHeight || 360));
  canvas.width = width * dpr; canvas.height = height * dpr;
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, width, height);
  const base = radarBase(width, height);
  if (!base) { canvas._project = null; return; }
  const project = ([la, lo]) => {
    const [x, y] = base.project([la, lo]);
    return [width / 2 + (x - width / 2) * mapView.scale + mapView.dx,
            height / 2 + (y - height / 2) * mapView.scale + mapView.dy];
  };
  canvas._project = project;
  const sets = radarSets();
  const shown = radarShown();
  const traffic = sets.traffic.filter(s => (s.age_minutes ?? 99999) <= 720);
  const boxes = [];
  const pxPerKm = height / ((base.maxLat - base.minLat) * 111.32 || 1);
  const toGeo = (x, y) => [base.minLat + (1 - y / height) * (base.maxLat - base.minLat),
                           base.minLon + (x / width) * (base.maxLon - base.minLon)];

  // сетка с подписями широты и долготы
  g.strokeStyle = showSchematic ? 'rgba(48,64,80,.65)' : 'rgba(70,110,145,.20)';
  g.lineWidth = 1;
  g.fillStyle = showSchematic ? '#607080' : '#2f5b6e';
  g.font = (showSchematic ? '8px' : '9px') + ' Consolas, monospace';
  const steps = showSchematic ? 4 : 6;
  for (let i = 0; i <= steps; i++) {
    const y = i * height / steps, x = i * width / steps;
    g.beginPath(); g.moveTo(0, y); g.lineTo(width, y); g.stroke();
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, height); g.stroke();
    if (i && i < steps && (!showSchematic || i === steps / 2)) {
      g.fillText(toGeo(0, y)[0].toFixed(1), 3, y - 3);
      g.fillText(toGeo(x, 0)[1].toFixed(1), x + 3, height - 5);
    }
  }

  // порты: пунктирный радиус и подпись, как на настольном радаре
  (ports || []).forEach(p => {
    if (!p.lat || !p.lon) return;
    const [x, y] = project([Number(p.lat), Number(p.lon)]);
    if (x < -10 || x > width + 10 || y < -10 || y > height + 10) return;
    const radius = Math.max(4, Number(p.radius_km || 5) * pxPerKm * mapView.scale);
    const name = String(p.name || '');
    if (showSchematic) {
      g.strokeStyle = '#1c6b42'; g.lineWidth = 2;
      g.beginPath(); g.arc(x, y, radius, 0, Math.PI * 2); g.stroke();
      g.font = '600 8px Consolas, monospace';
      const w = g.measureText(name).width;
      const box = [x - w / 2, y + radius + 1, w, 10];
      if (!freeSpace(box, boxes)) return;
      boxes.push(box);
      g.fillStyle = '#607080'; g.textAlign = 'center';
      g.fillText(name, x, y + radius + 9);
      g.textAlign = 'start';
    } else {
      g.strokeStyle = 'rgba(31,122,82,.9)'; g.lineWidth = 1;
      g.setLineDash([3, 3]);
      g.beginPath(); g.arc(x, y, radius, 0, Math.PI * 2); g.stroke();
      g.setLineDash([]);
      g.fillStyle = '#35d08a'; g.fillRect(x - 2, y - 2, 4, 4);
      g.font = '600 10px Consolas, monospace';
      const w = g.measureText(name).width;
      const box = [x - w / 2, y - radius - 18, w, 12];
      if (!freeSpace(box, boxes)) return;
      boxes.push(box);
      g.fillStyle = '#7fe0b3'; g.textAlign = 'center';
      g.fillText(name, x, y - radius - 8);
      g.textAlign = 'start';
    }
  });

  // трек выбранного судна
  if (showTracks && tracks && radarSelected && tracks[radarSelected]) {
    const points = tracks[radarSelected];
    if (points.length > 1) {
      g.strokeStyle = '#c8a24a'; g.lineWidth = 1.4;
      g.beginPath();
      points.forEach((p, i) => { const [x, y] = project(p); i ? g.lineTo(x, y) : g.moveTo(x, y); });
      g.stroke();
    }
  }

  // прогноз — только по выбранному судну, как на настольном радаре
  if (showForecast && forecastPts && forecastPts.rows && radarSelected) {
    const row = forecastPts.rows.find(r => String(r.mmsi) === String(radarSelected));
    const ship = sets.ours.find(s => String(s.mmsi) === String(radarSelected));
    const day1 = row ? forecastPoint(row, 'day1') : null;
    const day2 = row ? forecastPoint(row, 'day2') : null;
    if (ship && day1) {
      const color = row.guessed ? '#ffc247' : '#39d3ff';
      g.strokeStyle = color; g.lineWidth = 1.8; g.setLineDash([4, 4]);
      const [x0, y0] = project([ship.lat, ship.lon]);
      const [x1, y1] = project(day1);
      g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1);
      if (day2) { const [x2, y2] = project(day2); g.lineTo(x2, y2); }
      g.stroke(); g.setLineDash([]);
      g.fillStyle = color;
      g.font = '9px Consolas, monospace';
      [[day1, 'через день'], [day2, 'через 2 дня']].forEach(([pt, label]) => {
        if (!pt) return;
        const [x, y] = project(pt);
        g.beginPath(); g.arc(x, y, 3, 0, Math.PI * 2); g.stroke();
        g.fillText(label, x + 6, y - 5);
      });
      const [xe, ye] = project(day2 || day1);
      g.fillText(String(row.eta_text || ''), xe + 6, ye + 12);
    }
  }

  // остальной трафик района — мелкие точки
  traffic.forEach(s => {
    const [x, y] = project([s.lat, s.lon]);
    if (x < 0 || x > width || y < 0 || y > height) return;
    g.fillStyle = Number(s.sog) > 0.5 ? '#3f6f88' : '#4a4f52';
    g.beginPath(); g.arc(x, y, 1.4, 0, Math.PI * 2); g.fill();
  });

  // наши суда
  let faded = 0, drawn = 0;
  shown.forEach(s => {
    const fresh = !shipIsStale(s);
    if (!fresh) faded++;
    const [x, y] = project([Number(s.lat), Number(s.lon)]);
    if (x < -20 || x > width + 20 || y < -20 || y > height + 20) return;
    drawn++;
    const selected = String(s.mmsi) === String(radarSelected);
    const color = !fresh ? '#5d7786'
      : showSchematic ? '#c62828' : (s.target ? '#ff3b30' : '#7fa8bf');
    const course = s.cog ?? s.heading;
    if (course !== null && course !== undefined) {
      const angle = Number(course) * Math.PI / 180;
      g.strokeStyle = color; g.lineWidth = 1.2;
      g.beginPath(); g.moveTo(x, y);
      g.lineTo(x + Math.sin(angle) * 14, y - Math.cos(angle) * 14);
      g.stroke();
    }
    g.fillStyle = color;
    g.beginPath(); g.arc(x, y, s.target ? 4.5 : 3, 0, Math.PI * 2); g.fill();
    if (selected) {
      g.strokeStyle = '#ffffff'; g.lineWidth = 2;
      g.beginPath(); g.arc(x, y, 11, 0, Math.PI * 2); g.stroke();
    }
    const age = s.age_minutes ?? 0;
    if (!fresh) {
      g.fillStyle = '#5d7786'; g.font = '9px Consolas, monospace';
      g.fillText(Math.round(age / 60) + ' ч назад', x + 7, y + 10);
    }
    if (selected) {
      const parts = [s.name || s.mmsi];
      if (s.sog !== null && s.sog !== undefined) parts.push(Number(s.sog).toFixed(1) + ' уз');
      if (s.destination) parts.push(s.destination);
      const label = parts.join(' · ');
      g.font = '600 10px Consolas, monospace';
      const w = g.measureText(label).width;
      const bx = Math.max(10, Math.min(width - w - 6, x + 9));
      const by = Math.max(18, y - 16);
      g.fillStyle = 'rgba(8,19,30,.88)'; g.fillRect(bx - 3, by - 11, w + 6, 15);
      g.fillStyle = '#ffffff'; g.fillText(label, bx, by);
      if (!fresh) {
        g.fillStyle = '#ffd166'; g.font = '9px Consolas, monospace';
        g.fillText('позиция ' + (age / 60).toFixed(1) + ' ч назад', bx, by + 20);
      }
    } else if (s.name && !showSchematic) {
      g.font = '9px -apple-system, Segoe UI, sans-serif';
      const text = String(s.name).slice(0, 18);
      const w = g.measureText(text).width;
      const box = [x + 7, y - 13, w, 11];
      if (freeSpace(box, boxes)) {
        boxes.push(box);
        g.fillStyle = '#d7e6ee';
        g.fillText(text, x + 7, y - 4);
      }
    }
  });

  // легенда со счётчиками — как в настольной версии
  const newest = shown.reduce((min, s) => Math.min(min, s.age_minutes ?? min), Infinity);
  const withoutPos = Math.max(0, (Number(ships && ships.total) || 0) - sets.all.length);
  const lines = [
    'на экране: ' + drawn + ' из ' + shown.length + ' · целей: '
      + shown.filter(s => s.target).length,
    'свежих: ' + (drawn - faded) + ' · приглушены (старый сигнал): ' + faded,
  ];
  if (withoutPos) lines.push('без позиции (только в списках): ' + withoutPos);
  lines.push('трафик района (точки): ' + traffic.length);
  if (Number.isFinite(newest)) {
    lines.push('последний сигнал AIS в районе: ' + (newest < 60 ? newest + ' мин назад'
      : (newest / 60).toFixed(1) + ' ч назад'));
  }
  lines.push('порог свежести: ' + staleLabelText() + ' · масштаб: '
    + mapView.scale.toFixed(2));
  lines.push('район: ' + base.centerLat.toFixed(2) + ', ' + base.centerLon.toFixed(2));
  g.font = '9px Consolas, monospace';
  const legendW = Math.min(width - 20, 290), legendH = lines.length * 12 + 10;
  g.fillStyle = 'rgba(10,26,36,.94)'; g.fillRect(10, 10, legendW, legendH);
  g.strokeStyle = '#1d3d4f'; g.lineWidth = 1; g.strokeRect(10, 10, legendW, legendH);
  g.fillStyle = '#9fc4d6';
  lines.forEach((line, i) => g.fillText(line, 18, 25 + i * 12));
}

async function ensureRadarLayers() {
  let loaded = false;
  if (showTracks && !tracks) {
    try { tracks = await api('/api/tracks?hours=24'); } catch (e) { tracks = {}; }
    loaded = true;
  }
  if (showForecast && !forecastPts) {
    try { forecastPts = await api('/api/forecast_all'); } catch (e) { forecastPts = {rows: []}; }
    loaded = true;
  }
  if (loaded) drawRadar();
}

function pickShip(canvas, ev) {
  if (!canvas._project) return;
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  let best = null, bestD = 18;
  mapShips.forEach(s => {
    const [sx, sy] = canvas._project([s.lat, s.lon]);
    const d = Math.hypot(sx - x, sy - y);
    if (d < bestD) { best = s; bestD = d; }
  });
  if (!best) {
    if (radarSelected) {
      radarSelected = null;
      mapShips = radarShown();
      drawRadar();
      renderRadarList();
    }
    return;
  }
  radarSelected = best.mmsi;
  mapShips = radarShown();
  drawRadar();
  renderRadarList();
  ensureRadarLayers();
  openVessel(best.mmsi);
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
  view.innerHTML = `<div class="bar"><button data-act="refresh">⟳ Обновить</button></div>
    <div class="note">${esc(seaRows.note || '')}</div>
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
  view.innerHTML = `<div class="bar"><button data-act="refresh">⟳ Пересчитать</button></div>
    <div class="note">${esc(planRows.note || '')}</div>`
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
  view.innerHTML = `<div class="bar"><button data-act="refresh">⟳ Пересчитать</button></div>
    <div class="note">${esc(allRows.note || '')}</div>`
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
  view.innerHTML = `<div class="bar"><button data-act="refresh">⟳ Обновить</button></div>
    <div class="chips" id="visitChips">
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
  view.innerHTML = `<div class="bar"><button data-act="refresh">⟳ Обновить</button></div>
    <div class="note">${esc(linkRows.note || '')}</div>`
    + rows.map(r => `<article class="card">
      <div class="row"><div><div class="name" style="font-size:15px">${esc(r.title)}</div>
      <div class="small">${esc(r.detail || '')}</div></div>
      <span class="tag ${r.state === 'on' ? 'ok' : r.state === 'off' ? 'bad' : 'warn'}">${esc(r.state_text)}</span></div>
      </article>`).join('')
    + `<div class="bar"><button class="primary" data-act="restart">Перезапустить сбор</button></div>`
    + `<div class="hint">База: ${esc(linkRows.database || '')}<br>Версия мобильной: ${esc(linkRows.version || '')} · версия программы на компьютере: ${esc(linkRows.desktop_version || '—')}</div>`;
}

async function restartCore(button) {
  await busy(button, async () => {
    try { await post('/api/restart'); button.textContent = 'Сбор перезапущен'; }
    catch (e) { button.textContent = 'Не удалось'; }
  });
  setTimeout(() => { if (button) button.textContent = 'Перезапустить сбор'; }, 2200);
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
function renderMore() {
  view.innerHTML = `<div class="note">Всё, что в настольной версии разложено по отдельным вкладкам.</div>`
    + MORE.map(([name, title, detail]) => `<article class="card tap" data-more="${name}">
      <div><div class="name" style="font-size:15px">${esc(title)}</div>
      <div class="small">${esc(detail)}</div></div><div class="sog">›</div></article>`).join('');
}

/* --------------------------------------------------- трафик района */
const TRAFFIC_SORTS = [['age', 'Свежие'], ['path', 'Длинный путь'],
                       ['speed', 'Быстрый ход'], ['near', 'Ближе к порту']];
async function renderTraffic() {
  if (!trafficRows) {
    view.innerHTML = '<div class="empty">Считаю трафик района…</div>';
    try { trafficRows = await api('/api/traffic'); } catch (e) {
      view.innerHTML = head('Трафик района') + '<div class="empty">Не удалось получить список</div>'; return; }
  }
  if (tab !== 'traffic') return;
  const rows = (trafficRows.rows || []).slice();
  const moving = rows.filter(r => Number(r.sog) >= 0.5).length;
  const sorters = {
    age: (a, b) => (a.age_minutes ?? 1e9) - (b.age_minutes ?? 1e9),
    path: (a, b) => (b.path_km || 0) - (a.path_km || 0),
    speed: (a, b) => (b.avg_knots || 0) - (a.avg_knots || 0),
    near: (a, b) => (a.near_km ?? 1e9) - (b.near_km ?? 1e9),
  };
  rows.sort(sorters[trafficSort] || sorters.age);
  const sortTitle = (TRAFFIC_SORTS.find(s => s[0] === trafficSort) || ['', 'свежие'])[1];
  view.innerHTML = head('Трафик района', '<button class="iconbtn" data-act="refresh">⟳</button>')
    + `<div class="note">${esc(trafficRows.note || '')}</div>`
    + `<div class="chips">${TRAFFIC_SORTS.map(([key, title]) =>
        `<button class="chip ${trafficSort === key ? 'on' : ''}" data-traffic-sort="${key}">${esc(title)}</button>`).join('')}</div>`
    + `<div class="hint">В районе: ${trafficRows.total ?? rows.length} · в ходу: ${moving} · на экране: ${rows.length} · порядок: ${esc(sortTitle.toLowerCase())}</div>`
    + (rows.length ? rows.map(r => `<article class="card" data-mmsi="${esc(r.mmsi)}">
      <div class="row"><div><div class="name">${esc(r.name || 'Без имени')}</div>
      <div class="small">MMSI ${esc(r.mmsi)}${r.type_label ? ' · ' + esc(r.type_label) : ''}${r.destination ? ' · → ' + esc(r.destination) : ''}</div></div>
      <div class="sog">${r.sog == null ? '—' : Number(r.sog).toFixed(1)} уз</div></div>
      <div class="meta">
        <span class="tag ${Number(r.sog) >= 0.5 ? 'ok' : ''}">${Number(r.sog) >= 0.5 ? 'в ходу' : 'стоит'}</span>
        ${r.near_km != null ? `<span class="tag">${esc(r.near_port)} · ${r.near_km} км</span>` : ''}
        <span class="tag">путь 24 ч: ${r.path_km ?? 0} км</span>
        ${r.avg_knots != null ? `<span class="tag">ср. ход ${r.avg_knots} уз</span>` : ''}
        ${r.origin ? `<span class="tag">откуда ${esc(r.origin)}</span>` : ''}
        ${r.cog != null ? `<span class="tag">курс ${esc(r.cog)}°</span>` : ''}
        <span class="tag ${(r.age_minutes ?? 0) > 180 ? 'bad' : ''}">${ago(r.age_minutes)}</span>
      </div></article>`).join('') : '<div class="empty">В районе нет судов с позицией</div>');
}

/* ------------------------------------------------------ ожидаемые */
const EXPECTED_FILTERS = [['all', 'Все'], ['fresh', 'Актуальные'], ['stale', 'Устаревшие']];
async function renderExpected() {
  if (!expectedRows) {
    view.innerHTML = '<div class="empty">Загружаю…</div>';
    try { expectedRows = await api('/api/expected'); } catch (e) {
      view.innerHTML = head('Ожидаемые') + '<div class="empty">На телефоне этот экран работает в автономной версии</div>'; return; }
  }
  if (tab !== 'expected') return;
  const all = expectedRows.rows || [];
  const staleCount = all.filter(r => r.stale).length;
  const rows = expectedFilter === 'stale' ? all.filter(r => r.stale)
    : expectedFilter === 'fresh' ? all.filter(r => !r.stale) : all;
  view.innerHTML = head('Ожидаемые', '<button class="iconbtn" data-act="refresh">⟳</button>')
    + `<div class="note">${esc(expectedRows.note || '')}</div>`
    + (expectedRows.stale ? `<div class="note">Страницы сейчас недоступны: показаны прошлые данные${expectedRows.stale_reason ? ' (' + esc(expectedRows.stale_reason) + ')' : ''}.</div>` : '')
    + `<div class="chips">${EXPECTED_FILTERS.map(([key, title]) => {
        const count = key === 'all' ? all.length : key === 'stale' ? staleCount : all.length - staleCount;
        return `<button class="chip ${expectedFilter === key ? 'on' : ''}" data-expected-filter="${key}">${esc(title)} · ${count}</button>`;
      }).join('')}</div>`
    + `<div class="bar"><button class="primary" data-act="expected-refresh">${expectedRows.loading ? 'Обновляю…' : 'Обновить с портов'}</button></div>`
    + `<div class="hint">Обновлено: ${esc((expectedRows.updated || '').slice(0, 16).replace('T', ' ') || 'ещё не было')} · автообновление раз в минуту`
      + (expectedRows.hidden ? ` · скрыто ${expectedRows.hidden} (ушли в сторону)` : '')
      + (expectedRows.added ? ` · добавлено ${expectedRows.added} по курсу` : '') + `</div>`
    + (rows.length ? rows.map(r => `<article class="card" data-mmsi="${esc(r.mmsi || '')}">
      <div class="row"><div><div class="name" style="font-size:15px">${esc(r.name || 'Без имени')}</div>
      <div class="small">${esc(r.port)}${r.type ? ' · ' + esc(r.type) : ''}</div></div>
      <div class="sog" style="font-size:12px">${esc(r.eta || 'ETA —')}</div></div>
      <div class="meta">
        ${r.last_heard ? `<span class="tag">сигнал ${esc(r.last_heard)}</span>` : ''}
        ${r.note ? `<span class="tag">${esc(r.note)}</span>` : ''}
        <span class="tag ${r.stale ? 'warn' : 'ok'}">${r.stale ? 'устарело' : 'ожидается'}</span>
        ${r.mmsi ? `<span class="tag">MMSI ${esc(r.mmsi)}</span>` : ''}
      </div></article>`).join('') : '<div class="empty">Пока никто не заявил наш порт</div>');
}

/* ---------------------------------------------------------- алерты */
const ALERT_FILTERS = [['all', 'Все'], ['ok', 'Доставлены'], ['bad', 'Ошибки']];
async function renderAlerts() {
  if (!alertsRows) {
    view.innerHTML = '<div class="empty">Загружаю…</div>';
    try { alertsRows = await api('/api/alerts'); } catch (e) {
      view.innerHTML = head('Алерты') + '<div class="empty">Не удалось получить журнал</div>'; return; }
  }
  if (tab !== 'alerts') return;
  const all = alertsRows.rows || [];
  const badCount = all.filter(r => !r.ok).length;
  const rows = alertFilter === 'ok' ? all.filter(r => r.ok)
    : alertFilter === 'bad' ? all.filter(r => !r.ok) : all;
  view.innerHTML = head('Алерты', '<button class="iconbtn" data-act="refresh">⟳</button>')
    + `<div class="note">${esc(alertsRows.note || '')}</div>`
    + `<div class="chips">${ALERT_FILTERS.map(([key, title]) => {
        const count = key === 'all' ? all.length : key === 'bad' ? badCount : all.length - badCount;
        return `<button class="chip ${alertFilter === key ? 'on' : ''}" data-alert-filter="${key}">${esc(title)} · ${count}</button>`;
      }).join('')}</div>`
    + `<div class="hint">Отправлено: ${all.length - badCount} · с ошибкой: ${badCount}</div>`
    + (rows.length ? rows.map(r => `<article class="card">
      <div class="row"><div><div class="name" style="font-size:15px">${esc(r.name || r.mmsi || '—')}</div>
      <div class="small">${esc(r.origin || '—')} → ${esc(r.destination || '—')}</div></div>
      <span class="tag ${r.ok ? 'ok' : 'bad'}">${r.ok ? 'отправлен' : 'ошибка'}</span></div>
      <div class="meta"><span class="tag">${esc((r.sent_at || '').slice(0, 16).replace('T', ' '))}</span>
        ${r.mmsi ? `<span class="tag">MMSI ${esc(r.mmsi)}</span>` : ''}
        ${r.error ? `<span class="tag bad">${esc(r.error)}</span>` : ''}</div>
      </article>`).join('') : '<div class="empty">Журнал пуст</div>');
}

/* --------------------------------------------------------- сервисы */
const SERVICE_LINKS = [
  ['VesselFinder', 'https://www.vesselfinder.com/'],
  ['MarineTraffic', 'https://www.marinetraffic.com/'],
  ['MyShipTracking', 'https://www.myshiptracking.com/'],
  ['AIS Marine Traffic', 'https://www.aismarinetraffic.com/'],
  ['Equasis (тип, дедвейт)', 'https://www.equasis.org/'],
  ['IMO GISIS (реестр)', 'https://gisis.imo.org/'],
  ['Open Waters (карта и API)', 'https://openwaters.io/ais/'],
];
function sourceTag(result) {
  if (!result) return '<span class="tag">не проверялся</span>';
  const state = result.warn ? 'warn' : result.ok ? 'ok' : 'bad';
  const text = result.warn ? 'только в браузере' : result.ok ? 'работает' : 'не отвечает';
  return `<span class="tag ${state}">${text}</span>`;
}
async function renderServices() {
  if (!servicesRows) {
    view.innerHTML = '<div class="empty">Загружаю…</div>';
    try { servicesRows = await api('/api/services'); } catch (e) {
      view.innerHTML = head('Сервисы') + '<div class="empty">На телефоне этот экран работает в автономной версии</div>'; return; }
  }
  if (tab !== 'services') return;
  if (sourcesRows === null) {
    api('/api/sources').then(data => {
      sourcesRows = data;
      if (tab === 'services') renderServices();
    }).catch(() => { sourcesRows = {rows: [], unavailable: true}; });
  }
  const rows = servicesRows.rows || [];
  const online = sourcesRows && !sourcesRows.unavailable ? (sourcesRows.rows || []) : null;
  view.innerHTML = head('Сервисы', '<button class="iconbtn" data-act="refresh">⟳</button>')
    + `<div class="note">${esc(servicesRows.note || '')}</div>`
    + `<div class="hint">Сбор данных: ${servicesRows.running ? 'запущен' : 'остановлен'} · версия ${esc(servicesRows.version || '')}</div>`
    + rows.map(r => `<article class="card">
      <div class="row"><div><div class="name" style="font-size:15px">${esc(r.title)}</div>
      <div class="small">${esc(r.detail || '')}${r.needs_key ? (r.has_key ? ' · ключ есть' : ' · ключа нет') : ''}</div></div>
      <span class="tag ${r.state === 'on' ? 'ok' : r.state === 'off' ? 'bad' : 'warn'}">${esc(r.state_text)}</span></div>
      </article>`).join('')
    + `<div class="hint" style="margin-top:12px">Онлайн-сервисы для проверки судна</div>`
    + `<div class="links">${SERVICE_LINKS.map(([title, url]) =>
        `<a href="${url}" target="_blank" rel="noopener">${esc(title)}</a>`).join('')}</div>`
    + `<div class="hint" style="margin-top:14px">Источники без ключа — проверка вручную${sourcesRows && sourcesRows.updated ? ' · обновлено ' + esc(String(sourcesRows.updated).slice(0, 16).replace('T', ' ')) : ''}</div>`
    + (online === null ? '<div class="small">Проверка источников доступна в автономной версии.</div>'
      : online.map(s => `<article class="card">
        <div class="row"><div><div class="name" style="font-size:15px">${esc(s.title)}</div>
        <div class="small">${esc(s.how || '')}</div></div>${sourceTag(s.result)}</div>
        ${s.result ? `<div class="meta">
          ${s.result.detail ? `<span class="tag">${esc(s.result.detail)}</span>` : ''}
          ${s.result.seconds != null ? `<span class="tag">${esc(s.result.seconds)} с</span>` : ''}
          ${s.result.checked ? `<span class="tag">${esc(String(s.result.checked).slice(0, 16).replace('T', ' '))}</span>` : ''}
        </div>` : ''}
        <div class="bar" style="margin:8px 0 0">
          <button data-source-check="${esc(s.key)}">Проверить</button>
          ${s.url ? `<a class="chip" style="text-decoration:none" href="${esc(s.url)}" target="_blank" rel="noopener">Открыть сайт</a>` : ''}
        </div></article>`).join(''))
    + `<div class="bar"><button class="primary" data-act="restart">Перезапустить сбор</button></div>`;
}

/* -------------------------------------------------------- настройки */
async function renderSettings() {
  if (!settingsData) {
    view.innerHTML = '<div class="empty">Загружаю настройки…</div>';
    try { settingsData = await api('/api/settings'); } catch (e) {
      view.innerHTML = head('Настройки') + '<div class="empty">На телефоне этот экран работает в автономной версии</div>'; return; }
  }
  if (tab !== 'settings') return;
  const ports = settingsData.ports || [], numbers = settingsData.numbers || [],
        sources = settingsData.sources || [];
  view.innerHTML = head('Настройки', '<button class="iconbtn" data-act="refresh">⟳</button>')
    + `<form id="setForm">
      <div class="card">
        <div class="name" style="font-size:15px;margin-bottom:4px">Отслеживать суда, идущие в порты</div>
        <div class="small" style="margin-bottom:6px">Если не отмечено ни одного — берутся все наши порты.</div>
        ${ports.map(p => `<label class="check"><input type="checkbox" name="port" value="${esc(p.name)}" ${p.on ? 'checked' : ''}><span>${esc(p.label || p.name)}</span></label>`).join('')}
      </div>
      <div class="card">
        <div class="name" style="font-size:15px;margin-bottom:4px">Только из портов отправления</div>
        <div class="small" style="margin-bottom:6px">Если не отмечено ни одного — ограничения нет.</div>
        ${ports.map(p => `<label class="check"><input type="checkbox" name="origin" value="${esc(p.name)}" ${p.origin_on ? 'checked' : ''}><span>${esc(p.label || p.name)}</span></label>`).join('')}
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
      <div class="bar"><button class="primary" type="submit">Сохранить и перезапустить</button></div>
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
      view.innerHTML = head('Журнал') + '<div class="empty">На телефоне этот экран работает в автономной версии</div>'; return; }
  }
  if (tab !== 'log') return;
  const rows = logRows.rows || [];
  view.innerHTML = head('Журнал', '<button class="iconbtn" data-act="refresh">⟳</button>')
    + `<div class="hint">${esc(logRows.path || '')} · строк ${rows.length}</div>`
    + `<div class="bar"><button data-act="log-clear">Очистить журнал</button></div>`
    + `<pre class="log">${esc(rows.join(String.fromCharCode(10)) || 'журнал пуст')}</pre>`;
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
  const active = SUBTABS.includes(name) ? 'more' : name;
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === active));
  search.style.display = name === 'radar' ? 'block' : 'none';
  render();
}
document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
search.addEventListener('input', () => { if (tab === 'radar') renderRadarList(); });

refreshAll.addEventListener('click', () => busy(refreshAll, async () => {
  clearCaches(); clearMapLayers(); await loadShips(); render();
  ensureRadarLayers();
}, '…'));

view.addEventListener('click', async ev => {
  const more = ev.target.closest('[data-more]');
  if (more) { setTab(more.dataset.more); return; }
  const back = ev.target.closest('[data-back]');
  if (back) { setTab('more'); return; }
  const action = ev.target.closest('[data-act]');
  if (action) {
    const act = action.dataset.act;
    if (act === 'refresh') {
      await busy(action, async () => {
        clearCaches(); clearMapLayers(); await loadShips(); render();
        ensureRadarLayers();
      }, '…');
      return;
    }
    if (act === 'log-clear') {
      await busy(action, async () => {
        try {
          const result = await post('/api/log/clear');
          if (!result.ok) throw new Error(result.error || 'не очистилось');
        } catch (e) { statusEl.textContent = 'Журнал не очищен: ' + e.message; return; }
        logRows = null;
      }, '…');
      render();
      return;
    }
    if (act === 'restart') { await restartCore(action); clearCaches(); return; }
    if (act === 'expected-refresh') {
      await busy(action, async () => {
        await post('/api/expected/refresh');
        action.textContent = 'Запрашиваю порты…';
        setTimeout(async () => {
          expectedRows = null;
          try { expectedRows = await api('/api/expected'); } catch (e) {}
          if (tab === 'expected') renderExpected();
        }, 12000);
      });
      return;
    }
    if (act === 'fit') {
      fitMode = 'vessels';
      mapView = {scale: 1, dx: 0, dy: 0};
      renderRadar();
      return;
    }
    return;
  }
  const trafficSortBtn = ev.target.closest('[data-traffic-sort]');
  if (trafficSortBtn) { trafficSort = trafficSortBtn.dataset.trafficSort; renderTraffic(); return; }
  const expectedBtn = ev.target.closest('[data-expected-filter]');
  if (expectedBtn) { expectedFilter = expectedBtn.dataset.expectedFilter; renderExpected(); return; }
  const alertBtn = ev.target.closest('[data-alert-filter]');
  if (alertBtn) { alertFilter = alertBtn.dataset.alertFilter; renderAlerts(); return; }
  const sourceBtn = ev.target.closest('[data-source-check]');
  if (sourceBtn) {
    await busy(sourceBtn, async () => {
      try {
        await post('/api/sources/check',
                   new URLSearchParams({key: sourceBtn.dataset.sourceCheck}));
      } catch (e) { statusEl.textContent = 'Проверка не запустилась: ' + e.message; }
      setTimeout(async () => {
        sourcesRows = null;
        if (tab === 'services') await renderServices();
      }, 8000);
    }, 'Проверяю…');
    return;
  }
  const card = ev.target.closest('[data-mmsi]');
  if (card && card.dataset.mmsi) openVessel(card.dataset.mmsi);
});

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
      <div class="bar" style="margin-top:12px"><button data-vessel-refresh="${esc(v.mmsi)}">Обновить карточку</button></div>
      <div class="hint" id="cardInfo">Обновление запрашивает судно у источников и пишет новые данные в базу.</div>
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
sheet.addEventListener('click', async e => {
  if (e.target.dataset.close !== undefined) { sheet.classList.remove('open'); return; }
  const refresh = e.target.closest('[data-vessel-refresh]');
  if (!refresh) return;
  const info = document.getElementById('cardInfo');
  refresh.disabled = true; refresh.textContent = 'Обновляю…';
  try {
    const result = await post('/api/vessel/refresh',
                              new URLSearchParams({mmsi: refresh.dataset.vesselRefresh}));
    if (info) info.textContent = result.text || result.error
      || (result.ok ? 'Карточка обновлена.' : 'Обновить не удалось.');
    if (result.ok) { ships = null; trafficRows = null; allRows = null; }
  } catch (err) {
    if (info) info.textContent = 'Ошибка: ' + err.message;
  }
  refresh.disabled = false; refresh.textContent = 'Обновить карточку';
});

search.style.display = 'block';
if (location.hash) {
  const name = location.hash.slice(1);
  if (['radar', 'sea', 'plan', 'all', 'visits', 'link', 'more', ...SUBTABS].includes(name)) setTab(name);
}
loadShips();
setInterval(() => { clearCaches(); loadShips(); }, 30000);
/* «Ожидаемые» — как настольная вкладка: файл AIS-catcher перечитывается сам. */
setInterval(() => {
  if (tab !== 'expected' || (expectedRows && expectedRows.loading)) return;
  expectedRows = null;
  renderExpected();
}, 60000);
</script>
</body>
</html>
"""
