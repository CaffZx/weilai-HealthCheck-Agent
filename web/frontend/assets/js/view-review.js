// ============================================================
// 视图3：调整复盘
// ============================================================
async function loadReview(){
  const p = document.getElementById('reviewPeriod').value;
  document.getElementById('reviewBody').innerHTML = '<tr><td colspan="6" class="loading">加载中…</td></tr>';
  try {
    state.reviewData = await api(withTarget('/api/review/due') + `&period=${p}`);
    renderReview();
  } catch(e){
    document.getElementById('reviewBody').innerHTML = `<tr><td colspan="6" class="loading">加载失败：${esc(e.message)}</td></tr>`;
  }
}
function renderReview(){
  const d = state.reviewData; if (!d) return;
  const good = d.records.filter(r=>r.effect==='变好').length;
  const bad = d.records.filter(r=>r.effect==='变差').length;
  const watch = d.records.filter(r=>r.effect==='待观察').length;
  document.getElementById('reviewKpi').innerHTML = `
    <div class="kpi-card warning-card"><span>${d.period==='today'?'今日到期':'期间内到期'}</span><b>${d.total}</b><small>${d.period}</small></div>
    <div class="kpi-card good-card"><span>调整后变好</span><b>${good}</b><small>保留当前动作</small></div>
    <div class="kpi-card"><span>效果待观察</span><b>${watch}</b><small>样本不足</small></div>
    <div class="kpi-card danger-card"><span>调整后变差</span><b>${bad}</b><small>需二次处理</small></div>
  `;
  document.getElementById('reviewResultCount').textContent = d.records.length;
  document.getElementById('reviewBody').innerHTML = d.records.length ? d.records.map(r => `
    <tr>
      <td><div class="mini-product"><b>${esc(r['父ASIN']||'-')}</b><span>${esc(r['店铺账号']||'-')}</span></div></td>
      <td>${esc(r['问题点位']||'-')}</td>
      <td>${esc(r.actual_action||r.action_type||'-')}</td>
      <td>${fmtDate(r.review_at)}</td>
      <td>${r.effect ? `<span class="tag ${r.effect==='变差'?'s0':''}">${esc(r.effect)}</span>` : '<span style="color:#8a93a2">-</span>'}</td>
      <td>${esc(r.notes||'-')}</td>
    </tr>
  `).join('') : '<tr class="empty-row"><td colspan="6">当前区间无到期复查记录</td></tr>';
}
document.getElementById('reviewPeriod').onchange = () => loadReview();
document.getElementById('reviewReloadBtn').onclick = () => loadReview();

