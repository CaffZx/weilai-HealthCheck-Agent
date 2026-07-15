// ============================================================
// 视图2：全部异常
// ============================================================
async function loadAll(){
  document.getElementById('allBody').innerHTML = '<tr><td colspan="7" class="loading">加载中…</td></tr>';
  try {
    state.allData = await api(withTarget('/api/anomalies'));
    renderAll();
  } catch(e){
    document.getElementById('allBody').innerHTML = `<tr><td colspan="7" class="loading">加载失败：${esc(e.message)}</td></tr>`;
  }
}
function renderAll(){
  const d = state.allData; if (!d) return;
  const p0 = d.events.filter(e=>e.priority==='P0').length;
  const opened = d.events.filter(e=>e.status!=='已关闭'&&e.status!=='已完成').length;
  const closed = d.events.filter(e=>e.status==='已关闭'||e.status==='已完成').length;
  document.getElementById('allKpi').innerHTML = `
    <div class="kpi-card"><span>全部异常</span><b>${d.total}</b><small>该负责人名下</small></div>
    <div class="kpi-card danger-card"><span>P0 高风险</span><b>${p0}</b><small>${d.events.filter(e=>e.priority==='P0'&&e.status!=='已完成').length} 条待处理</small></div>
    <div class="kpi-card warning-card"><span>开放中</span><b>${opened}</b><small>待处理/处理中/待复查</small></div>
    <div class="kpi-card good-card"><span>已关闭</span><b>${closed}</b><small>已完成 + 已关闭</small></div>
  `;
  const catSel = document.getElementById('allCategory');
  const cats = [...new Set(d.events.map(e => e.category).filter(Boolean))];
  const curCat = catSel.value;
  catSel.innerHTML = '<option value="">全部异常大类</option>' + cats.map(c => `<option ${c===curCat?'selected':''}>${esc(c)}</option>`).join('');

  const q = (document.getElementById('allSearch').value||'').toLowerCase();
  const pf = document.getElementById('allPriority').value;
  const cf = document.getElementById('allCategory').value;
  const sf = document.getElementById('allStatus').value;
  const rows = d.events.filter(e => {
    if (q && !`${e.parent_asin||''}${e.product_name||''}${e.issue||''}`.toLowerCase().includes(q)) return false;
    if (pf && e.priority !== pf) return false;
    if (cf && e.category !== cf) return false;
    if (sf && e.status !== sf) return false;
    return true;
  });
  document.getElementById('allResultCount').textContent = rows.length;
  document.getElementById('allBody').innerHTML = rows.length ? rows.map(e => `
    <tr>
      <td><span class="priority ${e.priority.toLowerCase()}">${e.priority}</span></td>
      <td><div class="mini-product"><b>${esc(e.product_name||e.parent_asin)}</b><span>${esc(e.parent_asin)} · ${esc(e.shop_account||'-')}</span></div></td>
      <td><span class="issue-type">${esc(e.category||'-')}</span></td>
      <td>${esc(e.issue||'-')}</td>
      <td>${e.days} 天</td>
      <td><div class="score">${Math.round(e.score)}<small>/100</small></div></td>
      <td><span class="status ${statusClass(e.status)}">${esc(e.status)}</span></td>
    </tr>
  `).join('') : '<tr class="empty-row"><td colspan="7">无匹配数据</td></tr>';
}
document.getElementById('allReloadBtn').onclick = () => loadAll();
['allSearch','allPriority','allCategory','allStatus'].forEach(id => {
  document.getElementById(id).addEventListener(id === 'allSearch' ? 'input' : 'change', renderAll);
});

