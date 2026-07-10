"""HTML templates for the aitrack local/server web UI.

Kept separate from the HTTP handler so routing/auth logic is easier to review.
"""

from __future__ import annotations


def _start_page_html(*, server_mode: bool = False) -> str:
    token_control = "" if server_mode else (
        '<label title="Vajalik ainult aitrack serve keskserveri puhul">Server token '
        '<input id="tokenInput" type="password" placeholder="keskserveri token"></label>'
    )
    user_button = '<button onclick="location.href=\'/account\'">Kasutaja</button>' if server_mode else '<button onclick="showHelp()">Abi</button>'
    server_actions = "" if server_mode else (
        '<button onclick="refreshFromLogs()">Töötle lõpetatud tunnid</button>'
        '<button onclick="backfill()">Backfill 12h</button>'
    )
    page = r"""<!doctype html>
<html lang="et">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aitrack</title>
<style>
:root { color-scheme: light dark; --bg:#0f172a; --panel:#111827; --muted:#94a3b8; --text:#e5e7eb; --accent:#38bdf8; --ok:#22c55e; --bad:#f97316; --line:#334155; }
@media (prefers-color-scheme: light) { :root { --bg:#f8fafc; --panel:#ffffff; --muted:#64748b; --text:#0f172a; --accent:#0369a1; --ok:#15803d; --bad:#c2410c; --line:#cbd5e1; } }
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
header { padding:18px 22px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
h1 { margin:0; font-size:22px; }
main { padding:18px 22px 40px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px; margin-bottom:16px; box-shadow:0 8px 30px rgba(0,0,0,.12); }
.toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
button, input, select, textarea { font:inherit; }
button { border:1px solid var(--line); background:transparent; color:var(--text); border-radius:10px; padding:8px 11px; cursor:pointer; }
button.primary { background:var(--accent); color:white; border-color:var(--accent); }
button.good { background:var(--ok); color:white; border-color:var(--ok); }
button.warn { border-color:var(--bad); color:var(--bad); }
button:hover { filter:brightness(1.08); }
input, select, textarea { background:transparent; color:var(--text); border:1px solid var(--line); border-radius:10px; padding:8px; }
textarea { width:100%; min-height:92px; max-height:92px; resize:none; line-height:1.35; overflow:hidden; }
textarea.expanded, textarea:focus { max-height:none; overflow:auto; }
.small { color:var(--muted); font-size:13px; }
.status { color:var(--muted); min-height:20px; }
table { width:100%; border-collapse:collapse; }
th, td { border-top:1px solid var(--line); padding:8px; vertical-align:top; }
th { color:var(--muted); text-align:left; font-weight:600; font-size:13px; }
.hour { width:120px; }
.tool { width:110px; }
.actions { width:74px; text-align:right; }
.empty { text-align:center; color:var(--muted); padding:30px; }
@media (max-width: 900px) { table, thead, tbody, tr, td, th { display:block; } thead { display:none; } tr { border:1px solid var(--line); border-radius:12px; margin:10px 0; padding:8px; } td { border:0; padding:6px; } td::before { content:attr(data-label); display:block; color:var(--muted); font-size:12px; margin-bottom:3px; } .hour, .tool { width:100%; } }
</style>
</head>
<body>
<header>
  <div><h1>aitrack</h1><div class="small">Tänased ja varasemad tööpäeviku read — muuda, lisa ja kopeeri Google Sheetsi.</div></div>
  <div class="toolbar">__USER_BUTTON__<button onclick="location.href='/activity'">Server tegevused</button>__SERVER_ACTIONS__</div>
</header>
<main>
  <section class="panel toolbar">
    <label>Kuupäev <input type="date" id="dateInput"></label>
    <select id="daySelect" title="Olemasolevad päevad"></select>
    <label id="userSelectLabel" hidden>Kasutaja <select id="userSelect"></select></label>
    __TOKEN_CONTROL__
    <button onclick="loadDay()">Ava</button>
    <button onclick="addRow()">+ Lisa rida</button>
    <button class="primary" onclick="saveDay(true)">Salvesta</button>
    <button class="good" onclick="copyDay(false)">Kopeeri D–G</button>
    <button class="good" onclick="copyDay(true)">Kopeeri A–G</button>
    <span class="status" id="status"></span>
  </section>
  <section class="panel">
    <table id="rowsTable">
      <thead><tr><th>Tund</th><th>Objekt ja ülesanne</th><th>Saavutused</th><th>Takistused</th><th>Uued teadmised</th><th>Tööriist</th><th></th></tr></thead>
      <tbody id="rowsBody"><tr><td class="empty" colspan="7">Laen…</td></tr></tbody>
    </table>
  </section>
  <section class="panel small" id="help" hidden>
    <b>Kuidas kasutada?</b><br>
    1. Vali kuupäev. 2. Muuda/lisa read. 3. Vajuta Salvesta. 4. Vajuta “Kopeeri D–G” ja kleebi Sheetsis D-lahtrisse.<br>
    “Kopeeri A–G” kasuta siis, kui tahad ka kuupäeva/punktide/nädalapäeva veerud kaasa võtta ja kleebid A-lahtrisse.
  </section>
</main>
<script>
let currentDate = '';
let days = [];
let currentUserName = '';
let selectedUserName = '';
const $ = (id) => document.getElementById(id);
const SERVER_MODE = __SERVER_MODE__;
function setStatus(msg, isError=false) { $('status').textContent = msg; $('status').style.color = isError ? 'var(--bad)' : 'var(--muted)'; }
function authToken() { return $('tokenInput') ? $('tokenInput').value.trim() : ''; }
function selectedUserParam() { return SERVER_MODE && $('userSelect') && $('userSelect').value ? $('userSelect').value : ''; }
function userQueryParams() { const q = new URLSearchParams(); const u = selectedUserParam(); if (u) q.set('user', u); return q; }
async function api(path, opts={}) {
  opts.headers = Object.assign({}, opts.headers || {});
  const tok = authToken();
  if (tok) opts.headers['X-Aitrack-Token'] = tok;
  const res = await fetch(path, {credentials:'same-origin', ...opts});
  const data = await res.json().catch(() => ({}));
  if (SERVER_MODE && (res.status === 401 || res.status === 403)) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error(data.error || 'login puudub');
  }
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}
function escapeHtml(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function rowTemplate(row={}) {
  const key = escapeHtml(row.key || '');
  return `<tr data-key="${key}">
    <td data-label="Tund"><input class="hour" value="${escapeHtml(row.hour || '')}" placeholder="14:00–15:00"></td>
    <td data-label="Objekt"><textarea class="objekt">${escapeHtml(row.objekt || '')}</textarea></td>
    <td data-label="Saavutused"><textarea class="saavutus">${escapeHtml(row.saavutus || '')}</textarea></td>
    <td data-label="Takistused"><textarea class="takistus">${escapeHtml(row.takistus || '')}</textarea></td>
    <td data-label="Uued teadmised"><textarea class="teadmine">${escapeHtml(row.teadmine || '')}</textarea></td>
    <td data-label="Tööriist"><input class="tool" value="${escapeHtml(row.tool || 'Käsitsi')}"></td>
    <td class="actions"><button class="warn" onclick="deleteRow(this)">Kustuta</button></td>
  </tr>`;
}
function autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.max(92, el.scrollHeight + 2) + 'px';
}
function autoResizeAll() {
  document.querySelectorAll('textarea').forEach(autoResizeTextarea);
}
function wireTextareas(scope=document) {
  scope.querySelectorAll('textarea').forEach(el => {
    autoResizeTextarea(el);
    el.addEventListener('focus', () => { el.classList.add('expanded'); autoResizeTextarea(el); });
    el.addEventListener('click', () => { el.classList.add('expanded'); autoResizeTextarea(el); });
    el.addEventListener('input', () => autoResizeTextarea(el));
  });
  requestAnimationFrame(autoResizeAll);
  setTimeout(autoResizeAll, 0);
}
function deleteRow(button) {
  if (!confirm('Kas kustutan selle rea? Salvestamiseks vajuta pärast ka “Salvesta”.')) return;
  button.closest('tr').remove();
}
function collectRows() {
  return Array.from(document.querySelectorAll('#rowsBody tr[data-key]')).map(tr => ({
    key: tr.dataset.key || '',
    hour: tr.querySelector('.hour').value,
    objekt: tr.querySelector('.objekt').value,
    saavutus: tr.querySelector('.saavutus').value,
    takistus: tr.querySelector('.takistus').value,
    teadmine: tr.querySelector('.teadmine').value,
    tool: tr.querySelector('.tool').value
  }));
}
function renderRows(rows) {
  $('rowsBody').innerHTML = rows.length ? rows.map(rowTemplate).join('') : '<tr><td class="empty" colspan="7">Sellel päeval pole veel ridu. Vajuta “+ Lisa rida”.</td></tr>';
  wireTextareas($('rowsBody'));
}
async function init() {
  currentDate = new Date().toISOString().slice(0, 10);
  $('dateInput').value = currentDate;
  if ($('tokenInput')) {
    $('tokenInput').value = localStorage.getItem('aitrackToken') || '';
    $('tokenInput').addEventListener('input', () => localStorage.setItem('aitrackToken', authToken()));
  }
  await initUserSelect();
  await loadDays(false);
  renderDaySelect();
  await loadDay();
}
async function initUserSelect() {
  if (!SERVER_MODE) return;
  const me = await api('/api/me');
  currentUserName = me.user ? me.user.name : '';
  selectedUserName = currentUserName;
  if (!me.user || me.user.role !== 'admin') return;
  const data = await api('/api/activity/filters');
  const users = data.users || [];
  $('userSelect').innerHTML = users.map(u => `<option value="${escapeHtml(u.name || '')}">${escapeHtml(u.name || '')} (${escapeHtml(u.role || '')})</option>`).join('');
  $('userSelect').value = currentUserName;
  $('userSelectLabel').hidden = false;
  $('userSelect').onchange = async () => { selectedUserName = $('userSelect').value; await loadDays(false); renderDaySelect(); await loadDay(); };
}
async function loadDays(resetToToday=false) {
  const q = userQueryParams();
  const data = await api('/api/days' + (q.toString() ? '?' + q.toString() : ''));
  days = data.days || [];
  if (resetToToday || !currentDate) currentDate = data.today;
  $('dateInput').value = currentDate;
}
function renderDaySelect() {
  $('daySelect').innerHTML = days.map(d => `<option value="${d}">${d}</option>`).join('');
  if (!days.includes(currentDate)) $('daySelect').insertAdjacentHTML('afterbegin', `<option value="${currentDate}">${currentDate}</option>`);
  $('daySelect').value = currentDate;
  $('daySelect').onchange = () => { $('dateInput').value = $('daySelect').value; loadDay(); };
  $('dateInput').onchange = () => { currentDate = $('dateInput').value; $('daySelect').value = currentDate; loadDay(); };
}
async function loadDay() {
  currentDate = $('dateInput').value || currentDate;
  setStatus('Laen…');
  const q = userQueryParams();
  q.set('date', currentDate);
  const data = await api('/api/day?' + q.toString());
  renderRows(data.rows || []);
  setStatus(`Avatud ${currentDate}${selectedUserParam() ? ' · ' + selectedUserParam() : ''}`);
}
function addRow() {
  const body = $('rowsBody');
  if (!body.querySelector('tr[data-key]')) body.innerHTML = '';
  body.insertAdjacentHTML('beforeend', rowTemplate({hour:'', tool:'Käsitsi'}));
  wireTextareas(body.lastElementChild);
}
async function saveDay(show=true) {
  currentDate = $('dateInput').value || currentDate;
  const rows = collectRows();
  setStatus('Salvestan…');
  const body = {date: currentDate, rows};
  if (selectedUserParam()) body.user = selectedUserParam();
  const data = await api('/api/day', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  renderRows(data.rows || []);
  if (!days.includes(currentDate)) { days.push(currentDate); days.sort(); renderDaySelect(); }
  if (show) setStatus('Salvestatud');
}
async function copyRich(html, text) {
  if (navigator.clipboard && window.ClipboardItem) {
    await navigator.clipboard.write([new ClipboardItem({
      'text/html': new Blob([html], {type:'text/html'}),
      'text/plain': new Blob([text], {type:'text/plain'})
    })]);
    return;
  }
  const div = document.createElement('div');
  div.contentEditable = 'true'; div.style.position = 'fixed'; div.style.left = '-9999px'; div.innerHTML = html;
  document.body.appendChild(div);
  const range = document.createRange(); range.selectNodeContents(div);
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  document.execCommand('copy'); sel.removeAllRanges(); div.remove();
}
async function copyDay(full) {
  await saveDay(false);
  const q = userQueryParams();
  q.set('date', currentDate);
  q.set('full', full ? '1' : '0');
  const data = await api('/api/copy?' + q.toString());
  await copyRich(data.html, data.text);
  setStatus(full ? 'Kopeeritud A–G. Kleebi Sheetsis A-lahtrisse.' : 'Kopeeritud D–G. Kleebi Sheetsis D-lahtrisse.');
}
async function refreshFromLogs() {
  if (!confirm('Käivitada aitrack run? See võib võtta aega.')) return;
  setStatus('Töötlen logisid…');
  await api('/api/run', {method:'POST'});
  await init();
  setStatus('Logid töödeldud');
}
async function backfill() {
  if (!confirm('Töödelda viimased 12 tundi tagantjärele?')) return;
  setStatus('Backfill 12h…');
  await api('/api/backfill', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hours:12})});
  await init();
  setStatus('Backfill tehtud');
}
function showHelp() { $('help').hidden = !$('help').hidden; }
init().catch(e => setStatus(e.message, true));
</script>
</body>
</html>"""
    return (page
            .replace("__TOKEN_CONTROL__", token_control)
            .replace("__USER_BUTTON__", user_button)
            .replace("__SERVER_ACTIONS__", server_actions)
            .replace("__SERVER_MODE__", "true" if server_mode else "false"))


