// Xbox 360 Game Manager page: games on this computer -> the Xbox's hard drive, over Aurora's FTP.
// The server does the copying (xbox.py); this page shows what is where and queues games.

// --- small helpers: fetch with timeouts, and an element builder ---
class ApiError extends Error {
  constructor(message, status = 0, body = null) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(method, url, { body, timeout = 10000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  const opts = { method, signal: ctl.signal, headers: { Accept: 'application/json' }, cache: 'no-store' };
  if (body !== undefined) {
    if (body instanceof Blob) {
      opts.body = body;
      opts.headers['Content-Type'] = body.type || 'application/octet-stream';
    } else {
      opts.body = JSON.stringify(body);
      opts.headers['Content-Type'] = 'application/json';
    }
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (err) {
    throw new ApiError(err.name === 'AbortError' ? 'timed out' : 'network error');
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) {
    throw new ApiError(data?.error?.message ?? data?.error ?? `HTTP ${res.status}`, res.status, data);
  }
  return data;
}

const getJSON = (url, opts) => request('GET', url, opts);
const postJSON = (url, body, opts) => request('POST', url, { ...opts, body });

// Tiny element builder: h('div', {class: 'x', onclick: fn}, 'text', child, [more])
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : String(c));
  }
}

function replace(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

const $ = (id) => document.getElementById(id);
let data = null;
let filter = 'all';
let timer = null;
let busy = false;

const gb = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 1e6))} MB`);
const mbs = (n) => `${(n / 1e6).toFixed(1)} MB/s`;

function duration(s) {
  if (s < 90) return `${Math.max(1, Math.round(s))} s`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function initials(name) {
  const words = name.replace(/[^A-Za-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || '?').slice(0, 2)).toUpperCase();
}

function hue(name) {
  let n = 0;
  for (const c of name) n = (n * 31 + c.charCodeAt(0)) % 360;
  return n;
}

function cover(g) {
  const fallback = () => {
    const d = h('div', { class: 'xb-noart', 'aria-hidden': 'true' }, initials(g.name));
    d.style.setProperty('--hue', hue(g.name));
    return d;
  };
  if (!g.art) return fallback();
  const img = h('img', { src: `/api/art/${g.id}.jpg`, alt: '', loading: 'lazy', decoding: 'async' });
  img.addEventListener('error', () => img.replaceWith(fallback()), { once: true });
  return img;
}

async function act(url, body) {
  if (busy) return;
  busy = true;
  try {
    const res = await postJSON(url, body);
    data = res.status;
    render();
  } catch (err) {
    alert(err.message);
  } finally {
    busy = false;
    schedule();
  }
}

// --- now playing: the running game and the temperatures (Aurora's web API) ---
const TEMP_WARN = { cpu: 75, gpu: 72, memory: 70, case: 50 };  // the console's own targets are 82 / 78 / 76
const TEMP_LABEL = { cpu: 'CPU', gpu: 'GPU', memory: 'RAM', case: 'Case' };

function renderNow() {
  const n = data.now;
  const box = $('now');
  box.hidden = !n;
  if (!n) return;
  if (n.error) {
    replace(box, h('span', { class: 'xb-now-text' },
      n.error === 'login' ? 'Aurora web login failed (XBOX_WEB_USER / XBOX_WEB_PASS in .env).' : 'Aurora’s web API isn’t answering.'));
    return;
  }
  const g = n.game_id && data.games.find((x) => x.id === n.game_id);
  const temps = Object.entries(n.temps).map(([k, v]) =>
    h('span', { class: `xb-temp${v >= TEMP_WARN[k] ? ' warn' : ''}`, title: `${TEMP_LABEL[k]} temperature` },
      `${TEMP_LABEL[k]} ${Math.round(v)}°`));
  replace(box,
    n.in_menu ? null : cover(g ?? { id: '', name: n.name ?? '?', art: false }),
    h('div', { class: 'xb-now-main' },
      h('span', { class: 'xb-now-label' }, n.in_menu ? 'Xbox is on' : 'Now playing'),
      h('strong', null, n.in_menu ? 'Aurora menu' : n.name),
      h('div', { class: 'xb-temps' }, temps, spaceChip(data.space))));
}

function spaceChip(s) {
  if (!s) return null;
  if (s.free == null) return h('span', { class: 'xb-temp', title: 'Enter the drive size below for the free space' }, `${gb(s.used)} used`);
  return h('span', { class: `xb-temp space${s.free < 10e9 ? ' warn' : ''}`, title: `${gb(s.used)} used of ${gb(s.capacity)}` },
    `${gb(s.free)} free`);
}

// --- header: the console and the drive in it ---
function renderStatus() {
  const c = data.console;
  const s = $('status');
  if (!data.enabled) {
    s.className = 'xb-status warn';
    s.textContent = 'Turned off in config.json (enabled)';
  } else if (c.found) {
    s.className = 'xb-status ok';
    s.textContent = `Connected · ${c.ip}`;
  } else if (!c.checked_ms) {
    s.className = 'xb-status';
    s.textContent = 'Looking for the Xbox…';
  } else {
    s.className = 'xb-status warn';
    s.textContent = c.error === 'ftp' ? 'Xbox found, but FTP login failed' : 'Xbox not found — turn it on with Aurora open';
  }

  const d = data.drive;
  const box = $('drive');
  box.hidden = !c.found || !d;
  if (box.hidden) return;
  if (!d.id) {
    replace(box,
      h('strong', null, 'New drive'),
      h('span', { class: 'grow' }, 'This hard drive has no name yet. Name it so the page remembers which games are on it.'),
      h('button', { type: 'button', class: 'xb-btn', onclick: () => act('/api/name-drive', {}) }, 'Name this drive'));
    return;
  }
  const when = data.drive_scanned_ms ? `checked ${new Date(data.drive_scanned_ms).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}` : 'reading…';
  replace(box,
    h('strong', null, `Drive: ${d.label}`),
    h('span', { class: 'grow' }, data.scanning ? 'Reading what is on the drive…' : when),
    h('button', { type: 'button', class: 'xb-btn quiet', disabled: data.scanning || null,
      onclick: () => act('/api/rescan', {}) }, 'Check again'),
    space(data.space));
}

function space(s) {
  if (!s) return null;
  if (s.capacity == null) {
    const input = h('input', { type: 'number', min: 8, max: 20000, step: 1, inputmode: 'numeric', placeholder: 'e.g. 500',
      'aria-label': 'Drive size in GB' });
    const save = () => { const gb = Number(input.value); if (gb) act('/api/drive-size', { gb }); };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
    return h('div', { class: 'xb-space' },
      h('span', null, `${gb(s.used)} used. For the free space, enter the size printed on the drive:`),
      h('span', { class: 'xb-size' }, input, ' GB ',
        h('button', { type: 'button', class: 'xb-btn quiet', onclick: save }, 'Save')));
  }
  const pct = Math.min(100, (s.used / s.capacity) * 100);
  const bar = h('i');
  bar.style.width = `${pct.toFixed(1)}%`;
  return h('div', { class: 'xb-space' },
    h('div', { class: `xb-bar${pct > 90 ? ' full' : ''}`, role: 'img', 'aria-label': `${Math.round(pct)}% used` }, bar),
    h('span', null, h('strong', null, `about ${gb(s.free)} free`), ` · ${gb(s.used)} used of ${gb(s.capacity)}`));
}

// --- the copy in progress and the queue ---
function renderJob() {
  const j = data.job;
  const box = $('job');
  const byId = new Map(data.games.map((g) => [g.id, g]));
  const waiting = data.queue.filter((id) => !j || id !== j.id);
  box.hidden = !j && !waiting.length;
  if (box.hidden) return;
  const queue = waiting.length ? h('div', { class: 'xb-queue' }, 'Up next:',
    waiting.map((id) => h('span', { class: 'xb-chip' }, byId.get(id)?.name ?? id,
      h('button', { type: 'button', 'aria-label': 'Remove from queue', onclick: () => act('/api/cancel', { id }) }, '×')))) : null;
  if (!j) {
    replace(box, h('div', { class: 'xb-job-main' }, h('strong', null, 'Waiting for the Xbox…'),
      h('span', { class: 'xb-job-meta' }, 'Copies start when the Xbox is on with Aurora open.')), queue);
    box.classList.add('waiting');
    return;
  }
  const g = byId.get(j.id) ?? { id: j.id, name: j.name, art: false };
  const pct = j.total ? Math.min(100, (j.done / j.total) * 100) : 0;
  const bar = h('i');
  bar.style.width = `${pct.toFixed(1)}%`;
  const left = j.speed ? (j.total - j.done) / j.speed : null;
  replace(box,
    cover(g),
    h('div', { class: 'xb-job-main' },
      h('div', { class: 'xb-job-top' },
        h('strong', null, `Copying ${j.name}`),
        h('button', { type: 'button', class: 'xb-btn danger', onclick: () => act('/api/cancel', { id: j.id }) }, 'Stop')),
      h('div', { class: 'xb-bar', role: 'progressbar', 'aria-valuenow': Math.round(pct), 'aria-valuemin': 0, 'aria-valuemax': 100 }, bar),
      h('span', { class: 'xb-job-meta' },
        `${Math.floor(pct)}% · ${gb(j.done)} of ${gb(j.total)}`,
        j.speed ? ` · ${mbs(j.speed)} · about ${duration(left)} left` : ' · starting…'),
      j.file && h('span', { class: 'xb-job-file' }, j.file)),
    queue);
}

function renderLast() {
  const l = data.last;
  const box = $('last');
  const fresh = l && Date.now() - l.ended_ms < 6 * 3600 * 1000;
  box.hidden = !fresh;
  if (!fresh) return;
  const text = {
    done: `${l.name} is on the Xbox (${l.sent ? `${gb(l.sent)} in ${duration(l.seconds)}` : 'nothing was missing'}).`,
    cancelled: `${l.name}: copy stopped. Send it again to finish; finished files are skipped.`,
    failed: `${l.name}: copy failed. ${l.error ?? ''}`,
  }[l.result];
  box.className = `xb-last ${l.result === 'done' ? 'ok' : l.result === 'failed' ? 'fail' : ''}`;
  replace(box, h('span', { class: 'grow' }, text));
}

// --- game tiles ---
function tile(g) {
  const copying = data.job?.id === g.id;
  const queued = !copying && data.queue.includes(g.id);
  const pct = g.size ? Math.floor((g.on_xbox_size / g.size) * 100) : 0;
  const badge = copying ? ['copying', 'Copying']
    : g.state === 'complete' ? ['complete', '✓ On Xbox']
      : g.state === 'partial' ? ['partial', `Partly copied ${pct}%`] : null;
  let button;
  if (copying) {
    button = h('button', { type: 'button', class: 'xb-btn quiet', disabled: true }, 'Copying…');
  } else if (queued) {
    button = h('button', { type: 'button', class: 'xb-btn quiet', onclick: () => act('/api/cancel', { id: g.id }) }, 'Queued · Remove');
  } else if (g.can_play) {
    button = h('button', { type: 'button', class: 'xb-btn play', onclick: () => play(g) }, '▶ Play');
  } else if (g.state === 'complete') {
    button = h('button', { type: 'button', class: 'xb-btn quiet', disabled: true }, 'On Xbox');
  } else {
    button = h('button', { type: 'button', class: 'xb-btn', disabled: !!g.problem || !data.drive?.id || null,
      onclick: () => act('/api/send', { id: g.id }) }, g.state === 'partial' ? 'Finish copy' : 'Send to Xbox');
  }
  return h('article', { class: `xb-game ${g.state}` },
    h('div', { class: 'xb-art' }, cover(g), badge && h('span', { class: `xb-badge ${badge[0]}` }, badge[1])),
    h('div', { class: 'xb-info' },
      h('span', { class: 'xb-name' }, g.name),
      h('span', { class: 'xb-meta' }, gb(g.size)),
      g.on_drives.length ? h('span', { class: 'xb-note' }, `Also on ${g.on_drives.join(', ')}`) : null,
      g.problem && h('span', { class: 'xb-note fail' }, g.problem),
      h('span', { class: 'spacer' }),
      button));
}

function play(g) {
  const n = data.now;
  if (n && !n.error && !n.in_menu && !confirm(`Quit ${n.name} and start ${g.name}?`)) return;
  act('/api/play', { id: g.id });
}

function renderGrid() {
  const q = $('search').value.trim().toLowerCase();
  const list = data.games.filter((g) => (!q || g.name.toLowerCase().includes(q))
    && (filter === 'all' || (filter === 'on') === (g.state === 'complete')));
  const on = data.games.filter((g) => g.state === 'complete').length;
  $('count').textContent = data.drive?.id
    ? `${on} of ${data.games.length} games on ${data.drive.label}`
    : `${data.games.length} games on the server`;
  if (!data.library_ready) return replace($('grid'), h('p', { class: 'xb-empty' }, 'Reading the game library…'));
  replace($('grid'), list.length ? list.map(tile) : h('p', { class: 'xb-empty' }, 'No games match.'));
}

function render() {
  if (!data) return;
  renderStatus();
  renderNow();
  renderJob();
  renderLast();
  renderGrid();
}

// --- polling: every 2 s while copying, 10 s otherwise; paused in the background ---
async function poll() {
  try {
    data = await getJSON('/api/status');
    render();
  } catch (err) {
    $('status').className = 'xb-status warn';
    $('status').textContent = err.status === 403 ? 'Only on the home network' : 'Can’t reach the server';
  }
  schedule();
}

function schedule() {
  clearTimeout(timer);
  if (document.hidden) return;
  const active = data && (data.job || data.queue.length || data.scanning || !data.library_ready || !data.console.checked_ms);
  timer = setTimeout(poll, active ? 2000 : 10000);
}

$('search').addEventListener('input', renderGrid);
$('filter').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-f]');
  if (!b) return;
  filter = b.dataset.f;
  for (const x of $('filter').children) x.setAttribute('aria-pressed', String(x === b));
  renderGrid();
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
poll();
