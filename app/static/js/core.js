// core.js: data, formatting and plumbing shared by every module.
// No DOM of its own except the small el() builder. Imports nothing.

/* ================================================================ events */

const bus = new EventTarget();

/** Listen to an app event. Returns an unsubscribe function. */
export function on(type, fn) {
  const h = (e) => fn(e.detail);
  bus.addEventListener(type, h);
  return () => bus.removeEventListener(type, h);
}
export function emit(type, detail = {}) {
  bus.dispatchEvent(new CustomEvent(type, { detail }));
}

/* ================================================================ hooks */

const HOOKS = new Map();
/** A single named extension point (last registration wins). */
export function registerHook(name, fn) { HOOKS.set(name, fn); }
export function hasHook(name) { return HOOKS.has(name); }
export function callHook(name, ...args) {
  const fn = HOOKS.get(name);
  return fn ? fn(...args) : undefined;
}

/* ================================================================ navigation bridge */
// shell.js installs the real implementation; everything else calls these.

let SHELL = {
  goTab: () => {}, currentTab: () => 'download', setTabDot: () => {},
};
export function _bindShell(impl) { SHELL = { ...SHELL, ...impl }; }
/** Switch tab. Only call for explicit user actions (G-03). opts: {focus=true, scroll=true} */
export function navigate(tab, opts = {}) { return SHELL.goTab(tab, opts); }
export function currentTab() { return SHELL.currentTab(); }
/** Show or clear the 6 px "new" dot on a tab (G-03). */
export function setTabDot(tab, on, label = '') { SHELL.setTabDot(tab, on, label); }

/* ================================================================ api */

export const TOKEN = document.querySelector('meta[name="mt-token"]')?.content || '';

export class ApiError extends Error {
  constructor(message, extra = {}) {
    super(message);
    Object.assign(this, { status: 0, code: '', title: '', body: '', actions: [], detail: '', field: '' }, extra);
  }
  /** One sentence fit for a toast or an inline line. */
  get text() {
    if (this.title && this.body) return `${this.title}. ${this.body}`.replace(/\.\. /, '. ');
    return this.title || this.message;
  }
}

function toApiError(status, payload, fallback) {
  const d = payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : payload;
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    if (d.code || d.title) {
      return new ApiError(d.title || fallback, {
        status, code: d.code || '', title: d.title || '', body: d.body || '',
        actions: Array.isArray(d.actions) ? d.actions : [], detail: d.detail || '',
        params: d.params || {}, ignored: d.ignored,
      });
    }
    if ('field' in d || 'message' in d) {
      return new ApiError(d.message || fallback, { status, field: d.field || '', title: d.message || '' });
    }
  }
  if (Array.isArray(d)) {
    // FastAPI request validation: a list of {loc, msg}.
    return new ApiError('Something in that request was not valid.', {
      status, detail: d.map((x) => `${(x.loc || []).join('.')}: ${x.msg}`).join('\n'),
    });
  }
  if (typeof d === 'string' && d) return new ApiError(d, { status, title: d });
  return new ApiError(fallback, { status });
}

/**
 * fetch() for /api. Adds X-MT-Token on anything that is not a GET, parses
 * JSON or text, and throws ApiError with {status, code, title, body, actions,
 * detail, field} for structured backend errors.
 */
export async function api(url, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const headers = new Headers(opts.headers || {});
  if (method !== 'GET' && method !== 'HEAD') headers.set('X-MT-Token', TOKEN);
  let body = opts.body;
  if (body !== undefined && !(body instanceof FormData) && typeof body !== 'string') {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(url, { ...opts, method, headers, body });
  } catch (e) {
    throw new ApiError("Media Toolkit isn't responding. It may have been closed.", { code: 'offline', detail: String(e) });
  }
  const type = r.headers.get('content-type') || '';
  let payload = null;
  try { payload = type.includes('json') ? await r.json() : await r.text(); } catch (_) { payload = null; }
  if (!r.ok) throw toApiError(r.status, payload, r.statusText || `Request failed (${r.status})`);
  return payload;
}
export const get = (url) => api(url);
export const post = (url, body = {}) => api(url, { method: 'POST', body });
export const del = (url) => api(url, { method: 'DELETE' });

