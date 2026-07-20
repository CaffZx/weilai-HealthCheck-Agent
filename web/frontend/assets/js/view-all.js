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

function groupAllProducts(events){
  const order = {P0: 0, P1: 1, P2: 2};
  const groups = new Map();
  events.forEach(event => {
    const key = allProductKey(event);
    if (!groups.has(key)) groups.set(key, {key, events: []});
    groups.get(key).events.push(event);
  });
  return [...groups.values()].map(product => {
    product.events.sort((a, b) =>
      (order[a.priority] ?? 3) - (order[b.priority] ?? 3) ||
      Number(b.score || 0) - Number(a.score || 0));
    product.main = product.events[0];
    product.priority = product.main.priority || 'P2';
    product.score = Math.max(...product.events.map(event => Number(event.score || 0)));
    product.status = product.events.every(event => event.status === '已完成' || event.status === '已关闭')
      ? '已完成'
      : product.events.some(event => event.status === '处理中') ? '处理中' : '未完成';
    product.maxDays = Math.max(...product.events.map(event => Number(event.days || 0)));
    product.categories = [...new Set(product.events.map(event => event.category).filter(Boolean))];
    return product;
  });
}

function renderAll(){
  const d = state.allData;
  if (!d) return;
  const products = groupAllProducts(d.events || []);
  const p0 = products.filter(product => product.priority === 'P0').length;
  const opened = products.filter(product => product.status !== '已完成').length;
  const closed = products.filter(product => product.status === '已完成').length;
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
  const rows = products.filter(product => {
    const text = product.events.map(event =>
      (event.parent_asin || '') + (event.product_name || '') + (event.issue || '')
    ).join('').toLowerCase();
    if (query && !text.includes(query)) return false;
    if (priority && !product.events.some(event => event.priority === priority)) return false;
    if (category && !product.events.some(event => event.category === category)) return false;
    if (status && product.status !== status) return false;
    return true;
  });
  document.getElementById('allResultCount').textContent = rows.length;
  document.getElementById('allBody').innerHTML = rows.length ? rows.map(product => {
    const event = product.main;
    const adEvent = product.events.find(item => (item.category || '').includes('交易表现'));
    const adButton = adEvent
      ? `<a class="btn ghost-blue" style="padding:2px 8px;height:26px;font-size:10px;text-decoration:none" href="${buildAdAgentUrl(adEvent)}" target="_blank" rel="noopener" onclick="markDoingAndOpen('${esc(adEvent.event_uid)}')">🎯 广告决策</a>`
      : '';
    return `<tr data-product-key="${esc(product.key)}" style="cursor:pointer">
      <td><span class="priority ${String(product.priority).toLowerCase()}">${esc(product.priority)}</span></td>
      <td><div class="mini-product"><b>${esc(event.product_name || event.parent_asin)}</b><span>${esc(event.parent_asin)} · ${esc(event.shop_account || '-')}</span></div></td>
      <td><span class="issue-type">${product.events.length} 个异常</span><div class="all-category-list">${esc(product.categories.join('、') || '-')}</div></td>
      <td><div class="all-issue-list">${product.events.map(item => esc(item.issue || item.category || '-')).join('、')}</div></td>
      <td>${product.maxDays} 天</td>
      <td><div class="score">${Math.round(product.score)}<small>/100</small></div></td>
      <td><span class="status ${statusClass(product.status)}">${esc(product.status)}</span></td>
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
  const product = groupAllProducts(state.allData?.events || []).find(item => item.key === productKey);
  if (!product) return;
  const event = product.main;
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
    return `<div class="all-drawer-anomaly">
      <div class="all-drawer-anomaly-head"><b><span class="priority ${String(item.priority || 'P2').toLowerCase()}">${esc(item.priority || '-')}</span> ${esc(item.issue || item.category || '异常')}</b><span>${Math.round(item.score || 0)} 分 · ${item.days || 0} 天 · ${esc(item.status)}</span></div>
      <div><b>判定依据：</b>${esc(reason)}</div>
      <div class="all-drawer-action"><b>建议动作：</b>${esc(item.recommendation || '查看详情后按异常处理建议执行')}</div>
    </div>`;
  }).join('');
  document.getElementById('drawerBody').innerHTML = `
    <div class="proof-banner" style="background:${product.status === '已完成' ? '#f0f4f8' : '#fff3e0'};border-color:${product.status === '已完成' ? '#d5dde5' : '#f5d7a3'}">
      <i style="background:${product.status === '已完成' ? '#6f7c8c' : '#d58a1e'}">!</i>
      <div><b style="color:#273140">${esc(product.status)} · ${esc(product.priority)} 产品</b><span>${product.events.length} 个异常 · 最高执行分 ${Math.round(product.score)}/100 · ${event.tier || '-'} 产品</span></div>
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
        <div class="proof-field"><span>最长持续</span><b>${product.maxDays} 天</b></div>
      </div>
    </section>
  `;
  drawer.classList.add('show');
}

document.getElementById('allReloadBtn').onclick = () => loadAll();
['allSearch','allPriority','allCategory','allStatus'].forEach(id => {
  document.getElementById(id).addEventListener(id === 'allSearch' ? 'input' : 'change', renderAll);
});
