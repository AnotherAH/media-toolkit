// download.js: the Download tab.
//   Link card: the link box (G-05, G-06) and a preview that fits the link
//   (D-07 video, D-01 playlist or channel, D-03 live, D-09 several links),
//   with the per-link choices inside it: part of the video, the exact stream
//   (D-10), the size estimate (D-11) and the playlist scope and filters
//   (D-01, D-12). Per-link choices reset after every job (D-02).
//   'What you get': remembered choices that save themselves (D-05, D-06).
//   'Recent downloads': this session's downloads as compact rows (D-08, G-14).
//   The '1,700+ other sites' link opens the site checker as a dialog (S-11).
import {
  el, get, post, bdi, setText, setChildren, on, CONFIG, DEFAULTS, STATE, bindSetting, saveSettings,
  setFieldError, showSaveState, validators, registerHook, navigate, notify, linkProblem,
  num, bytes, duration, parseTime, date, plural, truncate, hostPath, siteName, isYouTube, languageName,
  copyText,
} from './core.js';
import {
  icon, button, linkButton, iconButton, callout, disclosure, pageHead, card, segmented, segValue, setSegValue,
  linkBox, openDialog, setBusy, clearBusy, withBusy, spinner, toast,
} from './ui.js';
import {
  JobView, SESSION_START, sortNewest, renderCard, patchCard, displayTitle, putLink, allJobs, runAction,
  actionLabel,
} from './jobs.js';

/* ================================================================ choices and copy */

const QUALITIES = [
  ['best', 'Best available'], ['2160', '4K'], ['1440', '1440p'], ['1080', '1080p'],
  ['720', '720p'], ['480', '480p'], ['smallest', 'Smallest file'],
];
const AUDIO_FORMATS = [
  ['mp3', 'MP3 · plays everywhere'], ['m4a', 'M4A · smaller, same quality'], ['opus', 'Opus · smallest'],
  ['flac', 'FLAC · lossless, large'], ['wav', 'WAV · uncompressed, very large'],
];
const SUBTITLES = [['none', 'None'], ['en', 'English'], ['all', 'All available'], ['custom', 'Other languages…']];
const CONTAINERS = [['mp4', 'MP4'], ['mkv', 'MKV'], ['webm', 'WebM']];
const RECODE_QUALITY = [['high', 'High'], ['balanced', 'Balanced'], ['small', 'Small file']];
const THUMB_FORMATS = [['', 'Original'], ['jpg', 'JPG'], ['png', 'PNG']];
const ORDERS = [
  { value: '', label: 'Playlist order' }, { value: 'reverse', label: 'Reverse' }, { value: 'random', label: 'Shuffle' },
];
const NO_RECODE = 'No, keep the original (fastest)';
const H264_NOTE = 'Most sites only offer H.264 up to 1080p. Turn off “Plays on any device” for higher resolutions.';
const LIVE_TITLE = 'Live streams are recorded on the Live tab';
const LIVE_TEXT = 'This is a live stream. The Download tab saves finished videos; record it on the Live tab instead.';
const UPCOMING_TEXT = "This stream hasn't started yet. The Live tab can wait for it and record it.";

// The remembered choices (D-06) and the spec defaults, used until GET /api/settings answers.
const FALLBACK = {
  dl_mode: 'video', dl_quality: '1080', dl_compatible: true, dl_audio_codec: 'mp3', dl_container: 'mp4',
  dl_subtitles: 'none', dl_subtitle_langs: 'en', dl_auto_subs: false, dl_embed_subs: true, sponsorblock: false,
  embed_chapters: true, dl_split_chapters: false, recode_encoder: '', recode_quality: 'balanced',
  normalize_audio: false, write_description: false, write_comments: false, dl_max_comments: 200,
  dl_write_thumbnail: false, convert_thumbnails: '', dl_write_link: false, write_info_json: false,
};
const REMEMBERED = Object.keys(FALLBACK);
const defaultOf = (key) => (key in DEFAULTS ? DEFAULTS[key] : FALLBACK[key]);

const LANG_CODE = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;
function langCodes(v) {
  const codes = String(v || '').split(/[,\s]+/).filter(Boolean);
  if (!codes.length) return 'Enter at least one language code, e.g. fa.';
  return codes.every((c) => LANG_CODE.test(c)) ? '' : 'Use language codes separated by commas, e.g. fa, es.';
}
const QUALITY_VALUES = QUALITIES.map(([v]) => v);
function normQuality(v) {
  const s = String(v ?? '');
  if (QUALITY_VALUES.includes(s)) return s;
  if (s === 'compatible') return 'best';
  if (s === '360') return '480';
  return '1080';
}

/* ================================================================ state */

const R = {};               // DOM references
let box = null;             // linkBox handle
let currentLinks = [];      // the http(s) links in the box, de-duplicated
let currentKey = null;
let pasted = false;
let probeTimer = null;

/** Per-link choices: never saved, never carried to the next link (D-02). */
function freshLink(url = '') {
  return {
    url,
    clipOpen: false, clip: null, clipFrom: '0:00', clipTo: '', clipError: '',
    formatsOpen: false, exact: null,
    scope: 'video',                                         // 'Just this video' | 'Whole playlist'
    pl: {
      mode: 'all', first: 10, items: '', chosen: 0, archive: true, stopKnown: false,
      minLen: '', maxLen: '', after: '', before: '', title: '', views: '', maxMb: '', order: '', concat: false,
    },
  };
}
let L = freshLink();

/* ================================================================ link probes (UI-13: keyed by URL) */

const probes = new Map();   // url -> {url, state: 'wait'|'loading'|'ok'|'error', info, error, at}
const probeQueue = [];
let probing = 0;
const MAX_PROBES = 3;

function requestProbe(url) {
  const old = probes.get(url);
  if (old && (old.state !== 'error' || Date.now() - old.at < 30000)) return old;
  const p = { url, state: 'wait', at: Date.now() };
  probes.set(url, p);
  probeQueue.push(p);
  pumpProbes();
  return p;
}
function pumpProbes() {
  while (probing < MAX_PROBES && probeQueue.length) {
    const p = probeQueue.shift();
    if (!currentLinks.includes(p.url)) { probes.delete(p.url); continue; }   // no longer in the box
    probing++;
    p.state = 'loading';
    get(`/api/probe?url=${encodeURIComponent(p.url)}`)
      .then((info) => { p.state = 'ok'; p.info = info || {}; }, (e) => { p.state = 'error'; p.error = e; })
      .finally(() => { p.at = Date.now(); probing--; pumpProbes(); onProbeSettled(p); });
  }
  trimProbes();
}
function trimProbes() {
  if (probes.size <= 60) return;
  for (const [url, p] of probes) {
    if (probes.size <= 40) break;
    if (!currentLinks.includes(url) && p.state !== 'loading') probes.delete(url);
  }
}
function onProbeSettled(p) {
  // A slower answer for a link that is no longer in the box never reaches the screen (UI-13).
  if (currentLinks.includes(p.url)) renderPreview();
}
const probeOf = (url) => probes.get(url) || null;
const isLive = (info) => !!info && (info.is_live || info.live_status === 'is_live' || info.live_status === 'is_upcoming');
const isUpcoming = (info) => !!info && info.live_status === 'is_upcoming';
/** The probe result when exactly one link is in the box and it was read. */
function singleInfo() {
  if (currentLinks.length !== 1) return null;
  const p = probeOf(currentLinks[0]);
  return p?.state === 'ok' ? p.info : null;
}
function singleVideo() {
  const info = singleInfo();
  return info && info.kind !== 'playlist' && !isLive(info) ? info : null;
}
const audioMode = () => segValue(R.mode) === 'audio';
/** The exact stream picked for this link (D-10). In Audio only it counts only for a sound stream. */
function exactNow() {
  if (currentLinks.length !== 1 || L.url !== currentLinks[0] || !L.exact) return null;
  return audioMode() && L.exact.kind !== 'audio' ? null : L.exact;
}

/* ================================================================ small builders */

