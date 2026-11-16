/* Pairing screen for a panel opened from another device: ask the streaming computer for
   access, show a 6-digit code, wait for someone there to approve it. */
import { storeDeviceToken } from './api.js';
import { h } from './ui.js';

const POLL_MS = 2000;
const UNREACHABLE_MS = 30000;

/** "Firefox on macOS", "Safari on iPhone": simple user-agent parsing. */
export function deviceLabel(ua = navigator.userAgent) {
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
    : /Windows/.test(ua) ? 'Windows' : /Mac OS X|Macintosh/.test(ua) ? 'macOS' : /CrOS/.test(ua) ? 'ChromeOS'
    : /Linux|X11/.test(ua) ? 'Linux' : '';
  const browser = /Edg\//.test(ua) ? 'Edge' : /OPR\/|Opera/.test(ua) ? 'Opera'
    : /Firefox\/|FxiOS/.test(ua) ? 'Firefox' : /Chrome\/|CriOS/.test(ua) ? 'Chrome'
    : /Safari\//.test(ua) ? 'Safari' : 'Browser';
  return os ? `${browser} on ${os}` : browser;
}

async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let data = {};
  try { data = await r.json(); } catch (e) { /* no body */ }
  return { status: r.status, ok: r.ok, data };
}

const OFF_TEXT = 'Remote access is off on the streaming computer, or it can’t be reached.';
const spaced = (code) => `${code.slice(0, 3)} ${code.slice(3)}`;

export function showPairing(root) {
  root.hidden = false;
  root.className = 'pairing';
  let session = null;       // {request, secret, expires, code}
  let timer = null, tick = null, failingSince = 0;

  const card = h('div', { class: 'card pairing-card' });
  root.replaceChildren(card);

  function stop() { clearTimeout(timer); clearInterval(tick); timer = tick = null; }

  function render(...kids) {
    stop();
    card.replaceChildren(...kids.filter(Boolean));
  }

  function message(title, text, { again = true, error = false } = {}) {
    render(
      h('h1', { class: 'pairing-title', text: title }),
      h('p', { class: 'pairing-text' + (error ? ' err' : ''), role: 'alert', text }),
      again ? h('button', { class: 'primary', text: 'Ask again', onclick: form }) : null);
  }

  function form(note) {
    const input = h('input', { type: 'text', id: 'pairing-name', value: deviceLabel(), maxlength: '60', autocomplete: 'off' });
    const btn = h('button', { class: 'primary', type: 'submit', text: 'Ask for access' });
    const err = h('p', { class: 'help error', role: 'alert' });
    if (typeof note === 'string') err.textContent = note;
    render(
      h('h1', { class: 'pairing-title', text: 'Use this panel from this device' }),
      h('p', { class: 'pairing-text', text: 'This panel is run from the streaming computer. Ask for access, then approve the code that appears here on that computer.' }),
      h('form', { class: 'pairing-form', onsubmit: async (e) => {
        e.preventDefault();
        btn.disabled = true; btn.classList.add('busy'); err.textContent = '';
        await start((input.value || '').trim() || deviceLabel(), err);
        if (!session) { btn.disabled = false; btn.classList.remove('busy'); }
      } },
        h('div', { class: 'field' }, h('label', { for: 'pairing-name', text: 'Name this device' }), input,
          h('p', { class: 'help', text: 'Shown on the streaming computer when you ask.' })),
        err, btn));
  }

  async function start(name, errEl) {
    let res;
    try { res = await post('/api/pair/start', { name }); } catch (e) { errEl.textContent = OFF_TEXT; return; }
    if (res.status === 503) { errEl.textContent = OFF_TEXT; return; }
    if (res.status === 429) { errEl.textContent = 'Too many requests from this device; wait a few minutes.'; return; }
    if (!res.ok || !res.data.code) { errEl.textContent = (res.data && res.data.error) || 'Something went wrong. Try again.'; return; }
    session = { request: res.data.request, secret: res.data.secret, expires: res.data.expires * 1000, code: String(res.data.code) };
    failingSince = 0;
    waiting();
    schedule(0);
  }

  function waiting() {
    const left = h('strong', { class: 'pairing-left' });
    const code = h('div', { class: 'pairing-code', role: 'img', 'aria-label': 'Code: ' + session.code.split('').join(' ') },
      ...session.code.split('').map((d, i) => h('span', { class: 'digit' + (i === 2 ? ' gap' : ''), 'aria-hidden': 'true', text: d })));
    const status = h('p', { class: 'pairing-status', 'aria-live': 'polite' });
    render(
      h('h1', { class: 'pairing-title', text: 'Approve this code on the streaming computer' }),
      code,
      h('p', { class: 'pairing-text' }, 'A prompt with the same code (', spaced(session.code), ') is showing on the streaming computer. Waiting for approval. Expires in ', left, '.'),
      status);
    const upd = () => {
      const s = Math.max(0, Math.round((session.expires - Date.now()) / 1000));
      left.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      status.textContent = failingSince && Date.now() - failingSince > UNREACHABLE_MS ? 'Can’t reach the streaming computer. Still trying…' : '';
    };
    upd();
    tick = setInterval(upd, 1000);
  }

  function schedule(ms) {
    clearTimeout(timer);
    if (!session || document.hidden) return;
    timer = setTimeout(poll, ms);
  }

  async function poll() {
    if (!session || document.hidden) return;
    const mine = session;
    let res;
    try { res = await post('/api/pair/poll', { request: mine.request, secret: mine.secret }); } catch (e) { res = null; }
    if (session !== mine) return;
    if (!res || res.status >= 500) {
      if (!failingSince) failingSince = Date.now();
      schedule(POLL_MS); return;
    }
    failingSince = 0;
    const state = res.status === 404 ? 'expired' : res.data && res.data.state;
    if (!['pending', 'approved', 'denied', 'expired'].includes(state)) {   // not an answer we know: don't loop until expiry
      session = null;
      return message('Couldn’t check the request', (res.data && res.data.error) || `The streaming computer sent an unexpected reply (${res.status}).`, { error: true });
    }
    if (state === 'approved' && res.data.token) { stop(); session = null; storeDeviceToken(res.data.token); location.reload(); return; }
    if (state === 'denied') { session = null; return message('Request denied', 'The streaming computer denied this request.'); }
    if (state === 'expired' || Date.now() > mine.expires + 5000) { session = null; return message('Code expired', 'That code ran out of time before it was approved.'); }
    schedule(POLL_MS);
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden && session) schedule(0); else clearTimeout(timer); });
  form();
}
