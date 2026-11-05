/* Tiny DOM toolkit shared by every view. Markup is never parsed from strings: every
   string from the daemon, Twitch or OBS goes through text nodes (chat, names, titles...).
   The matching CSS components are documented at the top of style.css. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** h('div', {class: 'x', onclick: fn, text: 'hi'}, ...children). Strings become text nodes. */
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') Object.assign(el.style, v);
    else if (k === 'text') el.textContent = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'value' || k === 'checked' || k === 'selected' || k === 'disabled' || k === 'hidden') el[k] = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

/** Inline SVG icon by name (paths are static literals, so this is safe). */
const ICONS = {
  live: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM7.1 7.1a7 7 0 0 0 0 9.8m9.8 0a7 7 0 0 0 0-9.8M4.2 4.2a11 11 0 0 0 0 15.6m15.6 0a11 11 0 0 0 0-15.6',
  deck: 'M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM7 9h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2zM7 13h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z',
  twitch: 'M5 3h15v11l-4 4h-4l-3 3v-3H5zM11 8v4m4-4v4',
  obs: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 6.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM7.8 12.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM16.2 12.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z',
  settings: 'M4 6h9m4 0h3M4 12h3m4 0h9M4 18h11m4 0h1M15 4v4M9 10v4m8 2v4',
  sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4m0-14.2-1.4 1.4M6.3 17.7l-1.4 1.4',
  moon: 'M21 13.5A8.5 8.5 0 0 1 10.5 3a8.5 8.5 0 1 0 10.5 10.5z',
  x: 'M6 6l12 12M18 6 6 18',
  check: 'M4 12l5 5L20 6',
  warn: 'M12 3 2 21h20zM12 9v5m0 3v.01',
  info: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 8v5m0-8.5v.01',
  play: 'M7 4v16l13-8z',
  stop: 'M6 6h12v12H6z',
  pause: 'M7 5h4v14H7zm6 0h4v14h-4z',
  mic: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zm-7 9a7 7 0 0 0 14 0m-7 7v3',
  micOff: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zm-7 9a7 7 0 0 0 14 0m-7 7v3M4 4l16 16',
  volume: 'M4 9v6h4l5 4V5L8 9zm12-1a5 5 0 0 1 0 8m2.5-11a9 9 0 0 1 0 14',
  send: 'M3 12 21 3l-6 18-3-7z',
  refresh: 'M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5',
  camera: 'M4 7h4l2-2h4l2 2h4v12H4zm8 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.5 1.5M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5',
  clip: 'M4 6h12v12H4zM16 10l5-3v10l-5-3z',
  flag: 'M5 21V4h12l-2 4 2 4H5',
  dots: 'M5 12h.01M12 12h.01M19 12h.01',
  undo: 'M9 14 4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3',
  redo: 'm15 14 5-5-5-5m5 5H9a5 5 0 0 0 0 10h3',
  plus: 'M12 5v14M5 12h14',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm9 16-4.3-4.3',
  trash: 'M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  edit: 'M4 20h4L19 9l-4-4L4 16zm9.5-13.5 4 4',
  chevronRight: 'm9 6 6 6-6 6',
  chevronDown: 'm6 9 6 6 6-6',
  arrowRight: 'M5 12h14m-6-6 6 6-6 6',
  grid: 'M4 4h7v7H4zm9 0h7v7h-7zM4 13h7v7H4zm9 0h7v7h-7z',
  layers: 'M12 3 2 8l10 5 10-5zM2 13l10 5 10-5',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 4v5l3 2',
  home: 'M3 11 12 4l9 7M5 10v10h14V10M10 20v-6h4v6',
  monitor: 'M3 4h18v12H3zM8 20h8m-4-4v4',
  external: 'M14 4h6v6m0-6-9 9m7 1v6H4V6h6',
  lock: 'M6 11h12v10H6zm2 0V7a4 4 0 0 1 8 0v4',
  bolt: 'M13 2 4 14h7l-1 8 9-12h-7z',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  text: 'M5 6V4h14v2M12 4v16m-3 0h6',
  code: 'm8 7-5 5 5 5m8-10 5 5-5 5',
  dial: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 3v5',
  pointer: 'M5 3l14 7-6 2-2 6z',
  wifiOff: 'M2 2l20 20M8.5 16.4a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 5-2.7M19 12.9a10 10 0 0 0-2.3-1.6M2 8.8a15 15 0 0 1 4.2-2.6M22 8.8A15 15 0 0 0 10.7 5M12 20h.01',
  bot: 'M12 3v3M7 6h10a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V9a3 3 0 0 1 3-3zm2 6v2m6-2v2m-6 3.5h6M1.5 12v3m21-3v3',
  sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z',
  key: 'M14.5 3.5a6 6 0 1 0 0 .01zM10.3 13.7 3 21m2.5-2.5L8 21m-.5-4.5L10 19',
};
export function icon(name, cls = 'ic') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[name] || ICONS.dots);
  svg.append(p);
  return svg;
}