const vis = (node, show) => { if (node && node.hidden === !!show) node.hidden = !show; };
function selectEl(id, options, cls = 'select-short') {
  return el(`select#${id}.${cls}`, options.map(([v, l]) => el('option', { value: v }, l)));
}
function check(id, text, opts = {}) {
  const input = el(`input#${id}`, { type: 'checkbox', checked: !!opts.checked });
  const label = el('label.check', input, el('span.check-text', text, opts.help ? el('span.help', opts.help) : null));
  label.input = input;
  return label;
}
/** A labelled field whose label is a <label for>, so a chip or button can sit inside it. */
function field(labelText, control, extra = [], opts = {}) {
  return el('div.field', { class: opts.cls }, el('label.label', { for: control.id }, labelText), control, extra);
}
function metaLine(parts) {
  const out = [];
  parts.filter(Boolean).forEach((p, i) => { if (i) out.push(' · '); out.push(p); });
  return out;
}
function thumbEl(src, size, kindIcon = 'video') {
  const t = el(`div.thumb${size ? `.${size}` : ''}`, icon(kindIcon));
  if (src && /^https?:\/\//i.test(src)) {
    const img = el('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer', src });
    img.addEventListener('error', () => img.remove());
    t.append(img);
  }
  return t;
}
/** '4K', '1080p' for a picture height (D-07). */
function resLabel(h) {
  h = Number(h) || 0;
  if (h >= 4320) return '8K';
  if (h >= 2160) return '4K';
  return h ? `${h}p` : '';
}
function techDetails(title, body, detail) {
  const text = String(detail || '').trim();
  if (!text) return null;
  const copy = button('Copy details', 'quiet', { size: 'sm' });
  copy.addEventListener('click', async () => toast((await copyText(`${title}\n${body}\n\n${text}`)) ? 'Details copied.' : "Couldn't copy. Try again.", { tone: 'ok' }));
  return el('details.tech', el('summary', icon('chevron-right', { cls: 'chev' }), 'Technical details'), el('pre', { dir: 'auto' }, text), copy);
}

/* ================================================================ mount */

export function mount(section) {
  const sitesLink = linkButton('1,700+ other sites', () => openSiteChecker(sitesLink));
  section.append(
    pageHead('Download video or audio', ['Paste a link from YouTube, Instagram, TikTok, X, Reddit, Vimeo and ', sitesLink, '.']),
    buildLinkCard(),
    buildGetCard(),
    buildRecentCard(),
  );
  bindChoices();
  wireLinkBox();

  registerHook('download.setLink', (url, { run = true } = {}) => {
    R.input.value = url;
    pasted = !!run;
    R.input.dispatchEvent(new Event('input', { bubbles: true }));
    R.input.focus({ preventScroll: true });
    R.input.setSelectionRange(R.input.value.length, R.input.value.length);
  });

  on('config', () => { fillEncoders(); sync(); });
  on('capabilities', () => { fillEncoders(); sync(); });
  on('job-transition', ({ job, to }) => {
    if (job.kind === 'download' && to === 'done' && !job.from_history && notify.canAsk()) showNotifyPrompt();
  });
  sync();
}
export function focus() { R.input?.focus({ preventScroll: true }); }

/* ================================================================ link card */

function buildLinkCard() {
  R.input = el('textarea#dlUrl.linkbox', {
    rows: '1', placeholder: 'Paste a link, or several (one per line)', 'aria-label': 'Links to download',
    spellcheck: 'false', autocomplete: 'off', 'data-autofocus': '',
  });
  R.go = button('Download', 'primary', { id: 'dlGo', title: 'Download (Enter)', cls: 'primary-min' });
  R.hint = el('div.link-hint', { id: 'dlHint' });
  R.preview = el('div#dlPreview.dl-preview');
  return card({ cls: 'dl-link-card', body: [el('div.link-row', R.input, R.go), R.hint, R.preview] });
}

function wireLinkBox() {
  R.input.addEventListener('paste', () => { pasted = true; });
  box = linkBox(R.input, R.go, R.hint, {
    hintText: 'Enter to download · Shift+Enter for another line',
    label: buttonLabel,
    canSubmit,
    onSubmit: submit,
    onChange: linksChanged,
  });
}

function buttonLabel(parsed) {
  const n = parsed.links.length;
  if (n > 1) return `Download ${num(n)}`;
  const info = n === 1 ? singleInfo() : null;
  if (info?.kind === 'playlist') {
    const count = playlistCount(info);
    if (count === null) return 'Download all videos';
    return count ? `Download ${plural(count, 'video')}` : 'Download';
  }
  return 'Download';
}
/** How many videos the playlist choices select, or null when not known. */
function playlistCount(info) {
  const total = Number(info.count) || 0;
  const known = !info.count_more;
  const pl = L.pl;
  if (pl.mode === 'first') {
    const n = Math.max(1, Math.round(Number(pl.first) || 10));
    return known && total ? Math.min(n, total) : n;
  }
  if (pl.mode === 'items') return pl.chosen;
  return known ? total : null;
}
function canSubmit(parsed) {
  if (parsed.links.length !== 1) return true;
  const p = probeOf(parsed.links[0]);
  if (p?.state === 'ok' && isLive(p.info)) return LIVE_TITLE;
  if (p?.state === 'error' && p.error?.code === 'live_not_live') return LIVE_TITLE;
  if (p?.state === 'ok' && p.info.kind === 'playlist' && L.pl.mode === 'items' && !L.pl.items) return 'Choose the videos first';
  return true;
}

function linksChanged(parsed) {
  const links = parsed.links;
  const key = links.join('\n');
  if (key === currentKey) return;
  currentKey = key;
  currentLinks = links;
  // A different link starts with fresh per-link choices (D-02).
  if (!(links.length === 1 && links[0] === L.url)) L = freshLink(links.length === 1 ? links[0] : '');
  clearTimeout(probeTimer);
  const due = links.some((u) => !probes.has(u) || probes.get(u).state === 'error');
  const run = () => { for (const u of currentLinks) requestProbe(u); renderPreview(); };
  if (due) probeTimer = setTimeout(run, pasted ? 0 : 450);
  pasted = false;
  renderPreview();
}

/* ================================================================ preview slot */

function renderPreview() {
  if (!R.preview) return;
  const links = currentLinks;
  let sig;
  if (!links.length) sig = 'none';
  else if (links.length > 1) sig = 'multi';
  else {
    const p = probeOf(links[0]);
    const state = !p ? 'idle' : p.state === 'wait' || p.state === 'loading' ? 'loading' : p.state;
    sig = `one|${links[0]}|${state}`;
  }
  if (sig !== R.previewSig) {
    R.previewSig = sig;
    R.pv = null;
    R.rows = null;
    R.preview.removeAttribute('aria-busy');
    if (sig === 'none' || sig.endsWith('|idle')) setChildren(R.preview);
    else if (sig === 'multi') buildMulti();
    else {
      const url = links[0];
      const p = probeOf(url);
      if (p.state === 'ok') setChildren(R.preview, buildSingle(p.info, url));
      else if (p.state === 'error') setChildren(R.preview, buildProbeError(p.error, url));
      else {
        R.preview.setAttribute('aria-busy', 'true');
        setChildren(R.preview, el('div.skeleton-row', spinner(), 'Reading link…'));
      }
    }
  }
  if (sig === 'multi') updateRows();
  sync();
}

function buildSingle(info, url) {
  if (isLive(info)) return buildLive(info, url, isUpcoming(info));
  if (info.kind === 'playlist') return buildPlaylist(info, url);
  return buildVideo(info, url);
}

/* ---------------------------------------------------------------- A/B: one video */

function buildVideo(info, url) {
  const pv = { kind: 'video', info };
  R.pv = pv;
  const body = el('div.preview-body');
  body.append(
    el('div.preview-title', { dir: 'auto', title: info.title || '' }, info.title || hostPath(url)),
    el('div.preview-meta', metaLine([
      info.uploader ? bdi(info.uploader) : null,
      info.duration ? duration(info.duration) : null,
      info.upload_date ? date(info.upload_date) : null,
      info.site || siteName(url),
    ])),
  );
  if (info.in_playlist) {
    pv.scope = segmented({
      name: 'dlScope', legend: 'What to download',
      options: [{ value: 'video', label: 'Just this video' }, { value: 'all', label: 'Whole playlist' }],
      value: L.scope, onChange: (v) => { L.scope = v; sync(); },
    });
    pv.scopeHelp = el('p.help', { hidden: L.scope !== 'all' }, 'Saved to a folder named after the playlist.');
    body.append(el('div.dl-inpl', el('p.dl-inpl-text', 'This video is part of a playlist.'), pv.scope, pv.scopeHelp));
  }
  // chips: picture, subtitles, size estimate, clip
  const h = (info.heights || [])[0];
  pv.res = el('span.chip', h ? `Up to ${resLabel(h)}${info.max_fps >= 50 ? ` · ${info.max_fps} fps` : ''}` : 'Audio only');
  pv.subs = el('span.chip', { hidden: true });
  pv.size = el('span.chip.tabular', { hidden: true });
  pv.clipText = el('span.ltr');
  pv.clipChip = el('span.chip.dl-clip-chip', { hidden: true }, icon('scissors'), pv.clipText,
    iconButton('x', 'Remove the part', () => removeClip()));
  body.append(el('div.chips', pv.res, pv.subs, pv.size, pv.clipChip));
  // quiet links: part of the video, all formats
  pv.clipBtn = button('Download only part', 'quiet', { size: 'sm', icon: 'scissors' });
  pv.clipBtn.setAttribute('aria-expanded', String(L.clipOpen));
  pv.clipBtn.setAttribute('aria-controls', 'dlClip');
  pv.clipBtn.addEventListener('click', () => { L.clipOpen = !L.clipOpen; if (L.clipOpen) validateClip(false); sync(); if (L.clipOpen) pv.from.focus(); });
  pv.fmtBtn = button('See all formats', 'quiet', { size: 'sm', iconEnd: 'chevron-down' });
  pv.fmtBtn.setAttribute('aria-expanded', String(L.formatsOpen));
  pv.fmtBtn.setAttribute('aria-controls', 'dlFormats');
  pv.fmtBtn.addEventListener('click', () => toggleFormats(url));
  body.append(el('div.row.dl-pv-links', pv.clipBtn, pv.fmtBtn));
  body.append(buildClip(info, pv));
  pv.formats = el('div#dlFormats.dl-formats-wrap', { hidden: !L.formatsOpen });
  if (L.formatsOpen) renderFormats(url);
  return [el('div.preview.dl-pv', thumbEl(info.thumbnail, 'lg'), body), pv.formats];
}

