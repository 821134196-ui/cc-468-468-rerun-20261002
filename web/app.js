/* 防汛挡板系统前端（原生 JS，无依赖） */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const KIND_CN = { panel: '挡板', post: '立柱', seal: '密封条' };
const STATUS_CN = {
  created: '待领取', claimed: '已领取', installing: '安装中', deployed: '已部署',
  dismantled: '已拆除', cleaning: '清洗中', drying: '晾干中',
  return_check: '归架核对', completed: '已完成', cancelled: '已取消',
  available: '可用', locked: '已锁定', installed: '已安装',
  shelved: '已归架', damaged: '破损', missing: '缺件', written_off: '已核销',
};
const LEVEL_CN = { blue: '蓝色', yellow: '黄色', orange: '橙色', red: '红色' };
const INC_CN = {
  temp_vehicle_pass: '临时车辆放行', wrong_segment: '挡板错段', seal_damaged: '密封条破损',
  personnel_late: '人员迟到', water_ingress: '入口积水',
  early_dismantle: '提前拆除', return_shortage: '归架缺件',
};

const state = {
  me: null, personnel: [], dash: null, equipment: [],
  supplies: [], incidents: [], view: 'dashboard',
  task: null,             // 安装/归架视图当前任务详情
  claimTaskId: null,
  taskCache: new Map(),
};

// --------------------------------------------------------------- api

async function api(path, body) {
  const opt = body === undefined
    ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' },
             body: JSON.stringify({ person_id: state.me?.id ?? null, ...body }) };
  const res = await fetch(path, opt);
  const j = await res.json();
  if (!j.ok) throw new Error(j.error || '请求失败');
  return j.data;
}

async function refreshAll() {
  const [dash, equipment, supplies, incidents] = await Promise.all([
    api('/api/state'), api('/api/equipment'),
    api('/api/supplies'), api('/api/incidents'),
  ]);
  state.dash = dash; state.equipment = equipment;
  state.supplies = supplies; state.incidents = incidents;
  if (state.task) {  // 动作后丢弃缓存，重新取任务详情
    const id = state.task.id;
    state.task = null;
    await loadTask(id, true);
  }
  renderChrome();
  renderView();
}

async function loadTask(id, force = false) {
  if (id == null) { state.task = null; return null; }
  if (!force && state.taskCache.has(id) && state.task?.id === id) {
    state.task = state.taskCache.get(id); return state.task;
  }
  const t = await api(`/api/tasks/${id}`);
  state.taskCache.set(id, t); state.task = t; return t;
}

// --------------------------------------------------------------- utils

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function role() { return state.me?.role; }
function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`; el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}
async function guard(fn, okMsg) {
  try { await fn(); if (okMsg) toast(okMsg, 'ok'); }
  catch (e) { toast(e.message, 'error'); }
  await refreshAll();
}
function modal({ title, body, okText = '确定', onOk }) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = body;
  $('#modal').classList.remove('hidden');
  const close = () => $('#modal').classList.add('hidden');
  $('#modal-cancel').onclick = close;
  $('#modal-ok').onclick = async () => {
    try { const r = await onOk($('#modal-body')); if (r !== false) close(); }
    catch (e) { toast(e.message, 'error'); }
  };
}
function statusBadge(s) {
  const cls = ['deployed', 'shelved', 'available'].includes(s) ? 'green'
    : ['locked', 'claimed', 'installing', 'created', 'cleaning', 'drying',
       'return_check', 'dismantled'].includes(s) ? 'blue'
    : s === 'written_off' ? 'gray' : 'red';
  return `<span class="badge ${cls}">${STATUS_CN[s] ?? s}</span>`;
}
function levelBadge(lv) {
  return `<span class="badge ${lv}">● ${LEVEL_CN[lv]}预警</span>`;
}
function taskItems(t, kind) { return t.items.filter(i => i.kind === kind); }

// --------------------------------------------------------------- chrome

function renderChrome() {
  const d = state.dash;
  const banner = $('#alert-banner');
  if (d?.alert) {
    banner.className = `alert-banner ${d.alert.level}`;
    banner.innerHTML = `⚠ ${LEVEL_CN[d.alert.level]}预警进行中 · ${esc(d.alert.reason)}
      <span style="opacity:.85;font-weight:400;font-size:12px">
      开始 ${esc(d.alert.started_at)}，当前雨量 ${d.alert.rainfall_mm ?? '-'}mm</span>`;
  } else {
    banner.className = 'alert-banner blue';
    banner.textContent = '✓ 当前无活跃预警 · 模拟天气每 3 秒复查一次';
  }
  // 任务下拉保持选中
  ['#install-task', '#return-task'].forEach(sel => syncTaskSelect($(sel)));
}

function syncTaskSelect(sel) {
  if (!sel || !state.dash) return;
  const cur = sel.value;
  sel.innerHTML = state.dash.tasks
    .map(t => `<option value="${t.id}">#${t.id} ${esc(t.entrance_name)} · ${STATUS_CN[t.status]}</option>`)
    .join('');
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}

