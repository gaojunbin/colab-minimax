// Settings dialog: default GPU, idle auto-release and Google Drive persistence.
const IDLE_PRESETS = [15, 30, 60, 120];
const settingsUi = {
  dialog: $('#settings-dialog'),
  form: $('#settings-form'),
  gpuHint: $('#gpu-hint'),
  idleCustom: $('#idle-custom'),
  idleInput: $('#idle-input'),
  drive: $('#drive-switch'),
  saveBtn: $('#save-btn'),
};

const gpuControl = initSegmented($('#seg-gpu'), () => updateGpuHint(true));
const idleControl = initSegmented($('#seg-idle'), (value) => {
  const custom = value === 'custom';
  if (!custom) settingsUi.idleInput.value = value;
  setCustomIdle(custom);
  if (custom) settingsUi.idleInput.focus({ preventScroll: true });
});

function updateGpuHint(animate) {
  const selected = gpuControl.selected;
  setText(settingsUi.gpuHint, selected ? selected.dataset.hint : '', animate);
}

function setCustomIdle(open) {
  settingsUi.idleCustom.classList.toggle('is-open', open);
}

function fillSettings(current) {
  const s = current || {};
  gpuControl.set(s.default_gpu || 'L4', true);
  updateGpuHint(false);
  const minutes = Math.round((s.idle_timeout_seconds || 1800) / 60);
  settingsUi.idleInput.value = minutes;
  const preset = IDLE_PRESETS.includes(minutes);
  idleControl.set(preset ? minutes : 'custom', true);
  setCustomIdle(!preset);
  settingsUi.drive.checked = Boolean(s.enable_drive_mount);
}

function readIdleMinutes() {
  const choice = idleControl.value;
  const raw = choice === 'custom' ? settingsUi.idleInput.value : choice;
  const minutes = parseInt(raw, 10);
  return Number.isFinite(minutes) ? Math.min(240, Math.max(5, minutes)) : 30;
}

$('#open-settings').addEventListener('click', () => {
  // Never show (and possibly save) defaults before the real settings arrive.
  if (!state.settings) {
    toast('正在读取设置，请稍候');
    refreshStatus();
    return;
  }
  fillSettings(state.settings);
  settingsUi.dialog.showModal();
});

settingsUi.form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = {
    default_gpu: gpuControl.value,
    idle_timeout_seconds: readIdleMinutes() * 60,
    enable_drive_mount: settingsUi.drive.checked,
  };
  setLoading(settingsUi.saveBtn, true);
  try {
    const resp = await api('/api/v1/instance/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.settings) state.settings = { ...state.settings, ...data.settings };
    settingsUi.dialog.close('saved');
    toast('设置已保存');
    refreshStatus();
  } catch (err) {
    console.error(err);
    toast('保存失败，请重试', 'error');
  } finally {
    setLoading(settingsUi.saveBtn, false);
  }
});
