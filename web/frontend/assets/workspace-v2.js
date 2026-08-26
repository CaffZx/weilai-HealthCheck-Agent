const workspaceState = {
  overview: null,
  pages: {batches: 1, runs: 1, signals: 1},
};

async function api(path) {
  const response = await fetch(path, {headers: {Accept: 'application/json'}});
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function formatDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString('zh-CN', {hour12: false});
}

function statusClass(value) {
  if (['HEALTHY', 'SUCCEEDED', 'COMPLETED', 'DELIVERED', 'RESOLVED'].includes(value)) return 'good';
  if (['CRITICAL', 'FAILED', 'DEAD', 'INTERRUPTED'].includes(value)) return 'bad';
  if (['DEGRADED', 'PARTIAL_SUCCESS', 'COMPLETED_WITH_GAPS', 'DATA_INSUFFICIENT'].includes(value)) return 'warn';
  return 'info';
}

function statusPill(value) {
  return `<span class="status-pill ${statusClass(value)}">${escapeHtml(value || '-')}</span>`;
}

function severityBadge(value) {
  const className = String(value || '').toLowerCase();
  return `<span class="severity-badge ${escapeHtml(className)}">${escapeHtml(value || '-')}</span>`;
}

function toast(message) {
  const element = document.getElementById('toast');
  element.textContent = message;
  element.classList.add('show');
  setTimeout(() => element.classList.remove('show'), 2200);
}

function loadingRow(columns) {
  return `<tr><td colspan="${columns}" class="loading">正在读取 MySQL V2…</td></tr>`;
}

function emptyRow(columns, message = '暂无数据') {
  return `<tr><td colspan="${columns}" class="empty-row">${escapeHtml(message)}</td></tr>`;
}

function errorRow(columns, error) {
  return `<tr><td colspan="${columns}" class="empty-row">加载失败：${escapeHtml(error.message)}</td></tr>`;
}

function queryString(parameters) {
  const query = new URLSearchParams();
  Object.entries(parameters).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) query.set(key, value);
  });
  return query.toString();
}

function renderPager(elementId, data, pageName) {
  const element = document.getElementById(elementId);
  const current = data.page;
  const pages = Math.max(data.pages, 1);
  element.innerHTML = `
    <button class="btn" data-page-name="${pageName}" data-page="${current - 1}" ${current <= 1 ? 'disabled' : ''}>上一页</button>
    <span>第 ${current} / ${pages} 页</span>
    <button class="btn" data-page-name="${pageName}" data-page="${current + 1}" ${current >= pages ? 'disabled' : ''}>下一页</button>`;
}

function kpi(label, value, note) {
  return `<div class="kpi-card"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b><small>${escapeHtml(note)}</small></div>`;
}

