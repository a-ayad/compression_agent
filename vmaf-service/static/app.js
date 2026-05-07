/* CUDA VMAF — frontend logic (vanilla JS, no bundler) */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  caps: null,
  files: [],            // [{upload_id, filename, size, uploaded_at}]
  ref: null,            // upload_id
  picks: [],            // upload_ids of distorted (max 2, in selection order)
};

function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtAge(ts) {
  const s = (Date.now() / 1000) - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function vmafColor(score) {
  if (score == null) return 'text-slate-300';
  if (score >= 95) return 'text-emerald-400';
  if (score >= 90) return 'text-cyan-400';
  if (score >= 85) return 'text-amber-400';
  return 'text-orange-400';
}

// ── Capabilities ──────────────────────────────────────────────────────────
async function loadCaps() {
  const r = await fetch('/health');
  state.caps = await r.json();
  const c = state.caps;
  const tag = (label, on) => `<span class="pill ${on ? 'pill-on' : 'pill-off'}">${label}</span>`;
  $('#caps-badge').innerHTML = tag(c.cuda_filter ? 'libvmaf_cuda' : 'libvmaf (cpu only)', c.cuda_filter)
                              + ' ' + tag(c.gpu_present ? 'GPU' : 'no GPU', c.gpu_present);
  // If no CUDA path is available, force CPU and lock the toggle.
  if (!c.cuda_filter || !c.gpu_present) {
    $('#use-cuda').checked = false;
    $('#use-cuda').disabled = true;
  }
}

// ── Files list ────────────────────────────────────────────────────────────
async function loadFiles() {
  const r = await fetch('/api/files');
  const d = await r.json();
  state.files = d.files || [];
  renderFiles();
  // If a previously-picked file got deleted on the server, drop it.
  if (state.ref && !state.files.find(f => f.upload_id === state.ref)) state.ref = null;
  state.picks = state.picks.filter(p => state.files.find(f => f.upload_id === p));
  renderPicks();
  syncRunButton();
}

function renderFiles() {
  const list = $('#files-list');
  if (!state.files.length) {
    $('#files-empty').classList.remove('hidden');
    list.innerHTML = '';
    return;
  }
  $('#files-empty').classList.add('hidden');
  list.innerHTML = state.files.map(f => fileRow(f)).join('');
  for (const row of $$('.file-row')) {
    row.addEventListener('click', (ev) => {
      if (ev.target.closest('[data-action]')) return;  // don't pick when clicking a button
      onFileClick(row.dataset.id);
    });
    for (const btn of row.querySelectorAll('[data-action]')) {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const action = btn.dataset.action;
        const id = row.dataset.id;
        if (action === 'ref')      setRef(id);
        else if (action === 'add') togglePick(id);
        else if (action === 'del') deleteFile(id);
      });
    }
  }
}

function fileRow(f) {
  const isRef = state.ref === f.upload_id;
  const pickIdx = state.picks.indexOf(f.upload_id);   // -1, 0, or 1
  const tag = isRef
    ? '<span class="pill pill-on">Reference</span>'
    : (pickIdx === 0 ? '<span class="pill pill-on">Distorted A</span>'
      : pickIdx === 1 ? '<span class="pill pill-on">Distorted B</span>' : '');
  const selected = isRef || pickIdx >= 0 ? 'selected' : '';
  const buttons = `
    <button data-action="ref" class="btn-ghost text-xs">Set as ref</button>
    <button data-action="add" class="btn-ghost text-xs">${pickIdx >= 0 ? 'Unpick' : 'Pick distorted'}</button>
    <button data-action="del" class="btn-ghost text-xs text-rose-400">Delete</button>
  `;
  return `
    <div class="file-row ${selected}" data-id="${f.upload_id}">
      <div class="file-name" title="${f.filename}">${f.filename}</div>
      <div class="file-meta">${fmtBytes(f.size)} · ${fmtAge(f.uploaded_at)}</div>
      <div>${tag}</div>
      <div class="flex gap-3">${buttons}</div>
    </div>`;
}

