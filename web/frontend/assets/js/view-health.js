// ============================================================
// 视图4：数据健康
// ============================================================
async function loadHealth(){
  document.getElementById('healthDaily').innerHTML = '<div class="loading">加载中…</div>';
  document.getElementById('healthSnapshot').innerHTML = '';
  try {
    state.healthData = await api('/api/sync-health');
    renderHealth();
  } catch(e){
    document.getElementById('healthDaily').innerHTML = `<div class="loading">加载失败：${esc(e.message)}</div>`;
  }
}
function renderHealth(){
  const d = state.healthData; if (!d) return;
  document.getElementById('lastHealthCheck').textContent = `最近生成：${d.generated_at||'-'}`;
  const s = d.summary || {};
  const status = s.status || 'unknown';
  const cardCls = status==='ok'?'good-card':status==='warn'?'warning-card':'danger-card';
  document.getElementById('healthKpi').innerHTML = `
    <div class="kpi-card ${cardCls}"><span>整体状态</span><b>${status.toUpperCase()}</b><small>最低覆盖率 ${((s.min_coverage||0)*100).toFixed(1)}%</small></div>
    <div class="kpi-card"><span>平均覆盖率</span><b>${((s.avg_coverage||0)*100).toFixed(1)}%</b><small>所有表汇总</small></div>
    <div class="kpi-card warning-card"><span>检查日期</span><b>${(d.check_dates||[])[0]||'-'}</b><small>${(d.check_dates||[]).length} 天</small></div>
    <div class="kpi-card"><span>数据表数</span><b>${Object.keys(d.snapshot||{}).length + Object.keys((d.daily||{})[Object.keys(d.daily||{})[0]]||{}).length}</b><small>daily + snapshot</small></div>
  `;
  let dailyHtml = '';
  Object.entries(d.daily || {}).forEach(([date, tbls]) => {
    Object.entries(tbls).forEach(([tbl, r]) => {
      const cov = r.coverage || 0;
      const cls = cov >= 0.9 ? 'ok' : cov >= 0.7 ? 'warn' : 'fail';
      const em = cov >= 0.9 ? '正常' : cov >= 0.7 ? '偏低' : '失败';
      dailyHtml += `<div class="source-card ${cls}"><div class="source-icon">${esc(tbl.slice(0,3).toUpperCase())}</div><div><b>${esc(r.name)} · ${date}</b><span>${r.found}/${r.expected} · 缺 ${r.missing_count||0}</span></div><em>${em} ${(cov*100).toFixed(1)}%</em></div>`;
    });
  });
  document.getElementById('healthDaily').innerHTML = dailyHtml || '<div class="loading">无 daily 数据</div>';
  let snapHtml = '';
  Object.entries(d.snapshot || {}).forEach(([tbl, r]) => {
    const cov = r.coverage || 0;
    const cls = cov >= 0.9 ? 'ok' : cov >= 0.7 ? 'warn' : 'fail';
    const em = cov >= 0.9 ? '正常' : cov >= 0.7 ? '偏低' : '失败';
    snapHtml += `<div class="source-card ${cls}"><div class="source-icon">${esc(tbl.slice(0,3).toUpperCase())}</div><div><b>${esc(r.name)}</b><span>${r.found}/${r.expected} · 缺 ${r.missing_count||0}</span></div><em>${em} ${(cov*100).toFixed(1)}%</em></div>`;
  });
  document.getElementById('healthSnapshot').innerHTML = snapHtml || '<div class="loading">无 snapshot 数据</div>';
}
document.getElementById('healthReloadBtn').onclick = () => loadHealth();

