// ============================================================
// 视图6：数据看板
// ============================================================
async function loadDashboard(){
  document.getElementById('dashKpis').innerHTML = '<div class="loading" style="grid-column:1/-1">加载中…</div>';
  try {
    const rng = document.getElementById('dashRange').value || '7';
    state.dashboardData = await api(withTarget('/api/dashboard') + `&range=${rng}`);
    renderDashboard();
  } catch(e){
    document.getElementById('dashKpis').innerHTML = `<div class="loading" style="grid-column:1/-1">加载失败：${esc(e.message)}</div>`;
  }
}
function renderDashboard(){
  const d = state.dashboardData; if (!d) return;
  const k = d.kpi;
  document.getElementById('dashKpis').innerHTML = `
    <div class="dash-kpi blue"><div class="dash-kpi-label"><span>已完成任务</span><em>${d.range_days}天</em></div><div class="dash-kpi-value">${k.completed}</div><div class="dash-kpi-sub">目标 ${k.target}，达成 ${k.goal_rate}%</div></div>
    <div class="dash-kpi ${k.goal_rate>=100?'good':'warn'}"><div class="dash-kpi-label"><span>目标完成率</span><em>参考80/日</em></div><div class="dash-kpi-value">${k.goal_rate}%</div><div class="dash-kpi-sub ${k.goal_rate>=100?'good':'warn'}">${k.goal_rate>=100?'已达标':'未达标'}</div></div>
    <div class="dash-kpi ${k.on_time_rate>=90?'good':'warn'}"><div class="dash-kpi-label"><span>按时处理率</span><em>SLA</em></div><div class="dash-kpi-value">${k.on_time_rate}%</div><div class="dash-kpi-sub">${k.on_time_count} 条按时完成</div></div>
    <div class="dash-kpi ${k.recovery_rate>=80?'good':'warn'}"><div class="dash-kpi-label"><span>异常恢复率</span><em>复查确认</em></div><div class="dash-kpi-value">${k.recovery_rate}%</div><div class="dash-kpi-sub ${k.recovery_rate>=80?'good':'warn'}">${k.recovered} 变好 / ${k.not_recovered} 变差</div></div>
    <div class="dash-kpi ${k.p0_ontime_rate>=95?'good':'warn'}"><div class="dash-kpi-label"><span>P0 按时率</span><em>1天时限</em></div><div class="dash-kpi-value">${k.p0_ontime_rate}%</div><div class="dash-kpi-sub ${k.p0_overdue?'warn':'good'}">${k.p0_overdue} 条 P0 超时</div></div>
    <div class="dash-kpi ${k.repeat_rate<10?'good':'bad'}"><div class="dash-kpi-label"><span>重复异常率</span><em>≥2次</em></div><div class="dash-kpi-value">${k.repeat_rate}%</div><div class="dash-kpi-sub ${k.repeat_rate<10?'good':'bad'}">${k.repeat_products} 款重复出现</div></div>
  `;

  // 每日完成量柱图
  const chart = document.getElementById('dailyChart');
  chart.querySelectorAll('.day-col').forEach(x => x.remove());
  const daily = d.daily || [];
  const maxVal = Math.max(100, ...daily.map(x => x.count));
  daily.forEach((day, i) => {
    const col = document.createElement('div');
    col.className = 'day-col';
    const label = day.date.slice(5).replace('-','/');
    col.innerHTML = `<div class="day-value">${day.count}</div><div class="day-bar ${i===daily.length-1?'today':''} ${day.count<day.target?'low':''}" style="height:${Math.max(3, day.count/maxVal*100)}%"></div><div class="day-name">${label}</div>`;
    col.onclick = () => toast(`${label}：完成 ${day.count} 条`);
    chart.appendChild(col);
  });

  // 异常大类分布
  const anomaly = d.anomaly || [];
  const maxA = Math.max(1, ...anomaly.map(x => x.events));
  document.getElementById('anomalyBars').innerHTML = anomaly.map(a => `
    <div class="anomaly-row"><span>${esc(a.name)}</span><div class="anomaly-track"><i style="width:${a.events/maxA*100}%"></i></div><span class="anomaly-count">${a.events}</span></div>
  `).join('') || '<div class="loading">无异常数据</div>';

  // 恢复率折线
  const trend = d.trend || [];
  const w = 700, h = 180;
  const pts = trend.length > 1 ? trend.map((t, i) => [i/(trend.length-1)*w, h - t.rate/100*h]) : trend.length ? [[w/2, h - trend[0].rate/100*h]] : [];
  const pointsStr = pts.map(p => p.map(v => v.toFixed(1)).join(',')).join(' ');
  document.getElementById('recoveryLine').setAttribute('points', pointsStr);
  document.getElementById('recoveryArea').setAttribute('d', pts.length ? `M ${pts[0][0]} ${h} L ${pts.map(p => p.join(' ')).join(' L ')} L ${pts[pts.length-1][0]} ${h} Z` : '');
  document.getElementById('recoveryPoints').innerHTML = pts.map((p, i) => `<circle class="point" cx="${p[0]}" cy="${p[1]}" r="3" data-rate="${trend[i].rate}"><title>${trend[i].date} · ${trend[i].rate}%</title></circle>`).join('');
  document.getElementById('recoveryXAxis').innerHTML = trend.filter((_, i) => i === 0 || i === trend.length-1 || i === Math.floor(trend.length/2)).map(t => `<span>${t.date.slice(5).replace('-','/')}</span>`).join('');

  // 动作有效率
  const acts = d.action_effects || [];
  document.getElementById('actionEffectList').innerHTML = acts.length ? acts.map((a, i) => `
    <div class="action-line ${i===0?'best':a.rate<50?'low':''}">
      <span>${esc(a.name)}<small>已复查 ${a.checked}/${a.total}</small></span>
      <div class="effect-track"><i style="width:${a.rate}%"></i></div>
      <b>${a.rate}%</b>
    </div>
  `).join('') : '<div class="loading">尚无已复查动作</div>';

  // 高频异常产品
  const rp = d.repeat_products || [];
  document.getElementById('repeatProductBody').innerHTML = rp.length ? rp.map(p => `
    <tr>
      <td><div class="mini-product"><b>${esc(p.product_name || p.parent_asin)}</b><span>${esc(p.parent_asin)} · ${esc(p.shop_account || '-')}</span></div></td>
      <td><span class="repeat-num">${p.count}</span></td>
      <td>${esc(p.issue||'-')}</td>
      <td>${p.actions}</td>
      <td><span class="effect-pill ${p.effect==='变好'?'good':p.effect==='变差'?'bad':'watch'}">${esc(p.effect||'-')}</span></td>
      <td><span class="priority ${(p.priority||'').toLowerCase()}">${esc(p.priority||'-')}</span></td>
      <td>${Math.round(p.score||0)}</td>
      <td>${p.days||0}天</td>
    </tr>
  `).join('') : '<tr class="empty-row"><td colspan="8">无高频异常产品</td></tr>';
}
document.getElementById('dashReloadBtn').onclick = () => loadDashboard();
document.getElementById('dashRange').onchange = () => loadDashboard();
