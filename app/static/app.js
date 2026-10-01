/* 古籍修复湿度阶梯试验编排台 —— 原生 JS，无任何框架/依赖 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (url, opts = {}) => {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
};

const state = {
  samples: [],
  occs: [],
  capacity: 3,
  maxShiftDelta: null,   // 独立换档上限（%RH）；null = 不限制（兼容旧数据）
  result: null,        // 最近一次 schedule/validate 返回
  boardTimes: {},      // 当前泳道使用的入箱分钟（拖拽会改变）
  editingId: null,
  pxPerMin: 2,
  dragging: null,
  review: null,        // 最近一次复核结果
  reviewPx: 2,
};

const fmtMin = (m) => {
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), x = m % 60;
  return (d ? d + 'd' : '') + (h ? h + 'h' : '') + ((x || (!d && !h)) ? x + 'm' : '');
};

// --------------------------------------------------------------------------- //
// 数据加载
// --------------------------------------------------------------------------- //
async function loadAll() {
  const [samples, occs, settings] = await Promise.all([
    api('/api/samples'), api('/api/occupancies'), api('/api/settings'),
  ]);
  state.samples = samples;
  state.occs = occs;
  state.capacity = settings.capacity;
  state.maxShiftDelta = settings.max_shift_delta ?? null;
  $('#capacity').value = state.capacity;
  $('#maxShiftDelta').value = state.maxShiftDelta ?? '';
  renderSamples();
  renderOccs();
  const latest = await api('/api/schedule/latest').catch(() => ({}));
  if (latest && latest.segments) {
    state.result = latest;
    state.boardTimes = { ...(latest.board_times || {}) };
    renderTimeline();
    setStatus('已载入上次保存的排程，可直接拖拽调整。', 'ok');
  }
}

// --------------------------------------------------------------------------- //
// 样本卡片 / 弹层
// --------------------------------------------------------------------------- //
function renderSamples() {
  const box = $('#sampleList');
  box.innerHTML = '';
  if (!state.samples.length) {
    box.innerHTML = '<div style="color:#998f7d;font-size:12.5px;margin-top:8px">尚无样本，点击右上角新增。</div>';
    return;
  }
  for (const s of state.samples) {
    const card = document.createElement('div');
    card.className = 'sample-card';
    const steps = s.steps.map((st, i) =>
      `${i + 1}. ${num(st.target)}% × ${st.dwell}m @ ${num(st.rate)}%/m`).join('　');
    const excl = (s.excludes || []).map(id => state.samples.find(x => x.id === id))
      .filter(Boolean).map(x => x.code).join('、');
    card.innerHTML = `
      <div class="sc-head"><span>${esc(s.code)}</span>
        <span style="font-size:11px;color:#998f7d">#${s.id}</span></div>
      <div class="sc-meta">耐受 ${num(s.rh_min)}–${num(s.rh_max)}%RH ｜ 平衡 ${
        num(s.eq_rh)}% ｜ 冷却 ${s.eq_duration}m</div>
      <div class="sc-steps">${esc(steps)}</div>
      ${excl ? `<div class="sc-ex">⛓ 不可相邻：${esc(excl)}</div>` : ''}`;
    card.onclick = () => openSampleModal(s.id);
    box.appendChild(card);
  }
}

function openSampleModal(id = null) {
  state.editingId = id;
  const s = id ? state.samples.find(x => x.id === id) : null;
  $('#modalTitle').textContent = s ? `编辑样本 ${s.code}` : '新样本';
  $('#f_code').value = s?.code || '';
  $('#f_min').value = s?.rh_min ?? '';
  $('#f_max').value = s?.rh_max ?? '';
  $('#f_eq').value = s?.eq_rh ?? '';
  $('#f_eqdur').value = s?.eq_duration ?? 30;
  const sel = $('#f_excl');
  sel.innerHTML = '';
  for (const o of state.samples.filter(x => x.id !== id)) {
    const op = document.createElement('option');
    op.value = o.id; op.textContent = `${o.code} (#${o.id})`;
    if (s?.excludes?.includes(o.id)) op.selected = true;
    sel.appendChild(op);
  }
  renderStepRows(s ? s.steps.map(x => ({ ...x })) :
    [{ target: '', dwell: 30, rate: 2 }]);
  $('#modalDelete').style.display = s ? '' : 'none';
  $('#modalError').textContent = '';
  $('#modal').classList.remove('hidden');
}

function renderStepRows(steps) {
  const body = $('#stepBody');
  body.innerHTML = '';
  steps.forEach((st, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><input class="s-target" type="number" step="0.1" value="${st.target ?? ''}"></td>
      <td><input class="s-dwell" type="number" step="1" min="0" value="${st.dwell ?? ''}"></td>
      <td><input class="s-rate" type="number" step="0.1" min="0.1" value="${st.rate ?? ''}"></td>
      <td class="del">✕</td>`;
    tr.querySelector('.del').onclick = () => {
      if ($$('#stepBody tr').length > 1) tr.remove(); renumber();
    };
    body.appendChild(tr);
  });
}
function renumber() {
  $$('#stepBody tr').forEach((tr, i) => tr.firstElementChild.textContent = i + 1);
}

async function saveSampleFromModal() {
  const steps = $$('#stepBody tr').map(tr => ({
    target: parseFloat(tr.querySelector('.s-target').value),
    dwell: parseInt(tr.querySelector('.s-dwell').value, 10),
    rate: parseFloat(tr.querySelector('.s-rate').value),
  }));
  const body = {
    id: state.editingId,
    code: $('#f_code').value.trim(),
    rh_min: parseFloat($('#f_min').value),
    rh_max: parseFloat($('#f_max').value),
    eq_rh: $('#f_eq').value === '' ? null : parseFloat($('#f_eq').value),
    eq_duration: parseInt($('#f_eqdur').value, 10),
    steps,
    excludes: $$('#f_excl option:checked').map(o => parseInt(o.value, 10)),
  };
  try {
    await api('/api/samples', { method: 'POST', body });
    $('#modal').classList.add('hidden');
    await loadAll();
  } catch (e) {
    $('#modalError').textContent = e.message;
  }
}

// --------------------------------------------------------------------------- //
// 占用
// --------------------------------------------------------------------------- //
function renderOccs() {
  const box = $('#occList');
  box.innerHTML = '';
  if (!state.occs.length) {
    box.innerHTML = '<div style="color:#998f7d;font-size:12.5px;margin-top:6px">无已有占用。</div>';
    return;
  }
  for (const o of state.occs) {
    const div = document.createElement('div');
    div.className = 'occ-item';
    div.innerHTML = `<span>${fmtMin(o.start)}–${fmtMin(o.end)} ｜ ${num(o.rh)}%RH ｜ ${
      o.slots} 位${o.note ? ' ｜ ' + esc(o.note) : ''}</span><span class="x">✕</span>`;
    div.querySelector('.x').onclick = async () => {
      await api(`/api/occupancies/${o.id}`, { method: 'DELETE' });
      await loadAll();
    };
    box.appendChild(div);
  }
}

function openOccModal() {
  ['o_start', 'o_end', 'o_rh', 'o_note'].forEach(id => $('#' + id).value = '');
  $('#o_slots').value = 1;
  $('#occError').textContent = '';
  $('#occModal').classList.remove('hidden');
}

async function saveOcc() {
  const body = {
    start: parseInt($('#o_start').value, 10),
    end: parseInt($('#o_end').value, 10),
    rh: parseFloat($('#o_rh').value),
    slots: parseInt($('#o_slots').value, 10),
    note: $('#o_note').value.trim(),
  };
  try {
    await api('/api/occupancies', { method: 'POST', body });
    $('#occModal').classList.add('hidden');
    await loadAll();
  } catch (e) {
    $('#occError').textContent = e.message;
  }
}

// --------------------------------------------------------------------------- //
// 排程
// --------------------------------------------------------------------------- //
async function runSchedule() {
  setStatus('正在求解可执行顺序…', 'warn');
  try {
    state.result = await api('/api/schedule', {
      method: 'POST',
      body: { capacity: state.capacity,
              max_shift_delta: state.maxShiftDelta },
    });
    state.boardTimes = { ...(state.result.board_times || {}) };
    renderTimeline();
    if (state.result.feasible) {
      setStatus(`✓ 排程可行：${state.result.lanes.length} 个样本任务，总时长 ${
        fmtMin(state.result.end_minute)}。可直接拖拽泳道调整入箱时刻。`, 'ok');
    } else {
      setStatus('✗ 约束无解，请查看下方冲突与最小调整建议。', 'bad');
    }
  } catch (e) {
    setStatus('求解失败：' + e.message, 'bad');
  }
}

async function revalidate() {
  try {
    state.result = await api('/api/validate', {
      method: 'POST',
      body: { capacity: state.capacity,
              max_shift_delta: state.maxShiftDelta,
              board_times: state.boardTimes },
    });
    renderTimeline();
    if (state.result.feasible) {
      setStatus('✓ 拖拽后的手工排程通过全部约束校验' +
        '（容量 / 耐受区间 / 速率 / 独立换档上限 / 不可相邻）。', 'ok');
    } else {
      const n = state.result.violations.length;
      const nShift = state.result.violations.filter(v =>
        v.kind === 'shift-limit').length;
      setStatus(`✗ 当前手工排程存在 ${n} 处违规${nShift ?
        `（其中 ${nShift} 处独立换档上限超限，见下方调整建议）` : ''}，` +
        '时间轴上以红色标记。可继续拖动或重置。', 'bad');
    }
  } catch (e) {
    setStatus('校验失败：' + e.message, 'bad');
  }
}

// --------------------------------------------------------------------------- //
// 时间轴渲染
// --------------------------------------------------------------------------- //
function timelineMinutes() {
  const r = state.result;
  if (!r) return 60;
  let max = r.end_minute || 0;
  for (const o of state.occs) max = Math.max(max, o.end);
  for (const l of r.lanes || []) max = Math.max(max, l.unboard || l.board + 30);
  return Math.max(60, max + 20);
}

function renderTimeline() {
  const r = state.result;
  const conflictBox = $('#conflictBox');
  conflictBox.classList.add('hidden');
  conflictBox.innerHTML = '';
  $('#detail').classList.add('hidden');
  if (!r) return;

  if (!r.feasible && r.conflicts?.length) renderConflicts(r.conflicts);
  const shiftViolations = (r.violations || []).filter(v => v.kind === 'shift-limit');
  if (!r.feasible && shiftViolations.length) renderShiftViolations(shiftViolations);

  const total = timelineMinutes();
  state.pxPerMin = total > 1000 ? 0.8 : total > 500 ? 1.2 : 2;
  const W = Math.max(900, total * state.pxPerMin + 120);

  renderRuler(W, total);
  renderRhTrack(W, total, r);
  renderLanes(W, r);
  renderOccTrack(W, total);
  renderCapTrack(W, r);
  renderViolations(W, r);
}

function renderRuler(W, total) {
  const ruler = $('#ruler');
  ruler.style.width = W + 'px';
  ruler.innerHTML = '<span class="track-label" style="background:transparent">时间 →</span>';
  const step = total > 1000 ? 120 : total > 500 ? 60 : 30;
  for (let m = 0; m <= total; m += step) {
    const t = document.createElement('div');
    t.className = 'tick';
    t.style.left = (74 + m * state.pxPerMin) + 'px';
    t.textContent = fmtMin(m);
    ruler.appendChild(t);
  }
}

function renderRhTrack(W, total, r) {
  const track = $('#rhTrack');
  track.style.width = W + 'px';
  track.innerHTML = '<span class="track-label">箱内湿度</span>';
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('x', 74); svg.setAttribute('y', 0);
  svg.setAttribute('width', W - 74); svg.setAttribute('height', 64);
  svg.style.left = '74px';
  const L = 26, R = 8;
  const yOf = rh => {
    const lo = 0, hi = 100;
    return L + (1 - (rh - lo) / (hi - lo)) * (64 - L - R);
  };
  const xOf = m => m * state.pxPerMin;

  // 0/50/100 参考线
  for (const v of [0, 50, 100]) {
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', 0); line.setAttribute('x2', W - 80);
    line.setAttribute('y1', yOf(v)); line.setAttribute('y2', yOf(v));
    line.setAttribute('stroke', v === 50 ? '#e2dac8' : '#f0ecdf');
    svg.appendChild(line);
    const lab = document.createElementNS(svgNS, 'text');
    lab.setAttribute('x', 2); lab.setAttribute('y', yOf(v) - 2);
    lab.setAttribute('font-size', 9); lab.setAttribute('fill', '#a89f8c');
    lab.textContent = v;
    svg.appendChild(lab);
  }
  for (const seg of r.segments || []) {
    if (seg.kind === 'hold') {
      const line = document.createElementNS(svgNS, 'line');
      line.setAttribute('x1', xOf(seg.start));
      line.setAttribute('x2', xOf(seg.end));
      line.setAttribute('y1', yOf(seg.start_rh));
      line.setAttribute('y2', yOf(seg.end_rh));
      line.setAttribute('stroke', '#5e8b6e');
      line.setAttribute('stroke-width', 2);
      line.classList.add('seg-hit');
      line.dataset.start = seg.start;
      svg.appendChild(line);
    } else {
      const x1 = xOf(seg.start), x2 = xOf(seg.end);
      const line = document.createElementNS(svgNS, 'line');
      line.setAttribute('x1', x1); line.setAttribute('x2', x2);
      line.setAttribute('y1', yOf(seg.from_rh ?? seg.start_rh));
      line.setAttribute('y2', yOf(seg.end_rh));
      line.setAttribute('stroke', '#c76a3e');
      line.setAttribute('stroke-width', 2);
      svg.appendChild(line);
    }
    // 段可点击查看来源
    const hit = document.createElementNS(svgNS, 'rect');
    hit.setAttribute('x', xOf(seg.start));
    hit.setAttribute('x', xOf(seg.start));
    hit.setAttribute('y', 0);
    hit.setAttribute('width', Math.max(3, xOf(seg.end) - xOf(seg.start)));
    hit.setAttribute('height', 64);
    hit.setAttribute('fill', 'transparent');
    hit.style.cursor = 'pointer';
    hit.onclick = () => showSegmentDetail(seg);
    svg.appendChild(hit);
  }
  track.appendChild(svg);
}

function renderLanes(W, r) {
  const box = $('#lanes');
  box.innerHTML = '';
  const byId = Object.fromEntries(state.samples.map(s => [s.id, s]));
  for (const lane of r.lanes || []) {
    const row = document.createElement('div');
    row.className = 'lane movable';
    row.style.width = W + 'px';
    row.dataset.sampleId = lane.sample_id;
    const name = document.createElement('span');
    name.className = 'lane-name';
    name.textContent = lane.code;
    name.title = `${lane.code}：耐受 ${num(lane.rh_min)}-${num(lane.rh_max)}%，` +
      `入箱平衡 ${num(lane.eq_rh)}%（拖动右侧空白区域可整体平移入箱时刻）`;
    row.appendChild(name);

    // 整条在箱期背景（拖拽手柄）
    const handle = document.createElement('div');
    handle.className = 'block board';
    handle.style.opacity = '0.0';
    handle.style.left = (74 + lane.board * state.pxPerMin) + 'px';
    handle.style.width = Math.max(8, ((lane.unboard ?? lane.board + 5) - lane.board) *
      state.pxPerMin) + 'px';
    handle.dataset.drag = '1';
    row.appendChild(handle);

    for (const p of lane.pieces) {
      const b = document.createElement('div');
      b.className = 'block ' + p.kind;
      const w = Math.max(3, (p.end - p.start) * state.pxPerMin);
      b.style.left = (74 + p.start * state.pxPerMin) + 'px';
      b.style.width = w + 'px';
      const label = phaseLabel(p, lane);
      b.textContent = label;
      b.title = `${lane.code} · ${phaseName(p.kind)}${
        p.step >= 0 ? ' 第' + (p.step + 1) + '档' : ''}：${
        fmtMin(p.start)}–${fmtMin(p.end)}`;
      b.dataset.viol = '';
      row.appendChild(b);
    }
    box.appendChild(row);
  }
  bindDrag();
}

function phaseName(k) {
  return { ramp: '换档', hold: '保持', cooldown: '冷却保持',
    'cooldown-ramp': '冷却换档', board: '入箱平衡', wait: '等待/偏离' }[k] || k;
}
function phaseLabel(p, lane) {
  if (p.kind === 'hold' && p.step >= 0) {
    const st = byStep(lane.sample_id, p.step);
    return `第${p.step + 1}档 ${num(st?.target)}% 保持`;
  }
  if (p.kind === 'ramp') {
    return p.step >= 0 ? `→第${p.step + 1}档换档` : '换档';
  }
  if (p.kind.startsWith('cooldown')) return '冷却/平衡';
  if (p.kind === 'board') return '入箱';
  return '';
}
function byStep(sid, i) {
  return state.samples.find(s => s.id === sid)?.steps?.[i];
}

function renderOccTrack(W, total) {
  const track = $('#occTrack');
  track.style.width = W + 'px';
  track.innerHTML = '<span class="track-label">已有占用</span>';
  for (const o of state.occs) {
    const b = document.createElement('div');
    b.className = 'occ-block';
    b.style.left = (74 + o.start * state.pxPerMin) + 'px';
    b.style.width = Math.max(4, (o.end - o.start) * state.pxPerMin) + 'px';
    b.textContent = `🔒 ${o.note || '占用'} ${num(o.rh)}% ×${o.slots}位`;
    b.title = `占用 ${fmtMin(o.start)}-${fmtMin(o.end)}，固定 ${num(o.rh)}%RH，${o.slots} 位`;
    track.appendChild(b);
  }
}

function renderCapTrack(W, r) {
  const track = $('#capTrack');
  track.style.width = W + 'px';
  track.innerHTML = '<span class="track-label">容量占用</span>';
  for (const c of r.capacity_series || []) {
    const ratio = c.load / state.capacity;
    const d = document.createElement('div');
    d.className = 'cap-cell';
    d.style.left = (74 + c.start * state.pxPerMin) + 'px';
    d.style.width = Math.max(3, (c.end - c.start) * state.pxPerMin) + 'px';
    d.style.background = c.load > state.capacity ? 'var(--bad)' :
      ratio > 0.8 ? '#d19a52' : ratio > 0 ? '#7fa083' : '#e8e2d2';
    d.style.color = ratio > 0 ? '#fff' : '#b7af9d';
    d.textContent = c.load;
    d.title = `${fmtMin(c.start)}-${fmtMin(c.end)}：占用 ${c.load}/${state.capacity}`;
    track.appendChild(d);
  }
}

function renderViolations(W, r) {
  const mins = {};
  for (const v of r.violations || []) {
    (mins[v.minute] = mins[v.minute] || []).push(v);
    // 泳道块标红
    for (const row of $$('#lanes .lane')) {
      const sid = parseInt(row.dataset.sampleId, 10);
      if (v.samples && !v.samples.includes(sid)) continue;
      for (const b of $$('.block', row)) {
        const st = parseFloat(b.style.left) - 74;
        const en = st + parseFloat(b.style.width);
        if (v.minute * state.pxPerMin >= st - 1 &&
            v.minute * state.pxPerMin < en + 1) {
          b.classList.add('viol');
          b.title += `\n⛔ ${v.message}`;
        }
      }
    }
  }
  // 每条泳道顶层放红色标记线（画在 lanes 容器上）
  const box = $('#lanes');
  for (const [m, vs] of Object.entries(mins)) {
    const flag = document.createElement('div');
    flag.className = 'viol-flag';
    flag.style.left = (74 + m * state.pxPerMin) + 'px';
    flag.style.top = '0';
    flag.textContent = '⛔';
    flag.title = vs.map(x => x.message).join('\n');
    box.appendChild(flag);
  }
}

// --------------------------------------------------------------------------- //
// 段来源详情
// --------------------------------------------------------------------------- //
function showSegmentDetail(seg) {
  const d = $('#detail');
  d.classList.remove('hidden');
  const sources = (seg.sources || []).map(s => {
    if (s.type === 'occupancy') return `<div class="src-line">🔒 约束来源：<b>${esc(s.label)}</b></div>`;
    return `<div class="src-line">📄 约束来源：样本 <b>${esc(s.code)}</b> 第 ${s.step} 档（${s.phase}）</div>`;
  }).join('') || '<div class="src-line">（空载/过渡段，无样本约束）</div>';
  d.innerHTML = `
    <h4>${seg.kind === 'ramp' ? '换档段' : '保持段'} ${fmtMin(seg.start)}–${
      fmtMin(seg.end)} ｜ 湿度 ${num(seg.from_rh ?? seg.start_rh)}% → ${num(seg.end_rh)}%
      ｜ 在箱：${(seg.aboard || []).map(esc).join('、') || '（空箱）'}</h4>
    ${sources}`;
}

// --------------------------------------------------------------------------- //
// 冲突面板
// --------------------------------------------------------------------------- //
function renderConflicts(conflicts) {
  const box = $('#conflictBox');
  box.classList.remove('hidden');
  box.innerHTML = '<h4>冲突约束与最小可调整项</h4>';
  conflicts.forEach((c, i) => {
    const div = document.createElement('div');
    div.className = 'conflict';
    const tag = `<span class="tag ${c.severity === 'soft' ? 'soft' : ''}">${
      { capacity: '容量', range: '区间', rate: '换档速率',
        'shift-limit': '换档上限', adjacency: '不可相邻',
        occupancy: '已有占用', 'occupancy-window': '占用时间窗',
        horizon: '总机时', combination: '组合约束', 'occ-overlap': '占用重叠'
      }[c.kind] || c.kind}</span>`;
    let fix = '';
    if (c.suggestion) {
      fix = `<div class="fixit"><button data-i="${i}">⚙ 应用最小调整：${esc(c.suggestion.title)}</button></div>`;
    }
    div.innerHTML = `${tag}<span class="msg">${esc(c.message)}</span>${fix}`;
    box.appendChild(div);
  });
  $$('.fixit button', box).forEach(btn => {
    btn.onclick = () => applyFix(conflicts[+btn.dataset.i].suggestion);
  });
}

// 拖拽重校验中"独立换档上限"违规：指出相邻段、差值与可调整项
function renderShiftViolations(violations) {
  const box = $('#conflictBox');
  box.classList.remove('hidden');
  if (!box.innerHTML.trim()) box.innerHTML = '<h4>冲突约束与最小可调整项</h4>';
  violations.forEach((v, i) => {
    const segTxt = (v.adjacent_segments || []).length === 2
      ? `相邻段：${fmtMin(v.adjacent_segments[0].start)}–${
          fmtMin(v.adjacent_segments[0].end)}（${num(v.from_rh)}%）→ ${
          fmtMin(v.adjacent_segments[1].start)}–${
          fmtMin(v.adjacent_segments[1].end)}（${num(v.to_rh)}%）；`
      : `${fmtMin(v.minute)} 起换档 ${num(v.from_rh)}% → ${num(v.to_rh)}%；`;
    const div = document.createElement('div');
    div.className = 'conflict';
    const first = (v.adjustments || [])[0];
    const others = (v.adjustments || []).slice(1)
      .map(a => `<span class="mlink">· ${esc(a.label)}</span>`).join('<br>');
    div.innerHTML =
      `<span class="tag">换档上限</span>` +
      `<span class="msg">独立换档上限 ${num(v.limit)}%RH 被突破：${segTxt}` +
      `差值 <b>${num(v.delta)}%RH</b> 超限。</span>` +
      (first ? `<div class="fixit"><button data-i="${i}">⚙ 应用调整：${
        esc(first.label)}</button></div>` : '') +
      (others ? `<div class="alt-fix">其他可调整项：<br>${others}</div>` : '');
    box.appendChild(div);
    const btn = div.querySelector('.fixit button');
    if (btn) {
      btn.onclick = () => applyFix(first);
    }
  });
}

async function applyFix(sug) {
  try {
    if (sug.type === 'shift-limit') {
      // 放宽独立换档上限：保存设置；若当前有排程则立刻用同一拖拽结果重校验
      state.maxShiftDelta = sug.value;
      $('#maxShiftDelta').value = sug.value;
      await api('/api/settings', {
        method: 'POST',
        body: { max_shift_delta: sug.value },
      });
      if (state.result && (state.result.lanes || []).length) {
        await revalidate();
      }
      setStatus(`已把独立换档上限放宽为 ${sug.value}%RH，可直接拖拽或重新生成排程。`,
        'ok');
    } else if (sug.type === 'capacity') {
      state.capacity = Math.max(state.capacity, sug.value || state.capacity + 1);
      $('#capacity').value = state.capacity;
      await api('/api/settings', { method: 'POST', body: { capacity: state.capacity } });
      setStatus(`已把箱容量调整为 ${state.capacity}，请重新生成排程。`, 'ok');
    } else if (sug.type === 'adjacency' && sug.samples) {
      const [a, b] = sug.samples;
      const sa = state.samples.find(s => s.id === a);
      const sb = state.samples.find(s => s.id === b);
      if (sa && sb) {
        const ex = new Set([...(sa.excludes || []), b]);
        await api('/api/samples', {
          method: 'POST',
          body: { ...sa, excludes: [...ex] },
        });
        await loadAll();
        setStatus(`已把 ${sa.code} 与 ${sb.code} 标记为不可相邻，请重新生成排程。`, 'ok');
      }
    } else if (sug.type === 'rate' && sug.sample_id) {
      const s = state.samples.find(x => x.id === sug.sample_id);
      if (s) {
        const steps = s.steps.map((st, j) => j === sug.step - 1 ?
          { ...st, rate: Math.max(st.rate, sug.value) } : st);
        await api('/api/samples', { method: 'POST', body: { ...s, steps } });
        await loadAll();
        setStatus(`已放宽 ${s.code} 第 ${sug.step} 档速率，请重新生成排程。`, 'ok');
      }
    } else {
      setStatus('该建议需要手工调整已有占用段的时间或湿度，请在左侧修改后重新排程。', 'warn');
      return;
    }
  } catch (e) {
    setStatus('应用调整失败：' + e.message, 'bad');
  }
}

// --------------------------------------------------------------------------- //
// 拖拽
// --------------------------------------------------------------------------- //
function bindDrag() {
  $$('#lanes .lane').forEach(row => {
    const sid = parseInt(row.dataset.sampleId, 10);
    row.addEventListener('pointerdown', (e) => {
      if (!e.target.dataset.drag) return;
      if (!state.result?.feasible && state.result?.fatal === 'NO_FEASIBLE_SCHEDULE') {
        setStatus('无解排程无法拖拽；请先应用调整建议或修改输入后重新生成。', 'warn');
        return;
      }
      e.preventDefault();
      const startX = e.clientX;
      const orig = state.boardTimes[sid] ?? 0;
      const blocks = $$('.block', row);
      const init = blocks.map(b => parseFloat(b.style.left));
      document.body.style.cursor = 'grabbing';

      const move = (ev) => {
        const dm = Math.round((ev.clientX - startX) / state.pxPerMin);
        const nt = Math.max(0, orig + dm);
        state.boardTimes[sid] = nt;
        blocks.forEach((b, i) => {
          b.style.left = (init[i] + dm * state.pxPerMin) + 'px';
        });
      };
      const up = async () => {
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        document.body.style.cursor = '';
        await revalidate();
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
    });
  });
}

// --------------------------------------------------------------------------- //
// 导出
// --------------------------------------------------------------------------- //
function exportJSON() {
  if (!state.result) {
    alert('请先生成排程');
    return;
  }
  const payload = {
    exported_at: new Date().toISOString(),
    feasible: state.result.feasible,
    capacity: state.capacity,
    max_shift_delta: state.result.max_shift_delta ?? state.maxShiftDelta,
    board_times: state.boardTimes,
    end_minute: state.result.end_minute,
    end_display: fmtMin(state.result.end_minute),
    samples: state.result.samples || state.samples,
    occupancies: state.occs,
    chamber_segments: (state.result.segments || []).map(s => ({
      start_minute: s.start, end_minute: s.end,
      start: fmtMin(s.start), end: fmtMin(s.end),
      type: s.kind,
      rh_start: s.from_rh ?? s.start_rh, rh_end: s.end_rh,
      aboard: s.aboard, constraints: s.sources,
    })),
    sample_lanes: (state.result.lanes || []).map(l => ({
      sample_id: l.sample_id, code: l.code,
      board_minute: l.board, unboard_minute: l.unboard,
      board: fmtMin(l.board),
      unboard: l.unboard == null ? null : fmtMin(l.unboard),
      pieces: l.pieces.map(p => ({
        start_minute: p.start, end_minute: p.end,
        start: fmtMin(p.start), end: fmtMin(p.end),
        phase: phaseName(p.kind), step: p.step >= 0 ? p.step + 1 : null,
      })),
    })),
    violations: state.result.violations || [],
    conflicts: state.result.conflicts || [],
    minimal_adjustment: state.result.minimal_adjustment || null,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)],
    { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `humidity-schedule-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// --------------------------------------------------------------------------- //
function setStatus(msg, kind = '') {
  const el = $('#statusBar');
  el.textContent = msg;
  el.className = 'status ' + kind;
}
const num = (v) => (v == null || isNaN(v)) ? '' :
  (String(v).includes('.') ? String(parseFloat(v.toFixed(2))) : String(v));
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// --------------------------------------------------------------------------- //
// 事件绑定
// --------------------------------------------------------------------------- //
$('#addSampleBtn').onclick = () => openSampleModal();
$('#modalCancel').onclick = () => $('#modal').classList.add('hidden');
$('#modalSave').onclick = saveSampleFromModal;
$('#modalDelete').onclick = async () => {
  if (state.editingId && confirm('确定删除该样本及其全部阶梯？')) {
    await api(`/api/samples/${state.editingId}`, { method: 'DELETE' });
    $('#modal').classList.add('hidden');
    await loadAll();
  }
};
$('#addStepBtn').onclick = () => {
  const body = $('#stepBody');
  renderStepRows([...$$('#stepBody tr').map((tr, i) => ({
    target: parseFloat(tr.querySelector('.s-target').value),
    dwell: parseInt(tr.querySelector('.s-dwell').value, 10),
    rate: parseFloat(tr.querySelector('.s-rate').value),
  })), { target: '', dwell: 30, rate: 2 }]);
};
$('#addOccBtn').onclick = openOccModal;
$('#occCancel').onclick = () => $('#occModal').classList.add('hidden');
$('#occSave').onclick = saveOcc;
$('#saveCapBtn').onclick = async () => {
  const cap = parseInt($('#capacity').value, 10);
  if (!(cap >= 1)) return setStatus('容量必须 ≥1', 'bad');
  const raw = $('#maxShiftDelta').value.trim();
  let shift = null;
  if (raw !== '') {
    shift = parseFloat(raw);
    if (!(shift > 0) || shift > 100) {
      return setStatus('独立换档上限必须是 0.1–100 之间的正数（留空表示不限制）',
        'bad');
    }
  }
  try {
    const saved = await api('/api/settings', {
      method: 'POST',
      body: { capacity: cap, max_shift_delta: shift },
    });
    state.capacity = saved.capacity;
    state.maxShiftDelta = saved.max_shift_delta ?? null;
    $('#capacity').value = state.capacity;
    $('#maxShiftDelta').value = state.maxShiftDelta ?? '';
    setStatus(`设置已保存：容量 ${cap}；独立换档上限 ${
      state.maxShiftDelta == null ? '不限制' : state.maxShiftDelta + '%RH'}。`, 'ok');
  } catch (e) {
    setStatus('设置保存失败：' + e.message, 'bad');
  }
};
$('#runBtn').onclick = runSchedule;
$('#resetDragBtn').onclick = () => {
  if (state.result?.board_times) {
    state.boardTimes = { ...state.result.board_times };
    revalidate();
  }
};
$('#exportBtn').onclick = exportJSON;

// --------------------------------------------------------------------------- //
// 实测湿度复核
// --------------------------------------------------------------------------- //
const rvFmtMin = (m) => {
  const x = Number(m);
  const d = Math.floor(x / 1440), h = Math.floor((x % 1440) / 60),
        min = x - d * 1440 - h * 60;
  const ms = Math.abs(min - Math.round(min)) < 1e-7
    ? String(Math.round(min)) : String(parseFloat(min.toFixed(2)));
  return (d ? d + 'd' : '') + (h ? h + 'h' : '') + ((ms !== '0' || (!d && !h)) ? ms + 'm' : '');
};
const rvConclusionName = { pass: '通过', review: '复核', insufficient: '数据不足' };

// 从排程 segments 还原计划曲线整数分钟节点 rh[t]
function planSeriesFromSegments(segments) {
  if (!segments?.length) return [];
  const lastEnd = Math.max(...segments.map(s => s.end));
  const rh = new Array(lastEnd).fill(null);
  for (const seg of segments) {
    const s = seg.start, e = seg.end;
    if (seg.kind === 'hold') {
      for (let t = s; t < e; t++) rh[t] = seg.start_rh;
    } else {
      const frm = seg.from_rh ?? seg.start_rh;
      const slope = (seg.end_rh - frm) / (e - s);
      for (let k = 0; k < e - s; k++) rh[s + k] = frm + (k + 1) * slope;
    }
  }
  return rh.map(v => v == null ? null : Number(v.toFixed(5)));
}

function rvParams() {
  const tol = parseFloat($('#rvTol').value);
  const dose = parseFloat($('#rvDose').value);
  const gap = parseInt($('#rvGap').value, 10);
  const start = parseInt($('#rvStart').value || '0', 10);
  if (!(tol >= 0)) return { error: '允许偏差必须为 ≥0 的数字' };
  if (!(dose >= 0)) return { error: '累计偏差剂量上限必须为 ≥0 的数字' };
  if (!(gap >= 1)) return { error: '最大采样间隔必须为 ≥1 的整数分钟' };
  if (!(start >= 0)) return { error: '单列读数起始分钟必须为 ≥0 的整数' };
  return { tol, dose, gap, start };
}

async function runReview({ save = false } = {}) {
  const p = rvParams();
  if (p.error) { setRvStatus(p.error, 'bad'); return; }
  if (!state.result) {
    setRvStatus('请先在④生成（或载入）排程，再进行实测复核。', 'bad');
    return;
  }
  if (!state.result.feasible) {
    setRvStatus('当前排程不可行，无法复核：请先在④生成可行排程。', 'bad');
    return;
  }
  const text = $('#rvText').value;
  if (!text.trim()) {
    setRvStatus('请先粘贴传感器读数。', 'bad');
    return;
  }
  setRvStatus('正在按分段线性曲线精确积分…', 'warn');
  try {
    const body = {
      // 显式带上当前时间轴（可能含拖拽后的手工排程），避免用旧的库内快照
      schedule: state.result,
      readings_text: text, start_minute: p.start,
      tolerance: p.tol, dose_limit: p.dose, max_gap: p.gap,
      save, note: $('#rvNote').value.trim(),
    };
    const out = await api('/api/review', { method: 'POST', body });
    state.review = out;
    renderReview(out);
    if (save) loadReviewRecords();
    setRvStatus(out.overall_conclusion_label === '通过'
      ? `✓ 复核完成：${out.summary.pass}/${out.summary.total} 个样本通过。`
      : out.overall_conclusion_label === '复核'
        ? `⚠ ${out.summary.review} 个样本累计偏差剂量超限，需要复核。`
        : `⛔ ${out.summary.insufficient} 个样本数据不足（存在采样缺口或窗口未覆盖）。`,
      out.overall_conclusion_label === '通过' ? 'ok'
        : out.overall_conclusion_label === '复核' ? 'bad' : 'warn');
  } catch (e) {
    setRvStatus('复核失败：' + e.message, 'bad');
  }
}

function fillPlanReadings() {
  if (!state.result?.feasible) {
    setRvStatus('请先生成可行排程。', 'bad');
    return;
  }
  // 填入单列湿度：时间轴由「单列读数起始分钟」自动解释，保持与排程分钟对齐
  const rh = planSeriesFromSegments(state.result.segments);
  if (!rh.length) return setRvStatus('排程时间轴为空。', 'bad');
  $('#rvText').value = rh.map(v => String(Number(v.toFixed(2)))).join('\n');
  setRvStatus(`已按计划值填入 ${rh.length} 条读数（0 偏差基线，可据此修改实测值）。`, 'ok');
}

// ---- 渲染 ------------------------------------------------------------------ //
function setRvStatus(msg, kind = '') {
  const el = $('#rvStatus');
  el.textContent = msg;
  el.className = 'status ' + kind;
}

function reviewMinutes(out) {
  let max = out.schedule?.end_minute || 0;
  for (const s of out.samples) max = Math.max(max, s.unboard ?? s.board);
  for (const [t] of out.readings || []) max = Math.max(max, t);
  return Math.max(30, max + 10);
}

function renderReview(out) {
  const total = reviewMinutes(out);
  state.reviewPx = total > 1000 ? 0.8 : total > 500 ? 1.2 : 2;
  const W = Math.max(640, total * state.reviewPx + 120);
  renderRvRuler(W, total);
  renderRvChart(W, total, out);
  renderRvLanes(W, out);
  renderRvSamples(out);
}

function renderRvRuler(W, total) {
  const ruler = $('#rvRuler');
  ruler.style.width = W + 'px';
  ruler.innerHTML = '<span class="track-label" style="background:transparent">时间 →</span>';
  const step = total > 1000 ? 120 : total > 500 ? 60 : 30;
  for (let m = 0; m <= total; m += step) {
    const t = document.createElement('div');
    t.className = 'tick';
    t.style.left = (74 + m * state.reviewPx) + 'px';
    t.textContent = fmtMin(m);
    ruler.appendChild(t);
  }
}

function renderRvChart(W, total, out) {
  const chart = $('#rvChart');
  chart.style.width = W + 'px';
  chart.innerHTML = '<span class="track-label">湿度%</span>';
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('x', 74); svg.setAttribute('y', 0);
  svg.setAttribute('width', W - 74); svg.setAttribute('height', 150);
  svg.style.left = '74px';
  const TOP = 18, BOT = 12;
  const xOf = m => m * state.reviewPx;
  const yOf = rh => TOP + (1 - rh / 100) * (150 - TOP - BOT);

  // 参考线
  for (const v of [0, 25, 50, 75, 100]) {
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', 0); line.setAttribute('x2', W - 80);
    line.setAttribute('y1', yOf(v)); line.setAttribute('y2', yOf(v));
    line.setAttribute('stroke', v === 50 ? '#e2dac8' : '#f0ecdf');
    svg.appendChild(line);
    const lab = document.createElementNS(svgNS, 'text');
    lab.setAttribute('x', 2); lab.setAttribute('y', yOf(v) - 2);
    lab.setAttribute('font-size', 9); lab.setAttribute('fill', '#a89f8c');
    lab.textContent = v;
    svg.appendChild(lab);
  }

  const tol = out.parameters.tolerance;
  const plan = out.plan_series || [];
  const N = plan.length;

  // 允许带（采样整分钟节点画带，按每个样本窗口取并集略复杂；
  // 这里以全时间轴计划带为底，再用样本窗口遮罩出有效区）
  const windowMins = new Set();
  for (const s of out.samples) {
    const end = s.unboard ?? s.board;
    for (let t = s.board; t < end; t++) windowMins.add(t);
  }
  const gapMins = new Set();
  for (const g of out.gaps || []) {
    for (let t = g.start; t < g.end; t++) gapMins.add(t);
  }
  if (N > 1) {
    const up = [], lo = [];
    for (let i = 0; i < N; i++) {
      if (!windowMins.has(i)) continue;
      up.push([xOf(i), yOf(plan[i][1] + tol)]);
      lo.push([xOf(i), yOf(plan[i][1] - tol)]);
    }
    if (up.length > 1) {
      // 连续分钟合并成一条条带 path，入箱/出箱（或缺口）处断开
      const segs2 = [];
      let cur = [0];
      for (let i = 1; i < up.length; i++) {
        const minute = Math.round(up[i][0] / state.reviewPx);
        const prevMinute = Math.round(up[i - 1][0] / state.reviewPx);
        const cont = minute === prevMinute + 1 && !gapMins.has(prevMinute);
        if (cont) cur.push(i);
        else { segs2.push(cur); cur = [i]; }
      }
      segs2.push(cur);
      for (const segIdx of segs2) {
        if (segIdx.length < 2) continue;
        let d = '';
        segIdx.forEach((idx, k) => { d += `${k ? 'L' : 'M'}${up[idx][0]},${up[idx][1]}`; });
        for (let k = segIdx.length - 1; k >= 0; k--) {
          const idx = segIdx[k];
          d += `L${lo[idx][0]},${lo[idx][1]}`;
        }
        d += 'Z';
        const poly = document.createElementNS(svgNS, 'path');
        poly.setAttribute('d', d);
        poly.setAttribute('fill', 'rgba(94,139,110,.15)');
        poly.setAttribute('stroke', 'none');
        svg.appendChild(poly);
      }
    }
  }

  // 计划曲线
  if (N > 1) {
    let d = `M${xOf(0)},${yOf(plan[0][1])}`;
    for (let i = 1; i < N; i++) d += `L${xOf(i)},${yOf(plan[i][1])}`;
    const path = document.createElementNS(svgNS, 'path');
    path.setAttribute('d', d);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', '#5e8b6e');
    path.setAttribute('stroke-width', '2');
    svg.appendChild(path);
  }

  // 采样缺口背景（缺口内不插值：断开实测线）
  for (const g of out.gaps || []) {
    const rect = document.createElementNS(svgNS, 'rect');
    rect.setAttribute('x', xOf(g.start));
    rect.setAttribute('width', (g.end - g.start) * state.reviewPx);
    rect.setAttribute('y', 0); rect.setAttribute('height', 150);
    rect.setAttribute('fill', 'rgba(192,57,43,.07)');
    svg.appendChild(rect);
  }

  // 实测曲线：缺口处断开
  const readings = out.readings || [];
  const drawMeasuredPath = (pts, stroke, width, dash) => {
    if (pts.length < 2) return;
    let d = `M${xOf(pts[0][0])},${yOf(pts[0][1])}`;
    for (let i = 1; i < pts.length; i++) {
      const gapHere = (out.gaps || []).some(g =>
        pts[i - 1][0] === g.start && pts[i][0] === g.end);
      d += `${gapHere ? 'M' : 'L'}${xOf(pts[i][0])},${yOf(pts[i][1])}`;
    }
    const path = document.createElementNS(svgNS, 'path');
    path.setAttribute('d', d);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', stroke);
    path.setAttribute('stroke-width', '' + width);
    if (dash) path.setAttribute('stroke-dasharray', dash);
    svg.appendChild(path);
  };
  drawMeasuredPath(readings, '#34507d', 1.8);

  // 超带部分用红色加粗重描（按分钟格逐段判定，含亚分钟交叉）
  const measAt = t => {
    // t 为整数分钟；在可插值采样区间内线性取值
    for (let i = 0; i < readings.length - 1; i++) {
      const [t0, v0] = readings[i], [t1, v1] = readings[i + 1];
      if (t0 <= t && t <= t1) {
        if ((out.gaps || []).some(g => t0 === g.start && t1 === g.end)) return null;
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0);
      }
    }
    return null;
  };
  for (let i = 0; i + 1 < N; i++) {
    if (!windowMins.has(i)) continue;
    const m0 = measAt(i), m1 = measAt(i + 1);
    if (m0 == null || m1 == null) continue;
    const d0 = m0 - plan[i][1], d1 = m1 - plan[i + 1][1];
    for (const side of [1, -1]) {
      const f0 = side > 0 ? d0 - tol : -d0 - tol;
      const f1 = side > 0 ? d1 - tol : -d1 - tol;
      if (f0 <= 0 && f1 <= 0) continue;
      let r0 = 0, r1 = 1;
      if (!(f0 > 0 && f1 > 0)) {
        const r = f0 / (f0 - f1);
        if (f0 <= 0) r0 = Math.max(0, r); else r1 = Math.min(1, r);
      }
      // 超带红线直接沿实测曲线的超带子段绘制
      const line = document.createElementNS(svgNS, 'line');
      line.setAttribute('x1', xOf(i + r0));
      line.setAttribute('x2', xOf(i + r1));
      const mv0 = m0 + (m1 - m0) * r0;
      const mv1 = m0 + (m1 - m0) * r1;
      line.setAttribute('y1', yOf(mv0));
      line.setAttribute('y2', yOf(mv1));
      line.setAttribute('stroke', '#c0392b');
      line.setAttribute('stroke-width', '2.4');
      svg.appendChild(line);
    }
  }

  // 读数点
  for (const [t, v] of readings) {
    const c = document.createElementNS(svgNS, 'circle');
    c.setAttribute('cx', xOf(t)); c.setAttribute('cy', yOf(v));
    c.setAttribute('r', 1.6); c.setAttribute('fill', '#34507d');
    svg.appendChild(c);
  }

  // 样本入箱/出箱竖线
  for (const s of out.samples) {
    for (const t of [s.board, s.unboard].filter(x => x != null)) {
      const line = document.createElementNS(svgNS, 'line');
      line.setAttribute('x1', xOf(t)); line.setAttribute('x2', xOf(t));
      line.setAttribute('y1', 4); line.setAttribute('y2', 146);
      line.setAttribute('stroke', '#b98a4e');
      line.setAttribute('stroke-dasharray', '3 3');
      line.setAttribute('stroke-width', '1');
      svg.appendChild(line);
    }
  }
  chart.appendChild(svg);
}

function renderRvLanes(W, out) {
  const box = $('#rvLanes');
  box.innerHTML = '';
  for (const s of out.samples) {
    const row = document.createElement('div');
    row.className = 'rv-lane';
    row.style.width = W + 'px';
    const name = document.createElement('span');
    name.className = 'lane-name';
    name.textContent = s.code;
    row.appendChild(name);
    const end = s.unboard ?? s.board;
    const win = document.createElement('div');
    win.className = 'rv-window';
    win.style.left = (74 + s.board * state.reviewPx) + 'px';
    win.style.width = Math.max(8, (end - s.board) * state.reviewPx) + 'px';
    win.style.background = { pass: '#5e8b6e', review: '#c0392b',
      insufficient: '#b08d3f' }[s.conclusion];
    win.textContent = `${rvConclusionName[s.conclusion]} · ${fmtMin(s.board)}–${
      s.unboard == null ? '?' : fmtMin(s.unboard)}`;
    row.appendChild(win);
    for (const u of s.uncovered || []) {
      const g = document.createElement('div');
      g.className = 'rv-gapband';
      g.style.left = (74 + u.start * state.reviewPx) + 'px';
      g.style.width = Math.max(3, (u.end - u.start) * state.reviewPx) + 'px';
      g.title = u.reason_label;
      row.appendChild(g);
    }
    box.appendChild(row);
  }
}

function renderRvSamples(out) {
  const box = $('#rvSamples');
  box.innerHTML = '';
  for (const s of out.samples) {
    const div = document.createElement('div');
    div.className = 'rv-sample ' + s.conclusion;
    const limitOver = s.dose > s.dose_limit + 1e-6;
    const intervals = (s.excess_intervals || []);
    const ivHtml = intervals.length
      ? intervals.map(v => {
          const dir = v.side === '+' ? '偏高' : '偏低';
          return `<span class="iv-row ${v.side === '-' ? 'neg' : ''}">${
            rvFmtMin(v.start)}–${rvFmtMin(v.end)} ${esc(v.phase)} ${dir} · 剂量 ${
            num(v.dose)} · 最大偏差 ${num(v.max_deviation)}%</span>`;
        }).join('')
      : '<span class="none">无超带区间 ✓</span>';
    const uncHtml = (s.uncovered || []).length
      ? `<div class="rv-uncovered">未覆盖/缺口（不插值）：${
          s.uncovered.map(u => `<span class="u">${rvFmtMin(u.start)}–${
            rvFmtMin(u.end)} ${esc(u.reason_label)}</span>`).join('')}</div>`
      : '';
    div.innerHTML = `
      <div class="rv-head">
        <span class="code">${esc(s.code)}</span>
        <span class="badge ${s.conclusion}">${
          rvConclusionName[s.conclusion] || s.conclusion}</span>
        <span style="font-size:11.5px;color:#998f7d">在箱窗口 ${
          fmtMin(s.board)}–${s.unboard == null ? '?' : fmtMin(s.unboard)}
          （${s.window_minutes ?? '—'} 分钟）</span>
      </div>
      <div class="rv-metrics">
        <span>累计偏差剂量：<b class="${limitOver ? 'over-limit' : ''}">${
          num(s.dose)}</b> / 上限 ${num(s.dose_limit)} %RH·分钟${
          limitOver ? '（超限）' : ''}</span>
        <span>窗口内最大偏差：<b>${num(s.max_deviation)}</b> %RH</span>
        <span>允许带 ±${num(s.tolerance)} %RH ｜ 采样间隔上限 ${s.max_gap} 分钟</span>
        <span>数据：${s.data_complete ? '完整' : '不完整（' +
          (s.uncovered || []).length + ' 处未覆盖）'}</span>
      </div>
      <div class="rv-intervals">超带区间（按计划阶段切分）：${ivHtml}</div>
      ${uncHtml}`;
    box.appendChild(div);
  }
}

// ---- 记录 ------------------------------------------------------------------ //
async function loadReviewRecords() {
  const box = $('#rvRecordList');
  box.innerHTML = '';
  let list = [];
  try {
    list = await api('/api/reviews');
  } catch { list = []; }
  if (!list.length) {
    box.innerHTML = '<div style="color:#998f7d;font-size:12px;margin-top:6px">尚无已保存复核记录。</div>';
    return;
  }
  for (const r of list) {
    const div = document.createElement('div');
    div.className = 'rv-record';
    const s = r.summary || {};
    const name = rvConclusionName[r.conclusion] || r.conclusion;
    div.innerHTML = `<span><span class="dot ${r.conclusion}"></span>#${r.id} · ${
      esc(r.created_at)} · <b>${name}</b> · 通过 ${s.pass ?? 0}/复核 ${
      s.review ?? 0}/数据不足 ${s.insufficient ?? 0}${
      r.note ? ' · ' + esc(r.note) : ''}</span>
      <span class="ops">
        <button class="mini" data-act="view">查看</button>
        <button class="mini" data-act="redo" title="只重排结论为「复核」/「数据不足」的样本，原排程冻结为历史">⑥ 补做排程</button>
        <button class="mini" data-act="json">JSON</button>
        <button class="mini del" data-act="del">删除</button>
      </span>`;
    div.querySelector('[data-act="view"]').onclick = async () => {
      const rec = await api(`/api/reviews/${r.id}`);
      state.review = rec.payload;
      renderReview(rec.payload);
      setRvStatus(`已载入复核记录 #${r.id}（${rec.created_at}）。`, 'ok');
    };
    div.querySelector('[data-act="redo"]').onclick = () => runRedo(r.id);
    div.querySelector('[data-act="json"]').onclick = () => {
      downloadReviewJSON(r.payload, `humidity-review-${r.id}.json`);
    };
    div.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`确定删除复核记录 #${r.id}？`)) return;
      await api(`/api/reviews/${r.id}`, { method: 'DELETE' });
      loadReviewRecords();
    };
    box.appendChild(div);
  }
}

function downloadReviewJSON(payload, filename) {
  const blob = new Blob([JSON.stringify(payload, null, 2)],
    { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename || `humidity-review-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportReviewJSON() {
  if (!state.review) {
    setRvStatus('请先执行一次复核。', 'bad');
    return;
  }
  const payload = state.review.review_id
    ? state.review
    : { exported_at: new Date().toISOString(), ...state.review };
  downloadReviewJSON(payload);
}

$('#rvRun').onclick = () => runReview({ save: false });
$('#rvSave').onclick = () => runReview({ save: true });
$('#rvFill').onclick = fillPlanReadings;
$('#rvExport').onclick = exportReviewJSON;
$('#rvReload').onclick = loadReviewRecords;

// --------------------------------------------------------------------------- //
// ⑥ 补做排程
// --------------------------------------------------------------------------- //
const redoState = {
  plan: null,        // 最近一次补做方案（可行/无解/空方案）
  reviewId: null,    // 对应复核记录 id
  px: 2,
};

function setRedoStatus(msg, kind = '') {
  const el = $('#redoStatus');
  el.textContent = msg;
  el.className = 'status ' + kind;
}

async function runRedo(reviewId) {
  setRedoStatus(`正在基于复核记录 #${reviewId} 求解补做排程…`, 'warn');
  $('#redoConflictBox').classList.add('hidden');
  $('#redoConflictBox').innerHTML = '';
  try {
    const plan = await api(`/api/reviews/${reviewId}/redo`, { method: 'POST' });
    redoState.plan = plan;
    redoState.reviewId = reviewId;
    renderRedo(plan);
    if (plan.empty) {
      setRedoStatus(`✓ ${plan.message || '该复核没有需要补做的样本（空方案，未保存）。'}`, 'ok');
    } else if (plan.feasible) {
      setRedoStatus(`✓ 补做排程可行并已保存：历史 0–${fmtMin(plan.base_minute)} ` +
        `冻结保留，${plan.lanes.length} 个失败样本从 ${fmtMin(plan.base_minute)} 之后重排，` +
        `总时长 ${fmtMin(plan.end_minute)}。衔接处：${plan.junction?.label || '—'}`, 'ok');
      loadRedoRecords();
    } else {
      setRedoStatus('✗ 补做无解：存在冲突约束，未保存任何半成品。请查看下方最小调整建议。', 'bad');
      renderRedoConflicts(plan.conflicts || [], reviewId);
    }
  } catch (e) {
    setRedoStatus('补做失败：' + e.message, 'bad');
  }
}

function redoMinutes(plan) {
  let max = plan.end_minute || 0;
  for (const l of (plan.lanes || [])) max = Math.max(max, l.unboard ?? l.board);
  for (const l of (plan.history_lanes || [])) {
    max = Math.max(max, l.unboard ?? l.board);
  }
  for (const o of (plan.occupancies || [])) max = Math.max(max, o.end);
  return Math.max(60, max + 20);
}

function renderRedo(plan) {
  const total = redoMinutes(plan);
  redoState.px = total > 1000 ? 0.8 : total > 500 ? 1.2 : 2;
  const W = Math.max(900, total * redoState.px + 120);
  renderRedoRuler(W, total);
  renderRedoRh(W, total, plan);
  renderRedoHistLanes(W, plan);
  renderRedoLanes(W, plan);
  renderRedoOcc(W, total, plan);
  renderRedoCap(W, plan);
  renderRedoInfo(plan);
}

function renderRedoRuler(W, total) {
  const ruler = $('#redoRuler');
  ruler.style.width = W + 'px';
  ruler.innerHTML = '<span class="track-label" style="background:transparent">时间 →</span>';
  const step = total > 1000 ? 120 : total > 500 ? 60 : 30;
  for (let m = 0; m <= total; m += step) {
    const t = document.createElement('div');
    t.className = 'tick';
    t.style.left = (74 + m * redoState.px) + 'px';
    t.textContent = fmtMin(m);
    ruler.appendChild(t);
  }
}

// 在所有轨道上叠加新旧时间轴分界虚线（含标签）
function renderRedoDivider(track, plan) {
  const base = plan.base_minute;
  if (base == null) return;
  const d = document.createElement('div');
  d.className = 'redo-divider';
  d.style.left = (74 + (base + 1) * redoState.px) + 'px';
  d.style.height = '100%';
  d.innerHTML = '<span class="divider-tag">历史结束 ' + fmtMin(base) + ' →补做</span>';
  track.appendChild(d);
}

function renderRedoRh(W, total, plan) {
  const track = $('#redoRhTrack');
  track.style.width = W + 'px';
  track.innerHTML = '<span class="track-label">箱内湿度</span>';
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('x', 74); svg.setAttribute('y', 0);
  svg.setAttribute('width', W - 74); svg.setAttribute('height', 64);
  svg.style.left = '74px';
  const L = 26, R = 8;
  const yOf = rh => L + (1 - rh / 100) * (64 - L - R);
  const xOf = m => m * redoState.px;
  for (const v of [0, 50, 100]) {
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', 0); line.setAttribute('x2', W - 80);
    line.setAttribute('y1', yOf(v)); line.setAttribute('y2', yOf(v));
    line.setAttribute('stroke', v === 50 ? '#e2dac8' : '#f0ecdf');
    svg.appendChild(line);
    const lab = document.createElementNS(svgNS, 'text');
    lab.setAttribute('x', 2); lab.setAttribute('y', yOf(v) - 2);
    lab.setAttribute('font-size', 9); lab.setAttribute('fill', '#a89f8c');
    lab.textContent = v;
    svg.appendChild(lab);
  }
  const base = plan.base_minute;
  // 历史段灰色（冻结），补做段沿用绿/橙
  for (const seg of (plan.segments || [])) {
    const isHist = seg.start <= base;
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', xOf(seg.start));
    line.setAttribute('x2', xOf(seg.end));
    if (seg.kind === 'hold') {
      line.setAttribute('y1', yOf(seg.start_rh));
      line.setAttribute('y2', yOf(seg.end_rh));
      line.setAttribute('stroke', isHist ? '#b9b2a0' : '#5e8b6e');
    } else {
      line.setAttribute('y1', yOf(seg.from_rh ?? seg.start_rh));
      line.setAttribute('y2', yOf(seg.end_rh));
      line.setAttribute('stroke', isHist ? '#a79e88' : '#c76a3e');
    }
    line.setAttribute('stroke-width', '2');
    line.classList.add('seg-hit');
    line.style.cursor = 'pointer';
    line.onclick = () => showRedoSegmentDetail(plan, seg);
    svg.appendChild(line);
  }
  // 衔接处标记
  if (plan.junction && !plan.empty) {
    const j = plan.junction;
    const mark = document.createElementNS(svgNS, 'text');
    mark.setAttribute('x', xOf(j.base_minute) + 3);
    mark.setAttribute('y', 12);
    mark.setAttribute('font-size', 11);
    mark.setAttribute('fill', j.within_limit ? '#7b4fa0' : '#c0392b');
    mark.textContent = '⇄ 衔接 Δ' + num(j.delta) + '%';
    const title = document.createElementNS(svgNS, 'title');
    title.textContent = j.label;
    mark.appendChild(title);
    svg.appendChild(mark);
  }
  track.appendChild(svg);
  renderRedoDivider(track, plan);
}

function redoPhaseLabel(p, sid) {
  if (p.kind === 'hold' && p.step >= 0) {
    const st = byStep(sid, p.step);
    return `第${p.step + 1}档 ${num(st?.target)}% 保持`;
  }
  if (p.kind === 'ramp') return p.step >= 0 ? `→第${p.step + 1}档换档` : '换档';
  if (String(p.kind).startsWith('cooldown')) return '冷却/平衡';
  if (p.kind === 'board') return '入箱';
  return '';
}

function renderRedoHistLanes(W, plan) {
  const box = $('#redoHistLanes');
  box.innerHTML = '';
  for (const lane of (plan.history_lanes || [])) {
    const row = document.createElement('div');
    row.className = 'lane hist-lane';
    row.style.width = W + 'px';
    const name = document.createElement('span');
    name.className = 'lane-name';
    name.textContent = '历史·' + lane.code;
    name.title = `原排程（冻结历史，不可修改）：${lane.code} ${
      fmtMin(lane.board)}–${lane.unboard == null ? '?' : fmtMin(lane.unboard)}`;
    row.appendChild(name);
    for (const p of (lane.pieces || [])) {
      const b = document.createElement('div');
      b.className = 'block ' + p.kind;
      b.style.left = (74 + p.start * redoState.px) + 'px';
      b.style.width = Math.max(3, (p.end - p.start) * redoState.px) + 'px';
      b.title = `历史 ${lane.code} · ${phaseName(p.kind)}${
        p.step >= 0 ? ' 第' + (p.step + 1) + '档' : ''}：${
        fmtMin(p.start)}–${fmtMin(p.end)}（冻结，不可修改）`;
      row.appendChild(b);
    }
    box.appendChild(row);
    renderRedoDivider(row, plan);
  }
}

function renderRedoLanes(W, plan) {
  const box = $('#redoLanes');
  box.innerHTML = '';
  for (const lane of (plan.lanes || [])) {
    const row = document.createElement('div');
    row.className = 'redo-lane';
    row.style.width = W + 'px';
    const name = document.createElement('span');
    name.className = 'lane-name';
    name.textContent = '补做·' + lane.code;
    name.title = `补做任务：${lane.code}，从历史结束分钟之后入箱，重走完整程序`;
    row.appendChild(name);
    for (const p of (lane.pieces || [])) {
      const b = document.createElement('div');
      b.className = 'block ' + p.kind;
      b.style.left = (74 + p.start * redoState.px) + 'px';
      b.style.width = Math.max(3, (p.end - p.start) * redoState.px) + 'px';
      b.textContent = redoPhaseLabel(p, lane.sample_id);
      b.title = `补做 ${lane.code} · ${phaseName(p.kind)}${
        p.step >= 0 ? ' 第' + (p.step + 1) + '档' : ''}：${
        fmtMin(p.start)}–${fmtMin(p.end)}`;
      row.appendChild(b);
    }
    box.appendChild(row);
    renderRedoDivider(row, plan);
  }
}

function renderRedoOcc(W, total, plan) {
  const track = $('#redoOccTrack');
  track.style.width = W + 'px';
  track.innerHTML = '<span class="track-label">已有占用</span>';
  for (const o of (plan.occupancies || [])) {
    const b = document.createElement('div');
    b.className = 'occ-block';
    b.style.left = (74 + o.start * redoState.px) + 'px';
    b.style.width = Math.max(4, (o.end - o.start) * redoState.px) + 'px';
    b.textContent = `🔒 ${o.note || '占用'} ${num(o.rh)}% ×${o.slots}位`;
    b.title = `占用 ${fmtMin(o.start)}-${fmtMin(o.end)}，固定 ${num(o.rh)}%RH，${o.slots} 位（沿用原有占用）`;
    track.appendChild(b);
  }
  renderRedoDivider(track, plan);
}

function renderRedoCap(W, plan) {
  const track = $('#redoCapTrack');
  track.style.width = W + 'px';
  track.innerHTML = '<span class="track-label">容量占用</span>';
  const cap = plan.capacity ?? state.capacity;
  for (const c of (plan.capacity_series || [])) {
    const ratio = c.load / cap;
    const d = document.createElement('div');
    d.className = 'cap-cell';
    d.style.left = (74 + c.start * redoState.px) + 'px';
    d.style.width = Math.max(3, (c.end - c.start) * redoState.px) + 'px';
    d.style.background = c.load > cap ? 'var(--bad)' :
      ratio > 0.8 ? '#d19a52' : ratio > 0 ? '#7fa083' : '#e8e2d2';
    d.style.color = ratio > 0 ? '#fff' : '#b7af9d';
    d.textContent = c.load;
    d.title = `${fmtMin(c.start)}-${fmtMin(c.end)}：占用 ${c.load}/${cap}`;
    track.appendChild(d);
  }
  renderRedoDivider(track, plan);
}

function renderRedoInfo(plan) {
  // 状态行下方的摘要信息条（冲突面板单独渲染，互不影响）
  let info = document.querySelector('#redoPanel .redo-info');
  if (info) info.remove();
  if (!plan.junction && !plan.failed_samples) return;
  info = document.createElement('div');
  info.className = 'redo-info';
  const failed = (plan.failed_samples || []).map(s =>
    `<span class="chip">${esc(s.code)}</span>`).join('') || '<span style="color:#998f7d">无</span>';
  const passed = (plan.passed_samples || []).map(id => {
    const s = (plan.history_lanes || []).find(l => l.sample_id === id);
    return `<span class="chip" style="border-color:#9cb39c;background:#eef5ec;color:#41604b">通过不重排 · ${esc(s?.code || '#' + id)}</span>`;
  }).join('');
  const j = plan.junction;
  const jHtml = j
    ? `<div>新旧衔接处：<b class="${j.within_limit ? 'junction-ok' : 'junction-bad'}">${esc(j.label)}</b></div>`
    : '';
  info.innerHTML =
    `<div>冻结历史：<b>0 – ${fmtMin(plan.base_minute)}</b>（结束湿度 ${num(plan.end_rh)}%RH）；` +
    `补做样本：${failed}</div>` +
    (passed ? `<div>通过样本（不重复安排）：${passed}</div>` : '') +
    jHtml;
  $('#redoStatus').after(info);
}

function showRedoSegmentDetail(plan, seg) {
  // 复用④的详情弹条（页面顶部 detail 面板）
  const d = $('#detail');
  d.classList.remove('hidden');
  const isHist = seg.start <= plan.base_minute;
  const sources = (seg.sources || []).map(s => {
    if (s.type === 'occupancy') return `<div class="src-line">🔒 约束来源：<b>${esc(s.label)}</b></div>`;
    return `<div class="src-line">📄 约束来源：样本 <b>${esc(s.code)}</b> 第 ${s.step} 档（${s.phase}）</div>`;
  }).join('') || '<div class="src-line">（空载/过渡段，无样本约束）</div>';
  d.innerHTML =
    `<h4>[${isHist ? '冻结历史' : '补做'}] ${seg.kind === 'ramp' ? '换档段' : '保持段'} ${
      fmtMin(seg.start)}–${fmtMin(seg.end)} ｜ 湿度 ${num(seg.from_rh ?? seg.start_rh)}% → ${
      num(seg.end_rh)}% ｜ 在箱：${(seg.aboard || []).map(esc).join('、') || '（空箱）'}</h4>` +
    sources +
    (isHist ? '<div class="src-line" style="color:#8a8272">该段属于不可修改的原排程历史。</div>' : '');
}

function renderRedoConflicts(conflicts, reviewId) {
  const box = $('#redoConflictBox');
  box.classList.remove('hidden');
  box.innerHTML = '<h4>补做冲突约束与最小可调整项（方案未保存）</h4>';
  conflicts.forEach((c) => {
    const div = document.createElement('div');
    div.className = 'conflict';
    const tag = `<span class="tag ${c.severity === 'soft' ? 'soft' : ''}">${
      { capacity: '容量', range: '区间', rate: '换档速率',
        'shift-limit': '换档上限', adjacency: '不可相邻',
        occupancy: '已有占用', 'occupancy-window': '占用时间窗',
        horizon: '总机时', combination: '组合约束', 'occ-overlap': '占用重叠'
      }[c.kind] || c.kind}</span>`;
    div.innerHTML = `${tag}<span class="msg">${esc(c.message)}</span>` +
      (c.suggestion ? `<div class="fixit"><button>${'⚙ ' + esc(c.suggestion.title)}</button></div>` : '');
    box.appendChild(div);
    const btn = div.querySelector('.fixit button');
    if (btn) btn.onclick = () => applyRedoFix(c.suggestion, reviewId);
  });
}

async function applyRedoFix(sug, reviewId) {
  try {
    if (sug.type === 'shift-limit') {
      // 放宽独立换档上限：持久化设置，并用新上限立即重试该复核的补做
      state.maxShiftDelta = sug.value;
      $('#maxShiftDelta').value = sug.value;
      await api('/api/settings', { method: 'POST',
        body: { max_shift_delta: sug.value } });
      setRedoStatus(`已把独立换档上限放宽为 ${sug.value}%RH，正在重试补做…`, 'warn');
      await runRedo(reviewId);
    } else if (sug.type === 'capacity') {
      state.capacity = Math.max(state.capacity, sug.value || state.capacity + 1);
      $('#capacity').value = state.capacity;
      await api('/api/settings', { method: 'POST',
        body: { capacity: state.capacity } });
      setRedoStatus(`已把箱容量调整为 ${state.capacity}，正在重试补做…`, 'warn');
      await runRedo(reviewId);
    } else {
      setRedoStatus('该建议需要调整样本阶梯或后移/移除历史之后的占用段；' +
        '补做的冻结历史与占用来自保存复核时的原排程，请在①③修改后重新排程、复核，再生成补做。', 'warn');
    }
  } catch (e) {
    setRedoStatus('应用调整失败：' + e.message, 'bad');
  }
}

async function loadRedoRecords() {
  const box = $('#redoRecordList');
  box.innerHTML = '';
  let list = [];
  try {
    list = await api('/api/redo');
  } catch { list = []; }
  if (!list.length) {
    box.innerHTML = '<div style="color:#998f7d;font-size:12px;margin-top:8px">尚无已保存的补做方案（空方案与无解不保存）。</div>';
    return;
  }
  const head = document.createElement('div');
  head.className = 'panel-head mt';
  head.innerHTML = '<h3 style="font-size:13.5px">已保存补做方案</h3>';
  box.appendChild(head);
  for (const r of list) {
    const div = document.createElement('div');
    div.className = 'redo-record';
    const sm = r.summary || {};
    div.innerHTML = `<span>#${r.id} · 复核记录 #${r.review_id} · ${esc(r.created_at)} ·
      历史止 ${fmtMin(r.base_minute)} → 补做止 ${fmtMin(r.end_minute)} ·
      失败样本 ${sm.failed ?? 0} 个，通过未重排 ${sm.passed ?? 0} 个
      ${r.junction?.label ? ' · ' + esc(r.junction.label) : ''}</span>
      <span class="ops">
        <button class="mini" data-act="view">查看时间轴</button>
        <button class="mini" data-act="json">导出 JSON</button>
        <button class="mini del" data-act="del">删除</button>
      </span>`;
    div.querySelector('[data-act="view"]').onclick = () => {
      redoState.plan = r.payload;
      redoState.reviewId = r.review_id;
      renderRedo(r.payload);
      setRedoStatus(`已载入补做方案 #${r.id}（对应复核 #${r.review_id}，${r.created_at}）。`, 'ok');
    };
    div.querySelector('[data-act="json"]').onclick = () =>
      downloadRedoJSON(r.payload, `humidity-redo-${r.id}.json`);
    div.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`确定删除补做方案 #${r.id}？（不影响原复核与原排程历史）`)) return;
      await api(`/api/redo/${r.id}`, { method: 'DELETE' });
      loadRedoRecords();
    };
    box.appendChild(div);
  }
}

function downloadRedoJSON(payload, filename) {
  const blob = new Blob([JSON.stringify(payload, null, 2)],
    { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename || `humidity-redo-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportRedoJSON() {
  if (!redoState.plan) {
    setRedoStatus('请先生成或载入一个补做方案。', 'bad');
    return;
  }
  const p = redoState.plan;
  const payload = {
    exported_at: new Date().toISOString(),
    review_id: redoState.reviewId,
    redo_id: p.redo_id ?? null,
    feasible: p.feasible,
    empty: !!p.empty,
    capacity: p.capacity,
    max_shift_delta: p.max_shift_delta ?? null,
    base_minute: p.base_minute,
    base_display: p.base_display,
    end_minute: p.end_minute,
    end_display: fmtMin(p.end_minute),
    junction: p.junction,
    failed_samples: (p.failed_samples || []).map(s => s.id),
    passed_samples: p.passed_samples || [],
    board_times: p.board_times || {},
    history_segments: p.history_segments || [],
    redo_segments: p.redo_segments || [],
    history_lanes: p.history_lanes || [],
    redo_lanes: p.lanes || [],
    occupancies: p.occupancies || [],
    capacity_series: p.capacity_series || [],
    conflicts: p.conflicts || [],
    minimal_adjustment: p.minimal_adjustment || null,
  };
  downloadRedoJSON(payload);
}

$('#redoReload').onclick = loadRedoRecords;
$('#redoExport').onclick = exportRedoJSON;

loadAll().catch(e => setStatus('初始化失败：' + e.message, 'bad'));
loadReviewRecords();
loadRedoRecords();