/* ---------------------------------------------------------------- clip (D-07) */

function buildClip(info, pv) {
  const dur = Number(info.duration) || 0;
  if (!L.clipTo && dur) L.clipTo = duration(dur);
  const mk = (value, label) => el('input.input-num', {
    type: 'text', value, dir: 'ltr', inputmode: 'numeric', spellcheck: 'false', autocomplete: 'off', 'aria-label': label,
    placeholder: label === 'End time' && !dur ? 'end' : null,
  });
  pv.from = mk(L.clipFrom, 'Start time');
  pv.to = mk(L.clipTo, 'End time');
  pv.clipErr = el('div.field-error', { id: 'dlClipErr', role: 'alert', hidden: true });
  const edit = () => { L.clipFrom = pv.from.value; L.clipTo = pv.to.value; validateClip(false); sync(); };
  const done = () => { validateClip(true); sync(); };
  for (const inp of [pv.from, pv.to]) {
    inp.addEventListener('input', edit);
    inp.addEventListener('blur', done);
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); done(); } });
  }
  pv.clipBox = el('div#dlClip.dl-clip', { hidden: !L.clipOpen },
    el('div.row.gap-3',
      el('label.inline-field', el('span.label', 'From'), pv.from),
      el('label.inline-field', el('span.label', 'To'), pv.to),
      button('Remove', 'quiet', { size: 'sm', icon: 'x', onClick: () => removeClip() })),
    pv.clipErr);
  validateClip(false);
  return pv.clipBox;
}
/** Check the From/To times. show=false keeps quiet while typing. Returns the error or ''. */
function validateClip(show) {
  const pv = R.pv;
  const dur = Number(pv?.info?.duration) || 0;
  const fromText = String(L.clipFrom || '').trim() || '0:00';
  const toText = String(L.clipTo || '').trim();
  const f = parseTime(fromText);
  const t = toText ? parseTime(toText) : (dur || Infinity);
  let err = '', where = null;
  if (!Number.isFinite(f)) { err = 'Use a time such as 1:05.'; where = 'from'; }
  else if (Number.isNaN(t)) { err = 'Use a time such as 1:05.'; where = 'to'; }
  else if (dur && f >= dur) { err = `That's past the end of the video (${duration(dur)})`; where = 'from'; }
  else if (t <= f) { err = 'End must be after start'; where = 'to'; }
  else if (dur && t > dur + 0.5) { err = `That's past the end of the video (${duration(dur)})`; where = 'to'; }
  L.clipError = err;
  const set = !err && (f > 0 || (dur ? t < dur - 0.5 : Number.isFinite(t)));
  L.clip = set ? { from: f, to: Math.min(t, dur || t) } : null;
  if (pv?.clipErr) {
    const visible = err && (show || !pv.clipErr.hidden);
    for (const inp of [pv.from, pv.to]) {
      const bad = visible && ((where === 'from' && inp === pv.from) || (where === 'to' && inp === pv.to));
      if (bad) { inp.setAttribute('aria-invalid', 'true'); inp.setAttribute('aria-describedby', 'dlClipErr'); }
      else { inp.removeAttribute('aria-invalid'); inp.removeAttribute('aria-describedby'); }
    }
    vis(pv.clipErr, !!visible);
    if (visible) setText(pv.clipErr, err);
  }
  return err;
}
function removeClip() {
  const pv = R.pv;
  const dur = Number(pv?.info?.duration) || 0;
  L.clip = null; L.clipError = ''; L.clipOpen = false;
  L.clipFrom = '0:00'; L.clipTo = dur ? duration(dur) : '';
  if (pv?.from) { pv.from.value = L.clipFrom; pv.to.value = L.clipTo; validateClip(false); }
  sync();
  // The chip or the editor that held the focus is gone: land on the toggle.
  pv?.clipBtn?.focus();
}
const clipSection = (c) => `${duration(c.from)}-${Number.isFinite(c.to) ? duration(c.to) : 'inf'}`;

/* ---------------------------------------------------------------- all formats (D-10) */

const formatsCache = new Map();    // url -> {state, rows, error}
function toggleFormats(url) {
  const pv = R.pv;
  L.formatsOpen = !L.formatsOpen;
  pv.fmtBtn.setAttribute('aria-expanded', String(L.formatsOpen));
  vis(pv.formats, L.formatsOpen);
  if (L.formatsOpen) renderFormats(url);
}
function renderFormats(url) {
  const pv = R.pv;
  if (!pv?.formats) return;
  let f = formatsCache.get(url);
  if (!f || (f.state === 'error' && Date.now() - f.at > 15000)) {
    f = { state: 'loading' };
    formatsCache.set(url, f);
    get(`/api/formats?url=${encodeURIComponent(url)}`)
      .then((r) => { f.state = 'ok'; f.rows = r?.formats || []; }, (e) => { f.state = 'error'; f.error = e; })
      .finally(() => { f.at = Date.now(); if (R.pv === pv && L.url === url && L.formatsOpen) renderFormats(url); });
  }
  if (f.state === 'loading') setChildren(pv.formats, el('div.dl-formats', el('div.skeleton-row', spinner(), 'Reading formats…')));
  else if (f.state === 'error') {
    setChildren(pv.formats, el('div.dl-formats.is-plain', callout('warn', {
      title: f.error?.title || "Couldn't list the formats",
      text: f.error?.body || 'Check that the link opens in your browser, then try again.',
    })));
  } else {
    // Audio only lists the sound streams; Video lists everything (a sound stream picked there is saved as is).
    pv.formatsMode = audioMode() ? 'audio' : 'video';
    setChildren(pv.formats, formatPicker(pv.formatsMode === 'audio' ? f.rows.filter((r) => r.kind === 'audio') : f.rows));
  }
}
function rowParts(row) {
  const parts = String(row.label || '').split(' · ');
  return { head: parts[0] || '', rest: parts.slice(1).join(' · ') };
}
function groupName(row) {
  if (row.kind === 'audio') return 'Audio only';
  const head = rowParts(row).head;
  const m = /^(\d+)p$/.exec(head);
  return m ? resLabel(Number(m[1])) : head || 'Other';
}
function exactLabel(row) {
  if (row.kind === 'audio') return ['Audio', row.acodec_name, row.abr ? `${row.abr} kbps` : ''].filter(Boolean).join(' ');
  const head = rowParts(row).head;
  return [`${head}${row.fps >= 50 ? row.fps : ''}`, row.vcodec_name].filter(Boolean).join(' ');
}
function formatPicker(rows) {
  if (!rows.length) return el('div.dl-formats', el('p.help', 'This link lists no separate formats. The Quality setting is used.'));
  const groups = new Map();
  for (const r of rows) {
    const g = groupName(r);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(r);
  }
  const list = el('fieldset.dl-fmt-list', el('legend.sr-only', 'Exact stream'));
  for (const [name, items] of groups) {
    list.append(el('div.dl-fmt-group', el('div.sub-heading', name), items.map((r) => {
      const input = el('input', { type: 'radio', name: 'dlExact', value: r.format_id, checked: L.exact?.format_id === r.format_id });
      input.addEventListener('change', () => { if (input.checked) setExact(r); });
      const text = r.kind === 'audio' ? rowParts(r).rest : rowParts(r).rest || r.label;
      return el('label.dl-fmt-row', { title: `ID ${r.format_id} · ${String(r.ext || '').toUpperCase()}` },
        input, el('span.dl-fmt-label.tabular', text), r.language ? el('span.help', { dir: 'auto' }, r.language) : null);
    })));
  }
  return el('div.dl-formats', el('p.help.dl-fmt-intro', 'Pick one to download exactly that stream instead of the Quality setting.'), list);
}
function setExact(row) {
  L.exact = row;
  sync();
}
function clearExact() {
  L.exact = null;
  for (const r of R.preview.querySelectorAll('input[name=dlExact]')) r.checked = false;
  sync();
}
const AUDIO_CODEC_OF = { AAC: 'm4a', Opus: 'opus', MP3: 'mp3', Vorbis: 'vorbis', FLAC: 'flac' };

