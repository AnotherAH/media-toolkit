// queue.js: the Queue tab, the full history (Q-01..Q-06, G-15).
//   Groups: In progress (full cards), Needs attention (failures and playlists
//   with failed items, full cards with their fixes), Finished (one line each
//   with the main action and a ⋯ menu) and Earlier (jobs from past sessions).
//   Filter chips, Retry all failed, Clear completed with Undo, and the folder
//   buttons that open what is saved in Settings (Q-04).
//   Every list is a keyed JobView, so nothing is rebuilt on a progress tick (G-01).
import { el, post, on, emit, CONFIG, navigate, revealPath, setText, setChildren, num, plural, truncate } from './core.js';
import { pageHead, button, iconButton, toast, toastError, segmented, menu, withBusy } from './ui.js';
import {
  JobView, sortQueue, allJobs, ACTIVE, FINAL, renderCard, patchCard, cardActions, errorActions, errorBlock,
  fileRows, runAction, removeJob, getJob, displayTitle, actionLabel,
} from './jobs.js';

/* ================================================================ grouping */

const byUpdated = (a, b) => (b.updated || 0) - (a.updated || 0) || (b.created || 0) - (a.created || 0);
/** A failure, or a finished playlist where some videos failed (Q-06). */
const needsAttention = (j) => j.status === 'error' || (j.status === 'done' && (Number(j.result?.failed) || 0) > 0);
const CLEARABLE = ['done', 'skipped', 'cancelled'];

const GROUPS = [
  { id: 'active', title: 'In progress', variant: 'full', sort: sortQueue, match: (j) => ACTIVE.includes(j.status) },
  { id: 'attention', title: 'Needs attention', variant: 'full', sort: byUpdated, match: (j) => !j.from_history && needsAttention(j) },
  { id: 'finished', title: 'Finished', variant: 'row', sort: byUpdated, match: (j) => !j.from_history && FINAL.includes(j.status) && !needsAttention(j) },
  { id: 'earlier', title: 'Earlier', variant: 'row', sort: byUpdated, match: (j) => !!j.from_history && FINAL.includes(j.status) },
];
const FILTERS = {
  all: () => true,
  active: (j) => ACTIVE.includes(j.status),
  failed: needsAttention,
  finished: (j) => FINAL.includes(j.status) && !needsAttention(j),
};
const NOTHING = {
  active: 'Nothing is running right now.',
  failed: 'Nothing failed.',
  finished: 'Nothing has finished yet.',
};

const Q = { filter: 'all', views: [], menus: new Map() };

/* ================================================================ mount */

export function mount(section) {
  Q.openDl = button('Open downloads folder', 'quiet', { size: 'sm', icon: 'folder', onClick: () => openFolder('download_dir', Q.openDl) });
  Q.openTr = button('Open transcripts folder', 'quiet', { size: 'sm', icon: 'folder', onClick: () => openFolder('transcript_dir', Q.openTr) });
  Q.retryAll = button('Retry all failed', 'secondary', { size: 'sm', icon: 'retry', onClick: retryAllFailed });
  Q.retryAll.hidden = true;
  Q.clear = button('Clear completed', 'secondary', { size: 'sm', onClick: clearCompleted });
  Q.clear.hidden = true;

  Q.filterSeg = segmented({
    name: 'qFilter', id: 'qFilter', legend: 'Show',
    options: [
      { value: 'all', label: 'All' }, { value: 'active', label: 'In progress' },
      { value: 'failed', label: 'Failed' }, { value: 'finished', label: 'Finished' },
    ],
    value: 'all',
    onChange: (v) => { Q.filter = v; for (const g of Q.views) g.view.update(); render(); },
  });
  Q.toolbar = el('div.q-toolbar', { hidden: true }, Q.filterSeg);
  Q.none = el('p.q-none', { hidden: true });
  Q.empty = el('div.card.q-empty', { hidden: true }, el('div.empty',
    el('div.empty-title', 'Nothing here yet'),
    el('p.empty-text', 'Downloads, transcripts and recordings you start show up here with live progress.'),
    el('div.empty-actions',
      button('Download a video', 'secondary', { onClick: () => navigate('download') }),
      button('Get a transcript', 'secondary', { onClick: () => navigate('transcript') }))));

  Q.list = el('div#qList.q-groups');
  for (const g of GROUPS) Q.list.append(buildGroup(g));

  section.append(pageHead('Queue', '', [Q.openDl, Q.openTr, Q.retryAll, Q.clear]), Q.toolbar, Q.none, Q.list, Q.empty);
  on('jobs', render);
  on('config', syncFolders);
  syncFolders();
  render();
}

