// ============================================================
// 视图5：处理记录
// ============================================================
async function loadHistory(){
  document.getElementById('historyBody').innerHTML = '<tr><td colspan="10" class="loading">加载中…</td></tr>';
  try {
    const q = document.getElementById('historySearch').value || '';
    const r = document.getElementById('historyRange').value || 'all';
    const s = document.getElementById('historyStatus').value || '';
    const ev = document.getElementById('historyEvent').value || '';
    const url = withTarget('/api/history') + `&range=${r}` + (q?`&q=${encodeURIComponent(q)}`:'') + (s?`&status=${encodeURIComponent(s)}`:'') + (ev?`&event=${ev}`:'');
    state.historyData = await api(url);
    renderHistory();
  } catch(e){
    document.getElementById('historyBody').innerHTML = `<tr><td colspan="10" class="loading">加载失败：${esc(e.message)}</td></tr>`;
  }
}
function renderHistory(){
  const d = state.historyData; if (!d) return;
  const rows = groupHistoryByProduct(d.records || []);
  document.getElementById('historyResultCount').textContent = rows.length;
  const k = d.kpi;
  document.getElementById('historyKpi').innerHTML = `
    <div class="kpi-card good-card"><span>完整处理凭证</span><b>${k.proof}</b><small>按单次操作统计</small></div>
    <div class="kpi-card"><span>涉及产品</span><b>${k.products}</b><small>按父 ASIN + 店铺去重</small></div>
    <div class="kpi-card danger-card"><span>处理后再次异常</span><b>${k.with_new_event}</b><small>新事件与原记录并存</small></div>
    <div class="kpi-card warning-card"><span>待补处理凭证</span><b>${k.pending}</b><small>结果或动作未填</small></div>
  `;
  if (!rows.length){
    document.getElementById('historyBody').innerHTML = '<tr class="empty-row"><td colspan="10">无匹配的处理记录</td></tr>';
    return;
  }
  document.getElementById('historyBody').innerHTML = rows.map(r => `
    <tr data-product-key="${esc(r.product_key)}">
      <td><span class="history-id">${esc(r.record_count)} 次处理</span><br><small>${esc(r.record_scope_label)}</small></td>
      <td>${esc((r.time||'').slice(0,16))}</td>
      <td><div class="mini-product history-product">${historyProductImageHtml(r.image_url)}<div class="mini-product-copy"><b>${esc(r.product_name || r.parent_asin)}</b><span>${esc(r.parent_asin)} · ${esc(r.shop_account||'-')}</span></div></div></td>
      <td><div class="mini-product"><b>${esc(r.issues.join('、'))}</b><span>覆盖 ${r.event_count} 个异常事件 · ${r.record_count} 条记录</span></div></td>
      <td>${esc(r.actual_action||'-')}${r.action_class?`<br><span style="color:#8a93a2;font-size:10px">最新类型：${esc(r.action_class)}</span>`:''}</td>
      <td>${esc(r.owners.join('、') || '-')}</td>
      <td><span class="effect-pill ${r.review==='已恢复'?'good':r.review==='未恢复'?'bad':'watch'}">${esc(r.review)}</span>${r.review_summary ? `<br><small class="history-summary-note">${esc(r.review_summary)}</small>` : ''}</td>
      <td><span class="proof-pill ${r.has_incomplete?'warn':''}">${r.complete==='完整'?'✓ ':''}${esc(r.complete)}</span></td>
      <td>${r.new_events.length ? `<span class="new-event-pill">${esc(r.new_events.map(event => event.issue || '新异常').join('、'))}</span>` : '—'}</td>
      <td><button class="btn" style="padding:0 8px;height:26px;font-size:11px" data-open-product="${esc(r.product_key)}">查看记录</button></td>
    </tr>
  `).join('');
  document.querySelectorAll('#historyBody button[data-open-product]').forEach(b => {
    b.onclick = () => openProductHistoryDrawer(b.dataset.openProduct);
  });
}
function groupHistoryByProduct(records){
  const groups = new Map();
  records.forEach(record => {
    const productKey = `${record.parent_asin || ''}\u001f${record.shop_account || ''}`;
    const group = groups.get(productKey) || {
      product_key: productKey,
      parent_asin: record.parent_asin,
      shop_account: record.shop_account,
      product_name: record.product_name,
      image_url: record.image_url,
      records: [],
    };
    group.records.push(record);
    if (!group.product_name && record.product_name) group.product_name = record.product_name;
    if (!group.image_url && record.image_url) group.image_url = record.image_url;
    groups.set(productKey, group);
  });
  return [...groups.values()].map(group => {
    const recordsForProduct = group.records.sort((left, right) => (right.time || '').localeCompare(left.time || ''));
    const latest = recordsForProduct[0];
    const issues = [...new Set(recordsForProduct.flatMap(record => record.issues || [record.issue || '异常']))];
    const eventUids = new Set(recordsForProduct.flatMap(record => record.event_uids || [record.event_uid]));
    const owners = [...new Set(recordsForProduct.map(record => record.owner).filter(Boolean))];
    const newEvents = [...new Map(recordsForProduct.filter(record => record.new_event).map(record => [record.new_event.event_uid, record.new_event])).values()];
    const reviewCounts = recordsForProduct.reduce((counts, record) => {
      counts[record.review] = (counts[record.review] || 0) + 1;
      return counts;
    }, {});
    const pendingCount = recordsForProduct.filter(record => record.complete === '待补').length;
    return {
      ...group,
      ...latest,
      records: recordsForProduct,
      record_count: recordsForProduct.length,
      record_scope_label: recordsForProduct.length === 1
        ? (latest.record_scope === 'product' ? '产品维护记录' : '单异常记录')
        : '多次操作汇总',
      issues,
      event_count: eventUids.size,
      owners,
      new_events: newEvents,
      complete: pendingCount ? `待补 ${pendingCount} 条` : '完整',
      has_incomplete: pendingCount > 0,
      review_summary: Object.entries(reviewCounts).map(([review, count]) => `${review} ${count}`).join(' · '),
    };
  }).sort((left, right) => (right.time || '').localeCompare(left.time || ''));
}
function historyProductImageHtml(imageUrl){
  return imageUrl
    ? `<span class="history-product-image"><img src="${esc(imageUrl)}" alt="产品主图" onerror="this.onerror=null;this.style.display='none';this.parentElement.classList.add('missing')"></span>`
    : '<span class="history-product-image missing" role="img" aria-label="暂无产品主图">📦</span>';
}
function openProductHistoryDrawer(productKey){
  const group = groupHistoryByProduct(state.historyData?.records || []).find(item => item.product_key === productKey);
  if (!group) return;
  const drawer = document.getElementById('historyDrawer');
  state._currentHistoryProductKey = productKey;
  state._currentRecord = null;
  setHistoryDrawerLevel('product');
  document.getElementById('drawerTitle').textContent = '产品处理记录';
  document.getElementById('drawerSubtitle').textContent = `${group.parent_asin} · ${group.shop_account || ''} · 共 ${group.record_count} 次操作`;
  document.getElementById('drawerBody').innerHTML = `
    <section class="proof-section">
      <div class="history-product-drawer-head">${historyProductImageHtml(group.image_url)}<div><b>${esc(group.product_name || group.parent_asin)}</b><span>${esc(group.parent_asin)} · ${esc(group.shop_account || '-')}</span><small>覆盖 ${group.event_count} 个异常事件 · ${group.record_count} 条处理记录</small></div></div>
    </section>
    <section class="proof-section">
      <div class="proof-section-head"><b>操作记录</b><span>按最近操作时间排序</span></div>
      <div class="history-record-list">${group.records.map(record => `
        <button class="history-record-item" data-open-record="${esc(record.record_id)}">
          <div><b>${esc(record.record_id)} · ${esc((record.time || '').slice(0, 16))}</b><span>${esc((record.issues || [record.issue || '异常']).join('、'))} · ${esc(record.owner || '-')}</span></div>
          <div><em class="effect-pill ${record.review==='已恢复'?'good':record.review==='未恢复'?'bad':'watch'}">${esc(record.review)}</em><small>${esc(record.actual_action || '待补动作')}</small></div>
        </button>
      `).join('')}</div>
    </section>
  `;
  drawer.classList.add('show');
  document.querySelectorAll('#drawerBody button[data-open-record]').forEach(button => {
    button.onclick = () => openHistoryDrawer(button.dataset.openRecord);
  });
}
async function openHistoryDrawer(recordId){
  const drawer = document.getElementById('historyDrawer');
  document.getElementById('drawerBody').innerHTML = '<div class="loading">加载中…</div>';
  drawer.classList.add('show');
  try {
    const r = await api(withTarget(`/api/history/${recordId}`));
    state._currentRecord = r;
    state._currentHistoryProductKey = `${r.parent_asin || ''}\u001f${r.shop_account || ''}`;
    setHistoryDrawerLevel('record');
    document.getElementById('drawerTitle').textContent = `处理记录 · ${r.record_id}`;
    document.getElementById('drawerSubtitle').textContent = `${r.record_scope === 'product' ? `覆盖 ${r.event_count || 0} 项异常` : r.event_id} · ${r.parent_asin} · ${r.shop_account||''}`;
    const before = (state.todayData?.events || []).find(e => e.event_uid === r.event_uid);
    const beforeMetrics = r.timeline.find(t => t.before_metrics)?.before_metrics;
    let snapshotHtml = '';
    if (beforeMetrics){
      try {
        const bm = JSON.parse(beforeMetrics);
        snapshotHtml = Object.entries(bm).map(([k,v]) => `<div class="proof-field"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`).join('');
      } catch(e){}
    }
    document.getElementById('drawerBody').innerHTML = `
      <div class="proof-banner"><i>✓</i><div><b>${r.complete === '完整' ? '已形成可追溯处理凭证' : '记录不完整，请补齐'}</b><span>点击完成时间、操作人、实际动作、复查节点均已保存</span></div></div>
      <section class="proof-section">
        <div class="proof-section-head"><b>基础信息</b><span>${r.complete === '完整' ? '记录完整' : '存在待补字段'}</span></div>
        <div class="proof-grid">
          <div class="proof-field"><span>动作记录ID</span><b>${esc(r.record_id)}</b></div>
          <div class="proof-field"><span>${r.record_scope === 'product' ? '覆盖异常' : '异常事件ID'}</span><b>${esc(r.record_scope === 'product' ? (r.issues || []).join('、') : r.event_id)}</b></div>
          <div class="proof-field"><span>产品</span><b>${esc(r.product_name || r.parent_asin)}<br>${esc(r.parent_asin)} · ${esc(r.parent_sku||'-')}</b></div>
          <div class="proof-field"><span>优先级 · 执行分</span><b>${esc(r.priority)} · ${Math.round(r.score)}/100</b></div>
          <div class="proof-field"><span>点击完成时间</span><b>${esc(r.time)}</b></div>
          <div class="proof-field"><span>巡检时间</span><b>${esc(r.inspection_time || '未关联')}</b></div>
          <div class="proof-field"><span>操作人</span><b>${esc(r.owner)}</b></div>
          <div class="proof-field"><span>处理结果</span><b>${esc(r.result||'待补')}</b></div>
          <div class="proof-field"><span>观察节点</span><b>${esc(r.review_at||'未设置')}</b></div>
          <div class="proof-field"><span>SLA</span><b>${r.on_time ? '✓ 按时' : '⚠ 超时'} · 处理耗时 ${r.handle_days ?? '-'} 天</b></div>
          <div class="proof-field"><span>动作类型</span><b>${esc(r.action_class||'-')}</b></div>
        </div>
      </section>
      ${snapshotHtml ? `<section class="proof-section"><div class="proof-section-head"><b>执行前数据快照</b><span>用于证明处理时的判定依据</span></div><div class="proof-grid">${snapshotHtml}</div></section>` : ''}
      ${r.calibration_classification ? `<section class="proof-section calibration-section">
        <div class="proof-section-head"><b>历史异常校正</b><span>${esc(calibrationLabel(r.calibration_classification))}</span></div>
        <div class="proof-grid">
          <div class="proof-field"><span>原判断</span><b>${esc(r.original_judge_basis || '原始事件已保留，可追溯')}</b></div>
          <div class="proof-field"><span>当前校正结论</span><b>${esc(r.corrected_basis || r.calibration_reason || '-')}</b></div>
          <div class="proof-field"><span>当前建议</span><b>${esc(r.corrected_recommendation || '-')}</b></div>
          <div class="proof-field"><span>校正原因</span><b>${esc(r.calibration_reason || '-')}</b></div>
          <div class="proof-field"><span>校正基线</span><b>${esc(r.calibration_baseline_batch || '-')}</b></div>
        </div>
      </section>` : ''}
      <section class="proof-section">
        <div class="proof-section-head"><b>实际动作与备注</b><span>运营填写</span></div>
        <div style="padding:12px 14px;font-size:12px;line-height:1.7"><b>${esc(r.actual_action||'待补')}</b><br><span style="color:#7b8794">${esc(r.notes||'无补充备注')}</span></div>
      </section>
      <section class="proof-section">
        <div class="proof-section-head"><b>${r.record_scope === 'product' ? '本产品维护轨迹' : '本次异常处理轨迹'}</b><span>已处理 ≠ 已恢复</span></div>
        <div class="timeline">
          <div class="timeline-item"><div class="timeline-time">首次命中</div><div class="timeline-rail"><i class="timeline-dot"></i></div><div class="timeline-content"><b>巡检命中：${esc(r.issue||'-')}</b><span>${esc(r.event_id)} · 进入 ${esc(r.priority)} 任务池 · 执行分 ${Math.round(r.score)}</span></div></div>
          ${r.timeline.map(t => `<div class="timeline-item ${t.action_type==='完成'?'done':t.action_type==='不处理'?'':''}"><div class="timeline-time">${esc((t.created_at||'').slice(5,16))}</div><div class="timeline-rail"><i class="timeline-dot"></i></div><div class="timeline-content"><b>${esc(t.action_type)}${t.result?` · ${esc(t.result)}`:''}</b><span>${esc(t.actual_action||'')}</span>${t.notes?`<br><em>备注：${esc(t.notes)}</em>`:''}</div></div>`).join('')}
          ${r.new_event ? `<div class="timeline-item new"><div class="timeline-time">${esc((r.new_event.first_seen||'').slice(0,10))}</div><div class="timeline-rail"><i class="timeline-dot"></i></div><div class="timeline-content"><b>新异常事件：${esc(r.new_event.issue||'-')}</b><span>${esc(r.new_event.event_id)}。此事件与原 ${esc(r.event_id)} 分开记录，不覆盖原凭证。</span></div></div>` : ''}
        </div>
      </section>
    `;
  } catch(e){
    document.getElementById('drawerBody').innerHTML = `<div class="loading">加载失败：${esc(e.message)}</div>`;
  }
}
function setHistoryDrawerLevel(level){
  const isRecord = level === 'record';
  document.getElementById('drawerBackBtn').hidden = !isRecord;
  document.getElementById('drawerCopyBtn').hidden = !isRecord;
}
function closeHistoryDrawer(){ document.getElementById('historyDrawer').classList.remove('show'); }
document.getElementById('drawerCloseX').onclick = closeHistoryDrawer;
document.getElementById('drawerCloseBtn').onclick = closeHistoryDrawer;
document.getElementById('drawerBackBtn').onclick = () => {
  if (state._currentHistoryProductKey) openProductHistoryDrawer(state._currentHistoryProductKey);
};
document.getElementById('historyDrawer').onclick = e => { if (e.target.id === 'historyDrawer') closeHistoryDrawer(); };
document.getElementById('drawerCopyBtn').onclick = () => {
  if (state._currentRecord) {
    navigator.clipboard?.writeText(state._currentRecord.record_id);
    toast(`已复制 ${state._currentRecord.record_id}`);
  }
};
document.getElementById('historyReloadBtn').onclick = () => loadHistory();
['historySearch','historyRange','historyStatus','historyEvent'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener(id === 'historySearch' ? 'input' : 'change', () => loadHistory());
});