/* ---------------------------------------------------------------- C: playlist or channel (D-01, D-12) */

function buildPlaylist(info, url) {
  const pv = { kind: 'playlist', info };
  R.pv = pv;
  const pl = L.pl;
  const total = Number(info.count) || 0;
  const countText = info.count_more ? `${num(total)}+ videos` : plural(total, 'video');
  const body = el('div.preview-body',
    el('div.preview-title', { dir: 'auto', title: info.title || '' }, info.title || hostPath(url)),
    el('div.preview-meta', metaLine([info.uploader ? bdi(info.uploader) : null, info.is_channel ? 'Channel' : 'Playlist', countText])));

  // Download ( All 24 | First [10] | Choose… )
  pv.first = el('input.input-tiny', {
    type: 'number', min: '1', step: '1', value: String(pl.first), inputmode: 'numeric',
    'aria-label': info.is_channel ? 'How many of the latest videos' : 'How many of the first videos',
  });
  pv.scope = segmented({
    name: 'dlPlScope', legend: 'Which videos to download',
    options: [
      { value: 'all', label: info.count_more || !total ? 'All' : `All ${num(total)}` },
      { value: 'first', label: [info.is_channel ? 'Latest ' : 'First ', pv.first] },
      { value: 'items', label: 'Choose…' },
    ],
    value: pl.mode,
    onChange: (v) => {
      if (v === 'items') { chooseVideos(info, pl.mode); return; }
      pl.mode = v;
      sync();
    },
  });
  const pickFirst = () => { if (pl.mode !== 'first') { pl.mode = 'first'; setSegValue(pv.scope, 'first'); } };
  pv.first.addEventListener('focus', pickFirst);
  pv.first.addEventListener('input', () => {
    pickFirst();
    const n = Math.round(Number(pv.first.value));
    pl.first = Number.isFinite(n) && n >= 1 ? n : 10;
    sync();
  });
  pv.first.addEventListener('blur', () => { if (!(Number(pv.first.value) >= 1)) pv.first.value = String(pl.first); });
  pv.chosen = el('p.help.dl-chosen', { hidden: true });

  // skip known, quick update
  pv.archive = check('dlArchive', 'Skip videos I already downloaded', { checked: pl.archive });
  pv.stopKnown = check('dlStopKnown', 'Stop at the first video I already have (quick update)', { checked: pl.stopKnown });
  pv.archive.input.addEventListener('change', () => { pl.archive = pv.archive.input.checked; sync(); });
  pv.stopKnown.input.addEventListener('change', () => { pl.stopKnown = pv.stopKnown.input.checked; });

  // Only videos that match…
  pv.filters = disclosure('Only videos that match…', { body: buildFilters(pl, pv) });
  pv.filters.classList.add('dl-filters');

  const folder = el('p.help', 'Saved to a folder named “', bdi(info.title || 'Playlist'), '”.');
  body.append(el('div.dl-pl',
    el('div.dl-pl-scope', el('span.label', { 'aria-hidden': 'true' }, 'Download'), pv.scope),
    pv.chosen,
    el('div.checks.dl-pl-checks', pv.archive, pv.stopKnown),
    pv.filters,
    folder));
  updateChosen();
  return el('div.preview.dl-pv', thumbEl(info.thumbnail, 'lg', 'queue'), body);
}

function buildFilters(pl, pv) {
  const num0 = (key, label, cls = 'input-tiny') => {
    const input = el(`input.${cls}`, { type: 'number', min: '0', step: 'any', value: pl[key], inputmode: 'decimal', 'aria-label': label });
    input.addEventListener('input', () => { pl[key] = input.value.trim(); checkFilters(pv); sync(); });
    return input;
  };
  const dateIn = (key, label) => {
    const input = el('input.dl-date', { type: 'date', value: pl[key], 'aria-label': label });
    input.addEventListener('input', () => { pl[key] = input.value; checkFilters(pv); sync(); });
    return input;
  };
  pv.minLen = num0('minLen', 'Shortest length in minutes');
  pv.maxLen = num0('maxLen', 'Longest length in minutes');
  pv.after = dateIn('after', 'Uploaded on or after');
  pv.before = dateIn('before', 'Uploaded on or before');
  pv.title = el('input.input-short', { type: 'text', value: pl.title, placeholder: 'e.g. tutorial', spellcheck: 'false', id: 'dlTitleHas' });
  pv.title.addEventListener('input', () => { pl.title = pv.title.value; sync(); });
  pv.views = num0('views', 'Minimum views', 'input-num');
  pv.maxMb = num0('maxMb', 'Largest file size in MB', 'input-num');
  pv.order = segmented({ name: 'dlOrder', legend: 'Order', options: ORDERS, value: pl.order, onChange: (v) => { pl.order = v; sync(); } });
  pv.concat = check('dlConcat', 'Join everything into one file', { checked: pl.concat });
  pv.concat.input.addEventListener('change', () => { pl.concat = pv.concat.input.checked; sync(); });
  pv.filterErr = el('div.field-error', { role: 'alert', hidden: true });
  return [
    el('div.dl-filter-grid',
      el('div.inline-field', el('span.label', 'Length from'), pv.minLen, el('span.label', 'to'), pv.maxLen, el('span.label', 'minutes')),
      el('div.inline-field.dl-dates',
        el('span.dl-pair', el('span.label', 'Uploaded between'), pv.after),
        el('span.dl-pair', el('span.label', 'and'), pv.before)),
      el('div.inline-field', el('label.label', { for: 'dlTitleHas' }, 'Title contains'), pv.title),
      el('div.inline-field', el('span.label', 'At least'), pv.views, el('span.label', 'views')),
      el('div.inline-field', el('span.label', 'Skip files larger than'), pv.maxMb, el('span.label', 'MB')),
      el('div.inline-field', el('span.label', { 'aria-hidden': 'true' }, 'Order'), pv.order)),
    pv.filterErr,
    el('div.checks.mt-3', pv.concat),
  ];
}
/** Contradictory filters are pointed out before the job fails (UI-22). */
function checkFilters(pv) {
  const pl = L.pl;
  let msg = '';
  let bad = [];
  if (pl.minLen !== '' && pl.maxLen !== '' && Number(pl.maxLen) < Number(pl.minLen)) {
    msg = 'The longest length must be more than the shortest.'; bad = [pv.maxLen];
  } else if (pl.after && pl.before && pl.before < pl.after) {
    msg = 'The second date must be after the first.'; bad = [pv.before];
  }
  for (const i of [pv.maxLen, pv.before]) {
    if (bad.includes(i)) i.setAttribute('aria-invalid', 'true'); else i.removeAttribute('aria-invalid');
  }
  vis(pv.filterErr, !!msg);
  setText(pv.filterErr, msg);
  return msg;
}
function filterCount(pl) {
  return [pl.minLen, pl.maxLen, pl.after, pl.before, pl.title.trim(), pl.views, pl.maxMb, pl.order].filter((v) => v !== '' && v !== null && v !== undefined).length
    + (pl.concat ? 1 : 0);
}
function updateChosen() {
  const pv = R.pv;
  if (pv?.kind !== 'playlist') return;
  const pl = L.pl;
  if (pl.mode === 'items' && pl.items) {
    setChildren(pv.chosen, `${plural(pl.chosen, 'video')} chosen · `, linkButton('Change', () => chooseVideos(pv.info, 'items')));
    vis(pv.chosen, true);
  } else vis(pv.chosen, false);
}

