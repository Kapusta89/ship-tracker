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
label { display:block; font-size:13px; color:#a9c2d8; margin:10px 0 6px; }
input { width:100%; padding:12px 14px; font-size:16px; border-radius:12px; border:1px solid var(--line);
  background:#12222f; color:var(--text); outline:none; }
button { width:100%; margin-top:14px; padding:14px; font-size:16px; font-weight:600; border:0; border-radius:14px;
  background:var(--accent); color:#04121f; }
.note { font-size:12px; color:#9db6cc; background:#10202e; border:1px solid var(--line);
  border-radius:12px; padding:10px 12px; margin:0 0 12px; }
.ok { color:#8ee0ab; font-size:13px; margin-top:10px; }
.err { color:#ff9c94; font-size:13px; margin-top:10px; }
</style>
</head>
<body>
<h1>Ship Tracker</h1>
<p class="sub">Автономная версия: данные собираются на телефоне, компьютер не нужен.</p>

<div class="note">Ключи хранятся только в памяти приложения на этом телефоне.
AISStream обязателен — без него нет живых позиций судов. Остальные источники
(Maritime Optima, ShipAtlas, MarineLink, Goradar, Ship Radar 24, ShipFinder,
AIS-catcher) работают без ключа.</div>

<form class="card" id="form">
  <label for="api_key">Ключ AISStream (обязателен)</label>
  <input id="api_key" name="api_key" autocomplete="off" spellcheck="false"
         placeholder="вставьте ключ с aisstream.io" value="__API_KEY__">

  <label for="gfw_key">Токен Global Fishing Watch (необязательно)</label>
  <input id="gfw_key" name="gfw_key" autocomplete="off" spellcheck="false"
         placeholder="если нужны разрывы AIS и заходы в порты" value="__GFW_KEY__">

  <label for="telegram_token">Токен Telegram-бота (необязательно)</label>
  <input id="telegram_token" name="telegram_token" autocomplete="off" spellcheck="false"
         placeholder="токен от @BotFather" value="__TELEGRAM_TOKEN__">

  <label for="telegram_chat_id">Чат для уведомлений (необязательно)</label>
  <input id="telegram_chat_id" name="telegram_chat_id" autocomplete="off" spellcheck="false"
         placeholder="например 6518041859" value="__TELEGRAM_CHAT__">

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


def render(api_key: str, gfw_key: str, telegram_token: str, telegram_chat: str) -> str:
    return (SETUP_PAGE
            .replace("__API_KEY__", api_key)
            .replace("__GFW_KEY__", gfw_key)
            .replace("__TELEGRAM_TOKEN__", telegram_token)
            .replace("__TELEGRAM_CHAT__", telegram_chat))
