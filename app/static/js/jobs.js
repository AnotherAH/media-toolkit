// jobs.js: the job store (SSE), plain-language status lines (G-08), the
// keyed JobView (G-01) with full cards and compact rows (Q-01), file rows
// (Q-02), error blocks and their action dispatcher (G-07), and the outcome
// announcements (G-02 suppression and coalescing, G-03, G-14 notifications).
import {
  api, post, del, get, el, setText, bdi, emit, on, callHook, hasHook, navigate, setTabDot,
  num, bytes, bytesOf, mbOf, speed, duration, eta, clock, relTime, ordinal, plural, truncate,
  hostPath, siteName, languageName, copyText, openPath, revealPath, installPack,
  loadModels, loadHardware, notify, scrollToEl,
} from './core.js';
import {
  icon, setIcon, button, iconButton, toast, toastError, setBusy, setBusyLabel, clearBusy, withBusy, flash,
} from './ui.js';

/* ================================================================ store */

export const ACTIVE = ['queued', 'running', 'stopping'];
export const FINAL = ['done', 'skipped', 'error', 'cancelled'];
/** Page load time (epoch seconds). Jobs created after it belong to "this session". */
export const SESSION_START = Date.now() / 1000;

const store = {
  list: [],                 // newest first, as the server sends it
  byId: new Map(),
  prev: new Map(),          // id -> status at the previous message
  queuePos: new Map(),      // id -> 1-based position among queued non-live jobs
  seeded: false,
  unseenFailed: new Set(),  // failures the user has not looked at (status chip rule 3)
  unseenTranscripts: [],    // finished transcripts not opened yet (G-03 dot), newest last
};
const views = new Set();

export const allJobs = () => store.list;
export const getJob = (id) => store.byId.get(id) || null;
export const isActive = (job) => ACTIVE.includes(job?.status);
export const isFinal = (job) => FINAL.includes(job?.status);
export const isSeeded = () => store.seeded;

/** Counts for badges, the status chip and the title (G-04, G-14). */
export function counts() {
  let active = 0, recording = 0, waiting = 0, activeWork = 0;
  const byKind = { download: 0, transcript: 0, live: 0 };
  for (const j of store.list) {
    if (!ACTIVE.includes(j.status)) continue;
    active++;
    byKind[j.kind] = (byKind[j.kind] || 0) + 1;
    if (j.kind === 'live') {
      if (j.status === 'running' && j.live_phase === 'recording') recording++;
      if (j.live_phase === 'waiting') waiting++;
    } else activeWork++;
  }
  return { active, recording, waiting, activeWork, byKind, unseenFailed: store.unseenFailed.size };
}
/** The Queue was opened (or the chip clicked): failures count as seen. */
export function markFailuresSeen() {
  if (!store.unseenFailed.size) return;
  store.unseenFailed.clear();
  emit('unseen', counts());
}
export const unseenTranscripts = () => store.unseenTranscripts.slice();
/** The transcript tab opened these; clears the G-03 dot. */
export function clearUnseenTranscripts() {
  store.unseenTranscripts = [];
  setTabDot('transcript', false);
}

function ingest(list, seed) {
  const byId = new Map(list.map((j) => [j.id, j]));
  const queued = list.filter((j) => j.status === 'queued' && j.kind !== 'live').sort((a, b) => a.created - b.created);
  store.queuePos = new Map(queued.map((j, i) => [j.id, i + 1]));
  const transitions = [];
  if (!seed) {
    for (const j of list) {
      const from = store.prev.get(j.id);
      if (from === undefined) {
        // A job that appeared already finished (a fast caption fetch between two messages).
        if (FINAL.includes(j.status) && !j.from_history && j.created >= SESSION_START - 5) transitions.push({ job: j, from: 'queued', to: j.status });
      } else if (from !== j.status) transitions.push({ job: j, from, to: j.status });
    }
  }
  for (const id of store.unseenFailed) if (byId.get(id)?.status !== 'error') store.unseenFailed.delete(id);
  store.list = list;
  store.byId = byId;
  store.prev = new Map(list.map((j) => [j.id, j.status]));
  store.seeded = true;
  // Views first, so the announcer can tell what the user can see right now.
  for (const v of views) v.update(list);
  emit('jobs', { jobs: list, seed, transitions });
  for (const t of transitions) {
    emit('job-transition', t);
    announcer(t);
  }
  afterTransitions();
}

let es = null;
let seeding = true;
/** Open the SSE stream. The first message after (re)connecting seeds silently (G-02a). */
export function connect() {
  if (es) return;
  es = new EventSource('/api/events');
  es.addEventListener('open', () => { seeding = true; });
  es.addEventListener('message', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch (_) { return; }
    const seed = seeding;
    seeding = false;
    ingest(Array.isArray(data.jobs) ? data.jobs : [], seed);
  });
  es.addEventListener('error', () => { seeding = true; });
}
/** Fetch the list once (used by tests and when SSE is not wanted). */
export async function refreshJobs() {
  const r = await get('/api/jobs');
  ingest(r.jobs || [], !store.seeded);
}

/* ================================================================ status lines (G-08) */

const u = (text) => ({ user: String(text ?? '') });       // a user-supplied part (rendered in <bdi>)
const RECONNECT_AFTER = 15;                                 // s without data before "Reconnecting" (L-08)

/** A job's title for people: never a bare URL (host + path until metadata arrives). */
export function displayTitle(job) {
  if (job.title && job.title !== job.url) return job.title;
  return hostPath(job.url) || job.title || 'Untitled';
}
function mediaFiles(job) { return (job.files || []).filter((f) => f.kind === 'video' || f.kind === 'audio'); }
function totalSize(files) { return files.reduce((a, f) => a + (Number(f.size) || 0), 0); }
function heightFrom(job) {
  const m = /\b\d{2,5}x(\d{3,4})\b/.exec(job.stage_detail || '');
  return m ? `${m[1]}p` : '';
}
/** 'from the uploader's English captions' etc. (short provenance for status lines). */
export function provenance(detail = {}, url = '') {
  const lang = languageName(detail.caption_lang || detail.source_lang || detail.language || '');
  const site = siteName(url) || 'the site';
  switch (detail.source) {
    case 'official': return lang ? `from the uploader's ${lang} captions` : "from the uploader's captions";
    case 'auto': return lang ? `from ${site}'s automatic ${lang} captions` : `from ${site}'s automatic captions`;
    case 'auto_translated': {
      const src = languageName(detail.source_lang || '');
      return src ? `${site}'s machine translation from ${src}` : `${site}'s machine translation`;
    }
    case 'whisper': return detail.task === 'translate' || detail.translated ? 'transcribed and translated on this PC' : 'transcribed on this PC';
    default: return '';
  }
}
/** '331 words' or, for Chinese/Japanese text, '1,204 characters'. */
export function wordCount(stats = {}) {
  return stats.script === 'cjk' ? plural(stats.words || stats.characters || 0, 'character') : plural(stats.words || 0, 'word');
}

