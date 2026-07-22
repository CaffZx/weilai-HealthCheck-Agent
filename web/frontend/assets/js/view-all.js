async function loadAll(){
  document.getElementById('allBody').innerHTML = '<tr><td colspan="8" class="loading">加载中…</td></tr>';
  try {
    state.allData = await api(withTarget('/api/anomalies'));
    renderAll();
  } catch(e){
    document.getElementById('allBody').innerHTML = '<tr><td colspan="8" class="loading">加载失败：' + esc(e.message) + '</td></tr>';
  }
}

function allProductKey(event){
  return (event.parent_asin || '') + '__' + (event.shop_account || '');
}

function productMatchesStatus(product, requestedStatus){
  return !requestedStatus || product.product_status === requestedStatus;
}

function allEventSeverity(event){
  return event?.severity || 'S2';
}

function renderAll(){
  const d = state.allData;
  if (!d) return;
  const products = d.products || [];
  const p0 = products.filter(product => product.priority === 'P0').length;
  const opened = products.filter(product => !['已完成', '已关闭'].includes(product.product_status)).length;
  const closed = products.filter(product => product.product_status === '已完成').length;
  document.getElementById('allKpi').innerHTML = `
    <div class="kpi-card"><span>全部产品</span><b>${products.length}</b><small>按 ASIN + 店铺聚合</small></div>
    <div class="kpi-card danger-card"><span>P0 高风险产品</span><b>${p0}</b><small>按产品最高优先级统计</small></div>
    <div class="kpi-card warning-card"><span>开放中</span><b>${opened}</b><small>产品仍有未完成异常</small></div>
    <div class="kpi-card good-card"><span>已完成产品</span><b>${closed}</b><small>产品内异常全部完成</small></div>
  `;

  const categorySelect = document.getElementById('allCategory');
  const categories = [...new Set((d.events || []).map(event => event.category).filter(Boolean))];
  const currentCategory = categorySelect.value;
  categorySelect.innerHTML = '<option value="">全部异常大类</option>' +
    categories.map(category => `<option value="${esc(category)}" ${category === currentCategory ? 'selected' : ''}>${esc(category)}</option>`).join('');

  const query = (document.getElementById('allSearch').value || '').toLowerCase();
  const priority = document.getElementById('allPriority').value;
  const category = document.getElementById('allCategory').value;
  const status = document.getElementById('allStatus').value;
  const assign = document.getElementById('allAssign').value;
  const rows = products.filter(product => {
    const text = product.events.map(event =>
      (event.parent_asin || '') + (event.product_name || '') + (event.issue || '')
    ).join('').toLowerCase();
    if (query && !text.includes(query)) return false;
    if (priority && product.priority !== priority) return false;
    if (category && !product.events.some(event => event.category === category)) return false;
    if (!productMatchesStatus(product, status)) return false;
    if (assign === 'to_me' && !product.assigned_to_me) return false;
    if (assign === 'by_me' && !product.assigned_by_me) return false;
    if (assign === 'assigned' && !product.is_assigned) return false;
    if (assign === 'none' && product.is_assigned) return false;
    return true;
  });
  document.getElementById('allResultCount').textContent = rows.length;
  document.getElementById('allBody').innerHTML = rows.length ? rows.map(product => {
    const event = product.events[0];
    const adEvent = product.events.find(item => (item.category || '').includes('交易表现'));
    const adButton = adEvent
      ? `<a class="btn ghost-blue" style="padding:2px 8px;height:26px;font-size:10px;text-decoration:none" href="${buildAdAgentUrl(adEvent)}" target="_blank" rel="noopener" onclick="markDoingAndOpen('${esc(adEvent.event_uid)}')">🎯 广告决策</a>`
      : '';
    return `<tr data-product-key="${esc(product.key)}" style="cursor:pointer">
      <td><span class="priority ${String(product.priority).toLowerCase()}">${esc(product.priority)}</span></td>
      <td><div class="mini-product all-mini-product">${productImageHtml(product.image_url || event.image_url, 'all-product-image')}<div class="mini-product-copy"><b>${esc(event.product_name || event.parent_asin)}</b><span>${esc(event.parent_asin)} · ${esc(event.shop_account || '-')}</span>${product.is_assigned ? `<span class="${product.assigned_to_me ? 'assign-badge to-me' : 'assign-badge'}">👤 ${product.assigned_to_me ? '指派给我' : '指派给'} ${esc((product.assignee_names||[]).join('、'))}</span>` : ''}</div></div></td>
      <td><span class="issue-type">${product.events.length} 个异常</span><div class="all-category-list">${esc([...new Set(product.events.map(item => item.category).filter(Boolean))].join('、') || '-')}</div></td>
      <td><div class="all-issue-list">${product.events.map(item => `${esc(allEventSeverity(item))} ${esc(item.issue || item.category || '-')}`).join('、')}</div></td>
      <td>${product.max_days} 天</td>
      <td><div class="score">${Math.round(product.product_score ?? Math.max(...product.events.map(item => Number(item.score || 0))))}<small>/100</small></div></td>
      <td><span class="status ${statusClass(product.product_status)}">${esc(product.product_status)}</span></td>
      <td onclick="event.stopPropagation()"><div style="display:flex;gap:4px;flex-wrap:wrap">
        <button class="btn" style="padding:2px 8px;height:26px;font-size:10px" onclick="openAnomalyDetail('${esc(product.key)}')">查看</button>
        ${adButton}
      </div></td>
    </tr>`;
  }).join('') : '<tr class="empty-row"><td colspan="8">无匹配数据</td></tr>';

  document.querySelectorAll('#allBody tr[data-product-key]').forEach(row => {
    row.onclick = () => openAnomalyDetail(row.dataset.productKey);
  });
}

