// ============================================================
// 视图3：调整复盘 / 完成执行后的效果观察
// ============================================================
async function loadReview(){
  document.getElementById('reviewBody').innerHTML = '<tr><td colspan="5" class="loading">Agent 正在读取观察数据…</td></tr>';
  try {
    const query = new URLSearchParams();
    const focus = state.reviewFocus;
    if (focus) {
      const search = document.getElementById('reviewSearch');
      if (search) search.value = focus.parent_asin || '';
      ['reviewShop', 'reviewPhase', 'reviewEffect', 'reviewSeverity', 'reviewFrom', 'reviewTo'].forEach(id => {
        const control = document.getElementById(id);
        if (control) control.value = '';
      });
      state.reviewFocus = null;
    }
    const controls = {
      q: document.getElementById('reviewSearch')?.value.trim(),
      shop: document.getElementById('reviewShop')?.value,
      phase: document.getElementById('reviewPhase')?.value,
      effect: document.getElementById('reviewEffect')?.value,
      severity: document.getElementById('reviewSeverity')?.value,
      review_from: document.getElementById('reviewFrom')?.value,
      review_to: document.getElementById('reviewTo')?.value,
    };
    Object.entries(controls).forEach(([key, value]) => { if (value) query.set(key, value); });
    state.reviewData = await api(withTarget('/api/review/observations') + (query.size ? `&${query}` : ''));
    renderReview();
    if (focus) focusReviewRecord(focus);
  } catch(e){
    document.getElementById('reviewBody').innerHTML = `<tr><td colspan="5" class="loading">加载失败：${esc(e.message)}</td></tr>`;
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
  const schedule = record.schedule_source === 'manual'
    ? '运营改期'
    : (record.follow_up_type || record.schedule_rule_id || '历史计划');
  return `<span class="observation-effect ${observationPhaseClass(phase)}">${esc(phase)}</span><small class="observation-confidence">${esc(schedule)} · ${esc(note)}</small>`;
}

function observationDetailsHtml(record){
  let report = {};
  try { report = typeof record.report_json === 'string' ? JSON.parse(record.report_json || '{}') : (record.report_json || {}); } catch (_) {}
  const details = report.details || [];
  if (!details.length) return '<div class="observation-empty">等待可比较指标</div>';
  return `<div class="observation-detail-list">${details.map(detail => `
    <div class="observation-detail-row compact">
      <div class="observation-detail-title"><b>${esc(detail.issue || '异常')}</b><span class="observation-effect ${observationEffectClass(detail.effect)}">${esc(detail.effect || '数据不足')}</span></div>
      <div class="observation-metrics"><span>执行前 ${esc(formatObservationMetrics(detail.baseline))}</span><span>观察期 ${esc(formatObservationMetrics(detail.current))}</span></div>
    </div>
  `).join('')}</div>`;
}

function formatObservationMetrics(metrics){
  if (!metrics || !Object.keys(metrics).length) return '暂无数据';
  return Object.entries(metrics).slice(0, 3).map(([label, value]) => `${label} ${Number.isFinite(Number(value)) ? Number(value).toFixed(2).replace(/\.00$/, '') : value}`).join(' · ');
}

async function confirmObservation(id, confirmation){
  const labels = { '保留当前动作':'确认保留当前动作？Agent 判断会保留该产品维护结果。', '继续观察':'确认继续观察？系统会安排下一轮观察。', '重新处理':'确认重新处理？该产品异常会重新进入任务池。' };
  if (!window.confirm(labels[confirmation])) return;
  const scroll = document.querySelector('.review-table-scroll');
  const scrollTop = scroll?.scrollTop || 0;
  const buttons = document.querySelectorAll(`[data-observation-action="${String(id)}"]`);
  buttons.forEach(button => { button.disabled = true; });
  try {
    const result = await api(`/api/review/observations/${id}/confirm`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({userId: state.viewerId, targetId: state.targetId, confirmation}),
    });
    const records = state.reviewData?.records || [];
    const index = records.findIndex(record => Number(record.observation_id || record.maintenance_id) === Number(id));
    if (index >= 0) {
      if (confirmation === '继续观察') {
        Object.assign(records[index], {
          observation_phase: '观察中', agent_status: '待观察', agent_effect: null,
          summary: `已确认继续观察，下一观察节点：${result.observation_at || '-'}`,
          report_json: '{}', can_confirm: false, allowed_confirmations: [],
          observation_at: result.observation_at, next_inspection_at: result.next_inspection_at,
        });
      } else {
        records.splice(index, 1);
      }
    }
    toast(`已确认：${confirmation}`);
    renderReview();
    if (scroll) scroll.scrollTop = Math.min(scrollTop, Math.max(0, scroll.scrollHeight - scroll.clientHeight));
  } catch(e) {
    toast(`确认失败：${e.message}`);
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

function observationActionsHtml(record){
  const allowed = record.allowed_confirmations || [];
  if (!record.can_confirm) return '<span class="observation-action-hint">等待 Agent 完成观察</span>';
  const buttons = [];
  const id = record.observation_id || record.maintenance_id;
  if (allowed.includes('保留当前动作')) buttons.push(`<button class="concl-btn" type="button" data-observation-action="${esc(id)}" data-confirmation="保留当前动作">完成处理</button>`);
  if (allowed.includes('继续观察')) buttons.push(`<button class="concl-btn" type="button" data-observation-action="${esc(id)}" data-confirmation="继续观察">继续观察</button>`);
  if (allowed.includes('重新处理')) buttons.push(`<button class="concl-btn danger" type="button" data-observation-action="${esc(id)}" data-confirmation="重新处理">重新处理</button>`);
  return `<div class="review-concl-btns">${buttons.join('')}</div>`;
}

function reviewProductImageHtml(record){
  if (!record.image_url) return '<span class="review-product-image image-fallback" role="img" aria-label="暂无产品主图">📦</span>';
  return `<span class="review-product-image"><img src="${esc(record.image_url)}" alt="产品主图" onerror="this.style.display='none';this.parentElement.classList.add('missing')"></span>`;
}

function syncReviewShopOptions(records){
  const select = document.getElementById('reviewShop');
  if (!select) return;
  const selected = select.value;
  const shops = [...new Set(records.map(record => record.shop_account).filter(Boolean))].sort();
  const current = [...select.options].slice(1).map(option => option.value);
  if (shops.join('\u0000') === current.join('\u0000')) return;
  select.innerHTML = `<option value="">全部店铺</option>${shops.map(shop => `<option value="${esc(shop)}">${esc(shop)}</option>`).join('')}`;
  select.value = shops.includes(selected) ? selected : '';
}

function reviewProductGroups(records){
  const groups = new Map();
  records.forEach(record => {
    const key = `${record.parent_asin || ''}__${record.shop_account || ''}`;
    if (!groups.has(key)) groups.set(key, {record, cases: []});
    groups.get(key).cases.push(record);
  });
  return [...groups.values()];
}

function productSummaryHtml(group){
  const record = group.record;
  const phases = group.cases.reduce((counts, item) => {
    const phase = item.observation_phase || '观察中';
    counts[phase] = (counts[phase] || 0) + 1;
    return counts;
  }, {});
  const phaseSummary = Object.entries(phases).map(([phase, count]) => `${phase} ${count}`).join(' · ');
  const nextReview = [...group.cases].map(item => item.observation_at).filter(Boolean).sort()[0] || '-';
  return `<tr class="review-product-summary"><td colspan="5"><div class="review-product-summary-content">
    ${reviewProductImageHtml(record)}
    <div class="review-product-summary-copy"><b>${esc(record.product_name || record.parent_asin || '-')}</b><span>父 ASIN：${esc(record.parent_asin || '-')} · 店铺：${esc(record.shop_account || '-')}</span></div>
    <div class="review-product-summary-facts"><span><small>异常观察</small><b>${group.cases.length} 条</b></span><span><small>当前分布</small><b>${esc(phaseSummary)}</b></span><span><small>最近观察节点</small><b>${esc(nextReview)}</b></span></div>
  </div></td></tr>`;
}

function reviewShortDate(value){
  const parts = String(value || '').slice(0, 10).split('-');
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : '-';
}

function observationProgressHtml(record){
  const parts = [`执行 ${reviewShortDate(record.executed_at)}`, `观察 ${reviewShortDate(record.observation_at)}`];
  if (record.next_inspection_at && record.next_inspection_at !== record.observation_at) {
    parts.push(`巡检 ${reviewShortDate(record.next_inspection_at)}`);
  }
  return `<span class="review-progress">${esc(parts.join(' · '))}</span>`;
}

function observationConclusionHtml(record){
  const effect = record.agent_effect || '尚未判断';
  const confidence = record.can_confirm ? ` · 置信度 ${Math.round(Number(record.confidence || 0) * 100)}%` : '';
  return `<div class="review-conclusion"><div><span class="observation-effect ${observationPhaseClass(record.observation_phase)}">${esc(record.observation_phase || '观察中')}</span><span class="observation-effect ${observationEffectClass(record.agent_effect)}">${esc(effect)}</span></div><span>${esc(record.summary || '等待观察期结束后由 Agent 判断')}${confidence}</span></div>`;
}

function renderReview(){
  const records = [...(state.reviewData?.records || [])].sort((left, right) =>
    `${left.parent_asin || ''}\u0000${left.shop_account || ''}\u0000${left.observation_at || ''}`
      .localeCompare(`${right.parent_asin || ''}\u0000${right.shop_account || ''}\u0000${right.observation_at || ''}`, 'zh-CN'));
  const observing = Number(state.reviewData?.observing_count || 0);
  const waitingInspection = Number(state.reviewData?.waiting_inspection_count || 0);
  const confirmation = Number(state.reviewData?.confirmation_count || 0);
  const improved = records.filter(r => r.observation_phase === '待运营确认' && r.agent_effect === '变好').length;
  syncReviewShopOptions(records);
  document.getElementById('reviewKpi').innerHTML = `
    <div class="review-kpi-item"><span>观察中</span><b>${observing}</b><small>等待观察节点</small></div>
    <div class="review-kpi-item warning"><span>等待巡检</span><b>${waitingInspection}</b><small>等待巡检数据</small></div>
    <div class="review-kpi-item good"><span>待运营确认</span><b>${confirmation}</b><small>Agent 已有结论</small></div>
    <div class="review-kpi-item"><span>判断改善</span><b>${improved}</b><small>可完成处理</small></div>
  `;
  const groups = reviewProductGroups(records);
  document.getElementById('reviewResultCount').textContent = `${groups.length} 个产品 · ${records.length} 条异常`;
  document.getElementById('reviewBody').innerHTML = records.length ? groups.map(group => `${productSummaryHtml(group)}${group.cases.map(r => `
    <tr class="observation-row" data-review-product="${esc(`${r.parent_asin || ''}__${r.shop_account || ''}`)}" data-review-event-uid="${esc(r.event_uid || '')}" data-review-issue="${esc(r.issue || '')}">
      <td><div class="review-anomaly-info"><span class="severity-badge ${String(r.severity || 'S2').toLowerCase()}">${esc(r.severity || 'S2')}</span><b>${esc(r.issue || '异常')}</b></div></td>
      <td>${observationProgressHtml(r)}</td>
      <td>${observationConclusionHtml(r)}</td>
      <td>${observationDetailsHtml(r)}</td>
      <td>${observationActionsHtml(r)}</td>
    </tr>`).join('')}`).join('') : '<tr class="empty-row"><td colspan="5">当前没有进行中的异常效果观察</td></tr>';
  document.querySelectorAll('[data-observation-action]').forEach(button => {
    button.onclick = () => confirmObservation(button.dataset.observationAction, button.dataset.confirmation);
  });
}

function focusReviewRecord(focus){
  const eventUids = new Set(focus.event_uids || []);
  const row = [...document.querySelectorAll('[data-review-issue]')].find(item =>
      item.dataset.reviewProduct === `${focus.parent_asin || ''}__${focus.shop_account || ''}`
      && focus.issue && item.dataset.reviewIssue === focus.issue)
    || [...document.querySelectorAll('[data-review-event-uid]')].find(item => eventUids.has(item.dataset.reviewEventUid))
    || document.querySelector(`[data-review-product="${CSS.escape(`${focus.parent_asin || ''}__${focus.shop_account || ''}`)}"]`);
  if (!row) return;
  row.scrollIntoView({block: 'center', behavior: 'smooth'});
  row.classList.add('review-focus-row');
  setTimeout(() => row.classList.remove('review-focus-row'), 5000);
}

document.getElementById('reviewReloadBtn').onclick = () => loadReview();
['reviewShop', 'reviewPhase', 'reviewEffect', 'reviewSeverity', 'reviewFrom', 'reviewTo'].forEach(id => {
  document.getElementById(id).onchange = () => loadReview();
});
document.getElementById('reviewSearch').oninput = (() => {
  let timer;
  return () => { clearTimeout(timer); timer = setTimeout(loadReview, 220); };
})();
