// ui.js: shared components. Every tab builds its UI from these so the app
// looks and behaves the same everywhere.
import { el, $, parseLinks, linkProblem, prefersReducedMotion } from './core.js';

/* ================================================================ icons */

/** <svg class="i"><use href="#i-NAME"/></svg>. opts: {cls, label} (label makes it non-decorative). */
export function icon(name, opts = {}) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', ['i', opts.cls].filter(Boolean).join(' '));
  if (opts.label) { svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', opts.label); }
  else svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}
/** Swap the symbol an existing icon shows. */
export function setIcon(svg, name) {
  const use = svg?.querySelector('use');
  if (use && use.getAttribute('href') !== `#i-${name}`) use.setAttribute('href', `#i-${name}`);
}
export const spinner = (cls = '') => icon('spinner', { cls: `spin ${cls}`.trim() });

/* ================================================================ small builders */

/**
 * A button. variant: 'primary' | 'secondary' | 'quiet' | 'danger'. opts:
 * {size:'sm', icon, iconCls, iconEnd (trailing, e.g. 'chevron-down'), title, onClick, type, id, ariaLabel, disabled, cls}
 */
export function button(label, variant = 'secondary', opts = {}) {
  const cls = ['btn', variant !== 'primary' && variant, opts.size === 'sm' && 'sm', opts.cls].filter(Boolean).join(' ');
  const b = el('button', {
    type: opts.type || 'button', class: cls, id: opts.id, title: opts.title,
    'aria-label': opts.ariaLabel, disabled: !!opts.disabled,
  });
  if (opts.icon) b.append(icon(opts.icon, { cls: opts.iconCls }));
  if (label) b.append(el('span.btn-label', label));
  if (opts.iconEnd) b.append(icon(opts.iconEnd));
  if (opts.onClick) b.addEventListener('click', opts.onClick);
  return b;
}
/** A button that looks like an inline link ('View in Queue', 'See all in Queue (5)'). */
export function linkButton(label, onClick, opts = {}) {
  const b = el('button.link', { type: 'button', id: opts.id, title: opts.title }, label);
  if (onClick) b.addEventListener('click', onClick);
  return b;
}
/** Icon-only button; always labelled (G-13). */
export function iconButton(name, ariaLabel, onClick, opts = {}) {
  const b = el('button', { type: 'button', class: ['icon-btn', opts.cls].filter(Boolean).join(' '), 'aria-label': ariaLabel, title: opts.title || ariaLabel });
  b.append(icon(name));
  if (onClick) b.addEventListener('click', onClick);
  return b;
}
/** Status pill: tone run|ok|err|warn|neutral|rec, always icon + word (V-05). */
export function pill(tone, iconName, word) {
  return el(`span.pill.${tone}`, icon(iconName), el('span.pill-word', word));
}
/**
 * Callout. tone info|warn|err|ok. content: {title, text, actions: [node], rows}
 * or a string. Returns the element.
 */
export function callout(tone, content, opts = {}) {
  const iconName = opts.icon || { info: 'info', warn: 'alert', err: 'x', ok: 'check' }[tone] || 'info';
  const c = typeof content === 'string' ? { text: content } : content;
  const body = el('div.callout-body',
    c.title ? el('div.callout-title', { dir: opts.dirAuto ? 'auto' : null }, c.title) : null,
    c.text ? el('div.callout-text', c.text) : null,
    c.body || null,
    c.actions?.length ? el('div.callout-actions', c.actions) : null);
  return el(`div.callout.${tone}`, { role: opts.role || null }, icon(iconName), body);
}
/** <details class="more"> with a chevron summary and a trailing summary span. */
export function disclosure(title, { sum = '', open = false, id, body = [] } = {}) {
  const sumEl = el('span.more-sum', sum);
  const d = el('details.more', { id, open },
    el('summary', icon('chevron-right', { cls: 'chev' }), el('span.more-title', title), sumEl),
    el('div.more-body', body));
  d.setSummary = (text) => { sumEl.textContent = text; };
  return d;
}
/**
 * Page title block (V-02): h1 20/600 + 13 px sub, optional actions on the right.
 * sub may be a string or nodes (e.g. with a link button inside).
 */
export function pageHead(title, sub = '', actions = []) {
  return el('div.page-head',
    el('div.titles', el('h1', title), sub ? el('p.sub', sub) : null),
    actions.length ? el('div.actions', actions) : null);
}
/**
 * A card section. opts: {title, sub, id, actions: [nodes], saveState (adds the
 * autosave indicator and data-save-scope), body: [nodes], cls, tag ('section')}.
 */