def _login_page_html() -> str:
    return r"""<!doctype html>
<html lang="et">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aitrack login</title>
<style>
:root { color-scheme: light dark; --bg:#0f172a; --panel:#111827; --muted:#94a3b8; --text:#e5e7eb; --accent:#38bdf8; --bad:#f97316; --line:#334155; }
@media (prefers-color-scheme: light) { :root { --bg:#f8fafc; --panel:#ffffff; --muted:#64748b; --text:#0f172a; --accent:#0369a1; --bad:#c2410c; --line:#cbd5e1; } }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; font-family:system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--text); padding:20px; }
.panel { width:min(420px,100%); background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:22px; box-shadow:0 12px 40px rgba(0,0,0,.18); }
h1 { margin:0 0 6px; font-size:24px; }
.small { color:var(--muted); font-size:13px; margin-bottom:18px; }
label { display:block; margin:12px 0 6px; color:var(--muted); font-size:13px; }
input, button { font:inherit; width:100%; border-radius:10px; padding:10px; }
input { background:transparent; color:var(--text); border:1px solid var(--line); }
button { margin-top:16px; border:1px solid var(--accent); background:var(--accent); color:white; cursor:pointer; }
.status { min-height:20px; margin-top:12px; color:var(--muted); }
.bad { color:var(--bad); }
</style>
</head>
<body>
<main class="panel">
  <h1>aitrack login</h1>
  <div class="small">Logi serveri tegevuste ja päevavaate vaatamiseks sisse.</div>
  <form id="loginForm">
    <label for="name">Kasutaja</label>
    <input id="name" name="name" autocomplete="username" required autofocus>
    <label for="password">Parool</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Logi sisse</button>
  </form>
  <div id="status" class="status"></div>
</main>
<script>
const $ = (id) => document.getElementById(id);
function nextUrl() {
  const n = new URLSearchParams(location.search).get('next') || '/activity';
  return n.startsWith('/') && !n.startsWith('//') ? n : '/activity';
}
$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('status').className = 'status';
  $('status').textContent = 'Login…';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: $('name').value.trim(), password: $('password').value})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || 'login ebaõnnestus');
    location.href = nextUrl();
  } catch (err) {
    $('status').className = 'status bad';
    $('status').textContent = err.message || 'login ebaõnnestus';
  }
});
</script>
</body>
</html>"""