/** 'Choose…' (D-12): a checklist of the playlist's videos, mapped to playlist_items. */
function chooseVideos(info, previousMode) {
  const pv = R.pv;
  const pl = L.pl;
  const entries = info.entries || [];
  const selected = new Set(expandItems(pl.items));
  const countEl = el('span.help.tabular');
  const ok = button('Choose', 'primary');
  const cancel = button('Cancel', 'secondary');
  const boxes = entries.map((e, i) => {
    const cb = el('input', { type: 'checkbox', value: String(i + 1), checked: selected.has(i + 1) });
    return el('label.check.dl-choose-row', cb,
      el('span.check-text.dl-choose-text',
        el('span.dl-choose-title', { dir: 'auto' }, e.title || 'Untitled'),
        e.duration ? el('span.help.tabular', duration(e.duration)) : null));
  });
  const inputs = boxes.map((b) => b.querySelector('input'));
  const update = () => {
    const k = inputs.filter((i) => i.checked).length;
    setText(countEl, `${num(k)} of ${num(inputs.length)} selected`);
    const lab = ok.querySelector('.btn-label');
    setText(lab, k ? `Choose ${plural(k, 'video')}` : 'Choose');
    ok.disabled = !k;
  };
  for (const i of inputs) i.addEventListener('change', update);
  const all = button('Select all', 'quiet', { size: 'sm', onClick: () => { inputs.forEach((i) => { i.checked = true; }); update(); } });
  const none = button('Select none', 'quiet', { size: 'sm', onClick: () => { inputs.forEach((i) => { i.checked = false; }); update(); } });
  const d = openDialog({
    title: 'Choose videos', cls: 'dl-choose-dlg',
    body: [
      el('div.dl-choose-tools', all, none, el('span.spacer'), countEl),
      entries.length ? el('div.dl-choose-list', boxes) : el('p.help', 'This playlist lists no videos to choose from.'),
      (info.count_more || (Number(info.count) || 0) > entries.length) && entries.length
        ? el('p.help.mt-2', `Showing the first ${num(entries.length)} videos.`) : null,
    ],
    footer: el('div.end', cancel, ok),
    initialFocus: inputs.find((i) => i.checked) || inputs[0] || cancel,
  });
  update();
  cancel.addEventListener('click', () => d.close(null));
  ok.addEventListener('click', () => d.close(inputs.filter((i) => i.checked).map((i) => Number(i.value))));
  d.closed.then((picked) => {
    if (R.pv !== pv) return;                     // the link changed meanwhile
    if (Array.isArray(picked) && picked.length) {
      pl.items = toRanges(picked);
      pl.chosen = picked.length;
      pl.mode = 'items';
    } else if (!pl.items) pl.mode = previousMode === 'items' ? 'all' : previousMode;
    setSegValue(pv.scope, pl.mode);
    updateChosen();
    sync();
  });
}
function toRanges(list) {
  const idx = [...new Set(list)].sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i < idx.length;) {
    let j = i;
    while (j + 1 < idx.length && idx[j + 1] === idx[j] + 1) j++;
    out.push(i === j ? `${idx[i]}` : `${idx[i]}-${idx[j]}`);
    i = j + 1;
  }
  return out.join(',');
}
function expandItems(text) {
  const out = [];
  for (const part of String(text || '').split(',')) {
    const m = /^(\d+)(?:-(\d+))?$/.exec(part.trim());
    if (!m) continue;
    const a = Number(m[1]), b = Number(m[2] || m[1]);
    for (let k = a; k <= b && out.length < 5000; k++) out.push(k);
  }
  return out;
}

/* ---------------------------------------------------------------- D: live or upcoming (D-03) */

function buildLive(info, url, upcoming) {
  R.pv = { kind: 'live', info };
  const pv = el('div.preview.dl-pv.is-small',
    thumbEl(info.thumbnail, '', 'record'),
    el('div.preview-body',
      el('div.preview-title', { dir: 'auto', title: info.title || '' }, info.title || hostPath(url)),
      el('div.preview-meta', metaLine([info.uploader ? bdi(info.uploader) : null, info.site || siteName(url)]))));
  const c = callout('info', {
    text: upcoming ? UPCOMING_TEXT : LIVE_TEXT,
    actions: [button('Record it on the Live tab', 'secondary', { size: 'sm', icon: 'record', onClick: () => handToLive(url) })],
  });
  c.classList.add('mt-3');
  return [pv, c];
}

/** The link moves to the Live tab (D-03): it can't be downloaded here, so it doesn't stay behind. */
function handToLive(url) {
  putLink('live', url);
  if (currentLinks.length === 1 && currentLinks[0] === url) {
    R.input.value = '';
    box.refresh();
  }
}

/* ---------------------------------------------------------------- F: the link could not be read */

function buildProbeError(e, url) {
  if (e?.code === 'live_not_live') {
    // A stream that has not started yet: that is the Live tab's job (D-03).
    const p = e.params || {};
    return buildLive({ title: p.title || '', uploader: p.uploader || '', thumbnail: p.thumbnail || '', live_status: 'is_upcoming' }, url, true);
  }
  R.pv = { kind: 'error' };
  const title = e?.title || "Couldn't read this link";
  const actions = (e?.actions || []).filter((a) => a === 'signin' || a === 'proxy')
    .map((a) => button(actionLabel(a), 'secondary', { size: 'sm', onClick: () => runAction(a, null) }));
  const tech = techDetails(title, e?.body || '', e?.detail);
  return callout('warn', { title, text: 'You can still try downloading it.', actions: [...actions, tech].filter(Boolean) });
}

/* ---------------------------------------------------------------- E: several links (D-09) */

function buildMulti() {
  R.multiHead = el('div.dl-multi-head.tabular');
  R.rowsEl = el('div.dl-rows', { role: 'list', 'aria-label': 'Links' });
  R.rows = new Map();
  setChildren(R.preview, el('div.dl-multi', R.multiHead, el('p.help', 'Each becomes its own download.'), R.rowsEl));
}
function updateRows() {
  const links = currentLinks;
  const keep = new Set(links);
  for (const [url, row] of R.rows) if (!keep.has(url)) { row.remove(); R.rows.delete(url); }
  links.forEach((url, i) => {
    let row = R.rows.get(url);
    if (!row) { row = makeRow(url); R.rows.set(url, row); }
    patchRow(row, url);
    const at = R.rowsEl.children[i];
    if (at !== row) R.rowsEl.insertBefore(row, at || null);
  });
  // '4 links · 3 videos · 1 channel (135 videos)'
  let videos = 0, live = 0, bad = 0;
  const lists = { playlist: [0, 0, false], channel: [0, 0, false] };
  for (const url of links) {
    const p = probeOf(url);
    if (p?.state === 'error') bad++;
    if (p?.state !== 'ok') continue;
    const info = p.info;
    if (isLive(info)) live++;
    else if (info.kind === 'playlist') {
      const g = lists[info.is_channel ? 'channel' : 'playlist'];
      g[0]++; g[1] += Number(info.count) || 0; g[2] = g[2] || !!info.count_more;
    } else videos++;
  }
  const parts = [plural(links.length, 'link')];
  if (videos) parts.push(plural(videos, 'video'));
  for (const [k, [n, count, more]] of Object.entries(lists)) {
    if (n) parts.push(`${plural(n, k)} (${num(count)}${more ? '+' : ''} videos)`);
  }
  if (live) parts.push(plural(live, 'live stream'));
  if (bad) parts.push(`${num(bad)} not readable`);
  setText(R.multiHead, parts.join(' · '));
}
function makeRow(url) {
  const p = {};
  p.thumb = el('div.thumb.xs', icon('video'));
  p.title = el('div.dl-row-title', { dir: 'auto' });
  p.meta = el('div.dl-row-meta.tabular');
  p.x = iconButton('x', 'Remove this link', () => removeLink(url));
  const row = el('div.dl-row', { role: 'listitem', dataset: { url } }, p.thumb, el('div.dl-row-body', p.title, p.meta), p.x);
  row._p = p;
  return row;
}
function patchRow(row, url) {
  const p = row._p;
  const pr = probeOf(url);
  const state = !pr ? 'wait' : pr.state === 'loading' ? 'wait' : pr.state;
  if (p.sig === state) return;
  p.sig = state;
  row.classList.toggle('is-error', state === 'error');
  row.classList.toggle('is-loading', state === 'wait');
  if (state === 'ok') {
    const info = pr.info;
    setText(p.title, info.title || hostPath(url));
    p.title.title = info.title || url;
    let meta;
    if (isLive(info)) meta = isUpcoming(info) ? 'Not live yet · record it on the Live tab' : 'Live now · record it on the Live tab';
    else if (info.kind === 'playlist') meta = `${info.is_channel ? 'Channel' : 'Playlist'} · ${info.count_more ? `${num(info.count)}+ videos` : plural(info.count || 0, 'video')}`;
    else meta = [info.duration ? duration(info.duration) : '', info.uploader || ''].filter(Boolean).join(' · ');
    setChildren(p.meta, meta || (info.site || siteName(url)));
    if (info.thumbnail && /^https?:\/\//i.test(info.thumbnail)) {
      const img = el('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer', src: info.thumbnail });
      img.addEventListener('error', () => img.remove());
      p.thumb.append(img);
    }
  } else if (state === 'error') {
    setText(p.title, hostPath(url));
    p.title.title = url;
    setChildren(p.meta, pr.error?.code === 'live_not_live' ? 'Not live yet · record it on the Live tab' : 'Not a supported link. Check it.');
  } else {
    setText(p.title, hostPath(url));
    p.title.title = url;
    setChildren(p.meta, spinner(), ' Reading…');
  }
}
/** × on a row: take that link out of the box (D-09). */
function removeLink(url) {
  const lines = R.input.value.split(/\r?\n/)
    .map((line) => line.split(/\s+/).filter((tok) => tok && tok !== url).join(' '))
    .filter((line) => line.trim());
  const index = currentLinks.indexOf(url);
  R.input.value = lines.join('\n');
  R.input.dispatchEvent(new Event('input', { bubbles: true }));
  // Keep the keyboard where it was: the next row's ×, or the link box.
  const next = R.rowsEl?.children[Math.min(index, (R.rowsEl?.children.length || 1) - 1)];
  (next?.querySelector('.icon-btn') || R.input).focus({ preventScroll: true });
}