/**
 * Everything a card shows about a job's state, in plain words.
 * Returns {tone, icon, word, detail: parts, short: parts, detailTone, bar, title, tick}
 *  - parts are strings or {user} (user-supplied, shown in <bdi>), joined with ' · '
 *  - short is the compact-row form (D-08)
 *  - bar: null | {value 0..100, indeterminate, label}
 *  - title: a replacement card title (live waiting), or ''
 *  - tick: true when the line depends on the clock (re-patched every second)
 */
export function statusOf(job, now = Date.now() / 1000) {
  const s = job.status, kind = job.kind;
  const pct = Math.max(0, Math.min(100, Math.round((Number(job.progress) || 0) * 100)));
  const out = { tone: 'neutral', icon: 'clock', word: '', detail: [], short: [], detailTone: '', bar: null, title: '', tick: false };
  const name = displayTitle(job);

  if (s === 'queued') {
    out.word = 'Waiting';
    if (kind === 'live') { out.detail = ['Starting…']; out.short = ['Starting…']; return out; }
    const pos = store.queuePos.get(job.id);
    out.detail = [pos ? `starts when a slot frees up (${ordinal(pos)} in line)` : 'starts when a slot frees up'];
    out.short = [pos ? `Waiting · ${ordinal(pos)} in line` : 'Waiting'];
    return out;
  }

  if (kind === 'live') return liveStatus(job, out, now, pct);

  if (s === 'running') {
    out.tone = 'run';
    out.tick = false;
    const stage = (job.stage || '').replace(/^(Starting|Waiting)$/, '');
    if (kind === 'download') {
      out.icon = 'down'; out.word = 'Downloading';
      const it = job.item;
      if (it && (it.count > 1 || it.index)) {
        out.detail = [`Video ${num(it.index)} of ${num(it.count || it.index)}`, it.title ? u(it.title) : null, `${pct}%`];
        out.short = [`Video ${num(it.index)} of ${num(it.count || it.index)}`, `${pct}%`];
        if (stage && stage !== 'Downloading') out.detail.push(stage);
      } else if (!stage || stage === 'Downloading' || stage.startsWith('Downloading')) {
        if (job.bytes_total) {
          out.detail = [`${pct}%`, bytesOf(job.bytes_done, job.bytes_total), speed(job.speed_bps), eta(job.eta_s)];
          out.short = [`${pct}%`, eta(job.eta_s)];
        } else if (job.bytes_done) {
          out.detail = [bytes(job.bytes_done), speed(job.speed_bps)];
          out.short = [bytes(job.bytes_done), speed(job.speed_bps)];
        } else if (stage === 'Downloading') {
          out.detail = pct ? [`${pct}%`] : [];
          out.short = pct ? [`${pct}%`] : ['Starting…'];
        } else {
          out.detail = ['Getting video details…'];
          out.short = out.detail.slice();
        }
      } else {
        out.detail = [stage];
        out.short = [stage];
      }
      const early = !job.bytes_done && !pct;
      out.bar = { value: pct, indeterminate: !!job.indeterminate || early, label: `Downloading ${name}` };
    } else {
      out.icon = 'text'; out.word = 'Transcribing';
      const rest = stage.startsWith('Transcribing') ? stage.slice('Transcribing'.length).trim() : stage;
      const parts = [rest || 'Getting ready…'];
      if (job.bytes_total) parts.push(mbOf(job.bytes_done, job.bytes_total));
      else if (!job.indeterminate && pct > 0) parts.push(`${pct}%`);
      if (job.eta_s) parts.push(eta(job.eta_s));
      out.detail = parts;
      out.short = parts.slice();
      const frac = job.bytes_total ? Math.round(100 * (job.bytes_done || 0) / job.bytes_total) : pct;
      out.bar = { value: frac, indeterminate: !!job.indeterminate, label: `Transcribing ${name}` };
    }
    return out;
  }

  if (s === 'stopping') {
    out.word = 'Cancelling';
    out.detail = [];
    out.short = ['Cancelling…'];
    out.bar = { value: pct, indeterminate: true, label: `Cancelling ${name}` };
    return out;
  }

  if (s === 'done') return doneStatus(job, out, now);

  if (s === 'skipped') {
    out.icon = 'info';
    const stage = job.stage || 'Skipped';
    if (/^Skipped:\s*/i.test(stage)) { out.word = 'Skipped'; out.detail = [stage.replace(/^Skipped:\s*/i, '')]; }
    else { out.word = 'Done'; out.detail = [stage]; }
    out.short = [stage];
    return out;
  }

  if (s === 'error') {
    out.tone = 'err'; out.icon = 'x'; out.word = 'Failed';
    out.detail = job.error ? [] : [job.message || 'Something went wrong.'];
    out.short = [job.error?.title || job.message || 'Failed'];
    out.detailTone = '';
    return out;
  }

  if (s === 'cancelled') {
    out.icon = 'x'; out.word = 'Cancelled';
    out.detail = pct > 0 && pct < 100 ? [`at ${pct}%`] : [];
    out.short = [pct > 0 && pct < 100 ? `Cancelled at ${pct}%` : 'Cancelled'];
    return out;
  }
  out.word = s || 'Waiting';
  return out;
}