// --------------------------------------------------------------- dashboard

function renderDashboard() {
  const d = state.dash;
  const activeN = d.tasks.length;
  const myEnts = d.entrances.filter(e => e.level_cm != null);
  const maxLv = Math.max(0, ...myEnts.map(e => e.level_cm));
  $('#stat-row').innerHTML = `
    <div class="stat ${d.alert ? 'danger' : 'ok'}">
      <div class="num">${d.alert ? LEVEL_CN[d.alert.level] : '无'}</div><div class="lbl">当前预警等级</div></div>
    <div class="stat"><div class="num">${activeN}</div><div class="lbl">在途部署任务</div></div>
    <div class="stat warn"><div class="num">${d.open_incidents}</div><div class="lbl">未闭异常</div></div>
    <div class="stat ${d.pending_supplies ? 'warn' : 'ok'}">
      <div class="num">${d.pending_supplies}</div><div class="lbl">待审批缺件/报损</div></div>`;
  $('#rain-phase').textContent = d.monitor_tick
    ? `${d.monitor_tick.phase} · 过程 ${d.monitor_tick.elapsed}s` : '';
  $('#rain-chart').innerHTML = rainChart(d.rain_series);
  $('#task-list').innerHTML = d.tasks.length ? d.tasks.map(t => `
    <div class="task-row">
      <div><div class="t-main">#${t.id} ${esc(t.entrance_name)}</div>
        <div class="t-sub">创建 ${esc(t.created_at.slice(5, 16))}
          ${t.early_removal_status === 'requested' ? ' · <b style="color:var(--orange)">提前解除待审批</b>' : ''}
          ${t.early_removal_status === 'approved' ? ' · <b style="color:var(--green)">提前拆除已批准</b>' : ''}
        </div></div>
      <div style="display:flex;gap:8px;align-items:center">
        ${statusBadge(t.status)}
        <button class="btn tiny" onclick="openTask(${t.id})">处理</button>
      </div></div>`).join('') : '<p class="hint">暂无在途任务，达到橙色阈值后会自动生成部署清单。</p>';
  const openInc = state.incidents.filter(i => i.status === 'open');
  $('#incident-list').innerHTML = openInc.length ? openInc.map(i => `
    <div class="incident-row">
      <span class="i-type badge ${i.type === 'water_ingress' || i.type === 'early_dismantle' ? 'red' : 'orange'}">
        ${INC_CN[i.type] ?? i.type}</span>
      <div style="flex:1;min-width:200px">${esc(i.detail)}
        <div class="t-sub">${esc(i.created_at)}${i.raised_name ? ' · ' + esc(i.raised_name) : ''}</div></div>
      <button class="btn tiny" onclick="resolveInc(${i.id})">复查关闭</button>
    </div>`).join('') : '<p class="hint">没有未闭异常。</p>';
}

window.openTask = (id) => {
  const t = state.dash.tasks.find(x => x.id === id);
  const installing = ['created', 'claimed', 'installing', 'deployed'].includes(t?.status);
  switchView(installing ? 'install' : 'return');
  const sel = installing ? $('#install-task') : $('#return-task');
  sel.value = String(id);
  onTaskPicked();
};
window.resolveInc = async (id) => modal({
  title: '复查并关闭异常',
  body: '<input id="m-res" placeholder="复查处理说明（如：水位已回落、排水完成）">',
  onOk: async b => { await api(`/api/incidents/${id}/resolve`, { resolution: $('#m-res').value || '复查关闭' }); }
});

// --------------------------------------------------------------- rain chart

