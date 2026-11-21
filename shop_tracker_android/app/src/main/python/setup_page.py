"""Страница настроек автономной версии (ключи вводятся на телефоне)."""

SETUP_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1622">
<title>Ship Tracker — настройка</title>
<style>
:root { color-scheme: dark; --bg:#0b1622; --card:#10202e; --line:#1d3144; --text:#e8f1fa; --dim:#7d95ad; --accent:#4aa8ff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:calc(16px + env(safe-area-inset-top)) 14px calc(16px + env(safe-area-inset-bottom)); }
h1 { font-size:20px; margin:0 0 4px; }
p.sub { color:var(--dim); font-size:13px; margin:0 0 16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:14px; margin-bottom:12px; }
.head { display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin:12px 0 6px; }
.head:first-child { margin-top:0; }
label { display:block; font-size:13px; color:#a9c2d8; }
input { width:100%; padding:12px 14px; font-size:16px; border-radius:12px; border:1px solid var(--line);
  background:#12222f; color:var(--text); outline:none; }
input::placeholder { color:#5d7b93; }
a.get { font-size:12px; color:var(--accent); text-decoration:none; border:1px solid var(--line);
  border-radius:8px; padding:3px 9px; white-space:nowrap; }
button { width:100%; margin-top:14px; padding:14px; font-size:16px; font-weight:600; border:0; border-radius:14px;
  background:var(--accent); color:#04121f; }
.note { font-size:12px; color:#9db6cc; background:#10202e; border:1px solid var(--line);
  border-radius:12px; padding:10px 12px; margin:0 0 12px; }
.tip { font-size:12px; color:var(--dim); margin:6px 0 0; }
.ok { color:#8ee0ab; font-size:13px; margin-top:10px; }
.err { color:#ff9c94; font-size:13px; margin-top:10px; }
</style>
</head>
<body>
<h1>Ship Tracker</h1>
<p class="sub">Автономная версия: данные собираются на телефоне, компьютер не нужен.</p>

<div class="note">Здесь нет чужих ключей — поля пустые. Введите свои: они хранятся
только в памяти приложения на этом телефоне и никуда не отправляются, кроме
самих сервисов. AISStream обязателен — без него нет живых позиций судов.
Остальные источники (Maritime Optima, ShipAtlas, MarineLink, Goradar,
Ship Radar 24, ShipFinder, AIS-catcher) работают без ключа.</div>

<form class="card" id="form">
  <div class="head">
    <label for="api_key">Ключ AISStream (обязателен)</label>
    <a class="get" href="https://aisstream.io/" target="_blank" rel="noopener">получить ключ</a>
  </div>
  <input id="api_key" name="api_key" autocomplete="off" spellcheck="false"
         placeholder="вставьте ключ с aisstream.io">

  <div class="head">
    <label for="gfw_key">Токен Global Fishing Watch (необязательно)</label>
    <a class="get" href="https://globalfishingwatch.org/our-apis/tokens" target="_blank" rel="noopener">получить токен</a>
  </div>
  <input id="gfw_key" name="gfw_key" autocomplete="off" spellcheck="false"
         placeholder="если нужны разрывы AIS и заходы в порты">

  <div class="head">
    <label for="telegram_token">Токен Telegram-бота (необязательно)</label>
    <a class="get" href="https://t.me/BotFather" target="_blank" rel="noopener">создать бота</a>
  </div>
  <input id="telegram_token" name="telegram_token" autocomplete="off" spellcheck="false"
         placeholder="токен от @BotFather">

  <div class="head">
    <label for="telegram_chat_id">Чат для уведомлений (необязательно)</label>
    <a class="get" href="https://t.me/userinfobot" target="_blank" rel="noopener">узнать chat_id</a>
  </div>
  <input id="telegram_chat_id" name="telegram_chat_id" autocomplete="off" spellcheck="false"
         placeholder="например 6518041859">
  <div class="tip">Свой chat_id бот показывает командой /id.</div>

  <button type="submit">Сохранить и запустить</button>
  <div class="ok" id="ok" hidden>Сохранено, сбор данных запущен.</div>
  <div class="err" id="err" hidden></div>
</form>

<script>
const form = document.getElementById('form');
form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const ok = document.getElementById('ok');
  const err = document.getElementById('err');
  ok.hidden = true; err.hidden = false;
  err.hidden = true;
  const body = new URLSearchParams(new FormData(form));
  try {
    const response = await fetch('/api/setup', { method: 'POST', body });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || 'не сохранилось');
    ok.hidden = false;
    setTimeout(() => { location.href = '/'; }, 900);
  } catch (e) {
    err.textContent = 'Ошибка: ' + e.message;
    err.hidden = false;
  }
});
</script>
</body>
</html>
"""


def render() -> str:
    """Экран подключений: поля всегда пустые, ключи вводит владелец телефона."""
    return SETUP_PAGE