def _account_page_html() -> str:
    return r"""<!doctype html>
<html lang="et">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aitrack kasutaja</title>
<style>
:root { color-scheme: light dark; --bg:#0f172a; --panel:#111827; --muted:#94a3b8; --text:#e5e7eb; --accent:#38bdf8; --ok:#22c55e; --bad:#f97316; --line:#334155; }
@media (prefers-color-scheme: light) { :root { --bg:#f8fafc; --panel:#ffffff; --muted:#64748b; --text:#0f172a; --accent:#0369a1; --ok:#15803d; --bad:#c2410c; --line:#cbd5e1; } }
* { box-sizing:border-box; }
body { margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--text); }
header { padding:18px 22px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
h1 { margin:0; font-size:22px; }
main { padding:18px 22px 40px; max-width:860px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px; margin-bottom:16px; box-shadow:0 8px 30px rgba(0,0,0,.12); }
.toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
button, input { font:inherit; }
button { border:1px solid var(--line); background:transparent; color:var(--text); border-radius:10px; padding:8px 11px; cursor:pointer; }
button.primary { background:var(--accent); color:white; border-color:var(--accent); }
button:hover { filter:brightness(1.08); }
label { display:block; margin:12px 0 6px; color:var(--muted); font-size:13px; }
input { width:min(420px,100%); background:transparent; color:var(--text); border:1px solid var(--line); border-radius:10px; padding:10px; display:block; }
.small { color:var(--muted); font-size:13px; }
.status { min-height:20px; margin-top:12px; color:var(--muted); }
.bad { color:var(--bad); }
.ok { color:var(--ok); }
.pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 7px; color:var(--muted); font-size:12px; }
pre.install-command { white-space:pre-wrap; word-break:break-word; border:1px solid var(--line); border-radius:12px; padding:10px; background:rgba(148,163,184,.08); user-select:all; }
.tabs button.active { background:var(--accent); color:white; border-color:var(--accent); }
</style>
</head>
<body>
<header>
  <div><h1>Kasutaja seaded</h1><div class="small">Parool ja tulevikus muud kasutaja seaded. <span id="userInfo"></span></div></div>
  <div class="toolbar"><button onclick="location.href='/'">Päevavaade</button><button onclick="location.href='/activity'">Server tegevused</button><button onclick="location.href='/admin'">Admin</button><button onclick="logout()">Logi välja</button></div>
</header>
<main>
  <section class="panel">
    <h2>Parooli muutmine</h2>
    <form id="passwordForm">
      <label for="currentPassword">Praegune parool</label>
      <input id="currentPassword" type="password" autocomplete="current-password" required>
      <label for="newPassword">Uus parool</label>
      <input id="newPassword" type="password" autocomplete="new-password" minlength="8" required>
      <label for="newPassword2">Korda uut parooli</label>
      <input id="newPassword2" type="password" autocomplete="new-password" minlength="8" required>
      <button class="primary" type="submit">Muuda parool</button>
    </form>
    <div id="status" class="status"></div>
  </section>
  <section class="panel">
    <h2>Installi aitrack arvutisse</h2>
    <p class="small">Loo ühekordne installikood ja kopeeri üks käsk terminali. Käsk laeb GitHubist repo, ühendab kliendi serveriga ning paigaldab minute-trackingu ja Pi extensioni.</p>
    <div class="toolbar"><button class="primary" onclick="createInstallCode()">Loo installikäsk</button><span id="installStatus" class="status"></span></div>
    <div id="installBox" hidden>
      <div class="toolbar tabs" style="margin-top:12px">
        <button id="installTabLinux" onclick="showInstallCommand('linux')">Linux</button>
        <button id="installTabMac" onclick="showInstallCommand('mac')">macOS</button>
        <button id="installTabWindows" onclick="showInstallCommand('windows')">Windows</button>
      </div>
      <pre id="installCommand" class="install-command"></pre>
      <div class="toolbar"><button onclick="copyInstallCommand()">Kopeeri käsk</button><span class="small" id="installExpires"></span></div>
      <p class="small">Pärast paigaldust tee Pi sees <code>/reload</code>. Projekti küsib installiskript ise või saad hiljem teha <code>aitrack add /tee/projektini</code>.</p>
    </div>
  </section>
  <section class="panel small">
    Tulevikus saab siia lisada kasutaja eelistused, teavitused ja muud seaded.
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function setStatus(msg, cls='') { $('status').className = 'status ' + cls; $('status').textContent = msg; }
async function api(path, opts={}) {
  const res = await fetch(path, {credentials:'same-origin', ...opts});
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error(data.error || 'login puudub');
  }
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}
let installCommands = {};
let selectedInstallOs = 'linux';
function setInstallStatus(msg, cls='') { $('installStatus').className = 'status ' + cls; $('installStatus').textContent = msg; }
function psQuote(s) { return String(s).replace(/'/g, "''"); }
function showInstallCommand(os) {
  selectedInstallOs = os;
  $('installCommand').textContent = installCommands[os] || '';
  for (const name of ['Linux', 'Mac', 'Windows']) $('installTab' + name).classList.toggle('active', os.toLowerCase().startsWith(name.toLowerCase().slice(0, 3)) || (os === 'mac' && name === 'Mac'));
}
async function createInstallCode() {
  setInstallStatus('Loon koodi…');
  try {
    const data = await api('/api/install-code', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const base = location.origin;
    const code = data.code;
    installCommands = {
      linux: `curl -fsSL ${base}/install-client.sh | bash -s -- --code ${code}`,
      mac: `curl -fsSL ${base}/install-client.sh | bash -s -- --code ${code}`,
      windows: `powershell -ExecutionPolicy Bypass -Command "iex (iwr -UseBasicParsing '${base}/install-client.ps1').Content; Install-AitrackClient -Code '${psQuote(code)}'"`,
    };
    $('installBox').hidden = false;
    $('installExpires').textContent = data.expires_at ? 'Kehtib kuni ' + new Date(data.expires_at).toLocaleString() : '';
    showInstallCommand(/win/i.test(navigator.platform) ? 'windows' : /mac/i.test(navigator.platform) ? 'mac' : 'linux');
    setInstallStatus('Installikäsk valmis', 'ok');
  } catch (err) {
    setInstallStatus(err.message || 'Installikoodi loomine ebaõnnestus', 'bad');
  }
}
async function copyInstallCommand() {
  const text = $('installCommand').textContent;
  try { await navigator.clipboard.writeText(text); setInstallStatus('Kopeeritud', 'ok'); }
  catch (_) { setInstallStatus('Kopeeri käsitsi käsukastist', 'bad'); }
}
$('passwordForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const current = $('currentPassword').value;
  const next = $('newPassword').value;
  const next2 = $('newPassword2').value;
  if (next !== next2) { setStatus('Uued paroolid ei klapi', 'bad'); return; }
  setStatus('Muudan…');
  try {
    await api('/api/me/password', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({current_password: current, new_password: next})});
    $('passwordForm').reset();
    setStatus('Parool muudetud', 'ok');
  } catch (err) {
    setStatus(err.message || 'Parooli muutmine ebaõnnestus', 'bad');
  }
});
async function logout() {
  await fetch('/api/logout', {method:'POST', credentials:'same-origin'}).catch(() => {});
  location.href = '/login?next=/account';
}
async function init() {
  const me = await api('/api/me');
  $('userInfo').innerHTML = me.user ? '(' + esc(me.user.name) + ', <span class="pill">' + esc(me.user.role) + '</span>)' : '';
}
init().catch(e => setStatus(e.message || 'login puudub', 'bad'));
</script>
</body>
</html>"""