/**
 * Multipart upload with real progress (T-13). onProgress(fraction 0..1).
 * Resolves with the parsed JSON, rejects with ApiError.
 */
export function upload(url, formData, { onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('X-MT-Token', TOKEN);
    xhr.responseType = 'text';
    if (onProgress) xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let payload = xhr.responseText;
      try { payload = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(toApiError(xhr.status, payload, `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new ApiError("Media Toolkit isn't responding. It may have been closed.", { code: 'offline' }));
    xhr.onabort = () => reject(new ApiError('Cancelled', { code: 'aborted' }));
    if (signal) signal.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(formData);
  });
}

/* ================================================================ DOM */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Escape text for the rare place that must build HTML. Prefer el(). */
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/**
 * Tiny DOM builder. el('button.btn.secondary#id', {attrs}, ...children)
 * attrs: class, text, dataset {}, on {event: fn}, style is not supported on
 * purpose (use classes). Boolean true sets an empty attribute, false/null skips.
 * Children: strings become text nodes (never HTML), nodes, arrays, null skipped.
 */
export function el(spec, attrs, ...children) {
  if (attrs && (attrs instanceof Node || typeof attrs !== 'object' || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  const m = /^([a-z0-9-]+)?((?:[.#][\w-]+)*)$/i.exec(spec);
  const tag = (m && m[1]) || 'div';
  const node = tag === 'svg' || tag === 'use' ? document.createElementNS('http://www.w3.org/2000/svg', tag) : document.createElement(tag);
  if (m && m[2]) {
    for (const part of m[2].match(/[.#][\w-]+/g)) {
      if (part[0] === '.') node.classList.add(part.slice(1));
      else node.id = part.slice(1);
    }
  }
  let value, checked;
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'value' && v !== null && v !== undefined) { value = v; continue; }
      if (k === 'checked') { checked = !!v; continue; }
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') { String(v).split(/\s+/).filter(Boolean).forEach((c) => node.classList.add(c)); }
      else if (k === 'text') node.textContent = v;
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      else if (k === 'html') throw new Error('el(): html is not allowed; build nodes instead');
      else node.setAttribute(k, v === true ? '' : String(v));
    }
  }
  append(node, children);
  // After the children, so a <select> can pick one of its options.
  if (value !== undefined && 'value' in node) node.value = String(value);
  if (checked !== undefined && 'checked' in node) node.checked = checked;
  return node;
}
function append(node, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(node, c);
    else node.append(c instanceof Node ? c : String(c));
  }
}
/** Replace all children of node. */
export function setChildren(node, ...children) { node.replaceChildren(); append(node, children); return node; }
/** Set textContent only when it changed (keeps selections and avoids layout work). */
export function setText(node, text) {
  const t = String(text ?? '');
  if (node.textContent !== t) node.textContent = t;
}
/** A <bdi> for a user-supplied string inside app text (titles, names, URLs). */
export const bdi = (text) => el('bdi', { dir: 'auto' }, String(text ?? ''));

export const prefersReducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
/** scrollIntoView that honours reduced motion (V-06). */
export function scrollToEl(node, block = 'start') {
  node?.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block });
}

/* ================================================================ formatting */

const NF = new Intl.NumberFormat('en-US');
/** 1550 -> '1,550' */
export const num = (n) => NF.format(Math.round(Number(n) || 0));

/** Bytes, Windows style (1024): '35.6 MB', '245 MB', '1.6 GB', '12 KB', '1.9 KB', '512 B'. */
export function bytes(n) {
  n = Number(n);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const dec = i === 0 ? (v < 10 ? 1 : 0) : (v < 100 ? 1 : 0);
  let s = v.toFixed(dec);
  if (s.endsWith('.0')) s = s.slice(0, -2);
  return `${s} ${units[i]}`;
}
/** '12.4 of 35.6 MB' (done shown in the unit of total). */
export function bytesOf(done, total) {
  if (!total) return bytes(done);
  const t = bytes(total);
  const unit = t.split(' ')[1];
  const pow = { B: 0, KB: 1, MB: 2, GB: 3, TB: 4 }[unit] || 0;
  const v = (Number(done) || 0) / 1024 ** pow;
  let s = v.toFixed(pow >= 2 && v < 100 ? 1 : 0);
  if (s.endsWith('.0')) s = s.slice(0, -2);
  return `${s} of ${t}`;
}
/** Whole megabytes with grouping: '812 of 1,550 MB'. */
export function mbOf(done, total) {
  const mb = (b) => num((Number(b) || 0) / 1048576);
  return total ? `${mb(done)} of ${mb(total)} MB` : `${mb(done)} MB`;
}
/** '2.1 MB/s' */
export function speed(bps) { return bps ? `${bytes(bps)}/s` : ''; }

/** Seconds -> '0:35', '10:35', '1:12:40'. */
export function duration(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const ss = String(s).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}
/** Parse 'm:ss', 'h:mm:ss' or plain seconds. Returns seconds or NaN. */
export function parseTime(text) {
  const t = String(text || '').trim();
  if (!t) return NaN;
  if (/^\d+(\.\d+)?$/.test(t)) return Number(t);
  const parts = t.split(':');
  if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return NaN;
  return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
}
/** ETA wording (G-08), used everywhere. */
export function eta(sec) {
  if (sec === null || sec === undefined || !Number.isFinite(Number(sec))) return '';
  sec = Math.max(0, Math.round(Number(sec)));
  if (sec < 60) return `${sec} s left`;
  if (sec < 90) return 'about 1 min left';
  if (sec < 90 * 60) return `about ${Math.round(sec / 60)} min left`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return m ? `about ${h} h ${m} min left` : `about ${h} h left`;
}
/** A span of time in words: '2 h 10 min', '45 min', '30 s'. */
export function span(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if (sec < 60) return `${sec} s`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  if (!h) return `${m} min`;
  return m ? `${h} h ${m} min` : `${h} h`;
}
const pad2 = (n) => String(n).padStart(2, '0');
/** Epoch seconds -> '14:32' (24 h, local time). */
export function clock(epoch) {
  const d = new Date(Number(epoch) * 1000);
  return Number.isFinite(d.getTime()) ? `${pad2(d.getHours())}:${pad2(d.getMinutes())}` : '';
}
const DATE_FMT = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
const SHORT_FMT = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' });
/** '20141110', '2014-11-10', epoch seconds or a Date -> '10 Nov 2014'. */
export function date(v) {
  const d = toDate(v);
  return d ? DATE_FMT.format(d) : '';
}
/** '23 Sep' */
export function shortDate(v) { const d = toDate(v); return d ? SHORT_FMT.format(d) : ''; }
function toDate(v) {
  if (v instanceof Date) return v;
  if (v === null || v === undefined || v === '') return null;
  const s = String(v);
  let m = /^(\d{4})(\d{2})(\d{2})$/.exec(s) || /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const n = Number(v);
  if (Number.isFinite(n) && n > 0) return new Date(n < 1e12 ? n * 1000 : n);
  const d = new Date(s);
  return Number.isFinite(d.getTime()) ? d : null;
}
/** Relative time from epoch seconds (Q-01): 'just now', '5 min ago', 'today 14:32', '23 Sep 14:32'. */
export function relTime(epoch, now = Date.now() / 1000) {
  const t = Number(epoch);
  if (!Number.isFinite(t) || !t) return '';
  const age = now - t;
  if (age < 60) return 'just now';
  if (age < 3600) return `${Math.floor(age / 60)} min ago`;
  const d = new Date(t * 1000), today = new Date(now * 1000);
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (d.toDateString() === today.toDateString()) return `today ${hm}`;
  return `${SHORT_FMT.format(d)} ${hm}`;
}
/** 2 -> '2nd' */
export function ordinal(n) {
  n = Math.round(Number(n) || 0);
  const s = ['th', 'st', 'nd', 'rd'], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}
/** plural(3, 'video') -> '3 videos'; plural(1, 'is', 'are', false) -> 'is' */
export function plural(n, one, many = one + 's', withNumber = true) {
  const word = Number(n) === 1 ? one : many;
  return withNumber ? `${num(n)} ${word}` : word;
}
/** Shorten with an ellipsis, never cutting inside a surrogate pair. */
export function truncate(text, max = 60) {
  const chars = Array.from(String(text ?? ''));
  return chars.length > max ? chars.slice(0, max - 1).join('').trimEnd() + '…' : chars.join('');
}
/** 'https://www.youtube.com/watch?v=x' -> 'youtube.com/watch?v=x' (unknown-title placeholder). */
export function hostPath(url) {
  try {
    const u = new URL(url);
    return (u.host.replace(/^www\./, '') + u.pathname.replace(/\/$/, '') + u.search).slice(0, 120);
  } catch (_) { return String(url || ''); }
}
const SITES = [
  [['youtube.com', 'youtu.be', 'youtube-nocookie.com'], 'YouTube'], [['instagram.com'], 'Instagram'],
  [['tiktok.com'], 'TikTok'], [['x.com', 'twitter.com'], 'X'], [['facebook.com', 'fb.watch', 'fb.com'], 'Facebook'],
  [['twitch.tv'], 'Twitch'], [['vimeo.com'], 'Vimeo'], [['reddit.com', 'redd.it'], 'Reddit'], [['kick.com'], 'Kick'],
  [['soundcloud.com'], 'SoundCloud'], [['dailymotion.com', 'dai.ly'], 'Dailymotion'],
];
/** Same rule as the backend's site_name(): 'YouTube', or the host without www. */
export function siteName(url) {
  let host = '';
  try { host = new URL(url).hostname.toLowerCase(); } catch (_) { return ''; }
  for (const [domains, name] of SITES) if (domains.some((d) => host === d || host.endsWith('.' + d))) return name;
  return host.replace(/^(www|m|mobile)\./, '');
}
/** True for youtube.com / youtu.be links (sponsor segments, deep links). */
export const isYouTube = (url) => siteName(url) === 'YouTube';

const LANG_EN = (() => { try { return new Intl.DisplayNames(['en'], { type: 'language' }); } catch (_) { return null; } })();
/** 'es' -> 'Spanish'. Falls back to the code. */
export function languageName(code) {
  if (!code) return '';
  try { return LANG_EN?.of(String(code).split('-')[0]) || code; } catch (_) { return code; }
}
/** 'fa' -> 'فارسی' (the language's own name). */
export function nativeLanguageName(code) {
  try { return new Intl.DisplayNames([code], { type: 'language' }).of(code) || code; } catch (_) { return code; }
}

/* ================================================================ links (G-06) */

const LINK_RE = /^https?:\/\/\S+$/i;
/**
 * Split pasted text on whitespace (never on commas), keep http(s) links,
 * drop duplicates. Returns {links, ignored, empty}.
 */
export function parseLinks(text) {
  const links = [];
  let ignored = 0;
  const tokens = String(text || '').split(/\s+/).filter(Boolean);
  for (const t of tokens) {
    if (LINK_RE.test(t) && t.length <= 4096) { if (!links.includes(t)) links.push(t); }
    else ignored++;
  }
  return { links, ignored, empty: tokens.length === 0 };
}
/** The inline message for a parse result (G-06), or null when nothing to say. */
export function linkProblem(parsed) {
  if (parsed.empty) return null;
  if (!parsed.links.length) return { tone: 'error', text: "That doesn't look like a link. Copy the address from your browser; it starts with https://" };
  if (parsed.ignored === 1) return { tone: 'note', text: "Ignored 1 line that isn't a link." };
  if (parsed.ignored > 1) return { tone: 'note', text: `Ignored ${num(parsed.ignored)} lines that aren't links.` };
  return null;
}

/* ================================================================ config (S-01) */

export const CONFIG = {};           // the saved settings, always current
export const DEFAULTS = {};         // config.DEFAULTS from the server
export const SETTINGS_META = {};    // filename_presets, sponsor_labels, folder_problems, notice, ...
let configLoaded = false;
let resolveConfigReady;
export const configReady = new Promise((r) => { resolveConfigReady = r; });

export async function loadConfig() {
  const r = await get('/api/settings');
  const { config, defaults, ...meta } = r;
  Object.assign(CONFIG, config || {});
  Object.assign(DEFAULTS, defaults || {});
  Object.assign(SETTINGS_META, meta);
  configLoaded = true;
  resolveConfigReady(CONFIG);
  emit('config', { source: 'load', keys: Object.keys(CONFIG) });
  return CONFIG;
}
export const isConfigLoaded = () => configLoaded;
/** Re-read folder_problems and notice (after a folder change) without re-applying settings. */
export async function refreshSettingsMeta() {
  const { config, defaults, ...meta } = await get('/api/settings');
  Object.assign(SETTINGS_META, meta);
  emit('settings-meta', SETTINGS_META);
  return SETTINGS_META;
}

/**
 * Save a partial settings patch. Updates CONFIG from the server's answer and
 * emits 'config' {source, keys}. Throws ApiError (with .field) on failure.
 */
export async function saveSettings(patch, source = null) {
  const r = await post('/api/settings', patch);
  const before = { ...CONFIG };
  Object.assign(CONFIG, r.config || {});
  const keys = Object.keys(CONFIG).filter((k) => JSON.stringify(before[k]) !== JSON.stringify(CONFIG[k]) || k in patch);
  emit('config', { source, keys });
  return CONFIG;
}

/** Client-side checks before posting (S-01). Each returns an error message or ''. */
export const validators = {
  proxy: (v) => (!v || /^(https?|socks4a?|socks5h?):\/\/.+/i.test(v) ? '' : 'Start with http://, https://, socks4:// or socks5://'),
  range: (min, max, msg) => (v) => {
    if (v === '' || v === null || v === undefined) return '';
    const n = Number(v);
    return Number.isFinite(n) && n >= min && n <= max ? '' : (msg || `Choose between ${num(min)} and ${num(max)}.`);
  },
  min: (min, msg) => (v) => (v === '' || (Number.isFinite(Number(v)) && Number(v) >= min) ? '' : (msg || `Enter ${min} or more.`)),
  country: (v) => (!v || /^[A-Za-z]{2}$/.test(v) ? '' : 'Use a two-letter country code such as US.'),
};
/** Speed limit: '5M' / '500K' / '1G' -> MB/s number ('' for none). */
export function rateToMBps(rate) {
  const m = /^\s*(\d+(?:\.\d+)?)\s*([KMG])?/i.exec(String(rate || ''));
  if (!m) return '';
  const n = Number(m[1]), u = (m[2] || 'M').toUpperCase();
  const v = u === 'K' ? n / 1024 : u === 'G' ? n * 1024 : n;
  return Math.round(v * 100) / 100;
}
/** MB/s number -> '5M' ('' for none). */
export const mbpsToRate = (v) => (v === '' || v === null || v === undefined || !Number(v) ? '' : `${Number(v)}M`);

const SAVE_FADE = new WeakMap();
function saveStateEl(control, explicit) {
  if (explicit instanceof Element) return explicit;
  if (typeof explicit === 'string') return document.querySelector(explicit);
  const scope = control.closest('[data-save-scope]');
  return scope ? scope.querySelector('.save-state') : null;
}
/** Show 'Saving…' / '✓ Saved' / an error in a section's .save-state (S-01). */
export function showSaveState(node, state, message = '') {
  if (!node) return;
  clearTimeout(SAVE_FADE.get(node));
  node.classList.remove('is-saved', 'is-error', 'is-fading');
  if (state === 'saving') node.textContent = 'Saving…';
  else if (state === 'saved') {
    node.textContent = '✓ Saved';
    node.classList.add('is-saved');
    SAVE_FADE.set(node, setTimeout(() => {
      node.classList.add('is-fading');
      SAVE_FADE.set(node, setTimeout(() => { node.textContent = ''; node.classList.remove('is-fading', 'is-saved'); }, 450));
    }, 2000));
  } else if (state === 'error') { node.textContent = message; node.classList.add('is-error'); }
  else node.textContent = '';
}

function fieldErrorEl(control, create) {
  const host = control.closest('.field, .setting-control, .inline-field, label.check') || control.parentElement;
  let node = host.querySelector(':scope > .field-error');
  if (!node && create) {
    node = el('div.field-error', { role: 'alert' });
    host.append(node);
  }
  return node;
}
/** Show (or clear with '') the red line under a control and set aria-invalid. */
export function setFieldError(control, message) {
  const node = fieldErrorEl(control, !!message);
  if (message) {
    control.setAttribute('aria-invalid', 'true');
    node.textContent = message;
    if (node.id === '') node.id = 'err-' + Math.random().toString(36).slice(2, 8);
    control.setAttribute('aria-describedby', node.id);
  } else {
    control.removeAttribute('aria-invalid');
    if (node) { node.remove(); control.removeAttribute('aria-describedby'); }
  }
}

function readControl(node) {
  if (node instanceof HTMLInputElement && node.type === 'checkbox') return node.checked;
  if (node.matches?.('fieldset, [role=radiogroup], .seg')) {
    const checked = node.querySelector('input[type=radio]:checked');
    return checked ? checked.value : '';
  }
  return node.value;
}
function writeControl(node, v) {
  if (node instanceof HTMLInputElement && node.type === 'checkbox') { node.checked = !!v; return; }
  if (node.matches?.('fieldset, [role=radiogroup], .seg')) {
    for (const r of node.querySelectorAll('input[type=radio]')) r.checked = String(r.value) === String(v);
    return;
  }
  if (node instanceof HTMLSelectElement) {
    const val = v === null || v === undefined ? '' : String(v);
    node.value = val;
    // A value the list does not offer, or a disabled option (FIN-15): keep the list's own first choice visible.
    return;
  }
  node.value = v === null || v === undefined ? '' : String(v);
}
function isFocused(node) {
  const a = document.activeElement;
  return a && (a === node || node.contains(a));
}

/**
 * Bind a control to a config key with autosave (S-01).
 * - checkbox, radio group (fieldset/.seg), select: save on change
 * - text/number/textarea: save on blur or Enter, debounced, only when changed
 * opts: {toServer(v) -> value | {key: value,...} patch, fromServer(configValue, CONFIG) -> ui value,
 *        validate(uiValue) -> message|'', status: element|selector (defaults to the
 *        [data-save-scope] ancestor's .save-state), debounce=400, onSaved(CONFIG), patch(uiValue) -> object}
 * Re-applies itself on every 'config' event for its key unless the control has focus.
 * Returns {apply(), save(), destroy()}.
 */
export function bindSetting(control, key, opts = {}) {
  const { toServer = (v) => v, fromServer = (v) => v, validate, debounce = 400, onSaved } = opts;
  const isText = (control instanceof HTMLInputElement && !['checkbox', 'radio'].includes(control.type)) || control instanceof HTMLTextAreaElement;
  const id = Symbol(key);
  let lastSaved = null;
  let timer = null;
  let inflight = null;

  const apply = () => {
    // Never overwrite what the user is touching right now.
    if (!(key in CONFIG) || isFocused(control)) return;
    writeControl(control, fromServer(CONFIG[key], CONFIG));
    lastSaved = JSON.stringify(readControl(control));
  };

  const save = async () => {
    const value = readControl(control);
    const sig = JSON.stringify(value);
    if (sig === lastSaved) { setFieldError(control, ''); return; }
    const problem = validate ? validate(value) : '';
    if (problem) { setFieldError(control, problem); return; }
    const out = opts.patch ? opts.patch(value) : toServer(value);
    const patch = opts.patch ? out : { [key]: out };
    const status = saveStateEl(control, opts.status);
    showSaveState(status, 'saving');
    const mine = (inflight = saveSettings(patch, id));
    try {
      await mine;
      if (mine !== inflight) return;
      lastSaved = sig;
      setFieldError(control, '');
      showSaveState(status, 'saved');
      onSaved?.(CONFIG);
    } catch (e) {
      if (mine !== inflight) return;
      showSaveState(status, '');
      const msg = e.field ? e.message : `Couldn't save: ${e.text || e.message}`;
      setFieldError(control, msg);
    }
  };

  const schedule = () => { clearTimeout(timer); timer = setTimeout(save, isText ? debounce : 0); };
  const onChange = () => { if (!isText) schedule(); };
  const onBlur = () => { if (isText) schedule(); };
  const onKey = (e) => {
    if (isText && e.key === 'Enter' && !(control instanceof HTMLTextAreaElement)) { e.preventDefault(); schedule(); }
  };
  control.addEventListener('change', onChange);
  control.addEventListener('blur', onBlur, true);
  control.addEventListener('keydown', onKey);
  const off = on('config', (d) => { if (d.source !== id && (!d.keys || d.keys.includes(key))) apply(); });
  if (configLoaded) apply();
  return {
    apply, save,
    destroy() {
      off(); clearTimeout(timer);
      control.removeEventListener('change', onChange);
      control.removeEventListener('blur', onBlur, true);
      control.removeEventListener('keydown', onKey);
    },
  };
}

/* ================================================================ machine state */
// One source of truth per fact (G-12). Every loader emits its event name.

export const STATE = {
  setup: null,        // GET /api/setup
  hw: null,           // GET /api/hardware  (nvidia_name, gpu_ready, gpu_pack_size_mb, models[], ...)
  cap: null,          // GET /api/capabilities
  packs: null,        // GET /api/packs     (progress {busy, task, percent, message, error})
  models: null,       // GET /api/models    ({models, downloads})
  about: null,        // GET /api/about
  loadErrors: {},     // what failed to load at startup: {hardware: ApiError, ...}
};

async function load(name, url, event) {
  try {
    STATE[name] = await get(url);
    delete STATE.loadErrors[event];
    emit(event, STATE[name]);
    return STATE[name];
  } catch (e) {
    STATE.loadErrors[event] = e;
    emit('load-error', { what: event, error: e });
    throw e;
  }
}
export const loadSetup = () => load('setup', '/api/setup', 'setup');
export const loadHardware = () => load('hw', '/api/hardware', 'hardware');
export const loadCapabilities = () => load('cap', '/api/capabilities', 'capabilities');
export const loadModels = () => load('models', '/api/models', 'models');
export const loadAbout = () => load('about', '/api/about', 'about');
export async function loadPacks() {
  const r = await load('packs', '/api/packs', 'packs');
  if (r?.progress?.busy) watchPacks();
  return r;
}
/** Settings > Re-check hardware. Returns the fresh summary (plus encoders). */
export async function recheckHardware() {
  const r = await post('/api/hardware/recheck');
  const models = STATE.hw?.models;
  STATE.hw = { ...(STATE.hw || {}), ...r, models: r.models || models };
  if (r.encoders && STATE.cap) STATE.cap = { ...STATE.cap, encoders: r.encoders, hardware: r.encoders.filter((e) => e.hw).map((e) => e.id) };
  emit('hardware', STATE.hw);
  if (STATE.cap) emit('capabilities', STATE.cap);
  loadHardware().catch(() => {});
  return r;
}

let packTimer = null;
/** Poll /api/packs every second while a pack download runs (UI-9). */
export function watchPacks() {
  if (packTimer) return;
  packTimer = setInterval(async () => {
    try {
      const r = await get('/api/packs');
      STATE.packs = r;
      emit('packs', r);
      if (!r.progress?.busy && !packInstalling) { clearInterval(packTimer); packTimer = null; }
    } catch (_) { /* keep trying while the install runs */ }
  }, 1000);
}
let packInstalling = null;
/**
 * Start a pack install ('gpu' | 'ffmpeg'). Polls progress from the first
 * moment (UI-9); resolves with the server's answer ({ok, error, ...}) once
 * the download ends. Emits 'packs' on every poll and 'pack-done' at the end.
 */
export function installPack(kind) {
  if (packInstalling) return packInstalling;
  const url = kind === 'ffmpeg' ? '/api/packs/ffmpeg' : '/api/packs/gpu';
  STATE.packs = { ...(STATE.packs || {}), progress: { busy: true, task: kind, percent: 0, message: '', error: '' } };
  emit('packs', STATE.packs);
  packInstalling = post(url).then((r) => r, (e) => ({ ok: false, error: e.text || e.message }))
    .then(async (r) => {
      packInstalling = null;
      try { await loadPacks(); } catch (_) {}
      if (kind === 'gpu') loadHardware().catch(() => {});
      if (kind === 'ffmpeg') { loadHardware().catch(() => {}); loadCapabilities().catch(() => {}); }
      emit('pack-done', { kind, result: r });
      return r;
    });
  watchPacks();
  return packInstalling;
}
export const packInstallRunning = () => !!packInstalling || !!STATE.packs?.progress?.busy;

/* ================================================================ files and pages */

/** Open a file with its default app (Play, Open) or a folder. Throws ApiError. */
export const openPath = (path) => post('/api/open', { path });
/** Show a file selected in Explorer, or open a folder. Throws ApiError. */
export const revealPath = (path) => post('/api/reveal', { path });
/** Open an allowed web page in the normal browser (help pages, transcript sources). */
export const openUrl = (url, stem = '') => post('/api/open-url', stem ? { url, stem } : { url });
/** Native folder picker. Resolves to the chosen path or ''. */
export async function pickFolder(start = '') { return (await post('/api/pick-folder', { path: start })).path || ''; }
/** Native file picker. kind is a hint for the filter ('cookies'). */
export async function pickFile(start = '', kind = '') { return (await post('/api/pick-file', { path: start, kind })).path || ''; }

/** Copy text; checks the fallback's result (UI-28). Resolves true/false. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    const ta = el('textarea', { class: 'sr-only', 'aria-hidden': 'true' });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }
}

/* ================================================================ notifications (G-14) */

export const notify = {
  supported: () => 'Notification' in window,
  /** Ask only once, after the first finished download, if never asked. */
  canAsk: () => 'Notification' in window && Notification.permission === 'default' && isConfigLoaded() && !CONFIG.notify_prompted,
  enabled: () => 'Notification' in window && Notification.permission === 'granted',
  async ask() {
    let result = 'default';
    try { result = await Notification.requestPermission(); } catch (_) {}
    saveSettings({ notify_prompted: true }).catch(() => {});
    return result;
  },
  decline() { return saveSettings({ notify_prompted: true }).catch(() => {}); },
  /** Show a system notification when the window is in the background. */
  send(title, body, onClick) {
    if (!notify.enabled() || (!document.hidden && document.hasFocus())) return null;
    try {
      const n = new Notification(title, { body, icon: '/static/icon.svg', tag: 'media-toolkit' });
      n.onclick = () => { window.focus(); n.close(); onClick?.(); };
      return n;
    } catch (_) { return null; }
  },
};