function rainChart(series) {
  if (!series?.length) return '<p class="hint">暂无读数，监控任务启动后开始记录。</p>';
  const W = 520, H = 180, PL = 34, PR = 12, PT = 14, PB = 24;
  const vals = series.map(p => p.value_mm);
  const maxV = Math.max(20, ...vals) * 1.15;
  const x = i => PL + (W - PL - PR) * (i / Math.max(1, series.length - 1));
  const y = v => PT + (H - PT - PB) * (1 - v / maxV);
  const path = series.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.value_mm).toFixed(1)}`).join('');
  const area = `${path} L${x(series.length - 1)},${H - PB} L${x(0)},${H - PB} Z`;
  const grid = [0, 8, 16].map(v => {
    if (v > maxV) return '';
    return `<line class="${v ? 'threshold' : 'axis'}" x1="${PL}" x2="${W - PR}" y1="${y(v)}" y2="${y(v)}"/>
      ${v ? `<text class="threshold-label" x="${W - PR - 4}" y="${y(v) - 3}">${v}mm</text>` : ''}`;
  }).join('');
  const ticks = [0, Math.floor(series.length / 2), series.length - 1];
  const xlabels = [...new Set(ticks)].map(i =>
    `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc(series[i].ts.slice(11, 16))}</text>`).join('');
  const dots = series.map((p, i) =>
    `<circle class="dot" cx="${x(i)}" cy="${y(p.value_mm)}" r="2.6" data-i="${i}"/>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="雨量趋势线形图">
    <defs><linearGradient id="rainFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#2b7bb8" stop-opacity=".22"/>
      <stop offset="100%" stop-color="#2b7bb8" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}
    <line class="axis" x1="${PL}" x2="${PL}" y1="${PT}" y2="${H - PB}"/>
    <line class="axis" x1="${PL}" x2="${W - PR}" y1="${H - PB}" y2="${H - PB}"/>
    <path d="${area}" fill="url(#rainFill)"/>
    <path class="line" d="${path}"/>${dots}${xlabels}
    <g id="crosshair"></g></svg>`;
}

function bindChartTip() {
  const root = $('#rain-chart svg');
  if (!root || root.dataset.bound) return;
  root.dataset.bound = '1';
  let tip;
  root.addEventListener('mousemove', e => {
    const dot = e.target.closest('circle.dot');
    if (!dot) { if (tip) tip.remove(), tip = null; return; }
    const p = state.dash.rain_series[+dot.dataset.i];
    if (!tip) tip = Object.assign(document.createElement('div'), { className: 'chart-tip' });
    tip.textContent = `${p.ts.slice(11)} · ${p.value_mm} mm/h`;
    tip.style.left = `${e.clientX + 12}px`; tip.style.top = `${e.clientY - 30}px`;
    document.body.appendChild(tip);
  });
  root.addEventListener('mouseleave', () => tip && tip.remove());
}

// --------------------------------------------------------------- schematic

async function renderSchematic() {
  const wrap = $('#schematic-list');
  const tasks = await Promise.all(state.dash.entrances.map(async e => {
    let t = null;
    if (e.active_task_id != null) {
      try { t = await api(`/api/tasks/${e.active_task_id}`); } catch { t = null; }
    }
    return [e, t];
  }));
  wrap.innerHTML = tasks.map(([e, t]) => schematic(e, t)).join('');
}

function schematic(ent, t) {
  const n = ent.slot_count;
  const panelBySlot = new Map();
  const sealBySlot = new Map();
  const postByIdx = new Map();
  if (t) {
    t.items.forEach(i => {
      if (i.kind === 'panel') panelBySlot.set(i.slot_index, i);
      if (i.kind === 'seal') sealBySlot.set(i.slot_index, i);
      if (i.kind === 'post') postByIdx.set(i.slot_index, i);
    });
  }
  const water = ent.level_cm;
  const waterHot = water != null && water >= ent.orange_cm;
  const parts = [];
  for (let s = 0; s <= n; s++) {
    const post = postByIdx.get(s);
    const pThere = post && post.locked_code;
    const pInstalled = post && post.installed_at;
    parts.push(`<div class="post-wrap" title="${pThere ? esc(post.locked_code) : '立柱空槽'}">
      <div class="post-box ${pThere ? '' : 'missing'} ${pInstalled ? '' : ''}"></div>
      <span class="seg-tag">柱${s}${pThere ? '·' + esc(post.locked_code) : ''}</span></div>`);
    if (s < n) {
      const p = panelBySlot.get(s + 1);
      const seal = sealBySlot.get(s + 1);
      let cls = 'empty', label = `槽${s + 1}<br>空槽待装`;
      if (p?.installed_at) { cls = 'installed'; label = `槽${s + 1} · ${esc(p.locked_code)}`; }
      else if (p?.locked_code) { cls = 'locked'; label = `槽${s + 1} · ${esc(p.locked_code)}<br>已领未装`; }
      const sealOk = seal?.installed_at;
      const checks = p ? (p.connection_confirmed_at ? '🔗' : '') + (p.seal_confirmed_at ? ' 💧' : '') : '';
      parts.push(`<div class="seg">
        <div class="panel-box ${cls}">${label}${checks ? '<br>' + checks : ''}</div>
        <div class="seal-line ${sealOk ? '' : 'missing'}" title="${sealOk ? '密封条已就位' : '密封条未就位'}"></div>
        <span class="seg-tag">密封 ${seal?.locked_code ? esc(seal.locked_code) : '—'}</span>
      </div>`);
    }
  }
  return `<div class="card schem">
    <div class="schem-title">
      <h2>${esc(ent.name)}</h2>
      ${water != null ? `<span class="badge ${waterHot ? 'red' : water >= ent.warn_cm ? 'yellow' : 'blue'}">
        水位 ${water}cm</span>` : ''}
      ${t ? statusBadge(t.status) : '<span class="badge gray">无在途任务</span>'}
      ${t?.channel_controlled ? '<span class="badge green">🚧 通道已封控</span>' : ''}
      ${t ? `<button class="btn tiny" onclick="openTask(${t.id})">进入处理</button>` : ''}
    </div>
    <div class="schem-ground">
      <div class="water-arrow">${'〰 '.repeat(14)}<br><span style="font-size:12px">坡道来水方向 →</span></div>
      <div class="barrier-line">${parts.join('')}</div>
    </div>
    <div class="schem-foot">
      <span><i class="legend-dot" style="background:var(--rain)"></i>已安装挡板</span>
      <span><i class="legend-dot" style="background:var(--rain-tint);border:1px dashed var(--rain-dark)"></i>已领未装</span>
      <span><i class="legend-dot" style="border:2px dashed var(--orange)"></i>缺段空槽</span>
      <span><i class="legend-dot" style="background:var(--green)"></i>密封条就位</span>
    </div></div>`;
}

// --------------------------------------------------------------- equipment / claim

async function renderEquipment() {
  const sel = $('#claim-task');
  sel.innerHTML = state.dash.tasks.length
    ? state.dash.tasks.map(t =>
      `<option value="${t.id}">#${t.id} ${esc(t.entrance_name)} · ${STATUS_CN[t.status]}</option>`).join('')
    : '<option value="">无在途任务</option>';
  if (state.claimTaskId && [...sel.options].some(o => +o.value === state.claimTaskId)) {
    sel.value = String(state.claimTaskId);
  } else {
    state.claimTaskId = sel.value ? +sel.value : null;
  }
  const t = state.claimTaskId ? await loadTask(state.claimTaskId) : null;
  const cl = $('#checklist');
  if (!t) { cl.innerHTML = '<p class="hint">暂无可领取的部署清单。</p>'; }
  else {
    const grouped = [
      ['挡板', taskItems(t, 'panel')],
      ['立柱', taskItems(t, 'post')],
      ['密封条', taskItems(t, 'seal')],
    ];
    cl.innerHTML = grouped.map(([name, items]) => `
      <h3 style="font-size:14px;margin:10px 0 2px;color:var(--rain-dark)">${name}</h3>
      ${items.map(i => `
        <div class="check-item">
          <span class="ci-name">${esc(i.pos_label)}</span>
          ${i.locked_code
            ? `<span class="ci-code">${esc(i.locked_code)}</span>${statusBadge(i.equipment_status)}`
            : `<span class="ci-code">${i.planned_code ? '建议 ' + esc(i.planned_code) : '库存不足'}</span>
               <span class="badge orange">待锁定</span>`}
          ${!i.locked_code ? `<button class="btn tiny" onclick="claimOne(${i.id},'${i.planned_code ?? ''}')">领取建议件</button>
            <button class="btn tiny ghost" onclick="claimOneManual(${i.id},'${i.kind}')">指定编号…</button>` : ''}
        </div>`).join('')}`).join('');
  }
  const kind = $('#eq-filter').value;
  const rows = state.equipment.filter(e => !kind || e.kind === kind).map(e => `
    <tr><td>${e.code}</td><td>${KIND_CN[e.kind]}</td><td>${esc(e.shelf ?? '—')}</td>
      <td>${statusBadge(e.status)}</td><td>${esc(e.task_entrance ?? '')}</td></tr>`).join('');
  $('#eq-table tbody').innerHTML = rows || '<tr><td colspan="5" class="hint">无器材</td></tr>';
}