/* ================================================================ What you get (D-05, D-06) */

function buildGetCard() {
  R.mode = segmented({
    name: 'dlMode', id: 'dlMode', legend: 'What to download',
    options: [{ value: 'video', label: 'Video' }, { value: 'audio', label: 'Audio only' }], value: FALLBACK.dl_mode,
  });
  R.codec = selectEl('dlCodec', AUDIO_FORMATS);
  R.codecExact = exactChip();
  R.formatField = el('div.inline-field.dl-format', { hidden: true }, el('label.label', { for: 'dlCodec' }, 'Format'), R.codec, R.codecExact);

  R.quality = selectEl('dlQuality', QUALITIES);
  R.qualityExact = exactChip();
  R.qualityHelp = el('p.help#dlQualityHelp', { hidden: true });
  R.quality.setAttribute('aria-describedby', 'dlQualityHelp');
  R.subs = selectEl('dlSubs', SUBTITLES);
  R.subLangs = el('input#dlSubLangs.input-short', { type: 'text', placeholder: 'e.g. fa, es', spellcheck: 'false', autocomplete: 'off', 'aria-label': 'Subtitle languages' });
  R.subLangsWrap = el('div.dl-sublangs', { hidden: true }, R.subLangs);
  R.videoFields = el('div.grid.cols-2.dl-video-fields',
    field('Quality', R.quality, [R.qualityExact, R.qualityHelp]),
    field('Subtitles', R.subs, [R.subLangsWrap]));

  R.compatible = check('dlCompatible', 'Plays on any device (H.264)', { checked: true });
  R.sponsor = check('dlSponsor', 'Skip sponsor segments');
  R.sponsor.hidden = true;
  R.getChecks = el('div.checks.dl-get-checks', R.compatible, R.sponsor);

  R.more = disclosure('More options', { id: 'dlMore', body: buildDrawer() });
  R.getCard = card({
    title: 'What you get', id: 'dlGet', saveState: true, cls: 'dl-get',
    body: [el('div.dl-mode-row', R.mode, R.formatField), R.videoFields, R.getChecks, el('hr.divider'), R.more],
  });
  R.getCard.addEventListener('change', () => sync());
  R.getCard.addEventListener('input', () => sync());
  return R.getCard;
}
function exactChip() {
  const text = el('span.dl-exact-text');
  const chip = el('span.chip.dl-exact', { hidden: true }, text, iconButton('x', 'Use the Quality setting instead', () => clearExact()));
  chip.text = text;
  return chip;
}

function buildDrawer() {
  R.container = selectEl('dlContainer', CONTAINERS);
  R.containerField = field('Container', R.container);
  R.rmChapters = el('input#dlRmChapters.input-short', { type: 'text', placeholder: 'e.g. intro, credits', spellcheck: 'false', autocomplete: 'off' });
  R.chapters = check('dlChapters', 'Keep chapter markers', { checked: true });
  R.split = check('dlSplit', 'One file per chapter');
  R.autoSubs = check('dlAutoSubs', 'Include automatic captions');
  R.embedSubs = check('dlEmbedSubs', 'Put subtitles inside the video file', { checked: true });

  R.encoder = el('select#dlEncoder.select-md', el('option', { value: '' }, NO_RECODE));
  R.encoderField = field('Re-encode video', R.encoder);
  R.recodeQ = selectEl('dlRecodeQ', RECODE_QUALITY);
  R.recodeQ.value = 'balanced';
  R.recodeQField = field('Re-encode quality', R.recodeQ);
  R.normalize = check('dlNormalize', 'Even out the volume');

  R.desc = check('dlDesc', 'Description (.txt)');
  R.comments = check('dlComments', 'Top comments');
  R.maxComments = el('input#dlMaxComments.input-num', { type: 'number', min: '1', max: '100000', step: '1', value: '200', inputmode: 'numeric' });
  R.maxCommentsField = el('label.inline-field', { hidden: true }, el('span.label', 'How many'), R.maxComments);
  R.thumbFile = check('dlThumbFile', 'Thumbnail image');
  R.thumbFmt = selectEl('dlThumbFmt', THUMB_FORMATS, 'dl-thumbfmt');
  R.thumbFmtField = el('label.inline-field', { hidden: true }, el('span.label', 'Thumbnail format'), R.thumbFmt);
  R.link = check('dlLink', 'Shortcut to the page');
  R.infoJson = check('dlInfoJson', 'Technical details (.json)');

  R.subsChecks = [R.autoSubs, R.embedSubs];
  R.resetBtn = button('Reset to defaults', 'quiet', { size: 'sm', onClick: () => resetDefaults() });
  return [
    el('div.sub-heading', 'FILE'),
    el('div.dl-drawer-grid', R.containerField, field('Remove chapters named', R.rmChapters)),
    el('div.checks', R.chapters, R.split, R.autoSubs, R.embedSubs),
    el('div.sub-heading', 'RE-ENCODE'),
    R.encoderGrid = el('div.dl-drawer-grid', R.encoderField, R.recodeQField),
    el('div.checks', R.normalize),
    el('div.sub-heading', 'EXTRA FILES'),
    el('div.checks.dl-extras',
      R.desc,
      el('div.dl-check-with', R.comments, R.maxCommentsField),
      el('div.dl-check-with.dl-thumb-row', R.thumbFile, R.thumbFmtField),
      R.link,
      R.infoJson),
    el('div.dl-drawer-foot', R.resetBtn),
  ];
}

const BINDINGS = {};
function bindChoices() {
  const b = (control, key, opts) => { BINDINGS[key] = bindSetting(control, key, opts); };
  b(R.mode, 'dl_mode', { fromServer: (v) => (v === 'audio' ? 'audio' : 'video') });
  b(R.quality, 'dl_quality', { fromServer: normQuality });
  b(R.compatible.input, 'dl_compatible');
  b(R.codec, 'dl_audio_codec', { fromServer: (v) => (AUDIO_FORMATS.some(([k]) => k === v) ? v : v === 'aac' ? 'm4a' : 'mp3') });
  b(R.subs, 'dl_subtitles', {
    fromServer: (v) => (SUBTITLES.some(([k]) => k === v) ? v : ['auto', 'both', 'manual'].includes(v) ? 'custom' : 'none'),
  });
  b(R.subLangs, 'dl_subtitle_langs', { validate: langCodes, toServer: (v) => String(v).trim(), debounce: 300 });
  b(R.sponsor.input, 'sponsorblock');
  b(R.container, 'dl_container', { fromServer: (v) => (CONTAINERS.some(([k]) => k === v) ? v : 'mp4') });
  b(R.chapters.input, 'embed_chapters');
  b(R.split.input, 'dl_split_chapters');
  b(R.autoSubs.input, 'dl_auto_subs');
  b(R.embedSubs.input, 'dl_embed_subs');
  b(R.encoder, 'recode_encoder');
  b(R.recodeQ, 'recode_quality', { fromServer: (v) => (RECODE_QUALITY.some(([k]) => k === v) ? v : 'balanced') });
  b(R.normalize.input, 'normalize_audio');
  b(R.desc.input, 'write_description');
  b(R.comments.input, 'write_comments');
  b(R.maxComments, 'dl_max_comments', {
    validate: validators.range(1, 100000, 'Choose between 1 and 100,000.'),
    toServer: (v) => (String(v).trim() === '' ? 200 : Math.round(Number(v))),
    debounce: 300,
  });
  b(R.thumbFile.input, 'dl_write_thumbnail');
  b(R.thumbFmt, 'convert_thumbnails', { fromServer: (v) => (['', 'jpg', 'png'].includes(v) ? v : '') });
  b(R.link.input, 'dl_write_link');
  b(R.infoJson.input, 'write_info_json');
  fillEncoders();
}

/** The re-encoder list comes from this PC's capabilities. Rebuilding it keeps
 *  the saved choice (FIN-5): the value is read back from the settings. */
