// Outputs gallery: lazy thumbnails, hover preview and a viewer dialog.
const gallery = {
  root: $('#gallery'),
  count: $('#outputs-count'),
  offlineHint: $('#offline-hint'),
  files: null,
};
const DRIVE_URL = 'https://drive.google.com/drive/my-drive';
const canHover = window.matchMedia('(hover: hover)').matches;
const relativeTime = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' });

const iconSvg = (id) => `<svg class="i"><use href="#${id}"/></svg>`;
const baseName = (path) => path.split('/').pop();

function mediaKind(name) {
  if (/\.(mp4|webm|mov|m4v)$/i.test(name)) return 'video';
  if (/\.(png|jpe?g|webp|gif)$/i.test(name)) return 'image';
  if (/\.(mp3|wav|flac|ogg|m4a|aac)$/i.test(name)) return 'audio';
  return 'file';
}

const KIND_ICONS = { video: 'i-film', image: 'i-film', audio: 'i-music', file: 'i-file', offline: 'i-cloud' };

function formatBytes(bytes) {
  if (!bytes) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(i === 0 || value >= 100 ? 0 : 1)} ${units[i]}`;
}

function formatWhen(mtime) {
  if (!mtime) return '';
  const diff = mtime * 1000 - Date.now();
  const abs = Math.abs(diff) / 1000;
  if (abs < 60) return '刚刚';
  if (abs < 3600) return relativeTime.format(Math.round(diff / 60000), 'minute');
  if (abs < 86400) return relativeTime.format(Math.round(diff / 3600000), 'hour');
  if (abs < 7 * 86400) return relativeTime.format(Math.round(diff / 86400000), 'day');
  return new Date(mtime * 1000).toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' });
}

const fileMeta = (file) => [formatBytes(file.size), formatWhen(file.mtime)].filter(Boolean).join(' · ');

function placeholder(kind) {
  const ph = document.createElement('span');
  ph.className = 'clip-ph';
  ph.innerHTML = iconSvg(KIND_ICONS[kind] || 'i-file');
  return ph;
}

// Videos only start loading once their card scrolls near the viewport.
const lazyMedia = 'IntersectionObserver' in window
  ? new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.src = entry.target.dataset.src;
        lazyMedia.unobserve(entry.target);
      });
    }, { rootMargin: '200px 0px' })
  : null;

function lazyLoad(el) {
  if (lazyMedia) lazyMedia.observe(el);
  else el.src = el.dataset.src;
}

function videoThumb(file, card) {
  const video = document.createElement('video');
  video.muted = true;
  video.loop = true;
  video.playsInline = true;
  video.preload = 'metadata';
  video.tabIndex = -1;
  video.setAttribute('aria-hidden', 'true');
  video.dataset.src = `${file.url}#t=0.1`;
  const badge = document.createElement('span');
  badge.className = 'clip-duration num';
  video.addEventListener('loadeddata', () => video.classList.add('is-loaded'), { once: true });
  video.addEventListener('loadedmetadata', () => {
    if (!Number.isFinite(video.duration)) return;
    badge.textContent = formatClock(video.duration);
    badge.classList.add('is-set');
  }, { once: true });
  if (canHover) {
    card.addEventListener('mouseenter', () => {
      if (video.src) video.play().catch(() => {});
    });
    card.addEventListener('mouseleave', () => {
      video.pause();
      if (video.readyState > 0) video.currentTime = 0.1;
    });
  }
  lazyLoad(video);
  return [video, badge];
}

function imageThumb(file) {
  const img = document.createElement('img');
  img.alt = '';
  img.loading = 'lazy';
  img.decoding = 'async';
  img.addEventListener('load', () => img.classList.add('is-loaded'), { once: true });
  img.src = file.url;
  return img;
}

function buildAction(file, ready) {
  const name = baseName(file.name);
  const link = document.createElement('a');
  link.className = 'icon-btn';
  if (ready) {
    link.href = file.url;
    link.setAttribute('download', name);
    link.title = '下载';
    link.setAttribute('aria-label', `下载 ${name}`);
    link.innerHTML = iconSvg('i-download');
  } else {
    link.href = DRIVE_URL;
    link.target = '_blank';
    link.rel = 'noopener';
    link.title = '在 Google Drive 中查看';
    link.setAttribute('aria-label', '在 Google Drive 中查看');
    link.innerHTML = iconSvg('i-external');
  }
  return link;
}