window.claimOne = (id, code) => guard(async () => {
  await api(`/api/tasks/${state.claimTaskId}/claim-one`, { item_id: id, code });
}, `${code} 已锁定`);
window.claimOneManual = (id, kind) => modal({
  title: `指定${KIND_CN[kind]}编号领取`,
  body: '<input id="m-code" placeholder="如 B-A07（输入他任务编号可验证排他冲突）">',
  onOk: async () => api(`/api/tasks/${state.claimTaskId}/claim-one`,
    { item_id: id, code: $('#m-code').value.trim().toUpperCase() })
});

// --------------------------------------------------------------- install view

async function onTaskPicked() {
  const view = state.view;
  const sel = view === 'install' ? $('#install-task') : $('#return-task');
  const id = sel.value ? +sel.value : null;
  await loadTask(id, true);
  renderView();
}

function renderInstall() {
  const t = state.task;
  const box = $('#slots');
  if (!t) { box.innerHTML = '<p class="hint">请选择在途任务。</p>'; $('#post-strip').innerHTML = ''; return; }
  const editable = ['created', 'claimed', 'installing'].includes(t.status);
  const totalConn = taskItems(t, 'panel').length;
  const connN = taskItems(t, 'panel').filter(p => p.connection_confirmed_at).length;
  const sealN = taskItems(t, 'panel').filter(p => p.seal_confirmed_at).length;
  $('#install-status').innerHTML = `
    <div class="card">
      <div class="slot-head"><h2>#${t.id} ${esc(t.entrance.name)} · ${t.entrance.slot_count} 段挡板</h2>
        ${statusBadge(t.status)}
        ${t.channel_controlled ? '<span class="badge green">🚧 已封控</span>' : '<span class="badge orange">未封控</span>'}
      </div>
      <div class="hint">连接确认 ${connN}/${totalConn} ｜ 密封确认 ${sealN}/${totalConn}</div>
      <div class="progress"><i style="width:${(connN + sealN) / (totalConn * 2) * 100}%"></i></div>
      ${t.status === 'deployed' ? '<p class="hint" style="color:var(--green)">部署已完成。临时放行请联系保安。</p>' : ''}
    </div>`;

  // 立柱条
  $('#post-strip').innerHTML = taskItems(t, 'post').map(p => `
    <div class="post-chip">
      <b>柱${p.slot_index}</b>
      <span class="ci-code">${p.locked_code ?? '未领'}</span>
      ${p.installed_at ? '<span class="badge green">就位</span>'
        : (p.locked_code && editable
            ? `<button class="btn tiny" onclick="placeItem(${p.id})">立柱就位</button>`
            : '<span class="badge orange">待处理</span>')}
    </div>`).join('');

  // 槽位卡片（实际拼接顺序）
  box.innerHTML = taskItems(t, 'panel').map(p => {
    const seal = taskItems(t, 'seal').find(s => s.slot_index === p.slot_index);
    const cls = p.connection_confirmed_at && p.seal_confirmed_at ? 'done'
      : (p.installed_at ? 'active' : '');
    return `<div class="slot-card ${cls}">
      <div class="slot-head">
        <h3>槽 ${p.slot_index} / ${t.entrance.slot_count}</h3>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <span class="ci-code">挡板 ${p.locked_code ?? '未领取'}</span>
          ${p.installed_at ? '<span class="badge green">已安装</span>' : ''}
        </div>
      </div>
      ${!p.installed_at && editable ? `
        <div class="code-scan">
          <input id="scan-${p.slot_index}" placeholder="扫码/输入挡板编号（错段会被拦截）"
            value="${p.locked_code ?? ''}">
          <button class="btn primary" onclick="installPanel(${p.slot_index})">安装入槽</button>
        </div>` : ''}
      ${!p.installed_at && p.locked_code && editable ? `
        <p class="hint">扫到别的挡板？
          <button class="btn tiny ghost" onclick="fixSegment(${p.slot_index})">扫码纠正错段</button></p>` : ''}
      <div style="margin-top:8px;font-size:13px;color:var(--ink-2)">
        密封条：<span class="ci-code">${seal?.locked_code ?? '未领'}</span>
        ${seal?.installed_at ? '<span class="badge green">已就位</span>'
          : (seal?.locked_code && editable
              ? `<button class="btn tiny" onclick="placeItem(${seal.id})">密封条就位</button>` : '')}
        ${editable ? `<button class="btn tiny ghost" onclick="damageSeal(${p.slot_index})">发现破损·更换</button>` : ''}
      </div>
      <div class="big-checks">
        <div class="big-check ${p.connection_confirmed_at ? 'checked' : ''}"
          onclick="confirmCheck(${p.slot_index},'connection')">
          <span class="box">${p.connection_confirmed_at ? '✓' : ''}</span>连接确认</div>
        <div class="big-check ${p.seal_confirmed_at ? 'checked' : ''}"
          onclick="confirmCheck(${p.slot_index},'seal')">
          <span class="box">${p.seal_confirmed_at ? '✓' : ''}</span>密封确认</div>
      </div>
    </div>`;
  }).join('');

  $('#install-photos').innerHTML = t.photos?.length
    ? `<h3 style="font-size:14px">本任务照片（${t.photos.length}）</h3>
       <div class="photo-strip">${t.photos.map(ph =>
         `<img src="/photos/${encodeURIComponent(ph.filename)}" title="${esc(ph.stage)} ${esc(ph.created_at)}">`).join('')}</div>`
    : '';
  // 拍照槽位下拉
  $('#cam-slot').innerHTML = '<option value="">整体照片</option>' +
    taskItems(t, 'panel').map(p => `<option value="${p.slot_index}">槽${p.slot_index}</option>`).join('');

  const guardish = ['guard', 'supervisor'].includes(role());
  const canControl = ['installing', 'deployed'].includes(t.status) && guardish;
  $('#btn-control').disabled = !canControl;
  $('#btn-control').textContent = t.channel_controlled ? '解除封控' : '封控通道';
  $('#btn-pass').disabled = !['installing', 'deployed'].includes(t.status) || !guardish;
  $('#btn-deploy').disabled = !editable;
}