export function card(opts = {}) {
  const head = opts.title ? el('div.card-head',
    el('h2', { id: opts.id ? `${opts.id}-title` : null }, opts.title),
    opts.saveState ? el('span.save-state', { 'aria-live': 'polite' }) : null,
    opts.actions?.length ? [el('span.spacer'), ...opts.actions] : null,
    opts.sub ? el('p.card-sub', opts.sub) : null) : null;
  return el(`${opts.tag || 'section'}.card`, {
    id: opts.id, class: opts.cls, 'aria-labelledby': opts.title && opts.id ? `${opts.id}-title` : null,
    'data-save-scope': opts.saveState ? '' : null,
  }, head, opts.body || []);
}

/** Visual flash on a section or row to show where an action landed (1.6 s). */
export function flash(node) {
  if (!node) return;
  node.classList.remove('flash');
  void node.offsetWidth;
  node.classList.add('flash');
  setTimeout(() => node.classList.remove('flash'), 1700);
}

/* ================================================================ busy buttons */

/** Put a button into a busy state: keeps its width, shows a spinner + label, disables it. */
export function setBusy(btn, label) {
  if (!btn) return;
  if (!btn._idle) {
    btn._idle = { nodes: Array.from(btn.childNodes), disabled: btn.disabled, minWidth: btn.style.minWidth };
    btn.style.minWidth = `${btn.getBoundingClientRect().width}px`;
  }
  btn.replaceChildren(spinner(), el('span.btn-label', label));
  btn.disabled = true;
  btn.classList.add('is-busy');
  btn.setAttribute('aria-busy', 'true');
}
/** Update the label of a busy button (e.g. 'Installing ffmpeg · 42%'). */
export function setBusyLabel(btn, label) {
  const l = btn?.querySelector('.btn-label');
  if (l && l.textContent !== label) l.textContent = label;
}
export function clearBusy(btn) {
  if (!btn || !btn._idle) return;
  btn.replaceChildren(...btn._idle.nodes);
  btn.disabled = btn._idle.disabled;
  btn.style.minWidth = btn._idle.minWidth;
  btn.classList.remove('is-busy');
  btn.removeAttribute('aria-busy');
  delete btn._idle;
}
/** Run fn while btn shows busyLabel. Returns fn's result; always restores. */
export async function withBusy(btn, busyLabel, fn) {
  setBusy(btn, busyLabel);
  try { return await fn(); } finally { if (btn.isConnected) clearBusy(btn); else delete btn._idle; }
}

/* ================================================================ announcements */

/** Say something to screen readers without showing it. */
export function announce(text) {
  const a = $('#announcer');
  if (!a) return;
  a.textContent = '';
  requestAnimationFrame(() => { a.textContent = text; });
}

/* ================================================================ toasts (G-02) */

const MAX_TOASTS = 3;
const toasts = [];     // newest last

/**
 * Show a toast. opts: {tone: 'info'|'ok'|'err'|'warn', actions: [{label, onClick}],
 * timeout (ms; default 5000, errors 10000; 0 = stay), key (replace a toast with the same key),
 * icon}. text may be a string or nodes (use bdi() for titles).
 * Returns {el, dismiss(), update(text)}.
 */
