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
  document.getElementById('historyResultCount').textContent = d.total;
  const k = d.kpi;
  document.getElementById('historyKpi').innerHTML = `
    <div class="kpi-card good-card"><span>已形成处理凭证</span><b>${k.proof}</b><small>完整记录数</small></div>
    <div class="kpi-card"><span>涉及产品</span><b>${k.products}</b><small>同产品可多条记录</small></div>
    <div class="kpi-card danger-card"><span>处理后再次异常</span><b>${k.with_new_event}</b><small>新事件与原记录并存</small></div>
    <div class="kpi-card warning-card"><span>待补完整记录</span><b>${k.pending}</b><small>结果或动作未填</small></div>
  `;
  const rows = d.records;
  if (!rows.length){
    document.getElementById('historyBody').innerHTML = '<tr class="empty-row"><td colspan="10">无匹配的处理记录</td></tr>';
    return;
  }
  document.getElementById('historyBody').innerHTML = rows.map(r => `
    <tr data-rid="${esc(r.record_id)}">
      <td><span class="history-id">${esc(r.record_id)}</span></td>
      <td>${esc((r.time||'').slice(0,16))}</td>
      <td><div class="mini-product"><b>${esc(r.product_name || r.parent_asin)}</b><span>${esc(r.parent_asin)} · ${esc(r.shop_account||'-')}</span></div></td>
      <td><div class="mini-product"><b>${esc(r.issue||'-')}</b><span>${esc(r.event_id)} · ${esc(r.priority)} · 分${Math.round(r.score)}</span></div></td>
      <td>${esc(r.actual_action||'-')}${r.action_class?`<br><span style="color:#8a93a2;font-size:10px">类型：${esc(r.action_class)}</span>`:''}</td>
      <td>${esc(r.owner)}</td>
      <td><span class="effect-pill ${r.review==='已恢复'?'good':r.review==='未恢复'?'bad':'watch'}">${esc(r.review)}</span></td>
      <td><span class="proof-pill ${r.complete==='待补'?'warn':''}">${r.complete==='完整'?'✓ ':''}${esc(r.complete)}</span></td>
      <td>${r.new_event ? `<span class="new-event-pill">${esc(r.new_event.issue||'新异常')}</span>` : '—'}</td>
      <td><button class="btn" style="padding:0 8px;height:26px;font-size:11px" data-open="${esc(r.record_id)}">查看凭证</button></td>
    </tr>
  `).join('');
  document.querySelectorAll('#historyBody button[data-open]').forEach(b => {
    b.onclick = () => openHistoryDrawer(b.dataset.open);
  });
}
async function openHistoryDrawer(recordId){
  const drawer = document.getElementById('historyDrawer');
  document.getElementById('drawerBody').innerHTML = '<div class="loading">加载中…</div>';
  drawer.classList.add('show');
  try {
    const r = await api(withTarget(`/api/history/${recordId}`));
    state._currentRecord = r;
    document.getElementById('drawerTitle').textContent = `处理记录 · ${r.record_id}`;
    document.getElementById('drawerSubtitle').textContent = `${r.event_id} · ${r.parent_asin} · ${r.shop_account||''}`;
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
          <div class="proof-field"><span>异常事件ID</span><b>${esc(r.event_id)}</b></div>
          <div class="proof-field"><span>产品</span><b>${esc(r.product_name || r.parent_asin)}<br>${esc(r.parent_asin)} · ${esc(r.parent_sku||'-')}</b></div>
          <div class="proof-field"><span>优先级 · 执行分</span><b>${esc(r.priority)} · ${Math.round(r.score)}/100</b></div>
          <div class="proof-field"><span>点击完成时间</span><b>${esc(r.time)}</b></div>
          <div class="proof-field"><span>操作人</span><b>${esc(r.owner)}</b></div>
          <div class="proof-field"><span>处理结果</span><b>${esc(r.result||'待补')}</b></div>
          <div class="proof-field"><span>复查节点</span><b>${esc(r.review_at||'不复查')}</b></div>
          <div class="proof-field"><span>SLA</span><b>${r.on_time ? '✓ 按时' : '⚠ 超时'} · 处理耗时 ${r.handle_days ?? '-'} 天</b></div>
          <div class="proof-field"><span>动作类型</span><b>${esc(r.action_class||'-')}</b></div>
        </div>
      </section>
      ${snapshotHtml ? `<section class="proof-section"><div class="proof-section-head"><b>执行前数据快照</b><span>用于证明处理时的判定依据</span></div><div class="proof-grid">${snapshotHtml}</div></section>` : ''}
      <section class="proof-section">
        <div class="proof-section-head"><b>实际动作与备注</b><span>运营填写</span></div>
        <div style="padding:12px 14px;font-size:12px;line-height:1.7"><b>${esc(r.actual_action||'待补')}</b><br><span style="color:#7b8794">${esc(r.notes||'无补充备注')}</span></div>
      </section>
      <section class="proof-section">
        <div class="proof-section-head"><b>本次事件处理轨迹</b><span>已处理 ≠ 已恢复</span></div>
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
function closeHistoryDrawer(){ document.getElementById('historyDrawer').classList.remove('show'); }
document.getElementById('drawerCloseX').onclick = closeHistoryDrawer;
document.getElementById('drawerCloseBtn').onclick = closeHistoryDrawer;
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