function liveStatus(job, out, now, pct) {
  const s = job.status, phase = job.live_phase;
  const name = displayTitle(job);
  if (s === 'running' && phase === 'waiting') {
    out.word = 'Waiting'; out.icon = 'clock'; out.tick = true;
    out.title = `${job.uploader || name}, waiting to go live`;
    const parts = ['Not live yet'];
    if (job.next_check_at) {
      const left = job.next_check_at - now;
      parts.push(left > 0 ? `next check in ${duration(left)}` : 'checking now');
    }
    if (job.give_up_at) parts.push(`gives up at ${clock(job.give_up_at)}`);
    out.detail = parts;
    out.short = parts.slice();
    return out;
  }
  if (s === 'running' && phase === 'recording') {
    out.tick = true;
    const quiet = now - (Number(job.updated) || now);
    if (quiet > RECONNECT_AFTER) {
      out.tone = 'warn'; out.icon = 'alert'; out.word = 'Reconnecting…';
      out.detail = [`no data for ${Math.round(quiet)} s`];
      out.short = [`Reconnecting… no data for ${Math.round(quiet)} s`];
      out.detailTone = 'warn';
      return out;
    }
    out.tone = 'rec'; out.icon = 'rec-dot'; out.word = 'REC';
    const secs = Number(job.rec_seconds) || 0;
    const limit = Number(job.rec_limit_seconds) || 0;
    const size = job.rec_bytes ? bytes(job.rec_bytes) : '';
    if (limit) {
      out.detail = [`${duration(secs)} of ${duration(limit)}`, `stops at ${clock(now + Math.max(0, limit - secs))}`, size];
      out.bar = { value: Math.min(100, Math.round(100 * secs / limit)), indeterminate: false, label: `Recording ${name}` };
    } else out.detail = [duration(secs), size];
    out.short = out.detail.slice();
    return out;
  }
  if (s === 'running') {
    out.word = 'Waiting'; out.icon = 'clock';
    out.detail = [job.stage && job.stage !== 'Starting' ? job.stage : 'Checking the stream…'];
    out.short = out.detail.slice();
    return out;
  }
  if (s === 'stopping') {
    if (phase === 'saving') {
      out.word = 'Saving'; out.icon = 'rec-dot';
      const line = `Saving the recording… ${duration(job.rec_seconds || 0)} recorded. Keep the app open.`;
      out.detail = [line];
      out.short = [line];
      out.bar = { value: 0, indeterminate: true, label: `Saving ${name}` };
    } else {
      out.word = 'Stopping'; out.detail = []; out.short = ['Stopping…'];
    }
    return out;
  }
  if (s === 'done') {
    const d = job.result?.detail || {};
    const files = mediaFiles(job);
    const size = d.size || totalSize(files);
    const container = (d.container || files[0]?.ext || '').toUpperCase();
    const parts = Number(d.parts || job.parts || files.length) || 0;
    out.tone = 'ok'; out.icon = 'check'; out.word = 'Saved';
    const secs = d.duration ?? job.rec_seconds;
    if (d.end_reason === 'connection_lost') {
      out.tone = 'warn'; out.detailTone = 'warn';
      out.detail = [`Connection lost at ${duration(secs || 0)}. Saved what was recorded.`];
    } else {
      out.detail = [secs ? duration(secs) : '', size ? bytes(size) : '', container, parts > 1 ? `${parts} parts` : ''];
      const outcome = { user: 'stopped by you', stream_ended: 'the stream ended' }[d.end_reason]
        || (d.end_reason === 'limit' ? `reached your ${num(job.options?.max_minutes || Math.round((job.rec_limit_seconds || 0) / 60))}-minute limit` : '');
      if (outcome) out.detail.push(outcome);
    }
    out.short = ['Saved', ...out.detail];
    return out;
  }
  if (s === 'error') {
    out.tone = 'err'; out.icon = 'x'; out.word = 'Failed';
    out.short = [job.error?.title || 'Failed'];
    return out;
  }
  if (s === 'cancelled') { out.icon = 'x'; out.word = 'Cancelled'; out.short = ['Cancelled']; return out; }
  if (s === 'skipped') { out.icon = 'info'; out.word = 'Skipped'; out.detail = [job.stage || '']; out.short = out.detail.slice(); return out; }
  out.word = s;
  return out;
}

function doneStatus(job, out, now) {
  out.tone = 'ok'; out.icon = 'check';
  const r = job.result || {};
  if (job.kind === 'transcript') {
    out.word = 'Done';
    out.detail = [wordCount(r.stats), provenance(r.detail, job.url || r.meta?.url), relTime(job.updated, now)];
    out.short = ['Done', wordCount(r.stats), relTime(job.updated, now)];
    return out;
  }
  const media = mediaFiles(job);
  const size = totalSize(media.length ? media : job.files || []);
  if (r.playlist) {
    const saved = Number(r.saved ?? r.count ?? media.length) || 0;
    if (r.stop_reason === 'limit') {
      const limit = Number(job.options?.playlist_first || job.options?.max_downloads || saved) || saved;
      out.word = 'Saved';
      out.detail = [`${plural(saved, 'video')}. Stopped at your limit of ${num(limit)}.`];
    } else if (r.stop_reason === 'up_to_date') {
      out.word = 'Done';
      out.detail = [`Up to date. Saved ${plural(saved, 'new video')} and stopped at the first one you already had.`];
    } else {
      out.word = 'Saved';
      out.detail = [plural(saved, 'video'), r.archived ? `${num(r.archived)} already downloaded` : '',
        r.failed ? `${num(r.failed)} couldn't be downloaded` : '', size ? bytes(size) : ''];
      if (r.failed) { out.tone = 'warn'; out.icon = 'alert'; }
    }
    out.short = ['Saved', ...out.detail];
    return out;
  }
  out.word = 'Saved';
  const main = media[0] || (job.files || [])[0];
  const ext = (main?.ext || '').toUpperCase();
  const middle = main?.kind === 'audio' ? (r.meta?.duration ? duration(r.meta.duration) : '') : heightFrom(job);
  out.detail = [relTime(job.updated, now), ext, middle, size ? bytes(size) : ''];
  out.short = ['Saved', relTime(job.updated, now), ext, size ? bytes(size) : ''];
  return out;
}

/** The whole status line as one plain string ('Downloading · 43% · …'). */
export function statusText(job, variant = 'full') {
  const st = statusOf(job);
  const parts = (variant === 'short' ? st.short : [st.word, ...st.detail]).filter((p) => p && (typeof p === 'string' ? p : p.user));
  return parts.map((p) => (typeof p === 'string' ? p : p.user)).join(' · ');
}

/* ================================================================ actions (G-07) */

const ACTIONS = new Map();
/**
 * Register or override an action. def: {label, run(job, ctx) -> Promise|void,
 * busy: 'label while running' (optional), icon (optional)}. ctx = {button, view}.
 */
export function registerAction(id, def) { ACTIONS.set(id, { ...(ACTIONS.get(id) || {}), ...def }); }
export const actionLabel = (id) => ACTIONS.get(id)?.label || id;
export function hasAction(id) { return ACTIONS.has(id); }
/** Run an action by id for a job (from an error block, a toast [Fix], anywhere). */
export async function runAction(id, job, ctx = {}) {
  const a = ACTIONS.get(id);
  if (!a) return;
  const btn = ctx.button;
  try {
    if (a.busy && btn) await withBusy(btn, a.busy, () => a.run(job, ctx));
    else await a.run(job, ctx);
  } catch (e) {
    toastError(e);
  }
}