function buildGroup(g) {
  const head = el('h2.q-group-head', { id: `q-${g.id}-head` }, g.title);
  const container = el('div', { id: `q-${g.id}` });
  const body = g.variant === 'row' ? el('div.q-rows', container) : container;
  const sec = el('section.q-group', { hidden: true, dataset: { group: g.id }, 'aria-labelledby': `q-${g.id}-head` }, head, body);
  const view = new JobView(container, {
    variant: g.variant === 'row' ? 'compact' : 'full',
    sort: g.sort,
    filter: (j) => g.match(j) && FILTERS[Q.filter](j),
    render: g.variant === 'row' ? renderRow : renderFull,
    patch: g.variant === 'row' ? patchRow : patchFull,
    onUpdate: (items) => {
      vis(sec, items.length > 0);
      setText(head, `${g.title} (${num(items.length)})`);
    },
  });
  Q.views.push({ group: g, view, sec });
  return sec;
}
const vis = (node, show) => { if (node.hidden === !!show) node.hidden = !show; };

/** Header buttons, empty states and menu clean-up after every change. */
function render() {
  const jobs = allJobs();
  const any = jobs.length > 0;
  vis(Q.empty, !any);
  vis(Q.toolbar, any);
  const shown = Q.views.some((g) => g.view.items.length > 0);
  vis(Q.none, any && !shown);
  if (any && !shown) setText(Q.none, NOTHING[Q.filter] || 'Nothing to show.');
  vis(Q.clear, jobs.some((j) => CLEARABLE.includes(j.status)));
  const failed = retryable();
  vis(Q.retryAll, failed.length >= 2);
  const label = Q.retryAll.querySelector('.btn-label');
  if (label && !Q.retryAll._idle) setText(label, `Retry all failed (${num(failed.length)})`);
  // Menus whose row is gone (cleared, retried, filtered out) go too.
  for (const [btn, m] of Q.menus) if (!btn.isConnected) { m.el.remove(); Q.menus.delete(btn); }
}

/* ================================================================ header actions */

function syncFolders() {
  Q.openDl.disabled = !CONFIG.download_dir;
  Q.openTr.disabled = !CONFIG.transcript_dir;
}
/** The saved folder, never an unsaved field in Settings (Q-04). */
async function openFolder(key, btn) {
  const path = CONFIG[key];
  if (!path) return;
  try { await withBusy(btn, 'Opening…', () => revealPath(path)); } catch (e) { toastError(e); }
}

/** Failures this session whose fix is a plain retry. */
function retryable() {
  return allJobs().filter((j) => !j.from_history && j.status === 'error' && errorActions(j).includes('retry'));
}
async function retryAllFailed() {
  const list = retryable();
  if (!list.length) return;
  await withBusy(Q.retryAll, 'Retrying…', async () => {
    const results = await Promise.allSettled(list.map((j) => post(`/api/jobs/${j.id}/retry`)));
    const bad = results.filter((r) => r.status === 'rejected');
    if (bad.length) toast(`${plural(bad.length, "item couldn't", "items couldn't")} be started again.`, { tone: 'err' });
  });
  render();
}

/** Clear completed (Q-03): done, skipped and cancelled go; failures stay. Undo for 10 s. */
async function clearCompleted() {
  let removed = 0;
  try {
    await withBusy(Q.clear, 'Clearing…', async () => {
      const r = await post('/api/jobs/clear-completed');
      removed = Number(r?.removed) || 0;
    });
  } catch (e) { toastError(e); return; }
  if (!removed) return;
  emit('queue-cleared', { count: removed });
  toast(`Cleared ${plural(removed, 'item')}`, {
    key: 'queue-cleared',
    timeout: 10000,
    actions: [{
      label: 'Undo',
      onClick: async () => {
        try {
          const r = await post('/api/jobs/restore');
          if (!r?.restored) toast("Couldn't undo. The cleared items are gone.", { tone: 'warn' });
        } catch (e) { toastError(e); }
      },
    }],
  });
}

/* ================================================================ full cards (In progress, Needs attention) */

function renderFull(job) {
  const card = renderCard(job, 'full');
  patchFullExtras(card, job);
  return card;
}
function patchFull(card, job, _view, now) {
  patchCard(card, job, now);
  patchFullExtras(card, job);
}
/** Q-06: a playlist where some videos failed gets [Retry failed items] beside 'Show which'. */
function patchFullExtras(card, job) {
  const extra = card.querySelector('.j-extra');
  if (!extra) return;
  const want = job.status === 'done' && (Number(job.result?.failed) || 0) > 0;
  let row = extra.querySelector(':scope > .q-retry-items');
  if (want && !row) {
    const btn = button('Retry failed items', 'secondary', { size: 'sm', icon: 'retry' });
    // A retry runs the same playlist again: videos already saved are recognised and skipped.
    btn.addEventListener('click', () => runAction('retry', getJob(job.id) || job, { button: btn }));
    row = el('div.q-retry-items', btn);
    extra.append(row);
  } else if (!want && row) row.remove();
}