window.installPanel = slot => guard(async () => {
  const code = $(`#scan-${slot}`).value.trim().toUpperCase();
  await api(`/api/tasks/${state.task.id}/install-panel`, { slot, code });
}, '挡板入槽成功');
window.fixSegment = slot => modal({
  title: `槽${slot} 错段纠正`,
  body: '<input id="m-fix" placeholder="扫入正确的挡板编号（两件扫反会自动交换）">',
  onOk: () => api(`/api/tasks/${state.task.id}/fix-segment`,
    { slot, code: $('#m-fix').value.trim().toUpperCase() })
});
window.placeItem = id => guard(() => api(`/api/items/${id}/place`), '已就位');
window.damageSeal = slot => guard(() =>
  api(`/api/tasks/${state.task.id}/damage-seal`, { slot }), '破损件已报损，备用密封条已换入');
window.confirmCheck = (slot, check) => guard(() =>
  api(`/api/tasks/${state.task.id}/confirm`, { slot, check }),
  check === 'connection' ? '连接已确认' : '密封已确认');

// --------------------------------------------------------------- water

function renderWater() {
  const d = state.dash;
  $('#water-entrances').innerHTML = d.entrances.map(e => {
    const w = e.level_cm;
    const hot = w >= e.orange_cm, warm = w >= e.warn_cm;
    const pct = Math.min(100, (w / e.red_cm) * 100);
    return `<div class="water-row">
      <div style="min-width:130px"><b>${esc(e.name)}</b>
        <div class="t-sub">阈值 黄${e.warn_cm}/橙${e.orange_cm}/红${e.red_cm}cm</div></div>
      <div class="water-gauge"><i class="${hot ? 'hot' : ''}" style="width:${pct}%"></i></div>
      <span class="badge ${hot ? 'red' : warm ? 'yellow' : 'blue'}" style="min-width:72px;text-align:center">
        ${w == null ? '—' : w + ' cm'}</span>
    </div>`;
  }).join('');
  $('#obs-entrance').innerHTML = d.entrances.map(e =>
    `<option value="${e.id}">${esc(e.name)}</option>`).join('');

  const crewTasks = d.tasks;
  $('#crew-list').innerHTML = crewTasks.length ? crewTasks.flatMap(t0 => [
    `<div class="t-sub" style="margin-top:6px">#${t0.id} ${esc(t0.entrance_name)}</div>`
  ]).join('') : '<p class="hint">当前无派工。</p>';
  // 派工明细从各任务详情取
  renderCrewDetails();

  const openW = state.incidents.filter(i =>
    ['water_ingress', 'temp_vehicle_pass', 'personnel_late'].includes(i.type) && i.status === 'open');
  $('#water-incidents').innerHTML = openW.length ? openW.map(i => `
    <div class="incident-row">
      <span class="badge ${i.type === 'water_ingress' ? 'red' : 'orange'}">${INC_CN[i.type]}</span>
      <div style="flex:1">${esc(i.detail)}<div class="t-sub">${esc(i.created_at)}</div></div>
      <button class="btn tiny" onclick="resolveInc(${i.id})">复查关闭</button>
    </div>`).join('') : '<p class="hint">暂无积水/通行异常。</p>';
}

