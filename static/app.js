/* Video Compression Agent — frontend logic */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  caps: null,
  upload: null,            // { upload_id, info }
  selectedBackend: 'ab-av1',
  selectedEncoder: null,   // encoder id
  selectedVmaf: 90,
  workers: null,
  encoders: [],
  backends: [],
  jobId: null,
  evtSource: null,
};

function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtDuration(s) {
  if (!s) return '—';
  const m = Math.floor(s / 60), ss = Math.floor(s % 60);
  if (m >= 60) {
    const h = Math.floor(m / 60), mm = m % 60;
    return `${h}h ${mm}m`;
  }
  return `${m}m ${ss}s`;
}
function vmafColor(score) {
  if (score == null) return 'text-slate-300';
  if (score >= 95) return 'text-emerald-400';
  if (score >= 90) return 'text-cyan-400';
  if (score >= 85) return 'text-amber-400';
  return 'text-orange-400';
}

// ── Capabilities + initial render ─────────────────────────────────────────
async function loadCaps() {
  const r = await fetch('/api/capabilities');
  state.caps = await r.json();
  state.encoders = state.caps.encoders;
  state.backends = state.caps.backends;
  renderCapsBadge();
  renderBackendToggle();
  renderEncoderGrid();
  renderVmafToggle();
}

function renderCapsBadge() {
  const c = state.caps.capabilities;
  const items = [];
  if (c.ffmpeg) items.push(['FFmpeg ✓', 'bg-emerald-500/15 text-emerald-400 border-emerald-500/25']);
  else          items.push(['FFmpeg ✗', 'bg-red-500/15 text-red-400 border-red-500/25']);
  if (c.libvmaf) items.push(['libvmaf ✓', 'bg-emerald-500/15 text-emerald-400 border-emerald-500/25']);
  if (c.nvenc.av1 || c.nvenc.hevc || c.nvenc.h264) {
    const codecs = ['h264','hevc','av1'].filter(k => c.nvenc[k]).join('/');
    items.push([`NVENC: ${codecs}`, 'bg-emerald-500/15 text-emerald-400 border-emerald-500/25']);
  }
  if (c.av1an) items.push(['Av1an ✓', 'bg-emerald-500/15 text-emerald-400 border-emerald-500/25']);
  else         items.push(['Av1an ✗', 'bg-amber-500/15 text-amber-400 border-amber-500/25']);

  $('#caps-badge').innerHTML = items
    .map(([t, cls]) => `<span class="px-2 py-1 rounded-md border ${cls}">${t}</span>`)
    .join('');
}

function renderBackendToggle() {
  const labels = {
    'ab-av1':  { title: 'ab-av1', sub: 'Sample-based CRF search · NVENC + software · sequential' },
    'av1an':   { title: 'Av1an',  sub: 'Scene-detected · parallel chunk encoding · software only' },
  };
  $('#backend-toggle').innerHTML = state.backends.map(b => {
    const meta = labels[b.name] || { title: b.name, sub: '' };
    const disabled = !b.available;
    const active = state.selectedBackend === b.name && !disabled;
    return `
      <div class="toggle-card ${active ? 'active' : ''} ${disabled ? 'disabled' : ''}" data-backend="${b.name}">
        <div class="flex items-center justify-between">
          <div class="toggle-card-title">${meta.title}</div>
          ${disabled ? '<span class="chip chip-warn">unavailable</span>' : ''}
        </div>
        <div class="toggle-card-sub">${meta.sub}${disabled ? '<br><span class="text-amber-400">' + (b.reason || '') + '</span>' : ''}</div>
      </div>`;
  }).join('');
  $$('#backend-toggle .toggle-card').forEach(el => {
    el.addEventListener('click', () => {
      if (el.classList.contains('disabled')) return;
      state.selectedBackend = el.dataset.backend;
      // If the current encoder isn't supported by the new backend, clear it
      const cur = state.encoders.find(e => e.id === state.selectedEncoder);
      if (cur && !cur.backends.includes(state.selectedBackend)) {
        state.selectedEncoder = null;
      }
      renderBackendToggle();
      renderEncoderGrid();
      updateEncodeBtn();
      $('#workers-row').classList.toggle('hidden', state.selectedBackend !== 'av1an');
    });
  });
}