async function retry(job, options) {
  await post(`/api/jobs/${job.id}/retry`, options ? { options } : {});
}
/** Put a link into a tab's link box and switch there (edit_link, record_live, download_instead). */
export function putLink(tab, url, { run = true } = {}) {
  navigate(tab, { focus: false });
  if (hasHook(`${tab}.setLink`)) return callHook(`${tab}.setLink`, url, { run });
  const box = document.getElementById({ download: 'dlUrl', transcript: 'trUrl', live: 'lvUrl' }[tab]);
  if (!box) return undefined;
  box.value = url;
  box.dispatchEvent(new Event('input', { bubbles: true }));
  box.focus();
  return undefined;
}
const SETTINGS_TARGETS = {
  signin: '#set-signin', proxy: '#setProxy', choose_folder: '#setDlChange', folders: '#set-folders',
  health: '#set-health', transcription: '#set-transcription', sites: '#siteSearch', about: '#set-about',
};
/** Open Settings at a section or control, open its <details>, focus it and flash it. */
export function revealSetting(target) {
  navigate('settings', { focus: false });
  if (hasHook('settings.reveal')) { callHook('settings.reveal', target); return; }
  requestAnimationFrame(() => {
    const node = document.querySelector(SETTINGS_TARGETS[target] || target);
    if (!node) return;
    for (let d = node.closest('details'); d; d = d.parentElement?.closest('details')) d.open = true;
    scrollToEl(node, 'center');
    if (node.matches('input, select, textarea, button')) node.focus({ preventScroll: true });
    flash(node.closest('.setting-row, section, .card') || node);
  });
}
/** 'Copy for AI chat' for any finished transcript job (T-06 header). */
export async function copyForAI(job, text = null) {
  const r = job.result || {};
  const m = r.meta || {};
  const body = text ?? await api(`/api/jobs/${job.id}/transcript?format=txt`);
  const lines = [`Transcript of “${m.title || job.title}”`];
  if (m.uploader) lines.push(`Channel: ${m.uploader}`);
  if (m.duration) lines.push(`Length: ${duration(m.duration)}`);
  if (m.url || job.url) lines.push(`Link: ${m.url || job.url}`);
  const ok = await copyText(`${lines.join('\n')}\n\n${String(body).trim()}\n`);
  if (!ok) { toast("Couldn't copy. Try again.", { tone: 'err' }); return false; }
  const words = wordCount(r.stats);
  toast(m.url || job.url ? `Copied ${words} with the title and link. Paste it into any AI chat.` : `Copied ${words} with the title. Paste it into any AI chat.`, { tone: 'ok' });
  return true;
}
/** The file a transcript's 'Show file' reveals (the .txt, else any file). */
export const transcriptFile = (job) => (job.files || []).find((f) => f.ext === 'txt') || (job.files || [])[0] || null;
/** The file Play/Open opens for a finished job. */
export function mainFile(job) {
  const media = mediaFiles(job);
  return media[0] || (job.files || [])[0] || null;
}
async function openFile(path) {
  try { await openPath(path); } catch (e) { toastError(e); }
}
async function revealFile(path) {
  try { await revealPath(path); } catch (e) { toastError(e); }
}
function revealTarget(job) {
  const f = mainFile(job) || transcriptFile(job);
  return f?.path || job.result?.output_dir || '';
}

// Defaults. Tab modules may override any of these with registerAction().
registerAction('retry', { label: 'Try again', busy: 'Starting…', run: (j) => retry(j) });
registerAction('resume', { label: 'Resume', busy: 'Starting…', run: (j) => retry(j) });
registerAction('retry_novad', { label: 'Try again without it', busy: 'Starting…', run: (j) => retry(j, { vad: false }) });
registerAction('retry_wait', { label: 'Wait and record', busy: 'Starting…', run: (j) => retry(j, { wait_for_live: true }) });
registerAction('update_retry', {
  label: 'Update and retry', busy: 'Updating…',
  async run(j) {
    const r = await post('/api/update-ytdlp');
    if (!r.ok) { toast(`Couldn't update: ${r.message || 'unknown problem'}`, { tone: 'err' }); return; }
    if (r.restart_required) { toast(`Updated to ${r.version}. Restart Media Toolkit to use it.`, { tone: 'ok', timeout: 10000 }); return; }
    await retry(j);
  },
});
registerAction('edit_link', {
  label: 'Edit link',
  async run(j) {
    putLink(j.kind === 'transcript' ? 'transcript' : j.kind === 'live' ? 'live' : 'download', j.url);
    try { await del(`/api/jobs/${j.id}`); } catch (_) { /* still running: leave it */ }
  },
});
registerAction('remove', { label: 'Remove', run: (j) => del(`/api/jobs/${j.id}`) });
registerAction('signin', { label: 'Set up sign-in', run: () => revealSetting('signin') });
registerAction('proxy', { label: 'Proxy settings', run: () => revealSetting('proxy') });
registerAction('choose_folder', { label: 'Choose folder', run: () => revealSetting('choose_folder') });
registerAction('record_live', { label: 'Record it', run: (j) => putLink('live', j.url) });
registerAction('download_instead', { label: 'Download it instead', run: (j) => putLink('download', j.url) });
registerAction('choose_file', {
  label: 'Choose another file',
  run() {
    navigate('transcript', { focus: false });
    if (hasHook('transcript.chooseFile')) return callHook('transcript.chooseFile');
    document.getElementById('trFile')?.click();
    return undefined;
  },
});
registerAction('smaller_model', {
  label: 'Choose a smaller model',
  run() {
    navigate('transcript', { focus: false });
    if (hasHook('transcript.smallerModel')) return callHook('transcript.smallerModel');
    const opts = document.getElementById('trOptions');
    if (opts) opts.open = true;
    const sel = document.getElementById('trModel');
    sel?.focus();
    flash(sel?.closest('.setting-row, .field') || sel);
    return undefined;
  },
});
registerAction('copy_details', {
  label: 'Copy details',
  async run(j) {
    const text = [j.error?.title, j.error?.body, '', j.error?.detail || j.message || ''].filter((x) => x !== undefined).join('\n').trim();
    toast((await copyText(text)) ? 'Details copied.' : "Couldn't copy. Try again.", { tone: 'ok' });
  },
});
registerAction('install_ffmpeg', {
  label: 'Download ffmpeg',
  async run(_j, ctx) {
    const btn = ctx.button;
    if (btn) setBusy(btn, 'Installing ffmpeg');
    const off = on('packs', (p) => {
      const pr = p?.progress || {};
      if (btn && pr.busy && pr.task === 'ffmpeg') setBusyLabel(btn, `Installing ffmpeg · ${Math.round(pr.percent || 0)}%`);
    });
    try {
      const r = await installPack('ffmpeg');
      if (r && r.ok !== false) {
        toast('ffmpeg is installed.', { tone: 'ok' });
        const waiting = allJobs().filter((x) => x.status === 'error' && x.error?.code === 'ffmpeg_missing');
        for (const x of waiting) retry(x).catch(() => {});
      } else toast(r?.error || "Couldn't install ffmpeg.", { tone: 'err' });
    } finally {
      off();
      if (btn?.isConnected) clearBusy(btn);
    }
  },
});
registerAction('view_transcript', {
  label: 'View transcript',
  run(j) {
    if (hasHook('transcript.open')) return callHook('transcript.open', j);
    navigate('transcript');
    return undefined;
  },
});
registerAction('copy_ai', { label: 'Copy for AI chat', busy: 'Copying…', run: (j) => copyForAI(j) });
registerAction('show_file', { label: 'Show file', run: (j) => revealFile(transcriptFile(j)?.path || j.result?.output_dir) });
registerAction('play', { label: 'Play', run: (j) => openFile(mainFile(j)?.path) });
registerAction('open', { label: 'Open', run: (j) => openFile(mainFile(j)?.path) });
registerAction('reveal', { label: 'Show in folder', run: (j) => revealFile(revealTarget(j)) });