async function loadOverview() {
  const kpis = document.getElementById('overviewKpis');
  kpis.innerHTML = kpi('加载中', '…', '正在读取运行态');
  try {
    const data = await api('/api/v1/patrol/workspace/overview');
    workspaceState.overview = data;
    const summary = data.summary;
    const health = data.health;
    const stage = data.rollout.stage;
    document.getElementById('stagePill').textContent = stage;
    document.getElementById('stagePill').className = `stage-pill ${stage.toLowerCase()}`;
    document.getElementById('freshInfo').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`;
    document.getElementById('healthDot').style.background = health.status === 'HEALTHY' ? '#40ca8c' : health.status === 'DEGRADED' ? '#e4a43b' : '#e05656';
    const notice = document.getElementById('rolloutNotice');
    const enabled = data.rollout.patrol_execution_enabled;
    notice.className = `rollout-notice${enabled ? ' ready' : ''}`;
    notice.innerHTML = enabled
      ? `<b>${escapeHtml(stage)} 已允许巡检</b><span>执行范围：${data.rollout.allowlisted_shop_ids.length ? escapeHtml(data.rollout.allowlisted_shop_ids.join(', ')) : '全部店铺'}</span>`
      : '<b>当前为 INTERNAL_ONLY，只读工作台可用</b><span>人工巡检、Scheduler 和 Worker 保持关闭，不会访问 MCP。</span>';
    kpis.innerHTML = [
      kpi('巡检批次', summary.batches, 'MySQL 累计批次'),
      kpi('队列任务', summary.jobs, 'MySQL 累计 Job'),
      kpi('巡检运行', summary.runs, '已落库 Run'),
      kpi('信号总数', summary.signals, '信号当前态'),
      kpi('活动信号', summary.active_signals, '尚未终态'),
      kpi('到期复扫', summary.due_signals, '已到 next_inspection_at'),
    ].join('');
    renderHealth(data);
    await renderRecentBatches();
  } catch (error) {
    kpis.innerHTML = kpi('连接失败', '—', error.message);
    document.getElementById('freshInfo').textContent = '工作台数据不可用';
    document.getElementById('healthDot').style.background = '#e05656';
    document.getElementById('rolloutNotice').innerHTML = `<b>无法读取 MySQL V2</b><span>${escapeHtml(error.message)}</span>`;
  }
}

function renderHealth(data) {
  const health = data.health;
  const metrics = health.metrics || {};
  document.getElementById('overviewHealth').innerHTML = [
    ['总体状态', statusPill(health.status)],
    ['数据库', statusPill(health.database?.status)],
    ['待处理 Job', escapeHtml(metrics.jobs?.PENDING || 0)],
    ['死信 Job', escapeHtml(metrics.jobs?.DEAD || 0)],
    ['活动 Scheduler 租约', escapeHtml(metrics.scheduler_leases || 0)],
    ['待投递 Outbox', escapeHtml(metrics.outbox?.PENDING || 0)],
  ].map(([label, value]) => `<div class="health-row"><span>${label}</span><b>${value}</b></div>`).join('');
  document.getElementById('healthKpis').innerHTML = [
    kpi('总体状态', health.status, `检查时间 ${formatDate(health.checked_at)}`),
    kpi('数据库', health.database?.status || '-', 'MySQL V2 runtime'),
    kpi('待处理任务', metrics.jobs?.PENDING || 0, 'PENDING Job'),
    kpi('运行中任务', metrics.jobs?.RUNNING || 0, 'RUNNING Job'),
    kpi('待投递结果', metrics.outbox?.PENDING || 0, 'PENDING Outbox'),
    kpi('Scheduler 租约', metrics.scheduler_leases || 0, '有效租约'),
  ].join('');
  const alerts = health.alerts || [];
  document.getElementById('healthAlerts').innerHTML = alerts.length
    ? alerts.map(alert => `<div class="alert-item"><b>${statusPill(alert.level)} ${escapeHtml(alert.code)}</b><span>${escapeHtml(alert.message)}</span></div>`).join('')
    : '<div class="alert-empty">✓ 当前没有运行告警</div>';
}

async function renderRecentBatches() {
  const body = document.getElementById('overviewBatchBody');
  body.innerHTML = loadingRow(5);
  try {
    const data = await api('/api/v1/patrol/workspace/batches?page=1&page_size=6');
    body.innerHTML = data.items.length ? data.items.map(batch => `
      <tr><td><button class="detail-action" data-batch-id="${escapeHtml(batch.batch_id)}">${escapeHtml(batch.batch_id)}</button></td><td>${escapeHtml(batch.trigger_type)}</td><td>${statusPill(batch.status)}</td><td>${batch.succeeded_count + batch.failed_count} / ${batch.total_count}</td><td>${formatDate(batch.started_at)}</td></tr>`).join('') : emptyRow(5);
  } catch (error) {
    body.innerHTML = errorRow(5, error);
  }
}

async function loadBatches(page = workspaceState.pages.batches) {
  workspaceState.pages.batches = page;
  const body = document.getElementById('batchBody');
  body.innerHTML = loadingRow(10);
  try {
    const data = await api(`/api/v1/patrol/workspace/batches?${queryString({page, page_size: 20, status: document.getElementById('batchStatus').value})}`);
    document.getElementById('batchCount').textContent = `共 ${data.total} 个批次`;
    body.innerHTML = data.items.length ? data.items.map(batch => `
      <tr><td><button class="detail-action mono" data-batch-id="${escapeHtml(batch.batch_id)}">${escapeHtml(batch.batch_id)}</button></td><td>${escapeHtml(batch.business_date)}</td><td>${escapeHtml(batch.trigger_type)}</td><td>${statusPill(batch.status)}</td><td>${batch.total_count}</td><td>${batch.pending_count}</td><td>${batch.running_count}</td><td>${batch.succeeded_count}</td><td>${batch.failed_count}</td><td>${formatDate(batch.started_at)}</td></tr>`).join('') : emptyRow(10);
    renderPager('batchPager', data, 'batches');
  } catch (error) {
    body.innerHTML = errorRow(10, error);
  }
}

async function loadRuns(page = workspaceState.pages.runs) {
  workspaceState.pages.runs = page;
  const body = document.getElementById('runBody');
  body.innerHTML = loadingRow(9);
  try {
    const data = await api(`/api/v1/patrol/workspace/runs?${queryString({page, page_size: 20, status: document.getElementById('runStatus').value})}`);
    document.getElementById('runCount').textContent = `共 ${data.total} 次运行`;
    body.innerHTML = data.items.length ? data.items.map(run => `
      <tr><td class="mono">${escapeHtml(run.run_id)}</td><td class="mono">${escapeHtml(run.operating_unit_id)}</td><td>${escapeHtml(run.trigger_type)}</td><td>${statusPill(run.status)}</td><td>${run.signal_count}</td><td>${run.data_gap_count}</td><td>${formatDate(run.started_at)}</td><td>${formatDate(run.finished_at)}</td><td><button class="detail-action" data-run-id="${escapeHtml(run.run_id)}">查看事实</button></td></tr>`).join('') : emptyRow(9);
    renderPager('runPager', data, 'runs');
  } catch (error) {
    body.innerHTML = errorRow(9, error);
  }
}

async function loadSignals(page = workspaceState.pages.signals) {
  workspaceState.pages.signals = page;
  const body = document.getElementById('signalBody');
  body.innerHTML = loadingRow(9);
  try {
    const data = await api(`/api/v1/patrol/workspace/signals?${queryString({
      page,
      page_size: 20,
      batch_id: document.getElementById('signalBatchId').value.trim(),
      parent_asin: document.getElementById('signalParentAsin').value.trim().toUpperCase(),
      signal_type: document.getElementById('signalType').value,
      severity: document.getElementById('signalSeverity').value,
      state: document.getElementById('signalState').value,
      latest_detection_only: true,
    })}`);
    document.getElementById('signalCount').textContent = `共 ${data.total} 条当前检出`;
    body.innerHTML = data.items.length ? data.items.map(signal => `
      <tr><td>${severityBadge(signal.severity)}</td><td><b>${escapeHtml(signal.issue_code)}</b><div class="muted">${escapeHtml(signal.signal_type)}</div>${signal.child_asins?.length ? `<div class="child-scope">子 ASIN：${escapeHtml(signal.child_asins.join(', '))}</div>` : ''}${signal.child_skus?.length ? `<div class="child-scope">子 SKU：${escapeHtml(signal.child_skus.join(', '))}</div>` : ''}</td><td class="identity-cell"><b>${escapeHtml(signal.parent_asin || signal.operating_unit_id)}</b><span>${escapeHtml(signal.shop_id ?? '-')} · ${escapeHtml(signal.site_code || '-')}</span></td><td>${statusPill(signal.signal_state)}</td><td>${escapeHtml(signal.action_timing_status)}</td><td>${signal.recurrence_count}</td><td>${formatDate(signal.last_detected_at)}</td><td>${escapeHtml(signal.diagnosis_summary || '-')}</td><td><button class="detail-action" data-signal-id="${escapeHtml(signal.signal_id)}">查看生命周期</button></td></tr>`).join('') : emptyRow(9);
    renderPager('signalPager', data, 'signals');
  } catch (error) {
    body.innerHTML = errorRow(9, error);
  }
}

async function showBatch(batchId) {
  openDrawer('BATCH', batchId, '正在读取批次任务…');
  try {
    const data = await api(`/api/v1/patrol/workspace/jobs?${queryString({page: 1, page_size: 100, batch_id: batchId})}`);
    document.getElementById('drawerBody').innerHTML = `<section class="detail-section"><h3 class="detail-section-action"><span>任务列表 · ${data.total}</span><button class="btn" data-batch-signals="${escapeHtml(batchId)}">查看本批异常</button></h3>${data.items.length ? data.items.map(job => `<div class="fact-row"><div class="fact-row-head"><b>${escapeHtml(job.parent_asin)} · ${escapeHtml(job.shop_id)} / ${escapeHtml(job.site_code)}</b>${statusPill(job.status)}</div><small>${escapeHtml(job.job_id)}<br>${escapeHtml(job.operating_unit_id)}${job.last_error_message ? `<br>失败：${escapeHtml(job.last_error_message)}` : ''}</small></div>`).join('') : '<div class="alert-empty">暂无任务</div>'}</section>`;
  } catch (error) {
    document.getElementById('drawerBody').innerHTML = `<div class="alert-item"><b>加载失败</b><span>${escapeHtml(error.message)}</span></div>`;
  }
}

async function showRun(runId) {
  openDrawer('RUN FACTS', runId, '仅展示事实元数据，不展示原始 MCP Payload');
  try {
    const [run, facts] = await Promise.all([
      api(`/api/v1/patrol/runs/${encodeURIComponent(runId)}`),
      api(`/api/v1/patrol/workspace/runs/${encodeURIComponent(runId)}/facts`),
    ]);
    const fields = [['状态', statusPill(run.status)], ['经营单元', escapeHtml(run.operating_unit_id)], ['信号数', run.signal_count], ['数据缺口', run.data_gap_count], ['开始时间', formatDate(run.started_at)], ['完成时间', formatDate(run.finished_at)]];
    document.getElementById('drawerBody').innerHTML = `<section class="detail-section"><h3>运行摘要</h3><div class="detail-grid">${fields.map(([label, value]) => `<div class="detail-field"><span>${label}</span><b>${value}</b></div>`).join('')}</div></section><section class="detail-section"><h3>事实工具 · ${facts.fact_count}</h3>${facts.facts.length ? facts.facts.map(fact => `<div class="fact-row"><div class="fact-row-head"><b>${escapeHtml(fact.fact_key)} · ${escapeHtml(fact.tool_name)}</b>${statusPill(fact.status)}</div><small>行数 ${escapeHtml(fact.row_count ?? '-')} · 耗时 ${escapeHtml(fact.latency_ms ?? '-')} ms · 核心事实 ${fact.is_core ? '是' : '否'}<br>${escapeHtml(fact.response_content_hash || fact.request_hash)}${fact.error_message ? `<br>错误：${escapeHtml(fact.error_message)}` : ''}</small></div>`).join('') : '<div class="alert-empty">该 Run 没有归档事实</div>'}</section>`;
  } catch (error) {
    document.getElementById('drawerBody').innerHTML = `<div class="alert-item"><b>加载失败</b><span>${escapeHtml(error.message)}</span></div>`;
  }
}

async function showSignal(signalId) {
  openDrawer('SIGNAL', signalId, '当前态与不可变 Occurrence 历史');
  try {
    const data = await api(`/api/v1/patrol/signals/${encodeURIComponent(signalId)}`);
    const signal = data.signal;
    const fields = [['严重度', severityBadge(signal.severity)], ['生命周期', statusPill(signal.signal_state)], ['问题代码', escapeHtml(signal.issue_code)], ['版本', escapeHtml(signal.version)], ['复发次数', escapeHtml(signal.recurrence_count)], ['下次复扫', formatDate(signal.next_inspection_at)]];
    document.getElementById('drawerBody').innerHTML = `<section class="detail-section"><h3>信号当前态</h3><div class="detail-grid">${fields.map(([label, value]) => `<div class="detail-field"><span>${label}</span><b>${value}</b></div>`).join('')}</div></section><section class="detail-section"><h3>生命周期 · ${data.occurrences.length}</h3>${data.occurrences.length ? data.occurrences.map(item => `<div class="fact-row"><div class="fact-row-head"><b>${escapeHtml(item.occurrence_type)}</b>${item.severity ? severityBadge(item.severity) : ''}</div><small>${formatDate(item.occurred_at)} · Run ${escapeHtml(item.run_id)}</small></div>`).join('') : '<div class="alert-empty">暂无生命周期记录</div>'}</section>`;
  } catch (error) {
    document.getElementById('drawerBody').innerHTML = `<div class="alert-item"><b>加载失败</b><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function openDrawer(eyebrow, title, subtitle) {
  document.getElementById('drawerEyebrow').textContent = eyebrow;
  document.getElementById('drawerTitle').textContent = title;
  document.getElementById('drawerSubtitle').textContent = subtitle;
  document.getElementById('drawerBody').innerHTML = '<div class="loading">加载中…</div>';
  document.getElementById('detailDrawer').classList.add('show');
}

function switchView(viewName) {
  document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.view === viewName));
  document.querySelectorAll('.v2-main>.view').forEach(view => view.classList.toggle('active', view.id === `view${viewName[0].toUpperCase()}${viewName.slice(1)}`));
  location.hash = viewName === 'overview' ? '' : viewName;
  if (viewName === 'batches') loadBatches();
  if (viewName === 'runs') loadRuns();
  if (viewName === 'signals') loadSignals();
  if (viewName === 'health' && workspaceState.overview) renderHealth(workspaceState.overview);
}