async function renderCrewDetails() {
  const crews = [];
  for (const t0 of state.dash.tasks) {
    const t = await loadTask(t0.id);
    (t?.crew || []).forEach(c => crews.push({ t: t0, c }));
  }
  if (!crews.length) return;
  const html = crews.map(({ t, c }) => `
    <div class="crew-row">
      <span>#${t.id} ${esc(t.entrance_name)} · ${esc(c.name)}（${{guard:'保安',worker:'安装工',supervisor:'主管'}[c.role]}）</span>
      <span>${c.arrived_at ? (c.late ? '<span class="badge red">迟到已登记</span>' : '<span class="badge green">已到场</span>')
        : `<button class="btn tiny" onclick="arrive(${t.id})">登记到场</button>`}</span>
    </div>`).join('');
  const host = $('#crew-list');
  host.insertAdjacentHTML('beforeend', html);
}

window.arrive = tid => guard(() => api(`/api/tasks/${tid}/arrive`), '到场已登记');

// --------------------------------------------------------------- return view

const FLOW = [
  ['deployed', '部署封控'], ['dismantled', '拆除'], ['drying', '清洗晾干'],
  ['return_check', '归架核对'], ['completed', '器材复用'],
];
function renderReturn() {
  const t = state.task;
  const f = $('#return-flow');
  if (!t) { f.innerHTML = '<p class="hint">请选择任务。</p>'; $('#return-items').innerHTML = ''; return; }
  const alertActive = !!state.dash.alert;
  const idx = FLOW.findIndex(([s]) => s === t.status);
  f.innerHTML = `
    <div class="flow-steps">
      ${FLOW.map(([s, name], i) =>
        `<div class="flow-step ${i < idx || t.status === 'completed' ? 'done' : i === idx ? 'current' : ''}">
          ${i < idx || t.status === 'completed' ? '✓ ' : ''}${name}</div>`).join('')}
    </div>
    <div class="flow-actions">
      ${t.status === 'deployed' ? `
        ${alertActive && t.early_removal_status === 'none' ?
          `<button class="btn warn" onclick="requestEarly()">申请提前拆除</button>` : ''}
        ${t.early_removal_status === 'requested' ?
          `<span class="badge orange">提前拆除待主管审批</span>
           ${role() === 'supervisor' ? `
             <button class="btn success" onclick="decideEarly(true)">主管批准</button>
             <button class="btn danger" onclick="decideEarly(false)">驳回</button>` : ''}` : ''}
        ${t.early_removal_status === 'approved' ? '<span class="badge green">已批准提前拆除</span>' : ''}
        ${t.early_removal_status === 'rejected' ? '<span class="badge red">申请被驳回</span>' : ''}
        <button class="btn primary" onclick="doDismantle()">${alertActive ? '预警已解除·拆除' : '拆除挡板'}</button>` : ''}
      ${t.status === 'dismantled' ? `<button class="btn primary" onclick="step('wash')">完成清洗 → 晾干</button>` : ''}
      ${t.status === 'drying' ? `<button class="btn primary" onclick="step('dry')">晾干完成 → 归架核对</button>` : ''}
      ${t.status === 'return_check' ? `<button class="btn success big" onclick="step('complete')">✓ 盘点完成，器材重新可用</button>` : ''}
      ${t.status === 'completed' ? '<span class="badge green">本任务器材已全部回到可用库存</span>' : ''}
    </div>
    <p class="hint">规则：预警未解除且无主管批准时拆除会被制止并登记“拆除过早”；入口积水异常未关闭同样禁止拆除。</p>`;

  $('#return-items').innerHTML = ['return_check', 'completed'].includes(t.status)
    ? t.items.map(i => `
      <div class="return-item">
        <div><div class="ri-main">${esc(i.pos_label)} · ${KIND_CN[i.kind]}</div>
          <div class="ri-sub">${esc(i.locked_code ?? '无编号')}
            ${i.returned_at ? ` · ${i.return_condition === 'ok' ? '完好' : i.return_condition === 'damaged' ? '破损' : '缺件'}` : ''}
            ${i.note ? ' · ' + esc(i.note) : ''}</div></div>
        ${!i.returned_at && t.status === 'return_check' ? `<div class="ri-btns">
          <button class="btn tiny success" onclick="ret(${i.id},'ok')">完好</button>
          <button class="btn tiny warn" onclick="ret(${i.id},'damaged')">破损</button>
          <button class="btn tiny danger" onclick="ret(${i.id},'missing')">缺件</button>
        </div>` : ''}
      </div>`).join('')
    : '<p class="hint">器材拆除、清洗并晾干后在此逐件核对。</p>';

  $('#supply-list').innerHTML = t.supplies.length ? t.supplies.map(s => supplyCard(s)).join('')
    : '<p class="hint">本任务暂无缺件/报损单。</p>';
}