function openAnomalyDetail(productKey){
  const product = state.allData?.products?.find(item => item.key === productKey);
  if (!product) return;
  const event = product.events[0];
  const drawer = document.getElementById('historyDrawer');
  document.getElementById('drawerTitle').textContent = (event.product_name || event.parent_asin) + ' · ' + product.events.length + ' 个异常';
  document.getElementById('drawerSubtitle').textContent = event.parent_asin + ' · ' + (event.shop_account || '-') + ' · ' + (event.site || '');
  const adEvent = product.events.find(item => (item.category || '').includes('交易表现'));
  const adButton = adEvent
    ? `<a class="btn ghost-blue" style="text-decoration:none" href="${buildAdAgentUrl(adEvent)}" target="_blank" rel="noopener" onclick="markDoingAndOpen('${esc(adEvent.event_uid)}')">🎯 广告决策</a>`
    : '';
  const anomalyList = product.events.map(item => {
    const basis = item.judge_basis || {};
    const reason = item.summary_reason || basis['命中依据'] || basis['判定过程'] || '未记录判定依据';
    const hitAsin = item.variant
      ? `<div><b>命中 ASIN：</b>${esc(item.variant)}</div>`
      : '';
    return `<div class="all-drawer-anomaly">
      <div class="all-drawer-anomaly-head"><b><span class="severity-badge ${String(allEventSeverity(item)).toLowerCase()}">${esc(allEventSeverity(item))}</span> ${esc(item.issue || item.category || '异常')}</b><span>${Math.round(item.score || 0)} 分 · ${item.days || 0} 天 · ${esc(item.status)}</span></div>
      ${hitAsin}
      <div><b>判定依据：</b>${esc(reason)}</div>
      <div class="all-drawer-action"><b>建议动作：</b>${esc(item.recommendation || '查看详情后按异常处理建议执行')}</div>
    </div>`;
  }).join('');
  document.getElementById('drawerBody').innerHTML = `
    <div class="proof-banner" style="background:${product.product_status === '已完成' ? '#f0f4f8' : '#fff3e0'};border-color:${product.product_status === '已完成' ? '#d5dde5' : '#f5d7a3'}">
      <i style="background:${product.product_status === '已完成' ? '#6f7c8c' : '#d58a1e'}">!</i>
      <div><b style="color:#273140">${esc(product.product_status)} · ${esc(product.priority)} 产品</b><span>${product.events.length} 个异常 · 最高执行分 ${Math.round(product.product_score ?? 0)}/100 · ${event.tier_display || event.tier || '待补充'}</span></div>
    </div>
    <section class="proof-section">
      <div class="proof-section-head"><b>产品内异常</b><span>${adButton}</span></div>
      <div>${anomalyList}</div>
    </section>
    <section class="proof-section">
      <div class="proof-section-head"><b>产品信息</b><span style="font-family:monospace">${esc(product.key)}</span></div>
      <div class="proof-grid">
        <div class="proof-field"><span>产品</span><b>${esc(event.product_name || event.parent_asin)}<br>${esc(event.parent_asin)}${event.parent_sku ? ' · ' + esc(event.parent_sku) : ''}</b></div>
        <div class="proof-field"><span>店铺 · 站点</span><b>${esc(event.shop_account || '-')} · ${esc(event.site || '-')}</b></div>
        <div class="proof-field"><span>巡检时间</span><b>${esc(event.inspection_time || '未关联')}</b></div>
        <div class="proof-field"><span>最长持续</span><b>${product.max_days} 天</b></div>
      </div>
    </section>
  `;
  drawer.classList.add('show');
}

document.getElementById('allReloadBtn').onclick = () => loadAll();
['allSearch','allPriority','allCategory','allStatus','allAssign'].forEach(id => {
  document.getElementById(id).addEventListener(id === 'allSearch' ? 'input' : 'change', renderAll);
});