document.addEventListener('click', event => {
  const nav = event.target.closest('.nav-item');
  if (nav) switchView(nav.dataset.view);
  const goto = event.target.closest('[data-goto]');
  if (goto) switchView(goto.dataset.goto);
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (action === 'refresh') loadOverview().then(() => toast('数据已刷新'));
  if (action === 'load-batches') loadBatches(1);
  if (action === 'load-runs') loadRuns(1);
  if (action === 'load-signals') loadSignals(1);
  if (action === 'clear-signals') {
    ['signalBatchId', 'signalParentAsin', 'signalType', 'signalSeverity', 'signalState'].forEach(id => {
      document.getElementById(id).value = '';
    });
    loadSignals(1);
  }
  const pageButton = event.target.closest('[data-page-name]');
  if (pageButton && !pageButton.disabled) {
    const page = Number(pageButton.dataset.page);
    if (pageButton.dataset.pageName === 'batches') loadBatches(page);
    if (pageButton.dataset.pageName === 'runs') loadRuns(page);
    if (pageButton.dataset.pageName === 'signals') loadSignals(page);
  }
  const batch = event.target.closest('[data-batch-id]');
  if (batch) showBatch(batch.dataset.batchId);
  const run = event.target.closest('[data-run-id]');
  if (run) showRun(run.dataset.runId);
  const signal = event.target.closest('[data-signal-id]');
  if (signal) showSignal(signal.dataset.signalId);
  const batchSignals = event.target.closest('[data-batch-signals]');
  if (batchSignals) {
    document.getElementById('signalBatchId').value = batchSignals.dataset.batchSignals;
    document.getElementById('signalParentAsin').value = '';
    document.getElementById('detailDrawer').classList.remove('show');
    switchView('signals');
  }
});

document.getElementById('globalRefresh').addEventListener('click', () => loadOverview().then(() => toast('数据已刷新')));
document.getElementById('drawerClose').addEventListener('click', () => document.getElementById('detailDrawer').classList.remove('show'));
document.getElementById('detailDrawer').addEventListener('click', event => {
  if (event.target.id === 'detailDrawer') event.currentTarget.classList.remove('show');
});

const initialView = location.hash.slice(1);
if (['batches', 'runs', 'signals', 'health'].includes(initialView)) switchView(initialView);
loadOverview();