function renderEncoderGrid() {
  const recommendedId = state.upload?.info?.recommendation?.encoder_id;
  const allowedForBackend = (e) => e.backends.includes(state.selectedBackend);
  const html = state.encoders.map(e => {
    const allowed = allowedForBackend(e);
    const available = e.available;
    const disabled = !available || !allowed;
    const isRec = e.id === recommendedId && !disabled;
    const isActive = state.selectedEncoder === e.id && !disabled;
    const reason = !available ? e.unavailable_reason : (!allowed ? `Not supported by ${state.selectedBackend}` : '');
    const chips = [
      e.type === 'hw' ? '<span class="chip chip-hw">HW</span>' : '<span class="chip chip-sw">SW</span>',
      isRec ? '<span class="chip chip-rec">recommended</span>' : '',
      disabled ? `<span class="chip chip-warn">unavailable</span>` : '',
    ].filter(Boolean).join('');
    return `
      <div class="toggle-card ${isActive ? 'active' : ''} ${disabled ? 'disabled' : ''}" data-encoder="${e.id}">
        <div class="flex items-center justify-between gap-2">
          <div class="toggle-card-title">${e.label}</div>
          <div class="flex gap-1">${chips}</div>
        </div>
        <div class="toggle-card-sub">${e.description || ''}${reason ? `<br><span class="text-amber-400">${reason}</span>` : ''}</div>
      </div>`;
  }).join('');
  $('#encoder-grid').innerHTML = html;

  $$('#encoder-grid .toggle-card').forEach(el => {
    el.addEventListener('click', () => {
      if (el.classList.contains('disabled')) return;
      state.selectedEncoder = el.dataset.encoder;
      renderEncoderGrid();
      updateEncodeBtn();
    });
  });
}

function renderVmafToggle() {
  const targets = state.caps.vmaf_targets || [
    { value: 85, label: 'VMAF 85', blurb: 'Good' },
    { value: 90, label: 'VMAF 90', blurb: 'Very good' },
    { value: 95, label: 'VMAF 95', blurb: 'Excellent' },
  ];
  $('#vmaf-toggle').innerHTML = targets.map(t => `
    <div class="toggle-card ${state.selectedVmaf === t.value ? 'active' : ''}" data-vmaf="${t.value}">
      <div class="toggle-card-title">${t.label}</div>
      <div class="toggle-card-sub">${t.blurb}</div>
    </div>`).join('');
  $$('#vmaf-toggle .toggle-card').forEach(el => {
    el.addEventListener('click', () => {
      state.selectedVmaf = parseInt(el.dataset.vmaf, 10);
      renderVmafToggle();
    });
  });
}

function updateEncodeBtn() {
  $('#encode-btn').disabled = !(state.upload && state.selectedEncoder);
}

// ── Upload ────────────────────────────────────────────────────────────────
function bindUpload() {
  const dz = $('#dropzone');
  const input = $('#file-input');

  dz.addEventListener('click', () => input.click());
  $('#browse-btn').addEventListener('click', (e) => { e.stopPropagation(); input.click(); });
  ['dragenter','dragover'].forEach(ev => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('drag'); }));
  ['dragleave','drop'].forEach(ev => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('drag'); }));
  dz.addEventListener('drop', (e) => {
    if (e.dataTransfer.files?.length) doUpload(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', () => {
    if (input.files?.length) doUpload(input.files[0]);
  });
}

function doUpload(file) {
  const fd = new FormData();
  fd.append('file', file);
  $('#upload-progress').classList.remove('hidden');
  $('#upload-progress-label').textContent = `Uploading "${file.name}" (${fmtBytes(file.size)})…`;
  $('#upload-progress-fill').style.width = '0%';

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload', true);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = (e.loaded / e.total) * 100;
      $('#upload-progress-fill').style.width = `${pct.toFixed(1)}%`;
    }
  };
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      const j = JSON.parse(xhr.responseText);
      state.upload = { upload_id: j.upload_id, info: j.info };
      $('#upload-progress-label').textContent = `Uploaded: ${file.name}`;
      renderAnalysis(j.info);
      $('#analysis-section').classList.remove('hidden');
      $('#controls-section').classList.remove('hidden');
      // Pre-select recommended encoder
      const rec = j.info?.recommendation?.encoder_id;
      if (rec) {
        const enc = state.encoders.find(e => e.id === rec);
        if (enc && enc.available && enc.backends.includes(state.selectedBackend)) {
          state.selectedEncoder = rec;
          renderEncoderGrid();
        }
      }
      updateEncodeBtn();
    } else {
      let msg = 'Upload failed';
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
      $('#upload-progress-label').innerHTML = `<span class="text-red-400">${msg}</span>`;
    }
  };
  xhr.onerror = () => {
    $('#upload-progress-label').innerHTML = `<span class="text-red-400">Upload failed (network error)</span>`;
  };
  xhr.send(fd);
}

