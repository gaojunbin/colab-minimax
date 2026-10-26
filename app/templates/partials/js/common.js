// Shared helpers for all control pages: DOM shortcuts, toast, clipboard,
// segmented controls, dialogs and the sticky navigation shadow.
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const toast = (() => {
  let timer = null;
  return (message, type = 'success') => {
    const el = $('#toast');
    if (!el) return;
    $('#toast-text', el).textContent = message;
    el.dataset.type = type;
    el.classList.add('is-visible');
    clearTimeout(timer);
    timer = setTimeout(() => el.classList.remove('is-visible'), 2600);
  };
})();

// Replaces text and replays a short blur-in, only when the value changed.
function setText(el, text, animate = true) {
  if (!el) return;
  const next = text == null ? '' : String(text);
  if (el.textContent === next) return;
  el.textContent = next;
  if (!animate) return;
  el.classList.remove('swap');
  void el.offsetWidth;
  el.classList.add('swap');
}

// Shows or hides an element, replaying a subtle pop-in when it appears.
function reveal(el, visible) {
  if (!el) return;
  if (visible && el.hidden) {
    el.hidden = false;
    el.classList.remove('pop');
    void el.offsetWidth;
    el.classList.add('pop');
  } else if (!visible) {
    el.hidden = true;
  }
}

function setLoading(btn, loading) {
  if (!btn) return;
  btn.classList.toggle('is-loading', loading);
  btn.setAttribute('aria-busy', loading ? 'true' : 'false');
}

// fetch wrapper that sends expired sessions back to the login page.
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  if (resp.status === 401) {
    window.location.href = '/_control/login';
    throw new Error('Unauthorized');
  }
  return resp;
}

// navigator.clipboard only exists on secure origins; plain-HTTP gateways
// fall back to a hidden textarea + execCommand.
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    // Fall through to the legacy path below.
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (err) {
    ok = false;
  }
  area.remove();
  return ok;
}

document.addEventListener('click', async (event) => {
  const btn = event.target.closest('[data-copy], [data-copy-target]');
  if (!btn) return;
  const target = btn.dataset.copyTarget ? document.getElementById(btn.dataset.copyTarget) : null;
  const text = btn.dataset.copy ?? (target ? target.textContent : '');
  const ok = await copyText(text.trim());
  if (!ok) {
    toast('复制失败，请手动选择文本', 'error');
    return;
  }
  btn.classList.add('is-copied');
  clearTimeout(btn._copyTimer);
  btn._copyTimer = setTimeout(() => btn.classList.remove('is-copied'), 1600);
});

// Segmented control built from radio inputs; the thumb slides via --idx.
function initSegmented(root, onChange) {
  const inputs = $$('input[type="radio"]', root);
  root.style.setProperty('--n', inputs.length);
  const sync = () => {
    const idx = inputs.findIndex((input) => input.checked);
    root.style.setProperty('--idx', Math.max(0, idx));
    root.classList.toggle('is-empty', idx < 0);
  };
  inputs.forEach((input) =>
    input.addEventListener('change', () => {
      sync();
      if (onChange) onChange(input.value);
    })
  );
  sync();
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add('is-ready')));
  return {
    get value() {
      const checked = inputs.find((input) => input.checked);
      return checked ? checked.value : null;
    },
    get selected() {
      return inputs.find((input) => input.checked) || null;
    },
    set(value, instant = false) {
      const match = inputs.find((input) => input.value === String(value));
      if (instant) root.classList.remove('is-ready');
      inputs.forEach((input) => {
        input.checked = input === match;
      });
      sync();
      if (instant) requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add('is-ready')));
    },
  };
}

// Dialogs: [data-close] buttons close their dialog; clicking the backdrop dismisses.
let pointerDownTarget = null;
document.addEventListener('pointerdown', (event) => {
  pointerDownTarget = event.target;
});
document.addEventListener('click', (event) => {
  const closer = event.target.closest('[data-close]');
  if (closer) {
    const dialog = closer.closest('dialog');
    if (dialog) dialog.close('cancel');
    return;
  }
  const target = event.target;
  if (target instanceof HTMLDialogElement && target.open && pointerDownTarget === target) {
    target.close('dismiss');
  }
});

// Cross-document fades run only between control pages: ComfyUI does not opt
// in, and an aborted transition would surface as an error on its page.
window.addEventListener('pageswap', (event) => {
  const transition = event.viewTransition;
  if (!transition) return;
  const entry = event.activation && event.activation.entry;
  const path = entry && entry.url ? new URL(entry.url).pathname : '';
  if (path.startsWith('/_control/')) return;
  transition.ready.catch(() => {});
  transition.skipTransition();
});

(() => {
  const nav = $('#nav');
  if (!nav) return;
  const update = () => nav.classList.toggle('is-scrolled', window.scrollY > 4);
  update();
  window.addEventListener('scroll', update, { passive: true });
})();