function buildClip(file, index, ready) {
  const kind = mediaKind(file.name);
  const card = document.createElement('article');
  card.className = 'clip';
  card.style.setProperty('--i', Math.min(index, 8));

  const media = document.createElement(ready ? 'button' : 'div');
  media.className = 'clip-media';
  media.append(placeholder(ready ? kind : 'offline'));
  if (ready) {
    media.type = 'button';
    media.setAttribute('aria-label', `预览 ${baseName(file.name)}`);
    media.addEventListener('click', () => openViewer(file));
    if (kind === 'video') media.append(...videoThumb(file, card));
    if (kind === 'image') media.append(imageThumb(file));
  }

  const name = document.createElement('div');
  name.className = 'clip-name';
  name.textContent = baseName(file.name);
  name.title = file.name;
  const meta = document.createElement('div');
  meta.className = 'clip-meta';
  meta.textContent = fileMeta(file);
  const text = document.createElement('div');
  text.className = 'clip-text';
  text.append(name, meta);

  const info = document.createElement('div');
  info.className = 'clip-info';
  info.append(text, buildAction(file, ready));
  card.append(media, info);
  return card;
}

function messageBlock(title, body) {
  const box = document.createElement('div');
  box.className = 'empty';
  box.innerHTML = `<span class="empty-icon">${iconSvg('i-film')}</span><h3></h3><p></p>`;
  $('h3', box).textContent = title;
  $('p', box).textContent = body;
  return box;
}

function releaseMedia(root) {
  $$('video, audio', root).forEach((media) => {
    media.pause();
    media.removeAttribute('src');
    media.load();
  });
}

function renderGallery() {
  const files = gallery.files;
  if (!files) return;
  const ready = state.phase === 'READY';
  if (lazyMedia) lazyMedia.disconnect();
  releaseMedia(gallery.root);
  gallery.root.setAttribute('aria-busy', 'false');
  gallery.count.textContent = files.length ? String(files.length) : '';
  gallery.offlineHint.hidden = ready || !files.length;
  if (!files.length) {
    gallery.root.replaceChildren(
      messageBlock('还没有生成结果', '通过 ComfyUI、API 或 MCP 生成的视频会保存到 Google Drive，并显示在这里。')
    );
    return;
  }
  gallery.root.replaceChildren(...files.map((file, i) => buildClip(file, i, ready)));
}

async function loadOutputs() {
  try {
    const resp = await api('/api/v1/outputs');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    gallery.files = data.files || [];
    renderGallery();
  } catch (err) {
    console.error(err);
    if (gallery.files) return;
    gallery.root.setAttribute('aria-busy', 'false');
    gallery.root.replaceChildren(messageBlock('暂时无法读取生成结果', '请稍后点击“刷新”重试。'));
  }
}

// ---- Viewer -------------------------------------------------------------------
const viewer = {
  dialog: $('#viewer'),
  media: $('#viewer-media'),
  name: $('#viewer-name'),
  meta: $('#viewer-meta'),
  download: $('#viewer-download'),
};

function openViewer(file) {
  const kind = mediaKind(file.name);
  const name = baseName(file.name);
  let node;
  if (kind === 'video' || kind === 'audio') {
    node = document.createElement(kind);
    node.controls = true;
    node.autoplay = true;
    node.playsInline = true;
    node.src = file.url;
  } else if (kind === 'image') {
    node = document.createElement('img');
    node.alt = name;
    node.src = file.url;
  } else {
    node = placeholder('file');
  }
  viewer.media.classList.toggle('is-plain', kind === 'audio' || kind === 'file');
  viewer.media.replaceChildren(node);
  viewer.name.textContent = name;
  viewer.name.title = file.name;
  viewer.meta.textContent = fileMeta(file);
  viewer.download.href = file.url;
  viewer.download.setAttribute('download', name);
  viewer.dialog.showModal();
}

viewer.dialog.addEventListener('close', () => {
  const playing = $('video, audio', viewer.media);
  if (playing) playing.pause();
  // Keep the frame visible while the dialog fades out, then stop the download.
  setTimeout(() => {
    if (viewer.dialog.open) return;
    releaseMedia(viewer.media);
    viewer.media.replaceChildren();
  }, 450);
});