def _admin_page_html() -> str:
    return r"""<!doctype html>
<html lang="et">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>aitrack admin</title>
<style>
:root { color-scheme: light dark; --bg:#0f172a; --panel:#111827; --muted:#94a3b8; --text:#e5e7eb; --accent:#38bdf8; --bad:#f97316; --line:#334155; --ok:#22c55e; }
@media (prefers-color-scheme: light) { :root { --bg:#f8fafc; --panel:#fff; --muted:#64748b; --text:#0f172a; --accent:#0369a1; --bad:#c2410c; --line:#cbd5e1; --ok:#15803d; } }
*{box-sizing:border-box} body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}
header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}
main{padding:18px 22px 40px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:16px;box-shadow:0 8px 30px rgba(0,0,0,.12)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}button,input,select{font:inherit}button{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:10px;padding:8px 11px;cursor:pointer}button.primary{background:var(--accent);color:white;border-color:var(--accent)}
input,select{background:transparent;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:8px}.small{color:var(--muted);font-size:13px}.status{min-height:20px;color:var(--muted)}.bad{color:var(--bad)}.ok{color:var(--ok)}
table{width:100%;border-collapse:collapse}th,td{border-top:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}th{color:var(--muted);font-size:13px}.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;color:var(--muted);font-size:12px}code{user-select:all}
</style>
</head>
<body>
<header><div><h1>aitrack admin</h1><div class="small">Kasutajad ja turvaaudit. Token kuvatakse ainult uue kasutaja loomisel.</div></div><div class="toolbar"><button onclick="location.href='/activity'">Tegevused</button><button onclick="location.href='/account'">Kasutaja</button><button onclick="logout()">Logi välja</button></div></header>
<main>
<section class="panel"><h2>Kasutajad</h2><div class="toolbar"><input id="newName" placeholder="kasutajanimi"><select id="newRole"><option>user</option><option>admin</option></select><input id="newPassword" type="password" placeholder="algparool (valikuline)"><button class="primary" onclick="addUser()">Lisa kasutaja</button><button onclick="loadUsers()">Värskenda</button></div><div id="userStatus" class="status"></div><table><thead><tr><th>Nimi</th><th>Roll</th><th>Web sessioonid</th><th>Work session'id</th><th>Tegevus</th></tr></thead><tbody id="usersBody"></tbody></table></section>
<section class="panel"><h2>Turvaaudit</h2><div class="toolbar"><input type="date" id="auditDate"><input id="auditType" placeholder="event_type"><button onclick="loadSecurity()">Ava</button></div><table><thead><tr><th>Aeg</th><th>Event</th><th>Kasutaja</th><th>IP/path</th><th>Detail</th></tr></thead><tbody id="auditBody"></tbody></table></section>
</main>
<script>
const $=id=>document.getElementById(id); const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
async function api(path,opts={}){const res=await fetch(path,{credentials:'same-origin',...opts});const data=await res.json().catch(()=>({}));if(res.status===401||res.status===403){if(data.error==='admini õigus puudub') throw new Error(data.error); location.href='/login?next=/admin'; throw new Error(data.error||'login puudub')}if(!res.ok||data.ok===false)throw new Error(data.error||res.statusText);return data}
function fmt(s){if(!s)return'';const d=new Date(s);return isNaN(d)?esc(s):d.toLocaleString()}
async function loadUsers(){const data=await api('/api/admin/users');$('usersBody').innerHTML=(data.users||[]).map(u=>`<tr><td>${esc(u.name)}</td><td><span class="pill">${esc(u.role)}</span></td><td>${esc(u.active_web_sessions)}</td><td>${esc(u.work_sessions)}<div class="small">aktiivseid ${esc(u.active_work_sessions)}</div></td><td><button onclick="setPw('${esc(u.name)}')">Sea parool</button> <button onclick="revoke('${esc(u.name)}')">Tühista web sessioonid</button></td></tr>`).join('')||'<tr><td colspan="5">Kasutajaid pole.</td></tr>'}
async function addUser(){try{const body={name:$('newName').value.trim(),role:$('newRole').value,password:$('newPassword').value};const data=await api('/api/admin/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});$('userStatus').className='status ok';$('userStatus').innerHTML='Kasutaja loodud. API token (kopeeri nüüd): <code>'+esc(data.token)+'</code>';$('newPassword').value='';await loadUsers()}catch(e){$('userStatus').className='status bad';$('userStatus').textContent=e.message}}
async function setPw(name){const pw=prompt('Uus parool kasutajale '+name+' (vähemalt 8 märki)');if(!pw)return;await api('/api/admin/users/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,password:pw})});await loadUsers()}
async function revoke(name){if(!confirm('Tühistan web sessioonid: '+name+'?'))return;await api('/api/admin/users/revoke-sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});await loadUsers()}
async function loadSecurity(){const q=new URLSearchParams();if($('auditDate').value)q.set('date',$('auditDate').value);if($('auditType').value)q.set('event_type',$('auditType').value);q.set('limit','200');const data=await api('/api/admin/security-events?'+q.toString());$('auditBody').innerHTML=(data.events||[]).map(e=>`<tr><td>${fmt(e.created_at)}</td><td><span class="pill">${esc(e.event_type)}</span></td><td>${esc(e.user||'')}</td><td>${esc(e.ip)}<div class="small">${esc(e.path)}</div></td><td>${esc(e.detail||'')}</td></tr>`).join('')||'<tr><td colspan="5">Auditit pole.</td></tr>'}
async function logout(){await fetch('/api/logout',{method:'POST',credentials:'same-origin'}).catch(()=>{});location.href='/login?next=/admin'}
async function init(){ $('auditDate').value=new Date().toISOString().slice(0,10); await loadUsers(); await loadSecurity(); }
init().catch(e=>{$('userStatus').className='status bad';$('userStatus').textContent=e.message});
</script>
</body></html>"""