function renderAnalysis(info) {
  $('#m-codec').textContent = info.codec_label || '—';
  $('#m-container').textContent = info.container || '';
  $('#m-res').textContent = `${info.width}×${info.height} @ ${info.fps} fps`;
  $('#m-duration').textContent = fmtDuration(info.duration_s);
  $('#m-bitrate').textContent = `${(info.bitrate_kbps/1000).toFixed(2)} Mbps`;
  $('#m-bpp').textContent = `${info.bpp.toFixed(3)} bits/pixel`;

  // Compressibility gauge — map class to visual progress
  const compMap = { highly_compressible: 95, compressible: 70, moderate: 40, already_efficient: 12 };
  $('#m-comp-fill').style.width = `${compMap[info.compressibility] ?? 50}%`;
  $('#m-comp-label').textContent = info.compressibility_label;
  $('#m-verdict').textContent = info.verdict;

  const rec = info.recommendation || {};
  const recEnc = state.encoders.find(e => e.id === rec.encoder_id);
  $('#m-rec-title').textContent = recEnc ? `${recEnc.label}` : (rec.codec ? rec.codec.toUpperCase() : '—');
  $('#m-rec-reason').textContent = rec.reasoning || '';

  $('#analysis-sub').textContent = `${fmtBytes(info.size_bytes)} · ${info.codec_label} · ${fmtDuration(info.duration_s)}`;
}

// ── Encode ────────────────────────────────────────────────────────────────
$('#encode-btn').addEventListener('click', startEncode);

$('#workers-slider').addEventListener('input', (e) => {
  state.workers = parseInt(e.target.value, 10);
  $('#workers-val').textContent = `${state.workers} workers`;
});

async function startEncode() {
  if (!state.upload || !state.selectedEncoder) return;
  $('#encode-btn').disabled = true;

  // Reset progress UI
  resetProgressUi();
  $('#progress-section').classList.remove('hidden');
  $('#result-section').classList.add('hidden');
  $('#progress-section').scrollIntoView({ behavior: 'smooth', block: 'start' });

  const body = {
    upload_id: state.upload.upload_id,
    backend: state.selectedBackend,
    encoder_id: state.selectedEncoder,
    target_vmaf: state.selectedVmaf,
    workers: state.workers,
  };
  const r = await fetch('/api/encode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    appendLog(`✖ ${err.detail || r.statusText}`);
    $('#encode-btn').disabled = false;
    return;
  }
  const { job_id } = await r.json();
  state.jobId = job_id;
  openProgressStream(job_id);
}

function resetProgressUi() {
  $('#progress-fill').style.width = '0%';
  $('#progress-pct').textContent = '0%';
  $('#progress-msg').textContent = 'Initializing…';
  $('#progress-log').textContent = '';
  $$('.stage').forEach(s => s.classList.remove('active','done'));
  $('.stage[data-stage="searching"]').classList.add('active');
}

function openProgressStream(jobId) {
  if (state.evtSource) state.evtSource.close();
  const es = new EventSource(`/api/progress/${jobId}`);
  state.evtSource = es;
  let errCount = 0;

  es.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }
    handleEvent(ev);
  };
  es.onerror = () => {
    // EventSource will auto-reconnect indefinitely after server closes the
    // stream. If we already saw a terminal event, just close. Otherwise allow
    // a small number of retries for genuine network blips, then give up.
    if (state.evtSource !== es) return;
    if (es.readyState === EventSource.CLOSED) {
      appendLog('… stream closed');
      return;
    }
    errCount += 1;
    if (errCount >= 4) {
      appendLog('… giving up on progress stream (server unreachable)');
      es.close();
      $('#encode-btn').disabled = !(state.upload && state.selectedEncoder);
    } else {
      appendLog(`… connection error (retry ${errCount}/3)`);
    }
  };
}