function fillEncoders() {
  if (!R.encoder) return;
  const list = (STATE.cap?.encoders || []).filter((e) => e.available !== false);
  const sig = list.map((e) => e.id).join(',');
  if (R.encoder._sig === sig) return;
  R.encoder._sig = sig;
  const focused = document.activeElement === R.encoder;
  const want = focused ? R.encoder.value : (CONFIG.recode_encoder ?? R.encoder.value);
  setChildren(R.encoder, el('option', { value: '' }, NO_RECODE),
    list.map((e) => el('option', { value: e.id }, String(e.label || e.id).replace(/\s+[—–]\s+/g, ', '))));
  R.encoder.value = list.some((e) => e.id === want) ? want : '';
  if (!focused) BINDINGS.recode_encoder?.apply();
}

async function resetDefaults() {
  const patch = Object.fromEntries(REMEMBERED.map((k) => [k, defaultOf(k)]));
  R.rmChapters.value = '';
  const state = R.getCard.querySelector('.save-state');
  showSaveState(state, 'saving');
  try {
    await withBusy(R.resetBtn, 'Resetting…', () => saveSettings(patch, 'download-reset'));
    for (const c of [R.subLangs, R.maxComments]) setFieldError(c, '');
    showSaveState(state, 'saved');
  } catch (e) {
    showSaveState(state, 'error', `Couldn't save: ${e.text || e.message}`);
  }
  sync();
}

/* ================================================================ derived state */

/** Everything that follows from the choices, the link and its preview. */
function sync() {
  if (!R.mode || !R.getCard) return;
  const video = (segValue(R.mode) || 'video') === 'video';
  const ex = exactNow();
  const info = singleVideo();

  // What you get: video or audio fields, the exact stream in place of Quality or Format
  vis(R.videoFields, video);
  vis(R.formatField, !video);
  vis(R.quality, !(ex && video));
  vis(R.qualityExact, !!ex && video);
  vis(R.codec, !(ex && !video));
  vis(R.codecExact, !!ex && !video);
  if (ex) {
    setText(R.qualityExact.text, `Exact: ${exactLabel(ex)}`);
    setText(R.codecExact.text, `Exact: ${exactLabel(ex)}`);
  }
  vis(R.subLangsWrap, video && R.subs.value === 'custom');
  if (!(video && R.subs.value === 'custom')) setFieldError(R.subLangs, '');
  const showH264 = video && !ex;
  vis(R.compatible, showH264);
  const yt = currentLinks.some((u) => isYouTube(u));
  vis(R.sponsor, yt);
  vis(R.getChecks, showH264 || yt);

  // Quality help (D-05)
  let help = '';
  if (video && !ex) {
    const q = R.quality.value;
    const h264 = R.compatible.input.checked;
    const maxH = Number(info?.heights?.[0]) || 0;
    if (info && maxH) {
      help = ['best', '2160', '1440'].includes(q) && h264 && maxH > 1080 ? H264_NOTE : `This video goes up to ${resLabel(maxH)}.`;
    } else if (!info && ['2160', '1440'].includes(q) && h264) help = H264_NOTE;
  }
  vis(R.qualityHelp, !!help);
  setText(R.qualityHelp, help);

  // More options: only what applies, and how many differ from the defaults (D-02, D-05)
  const subsOn = video && R.subs.value !== 'none';
  vis(R.containerField, video);
  for (const c of R.subsChecks) vis(c, subsOn);
  vis(R.encoderField, video);
  const encoder = video && !!R.encoder.value;
  vis(R.recodeQField, encoder);
  vis(R.encoderGrid, video);
  vis(R.maxCommentsField, R.comments.input.checked);
  vis(R.thumbFmtField, R.thumbFile.input.checked);
  const differs = (control, key) => (control.type === 'checkbox'
    ? control.checked !== !!defaultOf(key)
    : String(control.value) !== String(defaultOf(key) ?? ''));
  const counted = [
    [R.container, 'dl_container', video], [R.chapters.input, 'embed_chapters', true], [R.split.input, 'dl_split_chapters', true],
    [R.autoSubs.input, 'dl_auto_subs', subsOn], [R.embedSubs.input, 'dl_embed_subs', subsOn],
    [R.encoder, 'recode_encoder', video], [R.recodeQ, 'recode_quality', encoder], [R.normalize.input, 'normalize_audio', true],
    [R.desc.input, 'write_description', true], [R.comments.input, 'write_comments', true],
    [R.maxComments, 'dl_max_comments', R.comments.input.checked], [R.thumbFile.input, 'dl_write_thumbnail', true],
    [R.thumbFmt, 'convert_thumbnails', R.thumbFile.input.checked], [R.link.input, 'dl_write_link', true],
    [R.infoJson.input, 'write_info_json', true],
  ];
  const n = counted.filter(([c, k, on]) => on && differs(c, k)).length + (R.rmChapters.value.trim() ? 1 : 0);
  R.more.setSummary(n ? ` · ${n} on` : '');

  // The preview's own parts
  const pv = R.pv;
  if (pv?.kind === 'video') {
    const subs = pv.info.subtitles || [];
    const showSubs = video && subs.length > 0;
    vis(pv.subs, showSubs);
    if (showSubs) {
      const first = subs.find((c) => /^en\b/i.test(c)) || subs.find((c) => c === pv.info.language) || subs[0];
      setText(pv.subs, `Subtitles: ${languageName(first)}${subs.length > 1 ? ` + ${num(subs.length - 1)} more` : ''}`);
    }
    const size = sizeText(pv.info, video, ex);
    vis(pv.size, !!size);
    setText(pv.size, size);
    const showClip = !!L.clip && !L.clipOpen;
    vis(pv.clipChip, showClip);
    if (L.clip) setText(pv.clipText, `${duration(L.clip.from)}–${Number.isFinite(L.clip.to) ? duration(L.clip.to) : 'end'}`);
    vis(pv.clipBox, L.clipOpen);
    pv.clipBtn.setAttribute('aria-expanded', String(L.clipOpen));
    if (pv.scopeHelp) vis(pv.scopeHelp, L.scope === 'all');
    if (L.formatsOpen && pv.formatsMode && pv.formatsMode !== (video ? 'video' : 'audio')) renderFormats(L.url);
  } else if (pv?.kind === 'playlist') {
    vis(pv.stopKnown, L.pl.archive);
    const c = filterCount(L.pl);
    pv.filters.setSummary(c ? ` · ${c} on` : '');
  }
  box?.refresh();
}

/** '≈ 245 MB at 1080p' (D-11). */
function sizeText(info, video, ex) {
  const dur = Number(info.duration) || 0;
  // A part of the video is about its share of the whole.
  const share = L.clip && dur ? Math.max(0.001, Math.min(1, ((Number.isFinite(L.clip.to) ? L.clip.to : dur) - L.clip.from) / dur)) : 1;
  const { s, label } = sizeWhole(info, video, ex, dur);
  if (!s) return '';
  return share === 1 ? `≈ ${bytes(s)}${label}` : `≈ ${bytes(s * share)} for this part`;
}
/** {s: bytes, label: ' at 1080p'} for the whole video with the current choices. */
function sizeWhole(info, video, ex, dur) {
  if (ex) {
    let s = Number(ex.filesize) || 0;
    if (s && ex.kind === 'video' && info.audio_size) s += Number(info.audio_size) || 0;
    return { s, label: '' };
  }
  if (!video) {
    const codec = R.codec.value;
    let s = 0;
    if (codec === 'm4a' || codec === 'opus') s = Number(info.audio_size) || 0;
    else if (codec === 'mp3') s = dur * 30625;            // VBR at about 245 kbps
    else if (codec === 'wav') s = dur * 192000;           // 48 kHz, 16 bit, stereo
    else if (codec === 'flac') s = dur * 105000;          // about 55 % of WAV
    const name = (AUDIO_FORMATS.find(([k]) => k === codec)?.[1] || codec).split(' · ')[0];
    return { s, label: ` as ${name}` };
  }
  const q = R.quality.value;
  const maxH = Number(info.heights?.[0]) || 0;
  if (q === 'best') return { s: Number(info.size_best) || 0, label: maxH ? ` at ${resLabel(maxH)}` : '' };
  if (q === 'smallest') return { s: Number(info.size_smallest) || 0, label: ' for the smallest file' };
  const h = maxH ? Math.min(Number(q), maxH) : Number(q);
  return { s: Number(info.size_by_height?.[q]) || 0, label: ` at ${resLabel(h)}` };
}

/* ================================================================ submit */