function supplyCard(s) {
  const st = { requested: '待主管审批', approved: '已批准·待入库', restocked: '已补充入库', rejected: '已驳回' }[s.status];
  const cls = s.status === 'restocked' ? 'green' : s.status === 'requested' ? 'orange' : 'blue';
  return `<div class="supply-row">
    <div class="s-head">#${s.id} ${KIND_CN[s.kind]} ×${s.quantity}
      ${s.reason === 'shortage' ? '归架缺件补充' : '破损报损'}
      <span class="badge ${cls}">${st}</span></div>
    <div class="s-sub">${esc(s.note)}${s.equipment_code ? ' · 器材 ' + esc(s.equipment_code) : ''}
      · ${esc(s.requested_name ?? '')} 申请</div>
    <div class="ri-btns">
      ${s.status === 'requested' && role() === 'supervisor'
        ? `<button class="btn tiny success" onclick="supplyApprove(${s.id})">主管审批</button>` : ''}
      ${s.status === 'approved'
        ? `<button class="btn tiny primary" onclick="supplyRestock(${s.id})">补充入库</button>` : ''}
    </div></div>`;
}

window.requestEarly = () => modal({
  title: '申请预警未解除提前拆除',
  body: '<textarea id="m-reason" rows="3" placeholder="说明现场原因（如业主急救车辆通行、雨已停且地漏排空）"></textarea>',
  onOk: () => api(`/api/tasks/${state.task.id}/request-early`, { reason: $('#m-reason').value.trim() })
});
window.decideEarly = approve => guard(() =>
  api(`/api/tasks/${state.task.id}/decide-early`, { approve }),
  approve ? '已批准提前拆除' : '已驳回');
window.doDismantle = () => guard(() => api(`/api/tasks/${state.task.id}/dismantle`), '已拆除，器材进入清洗');
window.step = a => guard(() => api(`/api/tasks/${state.task.id}/${a}`),
  ({ wash: '清洗完成', dry: '晾干完成', complete: '归架完成，器材重新可用' })[a]);
window.ret = (id, condition) => modal({
  title: condition === 'ok' ? '归架核对·完好' : condition === 'damaged' ? '登记破损（将报损）' : '登记缺件（将补充）',
  body: '<input id="m-note" placeholder="备注（存放架/破损情况，选填）">',
  onOk: () => api(`/api/items/${id}/return`, { condition, note: $('#m-note').value })
});
window.supplyApprove = id => guard(() => api(`/api/supplies/${id}/approve`), '主管已审批');
window.supplyRestock = id => guard(async () => {
  const r = await api(`/api/supplies/${id}/restock`, {});
  toast(`新器材 ${r.new_code} 已入库`, 'ok');
});

// --------------------------------------------------------------- photos