/** The error actions a card offers (Q-03: Try again is always there unless the link itself is wrong). */
export function errorActions(job, { dropRemove = true } = {}) {
  const err = job.error || {};
  let ids = Array.isArray(err.actions) ? err.actions.slice() : ['retry'];
  if (!['unavailable', 'bad_link', 'playlist_not_supported'].includes(err.code) && !ids.some((a) => a.startsWith('retry'))) ids.push('retry');
  if (dropRemove) ids = ids.filter((a) => a !== 'remove');
  return ids.filter((a) => ACTIONS.has(a));
}

/* ================================================================ pieces */

const KIND_ICON = { download: 'video', transcript: 'text', live: 'record' };
const FILE_ICON = { video: 'video', audio: 'audio', transcript: 'text', subtitles: 'captions', notes: 'file', json: 'file', thumbnail: 'image', description: 'text', link: 'link', part: 'video', other: 'file' };
const FILE_KIND = { video: 'Video', audio: 'Audio', transcript: 'Transcript', subtitles: 'Subtitles', notes: 'Notes', json: 'Technical details', thumbnail: 'Thumbnail', description: 'Description', link: 'Shortcut to the page' };

/** Render ' · '-joined parts into a node, updating text in place when the shape is unchanged. */
export function renderParts(node, parts) {
  const clean = parts.filter((p) => p && (typeof p === 'string' ? p.trim() : p.user));
  // Plain app text (the common case) lives in one text node that is only ever
  // rewritten, so a phase change never adds or removes nodes (G-01).
  if (clean.every((p) => typeof p === 'string')) {
    const text = clean.join(' · ');
    if (node._shape === 'plain' && node.childNodes.length === 1) {
      if (node.firstChild.data !== text) node.firstChild.data = text;
    } else {
      node._shape = 'plain';
      node.replaceChildren(document.createTextNode(text));
    }
    return;
  }
  const shape = clean.map((p) => (typeof p === 'string' ? 't' : 'u')).join('');
  if (node._shape === shape && node.childNodes.length === clean.length * 2 - (clean.length ? 1 : 0)) {
    let i = 0;
    for (const p of clean) {
      const n = node.childNodes[i];
      const text = typeof p === 'string' ? p : p.user;
      const target = n.nodeType === 3 ? n : n.firstChild;
      if (target && target.data !== text) target.data = text;
      i += 2;
    }
    return;
  }
  node._shape = shape;
  node.replaceChildren();
  clean.forEach((p, i) => {
    if (i) node.append(' · ');
    node.append(typeof p === 'string' ? document.createTextNode(p) : bdi(p.user));
  });
}

/** One file row (Q-02): icon · 'Video · MP4 · 35.6 MB' · name (stem…, .ext) · Open. */
export function fileRow(f, { label } = {}) {
  const ext = (f.ext || '').toUpperCase();
  const name = f.name || '';
  const dot = name.lastIndexOf('.');
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const extPart = dot > 0 ? name.slice(dot) : '';
  // 'Video · MP4 · 35.6 MB'; the extension is left out when it only repeats the kind ('.description').
  const extShown = ext && !['description', 'url', 'webloc', 'desktop'].includes(f.ext) ? ext : '';
  const text = label || [f.label || FILE_KIND[f.kind] || ext || 'File', f.label || FILE_KIND[f.kind] ? extShown : '', f.size ? bytes(f.size) : ''].filter(Boolean).join(' · ');
  const open = button('Open', 'quiet', { size: 'sm', ariaLabel: `Open ${name}` });
  const row = el('div.file-row', { title: name },
    icon(FILE_ICON[f.kind] || 'file'),
    el('span.file-label', text),
    el('span.file-name', { dir: 'auto' }, el('span.stem', stem), el('span.ext', extPart)),
    open);
  const go = () => openFile(f.path);
  open.addEventListener('click', (e) => { e.stopPropagation(); go(); });
  row.addEventListener('click', go);
  return row;
}
/** File rows for a finished job, or null when the card should not list files (Q-02). */
export function fileRows(job) {
  const files = job.files || [];
  if (job.kind === 'transcript' || files.length < 2) return null;
  const media = mediaFiles(job);
  const parts = job.kind === 'live' && media.length > 1;
  let n = 0;
  return el('div.file-rows', files.map((f) => {
    const isPart = parts && (f.kind === 'video' || f.kind === 'audio');
    return fileRow(f, isPart ? { label: `Part ${++n} · ${bytes(f.size)}` } : {});
  }));
}

/**
 * The G-07 error block: bold title, one body line, action buttons (first
 * secondary, the rest quiet) and a collapsed 'Technical details'.
 * opts: {actions: ids (default errorActions(job)), extra: [nodes]}
 */
export function errorBlock(job, opts = {}) {
  const err = job.error || { title: job.message || 'Something went wrong.', body: '', detail: '' };
  let ids = opts.actions || errorActions(job);
  // The disclosure below has its own Copy details button.
  if (String(err.detail || '').trim()) ids = ids.filter((id) => id !== 'copy_details');
  const buttons = ids.map((id, i) => {
    const b = button(actionLabel(id), i === 0 ? 'secondary' : 'quiet', { size: 'sm' });
    b.addEventListener('click', () => runAction(id, getJob(job.id) || job, { button: b }));
    return b;
  });
  const detailText = String(err.detail || '').trim();
  let tech = null;
  if (detailText) {
    const pre = el('pre', { dir: 'auto' }, detailText);
    const copy = button('Copy details', 'quiet', { size: 'sm' });
    copy.addEventListener('click', async () => toast((await copyText(`${err.title}\n${err.body}\n\n${detailText}`)) ? 'Details copied.' : "Couldn't copy. Try again.", { tone: 'ok' }));
    tech = el('details.tech', el('summary', icon('chevron-right', { cls: 'chev' }), 'Technical details'), pre, copy);
  }
  return el('div.callout.err.err-block', icon('x'),
    el('div.callout-body',
      el('div.callout-title', err.title || 'Something went wrong.'),
      err.body ? el('div.callout-text', err.body) : null,
      buttons.length || tech ? el('div.callout-actions', buttons, tech) : null,
      opts.extra || null));
}

