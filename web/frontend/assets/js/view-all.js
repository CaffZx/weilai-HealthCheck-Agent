// ============================================================
// 视图2：全部异常
// ============================================================
async function loadAll(){
  document.getElementById('allBody').innerHTML = '<tr><td colspan="8" class="loading">加载中…</td></tr>';
  try {
    state.allData = await api(withTarget('/api/anomalies'));
    renderAll();
  } catch(e){
    document.getElementById('allBody').innerHTML = `<tr><td colspan="8" class="loading">加载失败：${esc(e.message)}</td></tr>`;
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
  document.getElementById('allBody').innerHTML = rows.length ? rows.map((e, i) => {
    const 是交易表现 = (e.category || '').includes('交易表现');
    const adBtn = 是交易表现
      ? `<a class="btn ghost-blue" style="padding:2px 8px;height:26px;font-size:10px;text-decoration:none" href="${buildAdAgentUrl(e)}" target="_blank" rel="noopener" onclick="markDoingAndOpen('${esc(e.event_uid)}')" title="打开广告辅助决策 agent">🎯 广告决策</a>`
      : '';
    return `<tr data-uid="${esc(e.event_uid)}" style="cursor:pointer">
      <td><span class="priority ${e.priority.toLowerCase()}">${e.priority}</span></td>
      <td><div class="mini-product"><b>${esc(e.product_name||e.parent_asin)}</b><span>${esc(e.parent_asin)} · ${esc(e.shop_account||'-')}</span></div></td>
      <td><span class="issue-type">${esc(e.category||'-')}</span></td>
      <td>${esc(e.issue||'-')}</td>
      <td>${e.days} 天</td>
      <td><div class="score">${Math.round(e.score)}<small>/100</small></div></td>
      <td><span class="status ${statusClass(e.status)}">${esc(e.status)}</span></td>
      <td onclick="event.stopPropagation()">
        <div style="display:flex;gap:4px;flex-wrap:wrap">
          <button class="btn" style="padding:2px 8px;height:26px;font-size:10px" onclick="openAnomalyDetail('${esc(e.event_uid)}')">查看</button>
          ${adBtn}
        </div>
      </td>
    </tr>`;
  }).join('') : '<tr class="empty-row"><td colspan="8">无匹配数据</td></tr>';

  // 行本身点击也打开详情
  document.querySelectorAll('#allBody tr[data-uid]').forEach(tr => {
    tr.onclick = () => openAnomalyDetail(tr.dataset.uid);
  });
}

// 点击行 → 弹抽屉展示异常详情（复用 history 抽屉容器）
function openAnomalyDetail(eventUid){
  const e = state.allData?.events.find(x => x.event_uid === eventUid);
  if (!e) return;
  const drawer = document.getElementById('historyDrawer');
  document.getElementById('drawerTitle').textContent = `${e.issue || '异常事件'} · ${e.priority}`;
  document.getElementById('drawerSubtitle').textContent = `${e.parent_asin} · ${e.shop_account || '-'} · ${e.site || ''}`;

  const basis = e.judge_basis || {};
  const basisTxt = basis['命中依据'] || basis['判定过程'] || JSON.stringify(basis);
  const 是交易表现 = (e.category || '').includes('交易表现');
  const adBtn = 是交易表现
    ? `<a class="btn ghost-blue" style="text-decoration:none" href="${buildAdAgentUrl(e)}" target="_blank" rel="noopener" onclick="markDoingAndOpen('${esc(e.event_uid)}')">🎯 广告决策</a>`
    : '';

  document.getElementById('drawerBody').innerHTML = `
    <div class="proof-banner" style="background:${e.status==='已完成'||e.status==='已关闭'?'#f0f4f8':'#fff3e0'};border-color:${e.status==='已完成'||e.status==='已关闭'?'#d5dde5':'#f5d7a3'}">
      <i style="background:${e.status==='已完成'||e.status==='已关闭'?'#6f7c8c':'#d58a1e'}">!</i>
      <div><b style="color:#273140">${esc(e.status)} · 严重度 ${e.severity}</b><span>持续 ${e.days} 天 · 执行分 ${Math.round(e.score)}/100 · ${e.tier || '-'} 产品</span></div>
    </div>
    <section class="proof-section">
      <div class="proof-section-head"><b>异常判定</b><span>${adBtn}</span></div>
      <div style="padding:12px 14px;font-size:12px;line-height:1.6">
        <b>${esc(e.issue)} · ${esc(e.category)}</b><br>
        <span style="color:#7b8794">${esc(basisTxt)}</span>
      </div>
    </section>
    <section class="proof-section">
      <div class="proof-section-head"><b>事件信息</b><span style="font-family:monospace">${esc(e.event_uid)}</span></div>
      <div class="proof-grid">
        <div class="proof-field"><span>产品</span><b>${esc(e.product_name || e.parent_asin)}<br>${esc(e.parent_asin)}${e.parent_sku ? ' · ' + esc(e.parent_sku) : ''}</b></div>
        <div class="proof-field"><span>店铺 · 站点</span><b>${esc(e.shop_account || '-')} · ${esc(e.site || '-')}</b></div>
        <div class="proof-field"><span>首次命中</span><b>${fmtDate(e.first_seen)}</b></div>
        <div class="proof-field"><span>最近命中</span><b>${fmtDate(e.last_seen)}</b></div>
        <div class="proof-field"><span>作用层级</span><b>${esc(e.scope || '-')}</b></div>
        <div class="proof-field"><span>命中变体</span><b>${esc(e.variant || '-')}${e.variant_importance ? ' · ' + esc(e.variant_importance) : ''}</b></div>
      </div>
    </section>
    ${e.latest_action ? `
    <section class="proof-section">
      <div class="proof-section-head"><b>最近处理记录</b><span>${fmtDate(e.latest_action.created_at)}</span></div>
      <div style="padding:12px 14px;font-size:11px;line-height:1.6">
        <b>${esc(e.latest_action.action_type)}${e.latest_action.result ? ' · ' + esc(e.latest_action.result) : ''}</b><br>
        ${e.latest_action.actual_action ? esc(e.latest_action.actual_action) + '<br>' : ''}
        ${e.latest_action.notes ? '<span style="color:#7b8794">' + esc(e.latest_action.notes) + '</span>' : ''}
      </div>
    </section>` : ''}
  `;
  drawer.classList.add('show');
}

document.getElementById('allReloadBtn').onclick = () => loadAll();
['allSearch','allPriority','allCategory','allStatus'].forEach(id => {
  document.getElementById(id).addEventListener(id === 'allSearch' ? 'input' : 'change', renderAll);
});