$('#cam-input').addEventListener('change', async e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  if (!state.task) return toast('请先选择任务', 'error');
  const dataUrl = await new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result); fr.onerror = rej; fr.readAsDataURL(file);
  });
  await guard(async () => api('/api/photos', {
    data: dataUrl, stage: $('#cam-stage').value,
    slot_index: $('#cam-slot').value ? +$('#cam-slot').value : null,
    task_id: state.task.id, entrance_id: state.task.entrance_id,
  }), '现场照片已保存');
});

// --------------------------------------------------------------- wiring

function renderView() {
  if (state.view === 'dashboard') renderDashboard();
  if (state.view === 'schematic') renderSchematic();
  if (state.view === 'equipment') renderEquipment();
  if (state.view === 'install') renderInstall();
  if (state.view === 'water') renderWater();
  if (state.view === 'return') renderReturn();
  if (state.view === 'dashboard') requestAnimationFrame(bindChartTip);
}
async function switchView(v) {
  state.view = v;
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('hidden', s.id !== `view-${v}`));
  if (v === 'install' || v === 'return') {
    const sel = $(v === 'install' ? '#install-task' : '#return-task');
    if (sel?.options.length) {
      const installish = ['created', 'claimed', 'installing', 'deployed'];
      const fits = t0 => v === 'install'
        ? installish.includes(t0.status) : !installish.includes(t0.status);
      const want = state.dash.tasks.find(t0 =>
        t0.id === +sel.value && fits(t0.status))
        ?? state.dash.tasks.find(fits)
        ?? state.dash.tasks[0];
      if (want) { sel.value = String(want.id); await onTaskPicked(); return; }
    }
  }
  renderView();
}
$$('#tabs button').forEach(b => b.onclick = () => switchView(b.dataset.view));

$('#identity').addEventListener('change', e => {
  state.me = state.personnel.find(p => p.id === +e.target.value);
  renderView();
});
$('#install-task').addEventListener('change', onTaskPicked);
$('#return-task').addEventListener('change', onTaskPicked);
$('#claim-task').addEventListener('change', e => { state.claimTaskId = +e.target.value; renderEquipment(); });
$('#eq-filter').addEventListener('change', renderEquipment);
$('#btn-claim-all').onclick = () => guard(() =>
  api(`/api/tasks/${state.claimTaskId}/claim`), '整单器材已锁定（排他占用）');
$('#btn-control').onclick = () => {
  const t = state.task;
  const controlled = !t.channel_controlled;
  if (!controlled) return modal({
    title: '解除封控',
    body: '<input id="m-why" placeholder="解封原因（选填）">',
    onOk: () => api(`/api/tasks/${t.id}/control`, { controlled: false })
  });
  guard(() => api(`/api/tasks/${t.id}/control`, { controlled: true }), '通道已封控');
};
$('#btn-pass').onclick = () => modal({
  title: '临时车辆放行（保安执行）',
  body: '<input id="m-pass" placeholder="车牌与事由，如 京A12345 业主就医">',
  onOk: async b => {
    const r = await api(`/api/tasks/${state.task.id}/temp-pass`, { reason: $('#m-pass').value });
    setTimeout(() => modal({
      title: '车辆通过后恢复封控',
      body: '<p class="hint">放行期间通道为解封状态，恢复后才能继续完成部署。</p>',
      okText: '已通过，恢复封控',
      onOk: () => api(`/api/passes/${r.pass_id}/restore`),
    }), 200);
  }
});
$('#btn-deploy').onclick = () => guard(() => api(`/api/tasks/${state.task.id}/deploy`),
  '逐段确认与封控齐备，部署完成');
$('#btn-observe').onclick = () => guard(async () => {
  await api(`/api/entrances/${$('#obs-entrance').value}/observe`, {
    level_cm: parseFloat($('#obs-level').value),
    note: $('#obs-note').value,
    task_id: state.dash.entrances.find(e => e.id === $('#obs-entrance').value)?.active_task_id ?? null,
  });
  $('#obs-level').value = ''; $('#obs-note').value = '';
}, '观察已记录');
$('#btn-wx-set').onclick = () => guard(async () => {
  const rain = $('#wx-rain').value === '' ? null : parseFloat($('#wx-rain').value);
  await api('/api/weather/manual', { rain });
}, '已切入手动雨量（下一生效周期）');
$('#btn-wx-resume').onclick = () => guard(() => api('/api/weather/manual', { rain: null }),
  '已恢复自动过程线');

async function boot() {
  state.personnel = await api('/api/personnel');
  $('#identity').innerHTML = state.personnel.map(p =>
    `<option value="${p.id}">${p.name} · ${ {guard:'保安',worker:'安装工',supervisor:'物业主管'}[p.role]}</option>`).join('');
  state.me = state.personnel[1];
  $('#identity').value = String(state.me.id);
  await refreshAll();
  setInterval(refreshAll, 4000);
}
boot().catch(e => toast('启动失败：' + e.message, 'error'));