/** Transcript step list (T-01). Build once, then patchSteps() on every update. */
export function renderSteps(job) {
  const ol = el('ol.steps');
  patchSteps(ol, job);
  return ol;
}
const STEP_ICON = { done: 'check', active: 'spinner', pending: 'circle', failed: 'x', skipped: 'minus' };
export function patchSteps(ol, job) {
  const steps = job.steps || [];
  const keys = steps.map((s) => s.key).join('|');
  if (ol._keys !== keys) {
    ol._keys = keys;
    ol.replaceChildren(...steps.map((s) => el('li.step', { dataset: { key: s.key } }, icon('circle'), el('span.step-label'), el('span.step-note'))));
  }
  steps.forEach((s, i) => {
    const li = ol.children[i];
    if (li.dataset.state !== s.state) {
      li.dataset.state = s.state;
      const svg = li.querySelector('.i');
      setIcon(svg, STEP_ICON[s.state] || 'circle');
      svg.classList.toggle('spin', s.state === 'active');
    }
    setText(li.children[1], s.label);
    setText(li.children[2], s.note ? ` · ${s.note}` : '');
  });
}

/* ================================================================ card (Q-01) */

function pressAction(btn, fn) {
  // Cancel / Stop act on pointerdown (G-01): a re-render between press and
  // release can never swallow the click. Keyboard presses arrive as click.
  let downAt = 0;
  btn.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || btn.disabled) return;
    downAt = Date.now();
    fn();
  });
  btn.addEventListener('click', () => {
    if (Date.now() - downAt < 1500) return;
    if (!btn.disabled) fn();
  });
}
async function cancelJob(job, btn, busyLabel) {
  setBusy(btn, busyLabel);
  try {
    const r = await post(`/api/jobs/${job.id}/cancel`);
    if (!r?.cancelled) { clearBusy(btn); return; }
    if (job.kind !== 'live') toast('Cancelled', { tone: 'info', key: `cancel-${job.id}` });
  } catch (e) {
    clearBusy(btn);
    toastError(e);
  }
}
/** × Remove: no confirmation, a 160 ms fade, then DELETE (Q-03). */
export function removeJob(job, card) {
  if (card) { card.classList.add('is-leaving'); card._leaveAt = Date.now(); }
  del(`/api/jobs/${job.id}`).catch((e) => {
    if (card) { card.classList.remove('is-leaving'); delete card._leaveAt; }
    toastError(e);
  });
}

/** Descriptors of the buttons a card shows for a job's current state. */
export function cardActions(job, variant = 'full') {
  const s = job.status, live = job.kind === 'live';
  const A = (id, variantName, extra = {}) => ({ id, variant: variantName, label: actionLabel(id), ...extra });
  const compact = variant === 'compact';
  if (s === 'queued') return [{ id: 'cancel', variant: 'secondary', label: 'Cancel', busy: 'Cancelling…', press: true }];
  if (s === 'running') {
    if (live && job.live_phase === 'recording') return [{ id: 'stop', variant: 'secondary', label: 'Stop & save', icon: 'stop-square', iconCls: 'rec-square', busy: 'Stopping…', press: true }];
    if (live && job.live_phase === 'waiting') return [{ id: 'stop', variant: 'secondary', label: 'Stop waiting', busy: 'Stopping…', press: true }];
    return [{ id: 'cancel', variant: 'secondary', label: 'Cancel', busy: live ? 'Stopping…' : 'Cancelling…', press: true }];
  }
  if (s === 'stopping') {
    const label = live && job.live_phase === 'saving' ? 'Saving…' : live ? 'Stopping…' : 'Cancelling…';
    return [{ id: 'busy', variant: 'secondary', label, disabled: true, spinner: true }];
  }
  const out = [];
  if (s === 'done') {
    if (job.kind === 'transcript') {
      if (compact) return [A('view_transcript', 'secondary')];
      out.push(A('view_transcript', 'primary'), A('copy_ai', 'secondary'), A('show_file', 'quiet'));
    } else {
      const f = mainFile(job);
      const media = mediaFiles(job);
      const single = !job.result?.playlist && (live ? media.length >= 1 : media.length <= 1);
      if (f && single) out.push(f.kind === 'video' || f.kind === 'audio' ? A('play', compact ? 'secondary' : 'primary', { icon: 'play' }) : A('open', compact ? 'secondary' : 'primary'));
      if (!compact || !out.length) out.push(A('reveal', 'secondary', { icon: 'folder' }));
      if (compact) return out.slice(0, 1);
    }
  } else if (s === 'error') {
    if (compact) {
      const first = errorActions(job, { dropRemove: false })[0];
      return first ? [A(first, 'secondary')] : [];
    }
  } else if (s === 'cancelled') {
    out.push(A(job.kind === 'download' ? 'resume' : 'retry', 'secondary'));
    if (compact) return out;
  } else if (s === 'skipped') {
    if (/a live stream/i.test(job.stage || '')) out.push(A('record_live', 'secondary'));
    if (compact) return out.slice(0, 1);
  }
  if (!compact && FINAL.includes(s)) out.push({ id: 'remove-x', icon: 'x', ariaLabel: 'Remove from list' });
  return out;
}
function actionsKey(job, variant) {
  return [variant, job.status, job.live_phase || '', (job.files || []).length > 0, mediaFiles(job).length > 1, job.error?.code || '', job.result?.playlist ? 'p' : ''].join('|');
}
function buildActions(job, variant, card) {
  return cardActions(job, variant).map((a) => {
    if (a.id === 'remove-x') {
      return iconButton('x', a.ariaLabel, () => removeJob(getJob(job.id) || job, card));
    }
    const b = button(a.label, a.variant, { size: 'sm', icon: a.spinner ? null : a.icon, iconCls: a.iconCls, disabled: a.disabled });
    if (a.spinner) setBusy(b, a.label);
    if (a.id === 'cancel' || a.id === 'stop') pressAction(b, () => cancelJob(getJob(job.id) || job, b, a.busy));
    else if (a.id !== 'busy') b.addEventListener('click', () => runAction(a.id, getJob(job.id) || job, { button: b }));
    return b;
  });
}
function extraKey(job, variant) {
  if (variant === 'compact') return '';
  const f = (job.files || []).map((x) => `${x.path}:${x.size}`).join(',');
  return [job.status, job.error?.code || '', job.error?.detail?.length || 0, f, (job.result?.notes || []).join('|'), job.result?.failed || 0].join('#');
}
function buildExtra(job) {
  const nodes = [];
  if (job.status === 'error') nodes.push(errorBlock(job));
  if (job.status === 'done') {
    const rows = fileRows(job);
    if (rows) nodes.push(rows);
    const failed = job.result?.items_failed || [];
    if (failed.length) {
      nodes.push(el('details.tech.j-failed',
        el('summary', icon('chevron-right', { cls: 'chev' }), 'Show which'),
        el('ul', failed.map((x) => el('li', bdi(x.title || x.url || 'Untitled'), ': ', x.error?.title || x.error || "couldn't be downloaded")))));
    }
    for (const note of job.result?.notes || []) nodes.push(el('div.help', { dir: 'auto' }, note));
  }
  return nodes;
}