export function toast(text, opts = {}) {
  const tone = opts.tone || 'info';
  const region = $('#toasts');
  if (opts.key) {
    const same = toasts.find((t) => t.key === opts.key);
    if (same) same.dismiss(true);
  }
  const iconName = opts.icon || { info: 'info', ok: 'check', err: 'alert', warn: 'alert' }[tone];
  const textEl = el('div.toast-text');
  const node = el(`div.toast.${tone}`, { role: tone === 'err' ? 'alert' : 'status', 'aria-live': tone === 'err' ? 'assertive' : 'polite', 'aria-atomic': 'true' });
  const main = el('div.toast-main', textEl);
  const handle = { el: node, key: opts.key, timer: null, remaining: 0, started: 0 };
  if (opts.actions?.length) {
    main.append(el('div.toast-actions', opts.actions.slice(0, 2).map((a) => button(a.label, 'quiet', {
      size: 'sm', onClick: () => { handle.dismiss(); a.onClick?.(); },
    }))));
  }
  node.append(icon(iconName), main, iconButton('x', 'Dismiss', () => handle.dismiss()));

  const setContent = (t) => { textEl.replaceChildren(); textEl.append(...(Array.isArray(t) ? t : [t]).map((x) => (x instanceof Node ? x : String(x)))); };
  handle.update = (t) => setContent(t);
  handle.dismiss = (instant = false) => {
    clearTimeout(handle.timer);
    const i = toasts.indexOf(handle);
    if (i >= 0) toasts.splice(i, 1);
    if (instant || prefersReducedMotion()) node.remove();
    else { node.classList.add('is-leaving'); setTimeout(() => node.remove(), 170); }
  };
  const timeout = opts.timeout ?? (tone === 'err' ? 10000 : 5000);
  const start = (ms) => {
    if (!timeout) return;
    handle.remaining = ms; handle.started = Date.now();
    clearTimeout(handle.timer);
    handle.timer = setTimeout(() => handle.dismiss(), ms);
  };
  const pause = () => {
    if (!timeout || !handle.timer) return;
    clearTimeout(handle.timer); handle.timer = null;
    handle.remaining = Math.max(1500, handle.remaining - (Date.now() - handle.started));
  };
  const resume = () => {
    if (!timeout || handle.timer || node.matches(':hover') || node.contains(document.activeElement)) return;
    start(handle.remaining);
  };
  node.addEventListener('mouseenter', pause);
  node.addEventListener('mouseleave', resume);
  node.addEventListener('focusin', pause);
  node.addEventListener('focusout', () => setTimeout(resume, 0));

  toasts.push(handle);
  while (toasts.length > MAX_TOASTS) toasts[0].dismiss(true);
  region.append(node);
  // Filling the live region after it is in the page makes screen readers announce it.
  requestAnimationFrame(() => setContent(text));
  start(timeout);
  return handle;
}
/** Dismiss the newest toast (Esc). Returns true when one was open. */
export function dismissNewestToast() {
  const t = toasts[toasts.length - 1];
  if (!t) return false;
  t.dismiss();
  return true;
}
export const toastError = (e, fallback = 'Something went wrong.') => toast(e?.text || e?.message || fallback, { tone: 'err' });

/* ================================================================ dialogs */

/**
 * A modal <dialog class="dlg">. opts: {title, body: node|nodes, footer: node|nodes,
 * cls ('confirm' for 420 px), labelledBy, onCancel(e) (Esc; call e.preventDefault() to keep it),
 * initialFocus: node}. Returns {el, close(value), closed: Promise<value>}.
 */
export function openDialog(opts = {}) {
  const root = $('#dialogs') || document.body;
  const titleId = 'dlg-' + Math.random().toString(36).slice(2, 8);
  const dlg = el('dialog', { class: ['dlg', opts.cls].filter(Boolean).join(' '), 'aria-labelledby': opts.title ? titleId : opts.labelledBy });
  if (opts.title) dlg.append(el('h2.dlg-title', { id: titleId }, opts.title));
  if (opts.body) dlg.append(el('div.dlg-body', opts.body));
  if (opts.footer) dlg.append(el('div.dlg-foot', opts.footer));
  let resolve;
  const closed = new Promise((r) => { resolve = r; });
  let result;
  dlg.addEventListener('cancel', (e) => { opts.onCancel?.(e); });
  dlg.addEventListener('close', () => { resolve(result); if (!opts.keep) dlg.remove(); });
  root.append(dlg);
  dlg.showModal();
  (opts.initialFocus || dlg.querySelector('[autofocus]'))?.focus();
  return {
    el: dlg,
    closed,
    close(value) { result = value; if (dlg.open) dlg.close(); },
  };
}

/**
 * Confirm dialog (420 px, focus on Cancel, Esc cancels). Resolves true/false.
 * confirmDialog({title, body, confirm: 'Delete', cancel: 'Cancel', danger: true})
 */
export function confirmDialog({ title, body = '', confirm = 'OK', cancel = 'Cancel', danger = true } = {}) {
  const cancelBtn = button(cancel, 'secondary');
  const okBtn = button(confirm, danger ? 'danger' : 'primary');
  const d = openDialog({
    title, cls: 'confirm',
    body: typeof body === 'string' ? el('p', body) : body,
    footer: el('div.end', cancelBtn, okBtn),
    initialFocus: cancelBtn,
  });
  cancelBtn.addEventListener('click', () => d.close(false));
  okBtn.addEventListener('click', () => d.close(true));
  return d.closed.then((v) => v === true);
}