/* ================================================================ one-line rows (Finished, Earlier) */

function renderRow(job) {
  const card = renderCard(job, 'compact');
  card.classList.add('q-row');
  const more = iconButton('more', `More actions for ${truncate(displayTitle(job), 60)}`);
  more.classList.add('q-more');
  more.addEventListener('click', () => openMore(card, more));
  card.append(more);
  card._q = { more, expanded: false, details: null, note: null, dk: '' };
  patchRowExtras(card, job);
  return card;
}
function patchRow(card, job, _view, now) {
  patchCard(card, job, now);
  patchRowExtras(card, job);
}
function patchRowExtras(card, job) {
  const q = card._q;
  // Backend notes ('Already in your folder, so it wasn't downloaded again.') stay visible (UI-6).
  const text = job.status === 'done' ? (job.result?.notes || []).join(' ') : '';
  if (text) {
    if (!q.note) { q.note = el('div.help.q-note', { dir: 'auto' }); card.querySelector('.j-body')?.append(q.note); }
    q.note.hidden = false;
    setText(q.note, text);
  } else if (q.note) q.note.hidden = true;
  const label = `More actions for ${truncate(displayTitle(job), 60)}`;
  if (q.more.getAttribute('aria-label') !== label) { q.more.setAttribute('aria-label', label); q.more.title = 'More actions'; }
  if (q.expanded) {
    const dk = [job.status, (job.files || []).length, job.error?.code || '', job.error?.detail?.length || 0].join('|');
    if (q.dk !== dk) { q.dk = dk; setChildren(q.details, detailNodes(job)); }
  }
}
/** What a row can expand into: the error with its fixes, or the file list (Q-02). */
function detailNodes(job) {
  if (job.status === 'error') return [errorBlock(job)];
  const rows = fileRows(job);
  return rows ? [rows] : [];
}
const hasDetails = (job) => job.status === 'error' || !!(job.kind !== 'transcript' && (job.files || []).length >= 2);
function toggleDetails(card) {
  const q = card._q;
  const job = getJob(card.dataset.id);
  q.expanded = !q.expanded;
  card.classList.toggle('is-expanded', q.expanded);
  if (q.expanded) {
    if (!q.details) { q.details = el('div.q-details'); card.append(q.details); }
    q.dk = '';
    q.details.hidden = false;
    if (job) patchRowExtras(card, job);
  } else if (q.details) q.details.hidden = true;
}

/** The ⋯ menu: every action the one-line row has no room for. Built on first use. */
function openMore(card, btn) {
  if (Q.menus.has(btn)) return;               // ui.menu() toggles it from now on
  const job = getJob(card.dataset.id);
  if (!job) return;
  const primary = cardActions(job, 'compact')[0]?.id;
  const items = [];
  const skip = new Set(['remove-x', 'cancel', 'stop', 'busy', primary]);
  const run = (id) => () => runAction(id, getJob(job.id) || job, {});
  if (job.status === 'error') {
    for (const id of errorActions(job)) if (!skip.has(id)) items.push({ label: actionLabel(id), onSelect: run(id) });
  } else {
    for (const a of cardActions(job, 'full')) if (!skip.has(a.id)) items.push({ label: a.label, icon: a.icon, onSelect: run(a.id) });
  }
  let detailsItem = null;
  if (hasDetails(job)) {
    detailsItem = { label: detailsLabel(card, job), icon: 'chevron-down', onSelect: () => toggleDetails(card) };
    items.push(detailsItem);
  }
  if (primary !== 'remove') items.push({ label: 'Remove from list', icon: 'x', onSelect: () => removeJob(getJob(job.id) || job, card) });
  const m = menu(btn, items, { label: 'More actions', align: 'end' });
  Q.menus.set(btn, m);
  if (detailsItem) {
    const labelEl = m.el.querySelectorAll('.menu-item')[items.indexOf(detailsItem)]?.querySelector('span');
    m.el.addEventListener('beforetoggle', (e) => {
      if (e.newState === 'open' && labelEl) setText(labelEl, detailsLabel(card, getJob(job.id) || job));
    });
  }
  m.open();
}
function detailsLabel(card, job) {
  const open = card._q?.expanded;
  if (job.status === 'error') return open ? 'Hide details' : 'Show details';
  return open ? 'Hide files' : 'Show files';
}