function setRef(id) {
  if (state.ref === id) {
    state.ref = null;
  } else {
    state.ref = id;
    state.picks = state.picks.filter(p => p !== id);  // ref can't also be distorted
  }
  renderFiles(); renderPicks(); syncRunButton();
}

function togglePick(id) {
  if (state.ref === id) {
    state.ref = null;   // taking it as distorted releases the ref slot
  }
  const i = state.picks.indexOf(id);
  if (i >= 0) state.picks.splice(i, 1);
  else if (state.picks.length < 2) state.picks.push(id);
  else { /* full — replace last */ state.picks[1] = id; }
  renderFiles(); renderPicks(); syncRunButton();
}

function onFileClick(id) {
  // Single click: if no ref, set ref. Else add to distorted picks.
  if (!state.ref) setRef(id);
  else if (id !== state.ref) togglePick(id);
}

async function deleteFile(id) {
  if (!confirm('Delete this upload? It will be removed from disk.')) return;
  await fetch(`/api/upload/${id}`, { method: 'DELETE' });
  await loadFiles();
}

function renderPicks() {
  const ref = state.files.find(f => f.upload_id === state.ref);
  const a   = state.files.find(f => f.upload_id === state.picks[0]);
  const b   = state.files.find(f => f.upload_id === state.picks[1]);
  const any = ref || a || b;
  $('#picks-summary').classList.toggle('hidden', !any);
  $('#pick-ref').textContent = ref ? ref.filename : '— pick one —';
  $('#pick-a').textContent   = a   ? a.filename   : '— pick one —';
  $('#pick-b').textContent   = b   ? b.filename   : '(optional)';
}

function syncRunButton() {
  $('#run').disabled = !(state.ref && state.picks.length >= 1);
  // Deep inspect runs against 1 or 2 distorted (matches Run VMAF capability).
  $('#inspect').disabled = !(state.ref && state.picks.length >= 1);
}

// ── Upload ────────────────────────────────────────────────────────────────
function bindUpload() {
  const dz = $('#dropzone');
  const input = $('#file-input');
  dz.addEventListener('click', (e) => { if (!e.target.closest('button')) input.click(); });
  $('#browse-btn').addEventListener('click', (e) => { e.stopPropagation(); input.click(); });
  input.addEventListener('change', () => uploadMany(Array.from(input.files)));

  ['dragover', 'dragenter'].forEach(t =>
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(t =>
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove('drag'); }));
  dz.addEventListener('drop', (e) => {
    if (e.dataTransfer?.files?.length) uploadMany(Array.from(e.dataTransfer.files));
  });
}

async function uploadMany(files) {
  if (!files.length) return;
  const bar = $('#upload-progress');
  const fill = $('#upload-progress-fill');
  const lbl = $('#upload-progress-label');
  const pct = $('#upload-progress-pct');
  bar.classList.remove('hidden');
  let done = 0;
  for (const f of files) {
    lbl.textContent = `Uploading ${f.name} (${done + 1}/${files.length})`;
    pct.textContent = '0%';
    fill.style.width = '0%';
    try {
      await uploadOne(f, (p) => {
        const overall = (done + p) / files.length;
        fill.style.width = `${(overall * 100).toFixed(0)}%`;
        pct.textContent = `${Math.round(p * 100)}%`;
      });
    } catch (e) {
      lbl.innerHTML = `<span class="text-rose-400">Upload failed: ${e.message}</span>`;
      break;
    }
    done++;
  }
  fill.style.width = '100%';
  pct.textContent = '100%';
  setTimeout(() => bar.classList.add('hidden'), 1500);
  await loadFiles();
}

function uploadOne(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.upload.addEventListener('progress', (ev) => {
      if (ev.lengthComputable) onProgress(ev.loaded / ev.total);
    });
    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
      else reject(new Error(`HTTP ${xhr.status}: ${xhr.responseText}`));
    });
    xhr.addEventListener('error', () => reject(new Error('network error')));
    xhr.open('POST', '/api/upload');
    const fd = new FormData();
    fd.append('file', file);
    xhr.send(fd);
  });
}