function handleEvent(ev) {
  if (ev.type === 'log') {
    appendLog(ev.message);
  }
  if (ev.type === 'progress' || ev.type === 'stage') {
    if (ev.percent != null) {
      $('#progress-fill').style.width = `${Math.max(0, Math.min(100, ev.percent))}%`;
      $('#progress-pct').textContent = `${Math.round(ev.percent)}%`;
    }
    if (ev.message) $('#progress-msg').textContent = ev.message;
    if (ev.stage) updateStageStrip(ev.stage);
  }
  if (ev.type === 'done') {
    $('#progress-fill').style.width = '100%';
    $('#progress-pct').textContent = '100%';
    updateStageStrip('done');
    showResult(ev.data);
    $('#encode-btn').disabled = !(state.upload && state.selectedEncoder);
    if (state.evtSource) { state.evtSource.close(); state.evtSource = null; }
  }
  if (ev.type === 'error') {
    const msg = ev.message || 'Unknown error';
    $('#progress-msg').innerHTML = `<span class="text-red-400">${msg}</span>`;
    appendLog(`✖ ${msg}`);
    $('#encode-btn').disabled = !(state.upload && state.selectedEncoder);
    if (state.evtSource) { state.evtSource.close(); state.evtSource = null; }
  }
}

function updateStageStrip(stage) {
  const order = ['searching','encoding','measuring','done'];
  const idx = order.indexOf(stage);
  $$('.stage').forEach(s => {
    const pos = order.indexOf(s.dataset.stage);
    s.classList.remove('active','done');
    if (pos < idx) s.classList.add('done');
    else if (pos === idx) s.classList.add(stage === 'done' ? 'done' : 'active');
  });
}

function appendLog(msg) {
  const el = $('#progress-log');
  el.textContent += msg + '\n';
  el.scrollTop = el.scrollHeight;
}

// ── Result ────────────────────────────────────────────────────────────────
function showResult(r) {
  $('#result-section').classList.remove('hidden');
  $('#result-section').scrollIntoView({ behavior: 'smooth', block: 'start' });

  const inSize  = r.input.size;
  const outSize = r.output.size;
  const savings = r.savings_pct;

  $('#r-savings').textContent = `${savings.toFixed(1)}%`;
  $('#r-savings').className = `big-metric-value ${savings >= 30 ? 'text-emerald-400' : savings >= 10 ? 'text-cyan-400' : savings >= 0 ? 'text-amber-400' : 'text-red-400'}`;
  $('#r-sizes').textContent = `${fmtBytes(inSize)} → ${fmtBytes(outSize)}`;

  const v = r.output.achieved_vmaf;
  $('#r-vmaf').textContent = v != null ? v.toFixed(2) : '—';
  $('#r-vmaf').className = `big-metric-value ${vmafColor(v)}`;
  $('#r-vmaf-target').textContent = `target ${r.output.target_vmaf}` + (v != null ? ` · Δ${(v - r.output.target_vmaf).toFixed(2)}` : '');

  $('#r-time').textContent = `${r.elapsed_s}s`;
  $('#r-encoder').textContent = `${r.output.encoder_label} · ${r.backend}`;

  const orig = $('#v-original');
  orig.src = r.input.url;
  orig.load();
  $('#r-orig-meta').textContent = `${r.input.info.codec_label} · ${r.input.info.width}×${r.input.info.height} · ${fmtBytes(inSize)}`;

  const enc = $('#v-encoded');
  enc.src = r.output.url;
  enc.load();
  $('#r-enc-meta').textContent = `${r.output.encoder_label} · ${fmtBytes(outSize)}`;
  $('#r-download').href = r.output.url;
  $('#r-download').download = r.output.filename;

  $('#result-sub').textContent = `Saved ${fmtBytes(inSize - outSize)} · ${r.elapsed_s}s elapsed`;
}

// ── Boot ──────────────────────────────────────────────────────────────────
bindUpload();
loadCaps().catch(e => {
  alert('Failed to load capabilities: ' + e.message);
});