/* ================================================================ menu (popover) */

/**
 * Popover menu under an anchor button (T-06 'Save as'). items: [{label, onSelect, icon, disabled}]
 * Arrow keys move, Home/End jump, Enter/Space choose, Esc or a click outside closes and
 * focus returns to the anchor. Call once per anchor; it wires the anchor's click.
 * Returns {el, open(), close()}.
 */
export function menu(anchor, items, opts = {}) {
  const id = 'menu-' + Math.random().toString(36).slice(2, 8);
  const pop = el('div.menu', { id, popover: 'auto', role: 'menu', 'aria-label': opts.label || anchor.textContent.trim() });
  const buttons = items.map((it) => {
    const b = el('button.menu-item', { type: 'button', role: 'menuitem', tabindex: '-1', disabled: !!it.disabled },
      it.icon ? icon(it.icon) : null, el('span', it.label));
    b.addEventListener('click', () => { pop.hidePopover(); it.onSelect?.(); });
    return b;
  });
  pop.append(...buttons);
  (opts.root || document.body).append(pop);
  anchor.setAttribute('aria-haspopup', 'menu');
  anchor.setAttribute('aria-expanded', 'false');
  anchor.setAttribute('aria-controls', id);

  const place = () => {
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let top = r.bottom + 4;
    if (top + h > innerHeight - 8 && r.top - h - 4 > 8) top = r.top - h - 4;
    let left = opts.align === 'end' ? r.right - w : r.left;
    left = Math.max(8, Math.min(left, innerWidth - w - 8));
    pop.style.top = `${Math.round(top)}px`;
    pop.style.left = `${Math.round(left)}px`;
  };
  const focusItem = (i) => {
    const enabled = buttons.filter((b) => !b.disabled);
    if (!enabled.length) return;
    enabled[(i + enabled.length) % enabled.length].focus();
  };
  pop.addEventListener('toggle', (e) => {
    const open = e.newState === 'open';
    anchor.setAttribute('aria-expanded', String(open));
    if (open) { place(); focusItem(0); }
    else if (pop.contains(document.activeElement) || document.activeElement === document.body) anchor.focus();
  });
  pop.addEventListener('keydown', (e) => {
    const enabled = buttons.filter((b) => !b.disabled);
    const i = enabled.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); focusItem(i + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); focusItem(i - 1); }
    else if (e.key === 'Home') { e.preventDefault(); focusItem(0); }
    else if (e.key === 'End') { e.preventDefault(); focusItem(enabled.length - 1); }
    else if (e.key === 'Tab') { pop.hidePopover(); }
  });
  anchor.addEventListener('click', () => pop.togglePopover());
  anchor.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' && !pop.matches(':popover-open')) { e.preventDefault(); pop.showPopover(); }
  });
  addEventListener('resize', () => { if (pop.matches(':popover-open')) place(); });
  return { el: pop, open: () => pop.showPopover(), close: () => pop.hidePopover() };
}

/* ================================================================ segmented controls */

let segCount = 0;
/**
 * Segmented radio group (V: .seg). options: [{value, label, disabled}].
 * Returns the <fieldset>; read with segValue(fs), write with setSegValue(fs, v).
 * onChange(value) fires on user change. Arrow keys work natively.
 */
export function segmented({ name, legend, options, value, onChange, id }) {
  name = name || `seg${++segCount}`;
  const fs = el('fieldset.seg', { id, role: 'radiogroup' }, el('legend.sr-only', legend || name));
  for (const o of options) {
    const input = el('input', { type: 'radio', name, value: o.value, checked: String(o.value) === String(value), disabled: !!o.disabled });
    fs.append(el('label', input, el('span', o.label)));
  }
  fs.addEventListener('change', () => onChange?.(segValue(fs)));
  return fs;
}
export const segValue = (fs) => fs.querySelector('input[type=radio]:checked')?.value ?? '';
export function setSegValue(fs, v) {
  for (const r of fs.querySelectorAll('input[type=radio]')) r.checked = String(r.value) === String(v);
}

/**
 * The same look as a tablist (G-13: transcript view switch). options [{value, label}].
 * Returns the element; el.value / el.setValue(v). onChange(value).
 */
