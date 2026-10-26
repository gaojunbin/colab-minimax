// Dashboard entry: status polling, instance actions and initial load.
const state = { phase: null, ready: null, settings: null };
let statusInFlight = false;
let statusQueued = false;
let pollTimer = null;

function applyStatus(d) {
  const prevPhase = state.phase;
  state.phase = d.phase;
  if (d.settings) state.settings = d.settings;

  const view = renderHero(d);
  renderStats(d, view);
  renderDriveNotice(d);
  renderLogs(d.recent_logs, view);

  if (d.phase === 'READY' && BOOT.autoRedirect && hero.autoJumpToggle.checked) {
    window.location.href = '/?direct=1';
    return;
  }

  // Refetch outputs when the instance settles; otherwise re-render previews
  // from cache whenever readiness flips (renderGallery no-ops until loaded).
  const ready = d.phase === 'READY';
  const settled = prevPhase !== null && prevPhase !== d.phase && (d.phase === 'READY' || d.phase === 'STOPPED');
  if (settled) loadOutputs();
  else if (ready !== state.ready) renderGallery();
  state.ready = ready;
}

async function refreshStatus() {
  if (statusInFlight) {
    statusQueued = true;
    return;
  }
  statusInFlight = true;
  try {
    const resp = await api('/api/v1/instance/status?passive=1');
    if (resp.ok) applyStatus(await resp.json());
  } catch (err) {
    console.error(err);
  } finally {
    statusInFlight = false;
    if (statusQueued) {
      statusQueued = false;
      refreshStatus();
    }
  }
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(refreshStatus, 2500);
}

// Background tabs stop polling; returning to the tab refreshes immediately.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearInterval(pollTimer);
    return;
  }
  refreshStatus();
  startPolling();
});

function confirmAction({ title, message, confirmText }) {
  const dialog = $('#confirm-dialog');
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  $('#confirm-ok').textContent = confirmText;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
  });
}

async function startInstance() {
  const gpu = (state.settings && state.settings.default_gpu) || 'L4';
  setLoading(hero.startBtn, true);
  try {
    const resp = await api(`/api/v1/instance/start?gpu=${encodeURIComponent(gpu)}`, { method: 'POST' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    toast(`正在启动 ${gpu} 实例`);
  } catch (err) {
    console.error(err);
    toast('启动失败，请重试', 'error');
  } finally {
    setLoading(hero.startBtn, false);
  }
  refreshStatus();
}

async function stopInstance() {
  const confirmed = await confirmAction({
    title: '释放 GPU 实例？',
    message: '生成结果会先同步到 Google Drive，随后释放实例并停止消耗算力。',
    confirmText: '释放',
  });
  if (!confirmed) return;
  stopPending = true;
  setLoading(hero.stopBtn, true);
  toast('正在同步结果并释放实例');
  try {
    const resp = await api('/api/v1/instance/stop', { method: 'POST' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    toast('实例已释放');
  } catch (err) {
    console.error(err);
    toast('释放失败，请重试', 'error');
  } finally {
    stopPending = false;
    setLoading(hero.stopBtn, false);
  }
  refreshStatus();
  loadOutputs();
}

async function completeDriveMount(event) {
  const btn = event.currentTarget;
  setLoading(btn, true);
  try {
    const resp = await api('/api/v1/instance/drive-complete', { method: 'POST' });
    const data = await resp.json();
    toast(data.message || '已触发挂载', data.ok === false ? 'error' : 'success');
  } catch (err) {
    console.error(err);
    toast('请求失败，请重试', 'error');
  } finally {
    setLoading(btn, false);
  }
  refreshStatus();
}

async function syncOutputs(event) {
  const btn = event.currentTarget;
  btn.classList.add('is-spinning');
  try {
    const resp = await api('/api/v1/instance/sync', { method: 'POST' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    toast('结果列表已更新');
  } catch (err) {
    console.error(err);
    toast('刷新失败，请稍后重试', 'error');
  } finally {
    btn.classList.remove('is-spinning');
  }
  loadOutputs();
}

hero.startBtn.addEventListener('click', startInstance);
hero.stopBtn.addEventListener('click', stopInstance);
$('#drive-auth-done').addEventListener('click', completeDriveMount);
$('#sync-btn').addEventListener('click', syncOutputs);

(async () => {
  await refreshStatus();
  loadOutputs();
  startPolling();
})();