// ── Compare ───────────────────────────────────────────────────────────────
async function runCompare() {
  const body = {
    reference: { upload_id: state.ref },
    distorted: state.picks.map(id => ({ upload_id: id })),
    use_cuda: $('#use-cuda').checked,
    upscale_1080p: $('#upscale-1080p').checked,
    model: $('#model').value,
  };
  const status = $('#status');
  const resultsSec = $('#results-section');
  $('#run').disabled = true;
  status.textContent = 'Running…';
  resultsSec.classList.add('hidden');

  const t0 = performance.now();
  try {
    const r = await fetch('/api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    status.innerHTML = '';
    $('#results-sub').innerHTML =
      `Done in <strong class="text-slate-200">${elapsed}s</strong> · mode `
      + `<strong class="text-slate-200">${data.used_cuda ? 'CUDA' : 'CPU'}</strong> · model `
      + `<span class="text-slate-300">${data.model}</span> · ref <span class="text-slate-300">${data.reference_label}</span>`;
    $('#results').innerHTML = (data.scores || []).map((s, i) => {
      const delta = s.frame_delta || 0;
      const deltaSign = delta > 0 ? `+${delta}` : `${delta}`;
      const warn = delta !== 0 ? `
        <div class="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200">
          <div class="font-semibold">⚠ Frame-count mismatch (Δ=${deltaSign})</div>
          <div class="text-amber-300/80 mt-0.5">
            ref=${s.ref_frames} · dist=${s.dist_frames} · libvmaf compared ${s.frames} frames.
            Streams were realigned to t=0 before comparison; if the extra frame is at the start, per-frame VMAF may still be off by one.
          </div>
        </div>` : '';
      const upscaleChip = s.upscaled
        ? `<span class="pill pill-on" title="Both inputs were scaled to 1920×1080 before VMAF.">1080p↑</span>`
        : '';
      return `
      <div class="big-metric">
        <div class="flex items-baseline justify-between mb-2">
          <div>
            <div class="big-metric-label flex items-center gap-2">Distorted ${String.fromCharCode(65 + i)} ${upscaleChip}</div>
            <div class="text-sm text-slate-300 font-medium truncate" title="${s.label}">${s.label}</div>
          </div>
          <div class="big-metric-value ${vmafColor(s.mean)}">${s.mean.toFixed(2)}</div>
        </div>
        <div class="grid grid-cols-4 gap-2 text-[11px] text-slate-400 mt-3">
          <div>min<div class="text-slate-200 font-semibold">${s.min.toFixed(2)}</div></div>
          <div>max<div class="text-slate-200 font-semibold">${s.max.toFixed(2)}</div></div>
          <div>hmean<div class="text-slate-200 font-semibold">${s.harmonic_mean.toFixed(2)}</div></div>
          <div>frames<div class="text-slate-200 font-semibold">${s.frames}</div></div>
        </div>
        <div class="text-[11px] text-slate-500 mt-2">
          ${s.seconds}s wall · ${s.frames && s.seconds ? Math.round(s.frames / s.seconds) : '—'} fps · target ${s.target_w}×${s.target_h}
        </div>
        ${warn}
      </div>`;
    }).join('');
    resultsSec.classList.remove('hidden');
  } catch (e) {
    status.innerHTML = `<span class="text-rose-400">${e.message}</span>`;
  } finally {
    syncRunButton();
  }
}

// ── Deep inspect ─────────────────────────────────────────────────────────
async function runInspect() {
  if (!state.ref || state.picks.length < 1) return;
  const body = {
    reference: { upload_id: state.ref },
    distorted: state.picks.map(id => ({ upload_id: id })),
    use_cuda: $('#use-cuda').checked,
    upscale_1080p: $('#upscale-1080p').checked,
  };
  const status = $('#status');
  const sec = $('#inspect-section');
  $('#inspect').disabled = true;
  $('#run').disabled = true;
  status.textContent = `Inspecting ${state.picks.length} encode${state.picks.length > 1 ? 's' : ''} (multi-metric pass + frame extract)…`;
  sec.classList.add('hidden');

  const t0 = performance.now();
  try {
    const r = await fetch('/api/inspect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);

    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    status.textContent = '';
    $('#inspect-sub').innerHTML =
      `Done in <strong class="text-slate-200">${elapsed}s</strong> · mode `
      + `<strong class="text-slate-200">${d.used_cuda ? 'CUDA' : 'CPU'}</strong> · `
      + `ref <span class="text-slate-300">${d.reference_label}</span> vs `
      + `<strong class="text-slate-200">${d.scores.length}</strong> distorted`;

    // Reference panel
    $('#inspect-ref-img').src = d.ref_preview_b64;
    $('#inspect-ref-meta').innerHTML =
      `<div class="text-slate-200 font-medium">${d.reference_label}</div>` +
      `<div>${d.ref_codec} · ${d.ref_w}×${d.ref_h} · ${fmtKbps(d.ref_bitrate_kbps)} · ${d.ref_frames} frames</div>` +
      `<div>target ${d.target_w}×${d.target_h}${d.upscaled ? ' · 1080p↑' : ''} · lap-var ${d.lap_var_ref.toFixed(1)}</div>`;

    // Per-distorted score panels
    $('#inspect-scores').className = `grid ${d.scores.length === 2 ? 'md:grid-cols-2' : 'md:grid-cols-1'} gap-4 mb-5`;
    $('#inspect-scores').innerHTML = d.scores.map((s, i) => scorePanel(s, i)).join('');

    // Pair findings (only when 2 distorted)
    const pairBox = $('#inspect-pair');
    if (d.pair_findings && d.pair_findings.length) {
      pairBox.classList.remove('hidden');
      $('#inspect-pair-findings').innerHTML = d.pair_findings.map(findingCard).join('');
    } else {
      pairBox.classList.add('hidden');
    }
    sec.classList.remove('hidden');
  } catch (e) {
    status.innerHTML = `<span class="text-rose-400">${e.message}</span>`;
  } finally {
    syncRunButton();
  }
}

function scorePanel(s, i) {
  const letter = String.fromCharCode(65 + i);
  const tiles = [
    metricTile('VMAF (reg)', s.vmaf.toFixed(2), `min ${s.vmaf_min.toFixed(1)}`, vmafColor(s.vmaf)),
    metricTile('VMAF (neg)', s.vmaf_neg.toFixed(2), `min ${s.vmaf_neg_min.toFixed(1)}`, vmafColor(s.vmaf_neg)),
    metricTile('Δ reg−neg', signedFixed(s.enhancement_gap, 2), 'enhancement', gapColor(s.enhancement_gap)),
    metricTile('PSNR (Y)', s.psnr_y_db.toFixed(2) + ' dB', 'pixel fidelity', psnrColor(s.psnr_y_db)),
    metricTile('SSIM (Y)', s.ssim_y.toFixed(4), 'structure', 'text-slate-200'),
    metricTile('Detail', (s.detail_retention * 100).toFixed(0) + '%', 'lap-var ratio', detailColor(s.detail_retention)),
    metricTile('Bitrate', fmtKbps(s.dist_bitrate_kbps),
      s.bitrate_ratio ? `${(s.bitrate_ratio * 100).toFixed(1)}% of ref` : '',
      'text-slate-200'),
    metricTile('Frames', `${s.dist_frames}`,
      s.frame_delta ? `Δ=${signedFixed(s.frame_delta, 0)}` : 'aligned',
      s.frame_delta ? 'text-amber-300' : 'text-slate-200'),
  ].join('');
  return `
    <div class="rounded-xl bg-ink-900/40 border border-white/5 p-4">
      <div class="flex items-start gap-3 mb-3">
        <img class="w-44 rounded-lg border border-white/10 bg-black" src="${s.dist_preview_b64}"/>
        <div class="text-[11px] text-slate-400 leading-relaxed flex-1 min-w-0">
          <div class="text-xs uppercase tracking-wider text-slate-400">Distorted ${letter}</div>
          <div class="text-slate-200 font-medium truncate" title="${s.label}">${s.label}</div>
          <div>${s.dist_codec} · ${s.dist_w}×${s.dist_h} · ${fmtKbps(s.dist_bitrate_kbps)}</div>
          <div>${s.seconds}s wall · ${s.dist_frames && s.seconds ? Math.round(s.dist_frames / s.seconds) : '—'} fps</div>
        </div>
      </div>
      <div class="grid grid-cols-2 gap-2 mb-3 text-[12px]">${tiles}</div>
      <div class="text-[10px] uppercase tracking-wider text-slate-400 mb-1">Findings</div>
      <div class="space-y-1.5">${(s.findings || []).map(findingCard).join('')}</div>
    </div>`;
}

function fmtKbps(kbps) {
  if (!kbps) return '—';
  if (kbps >= 1000) return (kbps / 1000).toFixed(2) + ' Mbps';
  return Math.round(kbps) + ' kbps';
}
function signedFixed(n, d) { return (n >= 0 ? '+' : '') + n.toFixed(d); }
function gapColor(g) {
  if (g >= 4) return 'text-rose-400';
  if (g >= 2) return 'text-amber-400';
  if (g <= -1) return 'text-amber-300';
  return 'text-emerald-400';
}
function psnrColor(p) {
  if (p >= 42) return 'text-emerald-400';
  if (p >= 38) return 'text-cyan-400';
  if (p >= 33) return 'text-amber-400';
  return 'text-rose-400';
}
function detailColor(r) {
  if (r >= 0.95) return 'text-emerald-400';
  if (r >= 0.85) return 'text-cyan-400';
  if (r >= 0.70) return 'text-amber-400';
  return 'text-rose-400';
}
function metricTile(label, value, sub, valueClass) {
  return `
    <div class="rounded-lg bg-ink-900/60 border border-white/5 p-3">
      <div class="text-[10px] uppercase tracking-wider text-slate-400">${label}</div>
      <div class="text-lg font-semibold tabular-nums ${valueClass}">${value}</div>
      <div class="text-[10px] text-slate-500">${sub || ''}</div>
    </div>`;
}
function findingCard(f) {
  const palette = {
    good:    { dot: 'bg-emerald-400', border: 'border-emerald-500/40', bg: 'bg-emerald-500/10', text: 'text-emerald-200' },
    info:    { dot: 'bg-cyan-400',    border: 'border-cyan-500/40',    bg: 'bg-cyan-500/10',    text: 'text-cyan-200'    },
    warn:    { dot: 'bg-amber-400',   border: 'border-amber-500/40',   bg: 'bg-amber-500/10',   text: 'text-amber-200'   },
    alert:   { dot: 'bg-rose-400',    border: 'border-rose-500/40',    bg: 'bg-rose-500/10',    text: 'text-rose-200'    },
    summary: { dot: 'bg-fuchsia-400', border: 'border-fuchsia-500/40', bg: 'bg-fuchsia-500/10', text: 'text-fuchsia-100' },
  }[f.severity] || { dot: 'bg-slate-400', border: 'border-white/10', bg: 'bg-ink-800', text: 'text-slate-200' };
  return `
    <div class="rounded-lg border ${palette.border} ${palette.bg} px-3 py-2">
      <div class="flex items-start gap-2">
        <span class="mt-1.5 inline-block w-2 h-2 rounded-full ${palette.dot}"></span>
        <div>
          <div class="font-semibold ${palette.text}">${f.title}</div>
          <div class="text-[12px] text-slate-300/90 mt-0.5 leading-relaxed">${f.detail}</div>
        </div>
      </div>
    </div>`;
}

// ── Boot ──────────────────────────────────────────────────────────────────
bindUpload();
$('#clear-picks').addEventListener('click', () => {
  state.ref = null; state.picks = [];
  renderFiles(); renderPicks(); syncRunButton();
});
$('#run').addEventListener('click', runCompare);
$('#inspect').addEventListener('click', runInspect);

loadCaps().catch(() => {});
loadFiles().catch(() => {});