def _activity_page_html() -> str:
    return r"""<!doctype html>
<html lang="et">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aitrack server tegevused</title>
<style>
:root { color-scheme: light dark; --bg:#0f172a; --panel:#111827; --muted:#94a3b8; --text:#e5e7eb; --accent:#38bdf8; --bad:#f97316; --line:#334155; }
@media (prefers-color-scheme: light) { :root { --bg:#f8fafc; --panel:#ffffff; --muted:#64748b; --text:#0f172a; --accent:#0369a1; --bad:#c2410c; --line:#cbd5e1; } }
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
header { padding:18px 22px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
h1 { margin:0; font-size:22px; }
main { padding:18px 22px 40px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px; margin-bottom:16px; box-shadow:0 8px 30px rgba(0,0,0,.12); }
.toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
button, input, select { font:inherit; }
button { border:1px solid var(--line); background:transparent; color:var(--text); border-radius:10px; padding:8px 11px; cursor:pointer; }
button.primary { background:var(--accent); color:white; border-color:var(--accent); }
button:hover { filter:brightness(1.08); }
button.icon { padding:4px 8px; border-radius:8px; color:var(--muted); }
.filter-group { display:flex; gap:6px; align-items:center; }
.filter-toggle { padding:7px 10px; border-radius:999px; color:var(--muted); }
.filter-toggle.active { background:var(--accent); border-color:var(--accent); color:white; }
.date-wrap { position:relative; }
.date-picker { position:absolute; top:100%; left:0; z-index:20; width:310px; margin-top:6px; background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:10px; box-shadow:0 18px 60px rgba(0,0,0,.35); }
.date-picker[hidden] { display:none; }
.date-picker-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:8px; }
.date-weekdays, .date-days { display:grid; grid-template-columns:repeat(7,1fr); gap:4px; }
.date-weekdays span { text-align:center; color:var(--muted); font-size:12px; padding:3px 0; }
.date-day { padding:7px 0; border-radius:9px; text-align:center; }
.date-day.outside { color:var(--muted); opacity:.65; }
.date-day.in-range { background:rgba(56,189,248,.16); }
.date-day.selected { background:var(--accent); border-color:var(--accent); color:white; }
input, select { background:transparent; color:var(--text); border:1px solid var(--line); border-radius:10px; padding:8px; }
.small { color:var(--muted); font-size:13px; }
.status { color:var(--muted); min-height:20px; }
table { width:100%; border-collapse:collapse; }
th, td { border-top:1px solid var(--line); padding:8px; vertical-align:top; }
th { color:var(--muted); text-align:left; font-weight:600; font-size:13px; }
tr.session-colored { background:var(--row-bg-dark); }
tr.session-colored td:first-child { border-left:4px solid var(--row-accent-dark); }
tr.session-colored:hover { filter:brightness(1.08); }
@media (prefers-color-scheme: light) { tr.session-colored { background:var(--row-bg-light); } tr.session-colored td:first-child { border-left-color:var(--row-accent-light); } }
pre { margin:0; white-space:pre-wrap; word-break:break-word; max-height:160px; overflow:auto; }
.bad { color:var(--bad); }
.pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 7px; color:var(--muted); font-size:12px; }
.path { margin-top:4px; word-break:break-all; }
.empty { text-align:center; color:var(--muted); padding:26px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; }
.metric { border:1px solid var(--line); border-radius:12px; padding:10px; }
.metric b { display:block; font-size:22px; }
.modal-backdrop { position:fixed; inset:0; background:rgba(2,6,23,.72); display:grid; place-items:center; z-index:50; padding:18px; }
.modal-backdrop[hidden] { display:none; }
.modal { width:min(980px,100%); max-height:88vh; overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:16px; box-shadow:0 24px 80px rgba(0,0,0,.38); padding:16px; }
.modal header { padding:0 0 12px; border:0; }
.modal pre { max-height:none; white-space:pre-wrap; word-break:break-word; }
.json-key { color:#7dd3fc; }
.json-string { color:#86efac; }
.json-number { color:#fbbf24; }
.json-boolean { color:#f472b6; }
.json-null { color:#c4b5fd; }
body.modal-open { overflow:hidden; }
@media (max-width: 900px) { table, thead, tbody, tr, td, th { display:block; } thead { display:none; } tr { border:1px solid var(--line); border-radius:12px; margin:10px 0; padding:8px; } td { border:0; padding:6px; } td::before { content:attr(data-label); display:block; color:var(--muted); font-size:12px; margin-bottom:3px; } }
</style>
</head>
<body>
<header>
  <div><h1>aitrack server tegevused</h1><div class="small">Work session'id, prompt-eventid ja tegevuste ajalugu sisselogitud kasutaja õiguste piires. <span id="userInfo"></span></div></div>
  <div class="toolbar"><button onclick="location.href='/'">Päevavaade</button><button onclick="location.href='/account'">Kasutaja</button><button onclick="location.href='/admin'">Admin</button><button onclick="loadActivity()" class="primary">Värskenda</button><button onclick="logout()">Logi välja</button></div>
</header>
<main>
  <section class="panel toolbar">
    <div class="date-wrap" id="dateWrap">
      <label>Kuupäev <input id="dateInput" readonly placeholder="vali päev või vahemik" style="width:210px" autocomplete="off"></label>
      <div id="datePicker" class="date-picker" hidden onclick="event.stopPropagation()">
        <div class="date-picker-head"><button type="button" class="icon" onclick="moveCalendarMonth(-1)">‹</button><b id="datePickerTitle"></b><button type="button" class="icon" onclick="moveCalendarMonth(1)">›</button></div>
        <div class="date-weekdays"><span>E</span><span>T</span><span>K</span><span>N</span><span>R</span><span>L</span><span>P</span></div>
        <div id="datePickerDays" class="date-days"></div>
        <div class="toolbar" style="margin-top:8px"><button type="button" class="icon" onclick="setTodayRange()">Täna</button><span class="small">1. klikk algus, 2. klikk lõpp</span></div>
      </div>
    </div>
    <label>Projekt <input id="projectInput" list="projectOptions" placeholder="otsi projekti" autocomplete="off"><datalist id="projectOptions"></datalist></label>
    <label>Kasutaja <input id="userInput" list="userOptions" placeholder="kasutajanimi" style="width:130px" autocomplete="off"><datalist id="userOptions"></datalist></label>
    <label>Issue <input id="issueInput" list="issueOptions" placeholder="issue nr / pealkiri" style="width:220px" autocomplete="off"><datalist id="issueOptions"></datalist></label>
    <label>Tool <input id="toolInput" placeholder="pi" style="width:90px"></label>
    <div class="filter-group"><span>Status</span><input type="hidden" id="statusInput" value=""><button type="button" class="filter-toggle active" data-status-filter="" onclick="setSessionStatusFilter('')">Kõik</button><button type="button" class="filter-toggle" data-status-filter="active" onclick="setSessionStatusFilter('active')">Active</button><button type="button" class="filter-toggle" data-status-filter="stale" onclick="setSessionStatusFilter('stale')">Stale</button><button type="button" class="filter-toggle" data-status-filter="stuck" onclick="setSessionStatusFilter('stuck')">Stuck</button></div>
    <label>Worksessionid <input id="workSessionInput" placeholder="ws_… või id" style="width:170px" autocomplete="off"></label>
    <label>Agent <input id="agentInput" placeholder="agent_uid" style="width:130px"></label>
    <label>Piir <input type="number" id="limitInput" value="200" min="1" max="1000" style="width:90px"></label>
    <span class="status" id="status"></span>
  </section>
  <section class="panel grid" id="metrics"></section>
  <section class="panel">
    <h2>Tegevuste ajalugu</h2>
    <table><thead><tr><th>Aeg</th><th>Tüüp</th><th>Kasutaja</th><th>Projekt / issue</th><th>Tööriist</th><th>Sisu</th><th></th></tr></thead><tbody id="activityBody"><tr><td class="empty" colspan="7">Laen…</td></tr></tbody></table>
  </section>
  <section class="panel">
    <h2>Work session'id</h2>
    <table><thead><tr><th>Session UID</th><th>Aeg</th><th>Kasutaja</th><th>Projekt / issue</th><th>Staatus</th><th>Min</th><th>Kokkuvõte</th><th></th></tr></thead><tbody id="sessionsBody"></tbody></table>
  </section>
  <section class="panel">
    <h2>Prompt-eventid</h2>
    <table><thead><tr><th>Aeg</th><th>Kasutaja</th><th>Projekt / issue</th><th>Tööriist</th><th>Kestus</th><th>Prompt</th><th></th></tr></thead><tbody id="promptsBody"></tbody></table>
  </section>
  <section class="panel">
    <h2>Agent/subagent tree</h2>
    <div id="agentTree" class="small">Laen…</div>
  </section>
  <section class="panel">
    <h2>Raw eventid</h2>
    <table><thead><tr><th>Aeg</th><th>Kasutaja</th><th>Event</th><th>Agent/tool</th><th>Session</th><th>Payload</th><th></th></tr></thead><tbody id="rawEventsBody"></tbody></table>
  </section>
</main>
<div id="detailModal" class="modal-backdrop" hidden onclick="if (event.target === this) closeDetailModal()">
  <section class="modal" role="dialog" aria-modal="true" aria-labelledby="detailTitle">
    <header><h2 id="detailTitle">Toorandmed</h2><button class="icon" onclick="closeDetailModal()">Sulge</button></header>
    <pre id="detailPre">Laen…</pre>
  </section>
</div>
<script>
const $ = (id) => document.getElementById(id);
function setStatus(msg, isError=false) { $('status').textContent = msg; $('status').style.color = isError ? 'var(--bad)' : 'var(--muted)'; }
function esc(s) { return String(s ?? '').replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtTime(s) { if (!s) return ''; const d = new Date(s); return isNaN(d) ? esc(s) : d.toLocaleString(); }
function projectLabel(x) {
  const path = x.local_path || x.cwd || '';
  return `${esc(x.project_key || x.project || '')}${x.issue ? ' <span class="pill">' + esc(x.issue) + '</span>' : ''}${path ? '<div class="small path">' + esc(path) + '</div>' : ''}`;
}
async function api(path, opts={}) {
  const res = await fetch(path, {credentials: 'same-origin', ...opts});
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 || res.status === 403) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error(data.error || 'login puudub');
  }
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}
let dateRangeStart = '';
let dateRangeEnd = '';
let calendarMonth = null;
function pad2(n) { return String(n).padStart(2, '0'); }
function localDateString(d) { return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; }
function parseDateString(s) { const [y, m, d] = String(s || '').split('-').map(Number); return new Date(y || 1970, (m || 1) - 1, d || 1); }
function updateDateInput() {
  $('dateInput').value = dateRangeStart && dateRangeEnd && dateRangeStart !== dateRangeEnd ? `${dateRangeStart} – ${dateRangeEnd}` : dateRangeStart;
}
function openDatePicker() {
  calendarMonth = calendarMonth || parseDateString(dateRangeStart || localDateString(new Date()));
  renderDatePicker();
  $('datePicker').hidden = false;
}
function closeDatePicker() { $('datePicker').hidden = true; }
function moveCalendarMonth(delta) {
  calendarMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + delta, 1);
  renderDatePicker();
}
function setTodayRange() {
  dateRangeStart = localDateString(new Date());
  dateRangeEnd = '';
  calendarMonth = parseDateString(dateRangeStart);
  updateDateInput(); renderDatePicker(); closeDatePicker(); scheduleActivityLoad(0);
}
function selectDateRangeDay(day) {
  if (!dateRangeStart || dateRangeEnd) {
    dateRangeStart = day; dateRangeEnd = '';
  } else {
    if (day < dateRangeStart) { dateRangeEnd = dateRangeStart; dateRangeStart = day; }
    else { dateRangeEnd = day; }
    closeDatePicker();
  }
  updateDateInput(); renderDatePicker(); scheduleActivityLoad(0);
}
function renderDatePicker() {
  const month = calendarMonth || parseDateString(dateRangeStart || localDateString(new Date()));
  calendarMonth = new Date(month.getFullYear(), month.getMonth(), 1);
  $('datePickerTitle').textContent = calendarMonth.toLocaleDateString('et-EE', {month:'long', year:'numeric'});
  const first = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth(), 1);
  const startOffset = (first.getDay() + 6) % 7;
  const gridStart = new Date(first); gridStart.setDate(first.getDate() - startOffset);
  const days = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(gridStart); d.setDate(gridStart.getDate() + i);
    const value = localDateString(d);
    const outside = d.getMonth() !== calendarMonth.getMonth();
    const selected = value === dateRangeStart || value === dateRangeEnd;
    const inRange = dateRangeStart && dateRangeEnd && value > dateRangeStart && value < dateRangeEnd;
    const cls = ['date-day', outside ? 'outside' : '', selected ? 'selected' : '', inRange ? 'in-range' : ''].filter(Boolean).join(' ');
    days.push(`<button type="button" class="${cls}" onclick="selectDateRangeDay('${value}')">${d.getDate()}</button>`);
  }
  $('datePickerDays').innerHTML = days.join('');
}
function renderIssueOptions(issues) {
  $('issueOptions').innerHTML = (issues || []).map(i => {
    const key = String(i.issue_key || '').replace(/^#/, '');
    const title = String(i.title || '').trim();
    const value = esc(title ? `${key} - ${title}` : key);
    const label = esc(i.project_key || i.project_name || '');
    return `<option value="${value}" label="${label}"></option>`;
  }).join('');
}
function renderFilterOptions(data) {
  $('projectOptions').innerHTML = (data.projects || []).map(p => {
    const value = esc(p.project_key || p.name || '');
    const label = esc(p.name && p.name !== p.project_key ? p.name + ' · ' + p.project_key : p.project_key || p.name || '');
    return `<option value="${value}" label="${label}"></option>`;
  }).join('');
  $('userOptions').innerHTML = (data.users || []).map(u => `<option value="${esc(u.name || '')}" label="${esc(u.role || '')}"></option>`).join('');
  renderIssueOptions(data.issues || []);
}
async function loadFilterOptions() {
  try { renderFilterOptions(await api('/api/activity/filters')); } catch (_) {}
}
async function loadIssueOptions() {
  const params = new URLSearchParams();
  if ($('projectInput').value) params.set('project_key', $('projectInput').value);
  try { renderIssueOptions((await api('/api/activity/filters?' + params.toString())).issues || []); } catch (_) {}
}
let issueOptionsTimer = null;
function scheduleIssueOptions() {
  clearTimeout(issueOptionsTimer);
  issueOptionsTimer = setTimeout(loadIssueOptions, 200);
}
let activityLoadTimer = null;
function scheduleActivityLoad(delay=250) {
  clearTimeout(activityLoadTimer);
  activityLoadTimer = setTimeout(loadActivity, delay);
}
function updateStatusFilterButtons() {
  const selected = $('statusInput').value || '';
  document.querySelectorAll('[data-status-filter]').forEach(btn => btn.classList.toggle('active', (btn.dataset.statusFilter || '') === selected));
}
function setSessionStatusFilter(value) {
  $('statusInput').value = value || '';
  updateStatusFilterButtons();
  scheduleActivityLoad(0);
}
const SESSION_ROW_PALETTE = [
  {darkBg:'#122033', darkAccent:'#38bdf8', lightBg:'#e0f2fe', lightAccent:'#0284c7'},
  {darkBg:'#171f3f', darkAccent:'#818cf8', lightBg:'#e0e7ff', lightAccent:'#4f46e5'},
  {darkBg:'#221b36', darkAccent:'#c084fc', lightBg:'#f3e8ff', lightAccent:'#9333ea'},
  {darkBg:'#102a2c', darkAccent:'#2dd4bf', lightBg:'#ccfbf1', lightAccent:'#0f766e'},
  {darkBg:'#2a1827', darkAccent:'#f472b6', lightBg:'#fce7f3', lightAccent:'#db2777'},
  {darkBg:'#2a2112', darkAccent:'#f59e0b', lightBg:'#fef3c7', lightAccent:'#d97706'},
  {darkBg:'#132719', darkAccent:'#4ade80', lightBg:'#dcfce7', lightAccent:'#16a34a'},
  {darkBg:'#2c171b', darkAccent:'#fb7185', lightBg:'#ffe4e6', lightAccent:'#e11d48'},
  {darkBg:'#14223a', darkAccent:'#60a5fa', lightBg:'#dbeafe', lightAccent:'#2563eb'},
  {darkBg:'#112821', darkAccent:'#34d399', lightBg:'#d1fae5', lightAccent:'#059669'},
  {darkBg:'#261d15', darkAccent:'#fb923c', lightBg:'#ffedd5', lightAccent:'#ea580c'},
  {darkBg:'#1f2430', darkAccent:'#94a3b8', lightBg:'#f1f5f9', lightAccent:'#64748b'},
];
function sessionColorSeed(x) { return String(x?.work_session_uid || x?.session_uid || x?.work_session_id || ''); }
function paletteIndex(seed) {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i); h = Math.imul(h, 16777619); }
  return Math.abs(h >>> 0) % SESSION_ROW_PALETTE.length;
}
function rowColorAttrs(x) {
  const seed = sessionColorSeed(x);
  if (!seed) return '';
  const c = SESSION_ROW_PALETTE[paletteIndex(seed)];
  return ` class="session-colored" style="--row-bg-dark:${c.darkBg};--row-accent-dark:${c.darkAccent};--row-bg-light:${c.lightBg};--row-accent-light:${c.lightAccent}"`;
}
function detailButton(x) {
  const type = x.type || 'work_session';
  const id = x.id || x.work_session_id || '';
  const sid = x.work_session_uid || x.session_uid || '';
  if (!type || (!id && !sid)) return '';
  return `<button class="icon" title="Näita toorandmeid" data-detail-type="${esc(type)}" data-detail-id="${esc(id)}" data-detail-session="${esc(sid)}" onclick="showDetailFromButton(this)">◳</button>`;
}
function showDetailFromButton(btn) {
  showDetail(btn.dataset.detailType || '', btn.dataset.detailId || '', btn.dataset.detailSession || '');
}
function closeDetailModal() {
  $('detailModal').hidden = true;
  document.body.classList.remove('modal-open');
}
function syntaxHighlightJson(value) {
  const json = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return esc(json).replace(/(&quot;(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\&])*&quot;\s*:)|(&quot;(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\&])*&quot;)|\b(true|false)\b|\b(null)\b|(-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    (match, key, str, bool, nul, num) => {
      const cls = key ? 'json-key' : str ? 'json-string' : bool ? 'json-boolean' : nul ? 'json-null' : 'json-number';
      return `<span class="${cls}">${match}</span>`;
    });
}
async function showDetail(type, id, sessionUid) {
  const params = new URLSearchParams();
  params.set('type', type);
  if (id) params.set('id', id);
  if (sessionUid) params.set('work_session_uid', sessionUid);
  $('detailTitle').textContent = 'Toorandmed: ' + type;
  $('detailPre').textContent = 'Laen…';
  $('detailModal').hidden = false;
  document.body.classList.add('modal-open');
  try {
    const data = await api('/api/event-detail?' + params.toString());
    $('detailPre').innerHTML = syntaxHighlightJson(data.detail || data);
  } catch (e) {
    $('detailPre').textContent = e.message || 'Toorandmete laadimine ebaõnnestus';
  }
}
window.addEventListener('keydown', (e) => { if (e.key === 'Escape') { if (!$('detailModal').hidden) closeDetailModal(); closeDatePicker(); } });
document.addEventListener('click', (e) => { if (!$('dateWrap').contains(e.target)) closeDatePicker(); });
function renderMetrics(data) {
  const minutes = (data.sessions || []).reduce((a, s) => a + Number(s.minutes || 0), 0);
  $('metrics').innerHTML = `
    <div class="metric"><span class="small">Work session'id</span><b>${(data.sessions || []).length}</b></div>
    <div class="metric"><span class="small">Prompt-eventid</span><b>${(data.prompt_events || []).length}</b></div>
    <div class="metric"><span class="small">Raw eventid</span><b>${(data.raw_events || []).length}</b></div>
    <div class="metric"><span class="small">Minutid</span><b>${minutes}</b></div>
    <div class="metric"><span class="small">Periood</span><b style="font-size:16px">${esc(data.period || '')}</b></div>`;
}
function renderActivity(rows) {
  $('activityBody').innerHTML = rows.length ? rows.map(x => {
    const body = x.type === 'prompt_event'
      ? `<pre>${esc(x.prompt_text || '')}</pre>`
      : x.type === 'raw_event'
        ? `${x.summary && x.summary !== x.event_type ? '<pre>' + esc(x.summary) + '</pre>' : esc(x.event_type || '')}<div class="small">${esc(x.event_type || '')}${x.agent_uid ? ' · ' + esc(x.agent_uid) : ''}</div>`
        : `${esc(x.summary || '')}<div class="small">${esc(x.work_session_uid || '')}</div>`;
    return `<tr${rowColorAttrs(x)}><td data-label="Aeg">${fmtTime(x.at)}</td><td data-label="Tüüp"><span class="pill">${esc(x.type)}</span></td><td data-label="Kasutaja">${esc(x.user || '')}</td><td data-label="Projekt">${projectLabel(x)}</td><td data-label="Tööriist">${esc(x.tool || '')}</td><td data-label="Sisu">${body}</td><td data-label="Toorandmed">${detailButton(x)}</td></tr>`;
  }).join('') : '<tr><td class="empty" colspan="7">Tegevusi pole.</td></tr>';
}
function renderSessions(rows) {
  $('sessionsBody').innerHTML = rows.length ? rows.map(x => `<tr${rowColorAttrs(x)}>
    <td data-label="Session UID"><code>${esc(x.work_session_uid || '')}</code></td>
    <td data-label="Aeg">${fmtTime(x.started_at)}<div class="small">${x.ended_at ? fmtTime(x.ended_at) : 'aktiivne / lõpp puudub'}</div></td>
    <td data-label="Kasutaja">${esc(x.user || '')}<div class="small">${esc(x.device || '')}</div></td>
    <td data-label="Projekt">${projectLabel(x)}</td>
    <td data-label="Staatus"><span class="pill">${esc(x.status || '')}</span><div class="small">${esc(x.status_detail || x.result || '')}</div></td>
    <td data-label="Min">${esc(x.minutes || 0)}<div class="small">ticke ${esc(x.tick_count || 0)}</div></td>
    <td data-label="Kokkuvõte">${esc(x.summary || '')}${x.current_tool_name ? '<div class="small">Tool: ' + esc(x.current_tool_name) + ' · ' + fmtTime(x.current_tool_started_at) + '</div>' : ''}${x.agent_uid ? '<div class="small">Agent: ' + esc(x.agent_uid) + '</div>' : ''}</td>
    <td data-label="Toorandmed">${detailButton({...x, type:'work_session'})}</td>
  </tr>`).join('') : '<tr><td class="empty" colspan="8">Sessioone pole.</td></tr>';
}
function renderPrompts(rows) {
  $('promptsBody').innerHTML = rows.length ? rows.map(x => `<tr${rowColorAttrs(x)}>
    <td data-label="Aeg">${fmtTime(x.started_at)}</td>
    <td data-label="Kasutaja">${esc(x.user || '')}</td>
    <td data-label="Projekt">${projectLabel(x)}<div class="small">${esc(x.work_session_uid || '')}</div></td>
    <td data-label="Tööriist">${esc(x.tool || '')}</td>
    <td data-label="Kestus">${Math.round(Number(x.duration_seconds || 0) / 60)} min</td>
    <td data-label="Prompt"><pre>${esc(x.prompt_text || '')}</pre></td>
    <td data-label="Toorandmed">${detailButton({...x, type:'prompt_event'})}</td>
  </tr>`).join('') : '<tr><td class="empty" colspan="7">Prompt-evente pole.</td></tr>';
}
function renderAgentNode(x, depth=0) {
  return `<div style="margin-left:${depth * 18}px">${depth ? '↳ ' : ''}<code>${esc(x.agent_uid || '')}</code> <span class="pill">${esc(x.events || 0)} event</span> ${esc((x.tools || []).join(', '))}</div>${(x.children || []).map(c => renderAgentNode(c, depth + 1)).join('')}`;
}
function renderAgentTree(rows) {
  $('agentTree').innerHTML = rows && rows.length ? rows.map(x => renderAgentNode(x, 0)).join('') : 'Agent/subagent seoseid pole.';
}
function renderRawEvents(rows) {
  $('rawEventsBody').innerHTML = rows.length ? rows.map(x => `<tr${rowColorAttrs(x)}>
    <td data-label="Aeg">${fmtTime(x.occurred_at_utc)}</td>
    <td data-label="Kasutaja">${esc(x.user || '')}</td>
    <td data-label="Event"><span class="pill">${esc(x.event_type || '')}</span><div class="small">${esc(x.event_key || '')}</div></td>
    <td data-label="Agent/tool">${esc(x.agent_uid || '')}<div class="small">${esc(x.tool || x.tool_name || '')}${x.tool_name && x.tool && x.tool_name !== x.tool ? ' · raw: ' + esc(x.tool_name) : ''}${x.tool_call_id ? ' · ' + esc(x.tool_call_id) : ''}</div></td>
    <td data-label="Session"><code>${esc(x.work_session_uid || '')}</code></td>
    <td data-label="Payload"><pre>${esc(x.payload_json || '')}</pre></td>
    <td data-label="Toorandmed">${detailButton({...x, type:'raw_event'})}</td>
  </tr>`).join('') : '<tr><td class="empty" colspan="7">Raw evente pole.</td></tr>';
}
function renderWaiting(message) {
  $('metrics').innerHTML = '';
  $('activityBody').innerHTML = `<tr><td class="empty" colspan="7">${esc(message)}</td></tr>`;
  $('sessionsBody').innerHTML = `<tr><td class="empty" colspan="8">${esc(message)}</td></tr>`;
  $('promptsBody').innerHTML = `<tr><td class="empty" colspan="7">${esc(message)}</td></tr>`;
  $('agentTree').textContent = message;
  $('rawEventsBody').innerHTML = `<tr><td class="empty" colspan="7">${esc(message)}</td></tr>`;
}
async function loadActivity() {
  clearTimeout(activityLoadTimer);
  const params = new URLSearchParams();
  if (dateRangeStart && dateRangeEnd && dateRangeStart !== dateRangeEnd) { params.set('from', dateRangeStart); params.set('to', dateRangeEnd); }
  else if (dateRangeStart) { params.set('date', dateRangeStart); }
  params.set('limit', $('limitInput').value || '200');
  if ($('projectInput').value) params.set('project_key', $('projectInput').value);
  if ($('userInput').value) params.set('user', $('userInput').value);
  if ($('issueInput').value) params.set('issue', $('issueInput').value);
  if ($('toolInput').value) params.set('tool', $('toolInput').value);
  if ($('statusInput').value) params.set('status', $('statusInput').value);
  if ($('workSessionInput').value) params.set('work_session_uid', $('workSessionInput').value.trim());
  if ($('agentInput').value) params.set('agent_uid', $('agentInput').value);
  setStatus('Laen…');
  try {
    const data = await api('/api/activity?' + params.toString());
    renderMetrics(data); renderActivity(data.activity || []); renderSessions(data.sessions || []); renderPrompts(data.prompt_events || []); renderAgentTree(data.agent_tree || []); renderRawEvents(data.raw_events || []);
    setStatus('Laetud');
  } catch (e) {
    renderWaiting(e.message || 'Päring ebaõnnestus');
    setStatus(e.message, true);
  }
}
async function logout() {
  await fetch('/api/logout', {method:'POST', credentials:'same-origin'}).catch(() => {});
  location.href = '/login?next=/activity';
}
async function init() {
  dateRangeStart = localDateString(new Date());
  dateRangeEnd = '';
  calendarMonth = parseDateString(dateRangeStart);
  updateDateInput();
  $('dateInput').addEventListener('click', (e) => { e.stopPropagation(); openDatePicker(); });
  $('dateInput').addEventListener('focus', openDatePicker);
  $('projectInput').addEventListener('input', () => { scheduleIssueOptions(); scheduleActivityLoad(); });
  $('projectInput').addEventListener('change', () => { loadIssueOptions(); scheduleActivityLoad(0); });
  for (const id of ['userInput', 'issueInput', 'toolInput', 'workSessionInput', 'agentInput', 'limitInput']) {
    $(id).addEventListener('input', () => scheduleActivityLoad());
    $(id).addEventListener('change', () => scheduleActivityLoad(0));
  }
  updateStatusFilterButtons();
  const me = await api('/api/me');
  $('userInfo').textContent = me.user ? `(${me.user.name}, ${me.user.role})` : '';
  await loadFilterOptions();
  loadActivity();
}
init().catch(e => { renderWaiting(e.message || 'login puudub'); setStatus(e.message || 'login puudub', true); });
</script>
</body>
</html>"""
