// Status hero: maps backend phases to copy, boot steps, actions, stats and logs.
const hero = {
  root: $('#hero'),
  dot: $('#hero-dot'),
  title: $('#hero-title'),
  sub: $('#hero-sub'),
  percent: $('#hero-percent'),
  bootPanel: $('#boot-panel'),
  steps: $$('#steps .step'),
  enterBtn: $('#enter-btn'),
  startGroup: $('#start-group'),
  startBtn: $('#start-btn'),
  stopBtn: $('#stop-btn'),
  autoJump: $('#auto-jump'),
  autoJumpToggle: $('#auto-jump-toggle'),
};
const BOOT_PHASES = hero.steps.map((step) => step.dataset.phase);

const stats = {
  gpu: $('#stat-gpu'),
  cu: $('#stat-cu'),
  jobs: $('#stat-jobs'),
  idle: $('#stat-idle'),
};

let stopPending = false;
let shownPercent = 0;
let idleDeadline = null;

function formatClock(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

function formatUptime(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h) return `${h} 小时 ${m} 分钟`;
  return m ? `${m} 分钟` : '不到 1 分钟';
}

function describe(d) {
  const step = BOOT_PHASES.indexOf(d.phase);
  if (d.phase === 'READY') {
    return { mode: 'ready', tone: 'green', title: 'ComfyUI 已就绪', sub: `已运行 ${formatUptime(d.uptime_seconds)}` };
  }
  if (step !== -1) {
    const title = hero.steps[step].dataset.title;
    return { mode: 'booting', tone: 'blue', step, title, sub: d.detail || d.description };
  }
  if (d.phase === 'STOPPING') {
    return { mode: 'stopping', tone: 'blue', title: '正在释放实例', sub: d.detail || d.description };
  }
  if (d.phase === 'ERROR') {
    return { mode: 'error', tone: 'red', title: '实例异常', sub: d.error || d.detail || d.description };
  }
  return { mode: 'stopped', tone: 'gray', title: '实例已休眠', sub: '不产生算力费用' };
}

