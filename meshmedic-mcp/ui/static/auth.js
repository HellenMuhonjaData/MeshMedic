// Clinician sign-in for every MeshMedic page (Phase 1).
//
// - Shows a sign-in screen until /api/auth/me says a clinician is signed in.
// - Adds the session's CSRF token to every write request to /api/ (the web
//   app refuses writes without it) and shows the sign-in screen again if the
//   session has expired (401).
// - window.meshAuth.requestPin() asks for the signing PIN in-page and opens
//   the 15-minute signing window; it resolves true when the PIN was accepted.
(function () {
  let csrfToken = null;
  let me = null;
  const nativeFetch = window.fetch.bind(window);

  window.fetch = async (input, options = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    const method = (options.method || 'GET').toUpperCase();
    const opts = { ...options, credentials: 'same-origin' };
    if (url.startsWith('/api/') && method !== 'GET' && csrfToken) {
      const headers = new Headers(options.headers || {});
      headers.set('X-CSRF-Token', csrfToken);
      opts.headers = headers;
    }
    const response = await nativeFetch(input, opts);
    if (response.status === 401 && url.startsWith('/api/') && !url.startsWith('/api/auth/')) showSignIn();
    return response;
  };

  const css = `
  .mm-overlay { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center;
    background: var(--brand-deep, #0d2b30); background-image: radial-gradient(circle at 50% 0%, rgba(25,179,166,.35), transparent 60%);
    padding: 16px; }
  .mm-overlay.modal { background: rgba(13,43,48,.55); background-image: none; }
  .mm-panel { background: var(--paper, #fff); color: var(--ink, #13282e); border-radius: 14px; padding: 28px 28px 22px;
    width: 100%; max-width: 380px; box-shadow: 0 20px 60px rgba(0,0,0,.3); }
  .mm-panel h2 { font-family: var(--font-display, inherit); font-size: 20px; margin: 10px 0 4px; }
  .mm-panel p { color: var(--ink-soft, #587076); font-size: 13px; margin: 0 0 12px; }
  .mm-panel label { display: block; font-size: 12px; color: var(--ink-soft, #587076); margin: 10px 0 4px; }
  .mm-panel input { width: 100%; padding: 9px 10px; border: 1px solid var(--line, #dce6e5); border-radius: 8px; font: inherit; }
  .mm-panel button { width: 100%; margin: 16px 0 0; }
  .mm-panel .mm-error { color: var(--bad, #a12b2b); font-size: 13px; min-height: 18px; margin-top: 8px; }
  .mm-brand { display: flex; align-items: center; gap: 10px; }
  .mm-brand img { width: 40px; height: 40px; border-radius: 10px; }
  .mm-user { display: inline-flex; align-items: center; gap: 10px; color: #fff; font-size: 13px; }
  .mm-user button { margin: 0; padding: 5px 10px; font-size: 12px; background: transparent; border: 1px solid rgba(255,255,255,.3); color: #fff; }
  .mm-user button:hover:not(:disabled) { background: rgba(255,255,255,.1); border-color: rgba(255,255,255,.5); }`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  function el(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstChild;
  }

  function showSignIn() {
    if (!document.body || document.getElementById('mmSignIn')) return;
    const overlay = el(`
      <div class="mm-overlay" id="mmSignIn" role="dialog" aria-modal="true" aria-labelledby="mmSignInTitle">
        <form class="mm-panel" id="mmSignInForm">
          <div class="mm-brand"><img src="logo.svg" alt="" /><strong style="font-size:18px">MeshMedic</strong></div>
          <h2 id="mmSignInTitle">Clinician sign-in</h2>
          <p>Sign in to review and sign encounter documentation.</p>
          <label for="mmClinicianId">Clinician ID</label>
          <input id="mmClinicianId" autocomplete="username" required />
          <label for="mmPassword">Password</label>
          <input id="mmPassword" type="password" autocomplete="current-password" required />
          <button type="submit">Sign in</button>
          <div class="mm-error" id="mmSignInError" role="alert"></div>
        </form>
      </div>`);
    document.body.appendChild(overlay);
    document.getElementById('mmClinicianId').focus();
    document.getElementById('mmSignInForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const err = document.getElementById('mmSignInError');
      err.textContent = '';
      const res = await nativeFetch('/api/auth/login', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          clinician_id: document.getElementById('mmClinicianId').value,
          password: document.getElementById('mmPassword').value,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { err.textContent = data.error || 'Sign-in failed.'; return; }
      location.reload();  // every panel reloads with the new session
    });
  }

  function renderUser() {
    const header = document.querySelector('header');
    if (!header || !me || document.getElementById('mmUser')) return;
    const box = el(`<span class="mm-user" id="mmUser"><span>Signed in as <strong></strong></span><button type="button">Sign out</button></span>`);
    box.querySelector('strong').textContent = me.display_name;
    box.querySelector('button').addEventListener('click', async () => {
      await fetch('/api/auth/logout', { method: 'POST' });
      location.reload();
    });
    (header.lastElementChild && header.lastElementChild !== header.firstElementChild ? header.lastElementChild : header).appendChild(box);
  }

  function requestPin(message) {
    return new Promise((resolve) => {
      const overlay = el(`
        <div class="mm-overlay modal" role="dialog" aria-modal="true" aria-labelledby="mmPinTitle">
          <form class="mm-panel">
            <h2 id="mmPinTitle">Enter your signing PIN</h2>
            <p></p>
            <label for="mmPin">Signing PIN</label>
            <input id="mmPin" type="password" inputmode="numeric" autocomplete="off" required />
            <button type="submit">Confirm</button>
            <button type="button" class="ghost">Cancel</button>
            <div class="mm-error" role="alert"></div>
          </form>
        </div>`);
      overlay.querySelector('p').textContent = message ||
        'Signing is a clinician-only action. Your PIN keeps signing open for 15 minutes; each note is still signed individually.';
      document.body.appendChild(overlay);
      const input = overlay.querySelector('input');
      const err = overlay.querySelector('.mm-error');
      input.focus();
      const close = (ok) => { overlay.remove(); resolve(ok); };
      overlay.querySelector('button.ghost').addEventListener('click', () => close(false));
      overlay.querySelector('form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const res = await fetch('/api/auth/reauth', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin: input.value }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) { close(true); return; }
        err.textContent = data.error || 'PIN not accepted.';
        input.value = '';
        if (res.status === 423) overlay.querySelector('button[type=submit]').disabled = true;
      });
    });
  }

  window.meshAuth = { requestPin, get me() { return me; } };

  // This script loads in <head>; wait for the page before drawing anything.
  const pageReady = document.readyState === 'loading'
    ? new Promise((resolve) => document.addEventListener('DOMContentLoaded', resolve, { once: true }))
    : Promise.resolve();

  Promise.all([nativeFetch('/api/auth/me', { credentials: 'same-origin' }), pageReady])
    .then(async ([res]) => {
      if (!res.ok) { showSignIn(); return; }
      me = await res.json();
      csrfToken = me.csrf_token;
      renderUser();
      window.dispatchEvent(new CustomEvent('meshmedic:signed-in', { detail: me }));
    })
    .catch(() => pageReady.then(showSignIn));
})();