export const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
export const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/** Only #rrggbb colors from untrusted sources (chat) are ever applied. */
export function safeColor(v) { return /^#[0-9a-fA-F]{6}$/.test(v || '') ? v : ''; }

/** Only PNG data URLs rendered by the daemon are used as key images. */
export function safeImage(v) { return typeof v === 'string' && v.startsWith('data:image/png;base64,') ? v : ''; }

export function fmtDuration(s) {
  s = Math.max(0, Math.floor(s || 0));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return (hh ? hh + ':' : '') + String(mm).padStart(hh ? 2 : 1, '0') + ':' + String(ss).padStart(2, '0');
}

/* ------------------------------------------------------------------ toasts */

/** Good/info toasts fade after 3.5 s. Errors stay 8 s with a close button, and the timer
    pauses while the pointer or keyboard focus is on the toast. */
export function toast(msg, opts = {}) {
  const host = $('#toasts');
  const bad = !!opts.bad;
  const close = bad && h('button', { class: 'ghost icon-btn toast-close', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: () => dismiss() }, icon('x'));
  const t = h('div', { class: 'toast' + (bad ? ' bad' : opts.ok ? ' ok' : ''), role: bad ? 'alert' : 'status' },
    icon(bad ? 'warn' : opts.ok ? 'check' : 'info'),
    h('div', { class: 'grow' }, msg instanceof Node ? msg : h('span', { text: String(msg) })), close);
  host.append(t);
  requestAnimationFrame(() => t.classList.add('in'));
  let left = opts.ms || (bad ? 8000 : 3500), started = 0, timer = null, gone = false;
  function dismiss() {
    if (gone) return;
    gone = true; clearTimeout(timer);
    t.classList.remove('in'); setTimeout(() => t.remove(), 250);
  }
  const start = () => { if (gone || timer) return; started = Date.now(); timer = setTimeout(dismiss, left); };
  const pause = () => { if (!timer) return; clearTimeout(timer); timer = null; left = Math.max(1500, left - (Date.now() - started)); };
  if (bad) {
    t.addEventListener('mouseenter', pause);
    t.addEventListener('mouseleave', () => { if (!t.contains(document.activeElement)) start(); });
    t.addEventListener('focusin', pause);
    t.addEventListener('focusout', (e) => { if (!t.contains(e.relatedTarget) && !t.matches(':hover')) start(); });
  }
  start();
}

/* ------------------------------------------------------------------ modal (with focus trap) */

let lastFocus = null;
export function openModal(title, body, { wide } = {}) {
  lastFocus = document.activeElement;
  $('#modal-title').textContent = title;
  $('#modal-body').replaceChildren(body);
  $('#modal .modal-box').classList.toggle('wide', !!wide);
  $('#modal').hidden = false;
  const first = $('#modal-body').querySelector('input, button, select, textarea');
  (first || $('#modal-close')).focus();
}
export function closeModal() {
  $('#modal').hidden = true;
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}
export function modalOpen() { return !$('#modal').hidden; }

/** Promise<boolean> confirm dialog; `danger` styles the OK button red. */
export function confirmDialog(title, text, { ok = 'OK', danger = false } = {}) {
  return new Promise((resolve) => {
    const done = (v) => { closeModal(); resolve(v); };
    openModal(title, h('div', {},
      h('p', { class: 'hint', text }),
      h('div', { class: 'row end' },
        h('button', { class: 'ghost', text: 'Cancel', onclick: () => done(false) }),
        h('button', { class: danger ? 'danger' : 'primary', text: ok, onclick: () => done(true) }))));
  });
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Tab' || $('#modal').hidden) return;
  const focusable = $$('#modal .modal-box :is(button, input, select, textarea, a[href], summary, [contenteditable="true"], [tabindex]:not([tabindex="-1"]))')
    .filter((el) => !el.disabled && el.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

/* ------------------------------------------------------------------ form bits */

export function field(label, control, help, cls) {
  return h('label', { class: 'field' + (cls ? ' ' + cls : '') },
    h('span', { class: 'label', text: label }), control, help && h('span', { class: 'help' }, help));
}

export function switchInput(checked, onchange, label) {
  const input = h('input', { type: 'checkbox', role: 'switch', checked, onchange: (e) => onchange(e.target.checked) });
  return h('label', { class: 'switch' }, input, h('span', { class: 'track' }), label && h('span', { class: 'label', text: label }));
}

/** A button that shows a spinner while its async handler runs and toasts errors. */
export function asyncButton(props, handler, ...kids) {
  const b = h('button', props, ...kids);
  b.addEventListener('click', async () => {
    if (b.disabled) return;
    b.disabled = true; b.classList.add('busy');
    try { await handler(); } catch (e) { toast(e.message || String(e), { bad: true }); }
    finally { b.disabled = false; b.classList.remove('busy'); }
  });
  return b;
}

export function chip(state, text, title) {
  return h('span', { class: 'chip ' + (state === true ? 'ok' : state === false ? 'bad' : state || ''), text, title });
}

/** Empty state: empty('No clips yet', 'Make one with the Clip key.', 'clip'). */
export function empty(text, sub, iconName) {
  return h('div', { class: 'empty' }, iconName && h('div', { class: 'empty-icon' }, icon(iconName)),
    h('div', { class: 'what', text }), sub && h('div', { class: 'how', text: sub }));
}
