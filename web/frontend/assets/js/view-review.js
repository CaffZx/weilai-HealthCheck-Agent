// ============================================================
// 视图3：调整复盘 / 完成执行后的效果观察
// ============================================================
async function loadReview(){
  document.getElementById('reviewBody').innerHTML = '<tr><td colspan="9" class="loading">Agent 正在读取观察数据…</td></tr>';
  try {
    state.reviewData = await api(withTarget('/api/review/observations'));
    renderReview();
  } catch(e){
    document.getElementById('reviewBody').innerHTML = `<tr><td colspan="9" class="loading">加载失败：${esc(e.message)}</td></tr>`;
  }
}

function observationEffectClass(effect){
  if (effect === '变好') return 'good';
  if (effect === '变差') return 'bad';
  if (effect === '数据不足') return 'warn';
  return 'watch';
}

function observationPhaseClass(phase){
  if (phase === '待运营确认') return 'good';
  if (phase === '等待巡检') return 'warn';
  return 'watch';
}

function observationPhaseHtml(record){
  const phase = record.observation_phase || '观察中';
  let note = '';
  if (phase === '观察中') note = `还有 ${Number(record.days_until_observation || 0)} 天到观察节点`;
  else if (phase === '等待巡检') note = '巡检完成后自动判断';
  else note = 'Agent 已完成效果判断';
  return `<span class="observation-effect ${observationPhaseClass(phase)}">${esc(phase)}</span><small class="observation-confidence">${esc(note)}</small>`;
}

function observationDetailsHtml(record){
  let report = {};
  try { report = typeof record.report_json === 'string' ? JSON.parse(record.report_json || '{}') : (record.report_json || {}); } catch (_) {}
  const details = report.details || [];
  if (!details.length) return '<div class="observation-empty">暂无可比较的异常指标</div>';
  return `<div class="observation-detail-list">${details.map(detail => `
    <div class="observation-detail-row">
      <div class="observation-detail-title"><span class="severity-badge ${String(detail.severity || 'S2').toLowerCase()}">${esc(detail.severity || 'S2')}</span><b>${esc(detail.issue || '异常')}</b><span class="observation-effect ${observationEffectClass(detail.effect)}">${esc(detail.effect || '数据不足')}</span></div>
      <div class="observation-detail-reason">${esc(detail.reason || '暂无判断依据')}</div>
      <div class="observation-metrics"><span>执行前：${esc(formatObservationMetrics(detail.baseline))}</span><span>观察期：${esc(formatObservationMetrics(detail.current))}</span></div>
    </div>
  `).join('')}</div>`;
}

function formatObservationMetrics(metrics){
  if (!metrics || !Object.keys(metrics).length) return '暂无数据';
  return Object.entries(metrics).slice(0, 3).map(([label, value]) => `${label} ${Number.isFinite(Number(value)) ? Number(value).toFixed(2).replace(/\.00$/, '') : value}`).join(' · ');
}

function confirmObservation(id, confirmation){
  const labels = { '保留当前动作':'确认保留当前动作？Agent 判断会保留该产品维护结果。', '继续观察':'确认继续观察？系统会安排下一轮观察。', '重新处理':'确认重新处理？该产品异常会重新进入任务池。' };
  if (!window.confirm(labels[confirmation])) return;
  api(`/api/review/observations/${id}/confirm`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({userId: state.viewerId, targetId: state.targetId, confirmation}),
  }).then(() => {
    toast(`已确认：${confirmation}`);
    state.todayData = null;
    loadReview();
  }).catch(e => toast(`确认失败：${e.message}`));
}

function observationActionsHtml(record){
  const allowed = record.allowed_confirmations || [];
  if (!record.can_confirm) return '<span class="observation-action-hint">等待 Agent 完成观察</span>';
  const buttons = [];
  if (allowed.includes('保留当前动作')) buttons.push(`<button class="concl-btn" onclick="confirmObservation(${record.maintenance_id}, '保留当前动作')">保留动作</button>`);
  if (allowed.includes('继续观察')) buttons.push(`<button class="concl-btn" onclick="confirmObservation(${record.maintenance_id}, '继续观察')">继续观察</button>`);
  if (allowed.includes('重新处理')) buttons.push(`<button class="concl-btn danger" onclick="confirmObservation(${record.maintenance_id}, '重新处理')">重新处理</button>`);
  return `<div class="review-concl-btns">${buttons.join('')}</div>`;
}

function renderReview(){
  const records = state.reviewData?.records || [];
  const observing = Number(state.reviewData?.observing_count || 0);
  const waitingInspection = Number(state.reviewData?.waiting_inspection_count || 0);
  const confirmation = Number(state.reviewData?.confirmation_count || 0);
  const improved = records.filter(r => r.observation_phase === '待运营确认' && r.agent_effect === '变好').length;
  document.getElementById('reviewKpi').innerHTML = `
    <div class="kpi-card"><span>观察中</span><b>${observing}</b><small>已完成维护，等待观察节点</small></div>
    <div class="kpi-card warning-card"><span>等待巡检</span><b>${waitingInspection}</b><small>到期后等待新巡检数据</small></div>
    <div class="kpi-card good-card"><span>待运营确认</span><b>${confirmation}</b><small>Agent 已完成效果判断</small></div>
    <div class="kpi-card"><span>Agent 判断改善</span><b>${improved}</b><small>建议确认保留当前动作</small></div>
  `;
  document.getElementById('reviewResultCount').textContent = records.length;
  document.getElementById('reviewBody').innerHTML = records.length ? records.map(r => `
    <tr class="observation-row">
      <td><div class="mini-product"><b>${esc(r.parent_asin || '-')}</b><span>${esc(r.shop_account || '-')}</span></div></td>
      <td>${fmtDate(r.executed_at)}</td>
      <td>${fmtDate(r.observation_at)}</td>
      <td>${fmtDate(r.next_inspection_at)}</td>
      <td>${observationPhaseHtml(r)}</td>
      <td><span class="observation-effect ${observationEffectClass(r.agent_effect)}">${esc(r.agent_effect || '尚未判断')}</span>${r.can_confirm ? `<small class="observation-confidence">置信度 ${Math.round(Number(r.confidence || 0) * 100)}%</small>` : ''}</td>
      <td><div class="observation-summary">${esc(r.summary || '暂无 Agent 结论')}</div></td>
      <td>${observationDetailsHtml(r)}</td>
      <td>${observationActionsHtml(r)}</td>
    </tr>
  `).join('') : '<tr class="empty-row"><td colspan="9">当前没有进行中的产品效果观察</td></tr>';
}

document.getElementById('reviewReloadBtn').onclick = () => loadReview();