function collectOptions(links) {
  const video = (segValue(R.mode) || 'video') === 'video';
  const o = {
    mode: video ? 'video' : 'audio',
    quality: R.quality.value,
    compatible: R.compatible.input.checked,
    audio_codec: R.codec.value,
    container: R.container.value,
    subtitles: R.subs.value,
    subtitle_langs: R.subLangs.value.trim() || 'en',
    auto_subs: R.autoSubs.input.checked,
    embed_subs: R.embedSubs.input.checked,
    sponsorblock: R.sponsor.input.checked && links.some((u) => isYouTube(u)),
    embed_chapters: R.chapters.input.checked,
    split_chapters: R.split.input.checked,
    recode_encoder: R.encoder.value,
    recode_quality: R.recodeQ.value,
    normalize_audio: R.normalize.input.checked,
    write_description: R.desc.input.checked,
    write_comments: R.comments.input.checked,
    max_comments: Math.max(1, Math.round(Number(R.maxComments.value) || 200)),
    write_thumbnail: R.thumbFile.input.checked,
    convert_thumbnails: R.thumbFmt.value,
    write_link: R.link.input.checked,
    write_info_json: R.infoJson.input.checked,
  };
  const rm = R.rmChapters.value.trim();
  if (rm) o.remove_chapters = rm;
  if (links.length !== 1 || L.url !== links[0]) return o;

  // Per-link choices, only for the one link they were made for.
  const info = singleInfo();
  if (L.exact) {
    o.format_id = L.exact.format_id;
    if (L.exact.kind === 'audio') {
      o.mode = 'audio';
      o.audio_codec = AUDIO_CODEC_OF[L.exact.acodec_name] || o.audio_codec;
    } else o.mode = 'video';
  }
  if (L.clip && info?.kind !== 'playlist') o.section = clipSection(L.clip);
  if (info?.kind === 'playlist') {
    const pl = L.pl;
    o.playlist_mode = pl.mode;
    if (pl.mode === 'first') o.playlist_first = Math.max(1, Math.round(Number(pl.first) || 10));
    if (pl.mode === 'items') o.playlist_items = pl.items;
    o.archive = pl.archive;
    o.stop_at_known = pl.archive && pl.stopKnown;
    if (pl.order) o.playlist_order = pl.order;
    if (pl.concat) o.concat_playlist = true;
    const n = (v) => (v === '' || v === null || v === undefined ? undefined : Number(v));
    if (n(pl.minLen) > 0) o.min_duration = n(pl.minLen);
    if (n(pl.maxLen) > 0) o.max_duration = n(pl.maxLen);
    if (pl.after) o.date_after = pl.after.replace(/-/g, '');
    if (pl.before) o.date_before = pl.before.replace(/-/g, '');
    if (pl.title.trim()) o.title_contains = pl.title.trim();
    if (n(pl.views) > 0) o.min_views = Math.round(n(pl.views));
    if (n(pl.maxMb) > 0) o.max_filesize_mb = n(pl.maxMb);
  } else if (info?.in_playlist) {
    o.playlist_mode = L.scope === 'all' ? 'all' : 'video';
    if (L.scope === 'all') o.archive = true;
  }
  return o;
}

async function submit(parsed) {
  const links = parsed.links;
  // A part of the video with times that don't work is fixed here, not after the job fails.
  if (links.length === 1 && R.pv?.kind === 'video' && validateClip(true)) {
    L.clipOpen = true;
    sync();
    (R.pv.to.getAttribute('aria-invalid') ? R.pv.to : R.pv.from).focus();
    return;
  }
  if (links.length === 1 && R.pv?.kind === 'playlist' && checkFilters(R.pv)) {
    R.pv.filters.open = true;
    R.pv.filterErr.scrollIntoView({ block: 'nearest' });
    return;
  }
  const hints = {};
  for (const u of links) {
    const p = probeOf(u);
    if (p?.state === 'ok' && p.info?.title) hints[u] = { title: p.info.title, thumbnail: p.info.thumbnail || '', uploader: p.info.uploader || '' };
  }
  const options = collectOptions(links);
  const refocus = document.activeElement === R.input || document.activeElement === R.go;
  setBusy(R.go, 'Adding…');
  let created = null;
  try {
    const r = await post('/api/jobs', { url: links.join('\n'), kind: 'download', options, hints });
    created = r?.jobs || [];
  } catch (e) {
    clearBusy(R.go);
    const text = e.code === 'bad_link' ? linkProblem({ links: [], ignored: 1, empty: false }).text : (e.text || e.message);
    box.refresh();
    box.setHint(text, 'error');
    return;
  }
  clearBusy(R.go);
  // Per-link choices never carry over to the next link (D-02).
  L = freshLink();
  R.rmChapters.value = '';
  R.input.value = '';
  box.refresh();
  const viewInQueue = linkButton('View in Queue', () => navigate('queue'));
  box.showOk(created.length === 1
    ? ['✓ Added “', bdi(truncate(displayTitle(created[0]), 60)), '” · ', viewInQueue]
    : [`✓ Added ${num(created.length)} downloads · `, viewInQueue]);
  if (refocus) R.input.focus({ preventScroll: true });
}

/* ================================================================ Recent downloads (D-08, G-14) */

const isRecent = (j) => j.kind === 'download' && !j.from_history && (Number(j.created) || 0) >= SESSION_START - 1;

function buildRecentCard() {
  R.seeAll = linkButton('See all in Queue', () => navigate('queue'));
  R.notify = el('div.dl-notify', { hidden: true });
  R.recentList = el('div#dlRecent');
  R.recent = card({ title: 'Recent downloads', id: 'dlRecentCard', cls: 'dl-recent', actions: [R.seeAll], body: [R.notify, R.recentList] });
  R.recent.hidden = true;
  new JobView(R.recentList, {
    variant: 'compact', limit: 3, sort: sortNewest, filter: isRecent,
    render: (job) => { const c = renderCard(job, 'compact'); patchNote(c, job); return c; },
    patch: (node, job, _view, now) => { patchCard(node, job, now); patchNote(node, job); },
    onUpdate: (items) => {
      vis(R.recent, items.length > 0);
      setText(R.seeAll, `See all in Queue (${num(allJobs().filter(isRecent).length)})`);
    },
  });
  return R.recent;
}
/** Backend notes ('Already in your folder, so it wasn't downloaded again.') under a row (UI-6). */
function patchNote(cardEl, job) {
  const text = job.status === 'done' ? (job.result?.notes || []).join(' ') : '';
  let n = cardEl._note;
  if (!text) { if (n) n.hidden = true; return; }
  if (!n) { n = cardEl._note = el('div.help.dl-row-note', { dir: 'auto' }); cardEl.querySelector('.j-body')?.append(n); }
  n.hidden = false;
  setText(n, text);
}
function showNotifyPrompt() {
  if (!R.notify.hidden) return;
  const off = () => { R.notify.hidden = true; setChildren(R.notify); };
  setChildren(R.notify, callout('info', {
    text: "Get a notification when downloads finish while you're in another app?",
    actions: [
      button('Turn on', 'secondary', { size: 'sm', onClick: () => { off(); notify.ask(); } }),
      button('No thanks', 'quiet', { size: 'sm', onClick: () => { off(); notify.decline(); } }),
    ],
  }));
  R.notify.hidden = false;
}

/* ================================================================ site checker (S-11) */

function openSiteChecker(opener) {
  const input = el('input#dlSiteSearch', {
    type: 'text', placeholder: 'e.g. instagram', 'aria-label': 'Search supported sites', spellcheck: 'false', autocomplete: 'off',
  });
  const results = el('div.dl-sites', { role: 'status', 'aria-live': 'polite' });
  const close = button('Close', 'secondary');
  const d = openDialog({
    title: 'Check if a site is supported', cls: 'dl-sites-dlg',
    body: [el('p.help', 'Media Toolkit works with 1,700+ sites. Type part of a site name.'), input, results],
    footer: el('div.end', close),
    initialFocus: input,
  });
  close.addEventListener('click', () => d.close());
  d.closed.then(() => opener?.isConnected && opener.focus({ preventScroll: true }));
  let timer = null;
  let seq = 0;
  const search = async () => {
    const q = input.value.trim();
    const mine = ++seq;
    if (!q) { setChildren(results); return; }
    try {
      const r = await get(`/api/sites?q=${encodeURIComponent(q)}&limit=20`);
      if (mine !== seq) return;
      const matches = r?.matches || [];
      if (!matches.length) {
        setChildren(results, el('p.dl-site-none', 'Not in the list, but many sites still work. Paste the link on the Download tab to try it.'));
        return;
      }
      const more = Math.max(0, (Number(r.count) || matches.length) - matches.length);
      setChildren(results,
        el('ul.dl-site-list', matches.map((name) => el('li.dl-site', icon('check'), el('span', bdi(name), ' is supported')))),
        more ? el('p.help', `+${num(more)} more`) : null);
    } catch (e) {
      if (mine === seq) setChildren(results, el('p.help', e.text || e.message));
    }
  };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 200); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); search(); } });
}