/** Build a card (variant 'full' | 'compact'). patchCard() keeps it current. */
export function renderCard(job, variant = 'full') {
  const compact = variant === 'compact';
  const card = el('article.job', { dataset: { id: job.id, kind: job.kind }, 'aria-label': displayTitle(job) });
  if (compact) card.classList.add('compact');
  const thumb = el(compact ? 'div.thumb.sm' : 'div.thumb', icon(KIND_ICON[job.kind] || 'file'));
  const title = el('div.j-title', { dir: 'auto' });
  const pillIcon = icon('clock');
  const pillWord = el('span.pill-word');
  const pillEl = el('span.pill.neutral', pillIcon, pillWord);
  if (compact) { pillEl.classList.add('bare'); pillWord.classList.add('sr-only'); }
  const detail = el('span.j-detail');
  const status = el('div.j-status', { 'aria-live': 'off' }, pillEl, detail);
  const bar = el('div.bar', { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', hidden: true }, el('i'));
  const extra = el('div.j-extra');
  const actions = el('div.j-actions');
  card.append(thumb, el('div.j-body', title, status, bar, extra), actions);
  card._p = { thumb, title, pillEl, pillIcon, pillWord, detail, status, bar, fill: bar.firstChild, extra, actions, variant, max: 0 };
  patchCard(card, job);
  return card;
}

/**
 * Update a card in place. Only text, bar width, state attributes and the
 * thumbnail change on a tick; actions and the extra area are rebuilt only
 * when the job's state (status, live phase, error, files) changes (G-01).
 */
export function patchCard(card, job, now = Date.now() / 1000) {
  const p = card._p;
  const st = statusOf(job, now);
  const compact = p.variant === 'compact';
  // title: never a bare URL; until metadata arrives, host + path in --fg-2 (D-08)
  const isUrl = !job.title || job.title === job.url;
  const shown = st.title || displayTitle(job);
  setText(p.title, shown);
  p.title.classList.toggle('is-url', isUrl && !st.title);
  const full = st.title || job.title || job.url || '';
  if (p.title.title !== full) p.title.title = full;
  if (card.getAttribute('aria-label') !== shown) card.setAttribute('aria-label', shown);
  // thumbnail, set once
  if (job.thumbnail && !p.img && /^https?:\/\//i.test(job.thumbnail)) {
    p.img = el('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer', src: job.thumbnail });
    p.img.addEventListener('error', () => p.img?.remove());
    p.thumb.append(p.img);
  }
  // state attributes
  const tone = st.tone;
  if (card.dataset.tone !== tone) card.dataset.tone = tone;
  if (card.dataset.status !== job.status) card.dataset.status = job.status;
  const phase = job.live_phase || '';
  if (card.dataset.phase !== phase) card.dataset.phase = phase;
  // pill + detail
  const pillCls = `pill ${tone}${compact ? ' bare' : ''}`;
  if (p.pillEl.className !== pillCls) p.pillEl.className = pillCls;
  setIcon(p.pillIcon, st.icon);
  // Compact rows show only the icon; the word stays for screen readers unless the text repeats it.
  setText(p.pillWord, compact && st.short[0] === st.word ? '' : st.word);
  renderParts(p.detail, compact ? st.short : st.detail);
  const dcls = `j-detail${st.detailTone ? ` is-${st.detailTone}` : ''}`;
  if (p.detail.className !== dcls) p.detail.className = dcls;
  const tip = job.stage_detail || '';
  if (p.status.title !== tip) p.status.title = tip;
  // bar: only while active, never moving backwards within a run
  if (st.bar && ACTIVE.includes(job.status)) {
    p.bar.hidden = false;
    p.bar.classList.toggle('indeterminate', !!st.bar.indeterminate);
    if (st.bar.indeterminate) p.bar.removeAttribute('aria-valuenow');
    else {
      const v = job.kind === 'download' ? Math.max(p.max, st.bar.value) : st.bar.value;
      p.max = v;
      const w = `${v}%`;
      if (p.fill.style.width !== w) p.fill.style.width = w;
      if (p.bar.getAttribute('aria-valuenow') !== String(v)) p.bar.setAttribute('aria-valuenow', String(v));
    }
    if (p.bar.getAttribute('aria-label') !== st.bar.label) p.bar.setAttribute('aria-label', st.bar.label);
  } else if (!p.bar.hidden) {
    p.bar.hidden = true;
    p.max = 0;
  }
  if (!ACTIVE.includes(job.status)) p.max = 0;
  // actions and extra area: rebuilt only on a state change
  const ak = actionsKey(job, p.variant);
  if (p.ak !== ak) {
    p.ak = ak;
    p.actions.replaceChildren(...buildActions(job, p.variant, card));
  }
  const xk = extraKey(job, p.variant);
  if (p.xk !== xk) {
    const openTech = p.extra.querySelector('details.tech[open]');
    p.xk = xk;
    if (!(openTech && job.status === 'error' && p.xcode === job.error?.code)) p.extra.replaceChildren(...(compact ? [] : buildExtra(job)));
    p.xcode = job.error?.code;
  }
  // 'clock' lines change every second; finished cards only for their relative time.
  p.tick = st.tick ? 'clock' : FINAL.includes(job.status) ? 'slow' : false;
}

/* ================================================================ JobView (G-01) */

/** Q-04 order: running and stopping first, then queued in submission order, then finished newest first. */
export function sortQueue(a, b) {
  const rank = (j) => (j.status === 'running' || j.status === 'stopping' ? 0 : j.status === 'queued' ? 1 : 2);
  const ra = rank(a), rb = rank(b);
  if (ra !== rb) return ra - rb;
  if (ra === 0) return a.created - b.created;
  if (ra === 1) return a.created - b.created;
  return (b.updated || 0) - (a.updated || 0);
}
/** Newest first by creation (Recent downloads, Recordings). */
export const sortNewest = (a, b) => b.created - a.created;

/**
 * A keyed list of job elements in one container.
 * new JobView(container, {variant: 'full'|'compact', filter(job), sort(a, b), limit,
 *   render(job, view) -> element, patch(element, job, view), empty() -> node|null,
 *   onUpdate(items, view)})
 * - creates elements only for new ids, removes vanished ones, patches the rest,
 *   and moves nodes only when the order really changed
 * - render/patch default to renderCard/patchCard; a custom layout (the Transcript
 *   work area) passes its own and keeps the same rules
 */
export class JobView {
  constructor(container, opts = {}) {
    this.container = container;
    this.variant = opts.variant || 'full';
    this.filter = opts.filter || (() => true);
    this.sort = opts.sort || sortQueue;
    this.limit = opts.limit ?? Infinity;
    this.renderFn = opts.render || ((job) => renderCard(job, this.variant));
    this.patchFn = opts.patch || ((node, job) => patchCard(node, job));
    this.emptyFn = opts.empty || null;
    this.onUpdate = opts.onUpdate || null;
    this.els = new Map();
    this.items = [];
    this.emptyNode = null;
    if (!opts.custom) container.classList.add('job-list');
    if (this.variant === 'compact') container.classList.add('compact');
    views.add(this);
    this.update(store.list);
  }
  update(list = store.list) {
    const items = list.filter(this.filter).sort(this.sort).slice(0, this.limit);
    this.items = items;
    const ids = new Set(items.map((j) => j.id));
    for (const [id, node] of this.els) {
      if (ids.has(id)) continue;
      this.els.delete(id);
      // A card the user removed finishes its 160 ms fade first (Q-03).
      const wait = node._leaveAt ? 170 - (Date.now() - node._leaveAt) : 0;
      if (wait > 0) setTimeout(() => node.remove(), wait);
      else node.remove();
    }
    items.forEach((job, i) => {
      let node = this.els.get(job.id);
      if (!node) {
        node = this.renderFn(job, this);
        node.dataset.id = job.id;
        this.els.set(job.id, node);
      } else this.patchFn(node, job, this);
      const at = this.container.children[i + (this.emptyNode && this.emptyNode.parentNode === this.container ? 1 : 0)];
      if (at !== node) this.container.insertBefore(node, at || null);
    });
    if (!items.length && this.emptyFn) {
      if (!this.emptyNode) this.emptyNode = this.emptyFn();
      if (this.emptyNode && this.emptyNode.parentNode !== this.container) this.container.prepend(this.emptyNode);
    } else if (this.emptyNode?.parentNode) this.emptyNode.remove();
    this.onUpdate?.(items, this);
  }
  /** Re-patch clock-driven lines (waiting countdowns, REC health, 'just now'). */
  tick(now, slow = false) {
    for (const job of this.items) {
      const node = this.els.get(job.id);
      const t = node?._p?.tick;
      if (t === 'clock' || (slow && t === 'slow') || (t === true)) this.patchFn(node, job, this, now);
    }
  }
  element(id) { return this.els.get(id) || null; }
  /** Is this job's element on screen right now (not in a hidden tab or closed details)? */
  shows(id) {
    const node = this.els.get(id);
    return !!node && node.isConnected && (node.checkVisibility ? node.checkVisibility() : node.offsetParent !== null);
  }
  destroy() {
    views.delete(this);
    for (const node of this.els.values()) node.remove();
    this.els.clear();
    this.emptyNode?.remove();
  }
}
/** True when some view shows the job right now (G-02c suppression). */
export function isJobVisible(id) {
  for (const v of views) if (v.shows(id)) return true;
  return false;
}
let ticks = 0;
setInterval(() => {
  const now = Date.now() / 1000;
  const slow = ++ticks % 20 === 0;
  for (const v of views) v.tick(now, slow);
}, 1000);

/* ================================================================ announcements (G-02, G-03, G-14) */

const burst = { on: false, saved: 0, failed: 0, hidden: 0, titles: [] };
let lastFailToast = 0;
let foldedFails = 0;
let foldTimer = null;
const verbFor = { download: "Couldn't download", transcript: "Couldn't transcribe", live: "Couldn't record" };

function announcer({ job, to }) {
  if (job.from_history) return;
  const visible = isJobVisible(job.id);
  const work = job.kind !== 'live';
  if (to === 'done') {
    if (work && burst.on) { burst.saved++; burst.titles.push(displayTitle(job)); }
    if (job.kind === 'transcript') {
      loadModels().catch(() => {});          // a model may have been downloaded (UI-16, FIN-14)
      loadHardware().catch(() => {});        // and the backend verified
      if (callHook('transcript.ready', job) === true) return;
      if (!visible) {
        store.unseenTranscripts.push(job.id);
        setTabDot('transcript', true, `${store.unseenTranscripts.length} new`);
      }
    }
    if (visible) return;
    if (work && burst.on) { burst.hidden++; return; }
    if (job.kind === 'transcript') {
      toast(['Transcript ready: “', bdi(truncate(displayTitle(job), 80)), '”'], {
        tone: 'ok', actions: [{ label: 'View', onClick: () => runAction('view_transcript', getJob(job.id) || job) }],
      });
      notify.send('Transcript ready', displayTitle(job), () => runAction('view_transcript', getJob(job.id) || job));
    } else {
      const f = mainFile(job);
      const actions = [];
      if (f && (f.kind === 'video' || f.kind === 'audio') && !job.result?.playlist) actions.push({ label: 'Play', onClick: () => runAction('play', getJob(job.id) || job) });
      actions.push({ label: 'Show in folder', onClick: () => runAction('reveal', getJob(job.id) || job) });
      toast(['Saved “', bdi(truncate(displayTitle(job), 80)), '”'], { tone: 'ok', actions });
      notify.send('Download finished', displayTitle(job), () => navigate('queue'));
    }
    return;
  }
  if (to === 'error') {
    if (work && burst.on) burst.failed++;
    if (visible) return;
    store.unseenFailed.add(job.id);
    emit('unseen', counts());
    const now = Date.now();
    if (now - lastFailToast < 3000) {
      foldedFails++;
      if (work && burst.on) burst.hidden++;
      scheduleFoldedFailToast();
      return;
    }
    lastFailToast = now;
    const first = errorActions(job, { dropRemove: false })[0];
    toast([`${verbFor[job.kind] || "Couldn't finish"} “`, bdi(truncate(displayTitle(job), 60)), `”: ${job.error?.title || 'something went wrong'}`], {
      tone: 'err',
      actions: first ? [{ label: 'Fix', onClick: () => runAction(first, getJob(job.id) || job) }] : [{ label: 'View in Queue', onClick: () => navigate('queue') }],
    });
  }
}
function scheduleFoldedFailToast() {
  clearTimeout(foldTimer);
  foldTimer = setTimeout(() => {
    if (burst.on || !foldedFails) { if (!burst.on) foldedFails = 0; return; }
    toast(`${plural(foldedFails, 'more item', 'more items')} failed`, { tone: 'err', actions: [{ label: 'Review', onClick: () => navigate('queue') }] });
    foldedFails = 0;
    lastFailToast = Date.now();
  }, 3100);
}
function afterTransitions() {
  const c = counts();
  if (c.activeWork >= 2) burst.on = true;
  if (burst.on && c.activeWork === 0) {
    if (burst.hidden) {
      const parts = [`${num(burst.saved)} saved`];
      if (burst.failed) parts.push(`${num(burst.failed)} failed`);
      toast(`All done: ${parts.join(', ')}`, {
        tone: burst.failed ? 'warn' : 'ok', actions: [{ label: 'Review', onClick: () => navigate('queue') }],
      });
      if (burst.saved) notify.send(burst.saved === 1 ? 'Download finished' : `${num(burst.saved)} downloads finished`, burst.titles.slice(0, 3).join(', '), () => navigate('queue'));
    }
    Object.assign(burst, { on: false, saved: 0, failed: 0, hidden: 0, titles: [] });
    foldedFails = 0;
  }
}