function animatePercent(target) {
  const from = shownPercent;
  shownPercent = target;
  if (from === target) {
    hero.percent.textContent = `${target}%`;
    return;
  }
  const start = performance.now();
  const tick = (now) => {
    if (shownPercent !== target) return;
    const t = Math.min(1, (now - start) / 700);
    const eased = 1 - Math.pow(1 - t, 4);
    hero.percent.textContent = `${Math.round(from + (target - from) * eased)}%`;
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function renderProgress(view, progress) {
  const booting = view.mode === 'booting';
  hero.bootPanel.classList.toggle('is-open', booting);
  reveal(hero.percent, booting);
  if (booting) animatePercent(progress || 0);
  hero.steps.forEach((step, i) => {
    step.classList.toggle('is-done', booting ? i < view.step : view.mode === 'ready');
    step.classList.toggle('is-active', booting && i === view.step);
  });
}

function renderActions(view) {
  const { mode } = view;
  reveal(hero.enterBtn, mode === 'ready');
  reveal(hero.startGroup, mode === 'stopped' || mode === 'error');
  hero.startBtn.textContent = mode === 'error' ? '重新启动' : '启动实例';
  reveal(hero.stopBtn, mode !== 'stopped');
  setLoading(hero.stopBtn, mode === 'stopping' || stopPending);
  reveal(hero.autoJump, BOOT.autoRedirect && mode !== 'ready');
}

function renderHero(d) {
  const view = describe(d);
  hero.root.dataset.tone = view.tone;
  hero.dot.dataset.tone = view.tone;
  setText(hero.title, view.title);
  setText(hero.sub, view.sub, false);
  renderProgress(view, d.progress);
  renderActions(view);
  return view;
}

function setStat(el, value, unit = '', animate = true) {
  setText($('.v', el), value, animate);
  $('.unit', el).textContent = unit;
}

function renderIdleCountdown() {
  if (idleDeadline === null) return;
  setStat(stats.idle, formatClock((idleDeadline - Date.now()) / 1000), '', false);
}

function renderStats(d, view) {
  const hasBalance = d.compute_balance !== null && d.compute_balance !== undefined;
  setStat(stats.gpu, d.active_gpu || '—');
  setStat(
    stats.cu,
    hasBalance ? Number(d.compute_balance).toLocaleString('zh-CN', { maximumFractionDigits: 1 }) : '—',
    hasBalance ? 'CU' : ''
  );

  if (view.mode !== 'ready') {
    idleDeadline = null;
    setStat(stats.jobs, '—');
    setStat(stats.idle, String(Math.round((d.idle_timeout_seconds || 0) / 60)), '分钟');
    return;
  }

  setStat(stats.jobs, `${d.running_jobs} / ${d.queued_jobs}`);
  const busy = d.running_jobs > 0 || d.queued_jobs > 0 || d.active_api_tasks > 0;
  if (busy) {
    idleDeadline = null;
    setStat(stats.idle, '暂停');
  } else {
    const wasCounting = idleDeadline !== null;
    idleDeadline = Date.now() + d.remaining_idle_seconds * 1000;
    setStat(stats.idle, formatClock(d.remaining_idle_seconds), '', !wasCounting);
  }
}

setInterval(renderIdleCountdown, 1000);

function renderDriveNotice(d) {
  const box = $('#drive-auth');
  if (d.drive_auth_url) $('#drive-auth-link').href = d.drive_auth_url;
  reveal(box, Boolean(d.drive_auth_url));
}

// ---- Logs -------------------------------------------------------------------
const logs = {
  root: $('#logs'),
  toggle: $('#logs-toggle'),
  body: $('#logs-body'),
  view: $('#log-view'),
  peek: $('#logs-peek'),
};
const LOG_PREF_KEY = 'h3.logs.open';
const TIMESTAMP = /^(\[\d{2}:\d{2}:\d{2}\])\s?(.*)$/;
let logsSignature = '';
let logsTouched = false;
let logsAutoOpened = false;

function setLogsOpen(open, remember) {
  logs.root.classList.toggle('is-open', open);
  logs.body.classList.toggle('is-open', open);
  logs.toggle.setAttribute('aria-expanded', String(open));
  if (open) logs.view.scrollTop = logs.view.scrollHeight;
  if (!remember) return;
  try {
    localStorage.setItem(LOG_PREF_KEY, open ? '1' : '0');
  } catch (err) {
    // Storage may be unavailable (private mode); the preference is optional.
  }
}

logs.toggle.addEventListener('click', () => {
  logsTouched = true;
  setLogsOpen(!logs.root.classList.contains('is-open'), true);
});

try {
  if (localStorage.getItem(LOG_PREF_KEY) === '1') setLogsOpen(true, false);
} catch (err) {
  // Ignore unavailable storage.
}

function renderLogs(lines, view) {
  const list = lines || [];
  const last = list[list.length - 1] || '';
  const signature = `${list.length}|${last}`;
  if (signature !== logsSignature) {
    logsSignature = signature;
    const pre = logs.view;
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 32;
    const frag = document.createDocumentFragment();
    list.forEach((line, i) => {
      if (i) frag.append('\n');
      const match = TIMESTAMP.exec(line);
      if (!match) {
        frag.append(line);
        return;
      }
      const ts = document.createElement('span');
      ts.className = 'log-ts';
      ts.textContent = match[1];
      frag.append(ts, ` ${match[2]}`);
    });
    if (list.length) pre.replaceChildren(frag);
    else pre.textContent = '暂无日志';
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    logs.peek.textContent = last.replace(TIMESTAMP, '$2');
  }
  if (view.mode === 'error' && !logsTouched && !logsAutoOpened) {
    logsAutoOpened = true;
    setLogsOpen(true, false);
  }
}