export function tabSwitch({ label, options, value, onChange }) {
  const wrap = el('div.seg', { role: 'tablist', 'aria-label': label });
  const btns = options.map((o) => el('button', {
    type: 'button', role: 'tab', 'aria-selected': String(o.value === value), tabindex: o.value === value ? '0' : '-1', dataset: { value: o.value },
  }, o.label));
  wrap.append(...btns);
  const select = (v, fire) => {
    for (const b of btns) {
      const on = b.dataset.value === v;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    wrap.value = v;
    if (fire) onChange?.(v);
  };
  wrap.value = value;
  wrap.setValue = (v) => select(v, false);
  btns.forEach((b, i) => {
    b.addEventListener('click', () => { if (wrap.value !== b.dataset.value) select(b.dataset.value, true); });
    b.addEventListener('keydown', (e) => {
      const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const next = btns[(i + d + btns.length) % btns.length];
      next.focus();
      select(next.dataset.value, true);
    });
  });
  return wrap;
}

/* ================================================================ link box (G-05, G-06) */

/**
 * Wire a link textarea + primary button + hint line.
 *  input:   <textarea class="linkbox">
 *  button:  the primary button
 *  hint:    the .link-hint element (its .hint-text child is managed here)
 *  opts: {hintText: 'Enter to start · Shift+Enter for another line',
 *         label(parsed) -> button text (default keeps the button's text),
 *         canSubmit(parsed) -> true | 'reason for title' (extra rule, e.g. live link on Download),
 *         onSubmit(parsed), onChange(parsed)}
 * Enter and Ctrl+Enter submit, Shift+Enter adds a line, Esc never clears.
 * Returns {parse(), refresh(), showOk(nodes, ms), setHint(text), clear()}.
 */
export function linkBox(input, btn, hint, opts = {}) {
  const hintText = opts.hintText || 'Enter to start · Shift+Enter for another line';
  let textEl = hint.querySelector('.hint-text');
  if (!textEl) { textEl = el('span.hint-text'); hint.prepend(textEl); }
  let okTimer = null;
  let okShown = false;
  const baseTitle = btn.getAttribute('title') || '';

  const setHint = (nodes, tone) => {
    hint.classList.toggle('is-error', tone === 'error');
    hint.classList.toggle('is-ok', tone === 'ok');
    textEl.replaceChildren(...(Array.isArray(nodes) ? nodes : [nodes]).map((x) => (x instanceof Node ? x : String(x))));
    if (tone === 'error') {
      input.setAttribute('aria-invalid', 'true');
    } else input.removeAttribute('aria-invalid');
  };
  const refresh = () => {
    const parsed = parseLinks(input.value);
    const problem = linkProblem(parsed);
    if (!okShown || !parsed.empty) {
      if (okShown && !parsed.empty) { okShown = false; clearTimeout(okTimer); }
      if (problem) setHint(problem.text, problem.tone === 'error' ? 'error' : 'note');
      else if (!okShown) setHint(hintText);
    }
    let reason = '';
    if (parsed.empty) reason = 'Paste a link first';
    else if (!parsed.links.length) reason = 'Paste a link first';
    else if (opts.canSubmit) { const ok = opts.canSubmit(parsed); if (ok !== true) reason = ok || ''; }
    const label = opts.label ? opts.label(parsed) : null;
    const lab = btn.querySelector('.btn-label');
    if (label && !btn._idle) { if (lab) lab.textContent = label; else btn.textContent = label; }
    if (!btn._idle) {
      btn.disabled = !!reason;
      btn.title = reason || baseTitle;
    }
    opts.onChange?.(parsed);
    return parsed;
  };
  const submit = () => {
    const parsed = parseLinks(input.value);
    if (!parsed.links.length) { refresh(); input.focus(); return; }
    if (btn.disabled) return;
    opts.onSubmit?.(parsed);
  };
  input.addEventListener('input', refresh);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  btn.addEventListener('click', submit);
  refresh();
  return {
    parse: () => parseLinks(input.value),
    refresh,
    submit,
    setHint: (text, tone) => setHint(text, tone),
    /** The 6 s '✓ Added …' line in place of the hint (D-08, L-03). */
    showOk(nodes, ms = 6000) {
      okShown = true;
      setHint(nodes, 'ok');
      clearTimeout(okTimer);
      okTimer = setTimeout(() => { okShown = false; refresh(); }, ms);
    },
    clear() { input.value = ''; refresh(); },
  };
}
