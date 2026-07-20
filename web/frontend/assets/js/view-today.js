// ============================================================
// 视图1：今日任务（按产品聚合 · 详情平铺所有异常 + 任务清单）
// ============================================================
async function loadToday(){
  document.getElementById('taskBody').innerHTML = '<div class="loading">加载中…</div>';
  try {
    state.todayData = await api(withTarget('/api/tasks/today'));
    renderToday();
  } catch(e){
    document.getElementById('taskBody').innerHTML = `<div class="loading">加载失败：${esc(e.message)}</div>`;
  }
}

function productKey(e){
  return `${encodeURIComponent(e.parent_asin || '')}__${encodeURIComponent(e.shop_account || '')}`;
}

function sortEventsByPriority(events){
  const priority = {P0: 0, P1: 1, P2: 2};
  return [...events].sort((a, b) =>
    (priority[a.priority] ?? 3) - (priority[b.priority] ?? 3) ||
    Number(b.score || 0) - Number(a.score || 0) ||
    Number(b.days || 0) - Number(a.days || 0));
}

function groupProducts(events){
  const groups = new Map();
  events.forEach(e => {
    const key = productKey(e);
    if (!groups.has(key)) groups.set(key, {key, events: []});
    groups.get(key).events.push(e);
  });
  return [...groups.values()].map(group => {
    group.events = sortEventsByPriority(group.events);
    const main = group.events[0];
    const imageUrl = group.events.find(event => event.image_url)?.image_url || '';
    const productStatus = productStatusForEvents(group.events);
    return {...main, key: group.key, image_url: imageUrl, events: group.events, event_count: group.events.length,
      issue_names: [...new Set(group.events.map(e => e.issue || e.category || '其他'))],
      max_days: Math.max(...group.events.map(e => Number(e.days || 0))),
      product_status: productStatus};
  });
}

function formatScore(score){
  if (score === null || score === undefined || score === '') return '待计算';
  const value = Number(score);
  return Number.isFinite(value) ? Math.round(value) : '待计算';
}

function productStatusForEvents(events){
  if (!events.length) return '未完成';
  if (events.every(event => event.status === '已完成')) return '已完成';
  if (events.some(event => event.status === '处理中')) return '处理中';
  return '未完成';
}

function productImageHtml(imageUrl, className = 'detail-product-image'){
  return imageUrl
    ? `<span class="image-frame"><img class="${className}" src="${esc(imageUrl)}" alt="产品主图" onerror="this.onerror=null;this.style.display='none';this.nextElementSibling.style.display='grid'"><span class="${className} image-fallback" role="img" aria-label="主图暂不可用" style="display:none">📦</span></span>`
    : `<span class="${className} image-fallback" role="img" aria-label="暂无产品主图">📦</span>`;
}

function evidenceRowsForEvent(event){
  const basis = event.judge_basis || {};
  const fields = basis['触发字段'];
  const entries = fields && typeof fields === 'object' && !Array.isArray(fields)
    ? Object.entries(fields).filter(([, value]) => value !== null && value !== undefined && value !== '')
    : [];
  if (entries.length) {
    return entries.map(([label, value]) => `<div class="evidence-item"><span>${esc(label)}</span><b>${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</b></div>`).join('');
  }
  return '';
}

// 步骤清单：优先用后端 R6 步骤，缺失回落通用三步
function stepsForEvent(event){
  if (Array.isArray(event.steps) && event.steps.length) return event.steps;
  return [
    '核对本次判定数据与实际业务情况',
    event.recommendation || '根据本次异常判定完成人工处理',
    '记录实际动作并设置复查时间',
  ];
}

// 单条清单项（纯前端临时勾选）
function checkItemHtml(uid, idx, text){
  return `<label class="task-check-item" data-check-uid="${esc(uid)}" data-check-idx="${idx}"><span class="task-check-box">✓</span><span class="task-check-text">${esc(text)}</span></label>`;
}

// 选中的单个异常详情
function anomalyDetailHtml(e){
  const basis = e.judge_basis || {};
  const basisTxt = e.summary_reason || basis['判定过程'] || basis['命中依据'] || '系统暂未记录本次判定过程';
  const steps = stepsForEvent(e);
  const evidence = evidenceRowsForEvent(e);
  const 是交易表现 = (e.category || '').includes('交易表现');
  const adBtn = 是交易表现
    ? `<a class="btn ghost-blue" style="text-decoration:none;font-weight:700;padding:5px 11px;font-size:11px" href="${buildAdAgentUrl(e)}" target="_blank" rel="noopener" onclick="markDoingAndOpen('${esc(e.event_uid)}')" title="打开广告辅助决策 agent（自动标记为处理中）">🎯 广告决策</a>`
    : '';
  const priCls = String(e.priority || 'P2').toLowerCase();
  return `<article class="anomaly-detail" data-event-uid="${esc(e.event_uid)}">
    <div class="anomaly-detail-head">
      <span class="ab-title"><span class="priority ${priCls}">${esc(e.priority || '-')}</span><b>${esc(e.issue || e.category || '异常')}</b></span>
      <span class="ab-meta">执行分 ${formatScore(e.score)} · ${e.days || 0}天</span>
    </div>
    <div class="anomaly-detail-body">
      <div><div class="ab-section-label">判断依据</div><div class="ab-sub">${esc(basisTxt)}</div></div>
      ${evidence ? `<div><div class="ab-section-label">关键证据</div><div class="ab-evidence">${evidence}</div></div>` : ''}
      <div>
        <div class="ab-section-label">建议执行步骤（可勾选）</div>
        <div class="task-checklist">${steps.map((s, i) => checkItemHtml(e.event_uid, i, s)).join('')}</div>
        <div class="task-check-progress" data-progress-uid="${esc(e.event_uid)}">0 / ${steps.length} 已完成</div>
      </div>
      ${adBtn ? `<div class="ab-agent-row">${adBtn}</div>` : ''}
    </div>
  </article>`;
}

function renderToday(){
  const d = state.todayData; if (!d) return;
  const products = groupProducts(d.events);
  const open = d.total_products ?? d.total ?? products.length;
  const done = d.done_today_products ?? d.done_today ?? 0;
  const total = open + done;
  document.getElementById('doneNum').textContent = done;
  document.getElementById('totalNum').textContent = total;
  document.getElementById('remainNum').textContent = open;
  document.getElementById('doneTodayNum').textContent = done;
  const pct = total ? Math.round(done/total*100) : 0;
  document.getElementById('goalPct').textContent = `完成 ${pct}%`;
  document.getElementById('goalBar').style.width = pct + '%';

  const waitCnt = products.filter(e => e.product_status === '未完成').length;
  // 产品只归入其最高优先级的一个任务池，避免同一产品因包含多个异常而重复计数。
  const productP0 = products.filter(product => product.priority === 'P0').length;
  const productP1 = products.filter(product => product.priority === 'P1').length;
  const productP2 = products.filter(product => product.priority === 'P2').length;
  document.getElementById('queueList').innerHTML = `
    <div class="queue-item ${state.filters.quickMode==='P0'?'active':''}" data-queue="P0"><i class="qdot red"></i><div class="qname">必须立即处理<small>P0 · ${productP0} 个产品</small></div><div class="qcount">${productP0}</div></div>
    <div class="queue-item ${state.filters.quickMode==='P1'?'active':''}" data-queue="P1"><i class="qdot amber"></i><div class="qname">今日重点处理<small>P1 · ${productP1} 个产品</small></div><div class="qcount">${productP1}</div></div>
    <div class="queue-item ${state.filters.quickMode==='P2'?'active':''}" data-queue="P2"><i class="qdot blue"></i><div class="qname">今日常规处理<small>P2 · ${productP2} 个产品</small></div><div class="qcount">${productP2}</div></div>
    <div class="queue-item ${state.filters.quickMode==='wait'?'active':''}" data-queue="wait"><i class="qdot gray"></i><div class="qname">未完成<small>需要继续处理</small></div><div class="qcount">${waitCnt}</div></div>
    <div class="queue-item ${state.filters.quickMode==='all'?'active':''}" data-queue="all"><i class="qdot green"></i><div class="qname">全部任务<small>不筛选</small></div><div class="qcount">${products.length}</div></div>
  `;
  document.querySelectorAll('.queue-item[data-queue]').forEach(el => {
    el.onclick = () => { state.filters.quickMode = el.dataset.queue; renderToday(); };
  });

  const q = state.filters.q.toLowerCase(), pf = state.filters.priority, sf = state.filters.status, qm = state.filters.quickMode;
  const filteredProducts = products.filter(product => {
    const productSearchText = product.events.map(event =>
      `${event.parent_asin || ''}${event.product_name || ''}${event.parent_sku || ''}`
    ).join('').toLowerCase();
    if (q && !productSearchText.includes(q)) return false;
    if (pf && product.priority !== pf) return false;
    if (sf && product.product_status !== sf) return false;
    if (['P0', 'P1', 'P2'].includes(qm) && product.priority !== qm) return false;
    if (qm === 'wait' && product.product_status !== '未完成') return false;
    return true;
  });
  const body = document.getElementById('taskBody');
  if (!filteredProducts.length){
    body.innerHTML = '<div class="empty-row">当前筛选条件下无任务</div>';
    state.selectedProductKey = null;
    state.selectedEventUid = null;
    document.getElementById('detailPriority').textContent = '-';
    document.getElementById('detailScore').textContent = '执行分 -';
    document.getElementById('detailTitle').textContent = '当前筛选条件下无任务';
    document.getElementById('detailMeta').textContent = '请调整筛选条件后再查看';
    document.getElementById('detailScroll').innerHTML = '<div class="detail-empty">暂无可查看的产品任务</div>';
    ['rejectBtn','aiBtn','completeBtn'].forEach(id => document.getElementById(id).disabled = true);
    return;
  }
  body.innerHTML = filteredProducts.map((product, i) => {
    const e = product.events[0];
    const selected = product.key === state.selectedProductKey;
    return `<article class="product-task-card ${selected?'selected':''}" data-product-index="${i}" data-product-key="${esc(product.key)}">
      <div class="product-card-photo">${productImageHtml(product.image_url, 'product-card-image')}</div>
      <div class="product-card-copy"><b>${esc(e.product_name || e.parent_asin)}</b><span>${esc(e.parent_asin)} · ${esc(e.shop_account||'-')}</span><small>${product.event_count} 个异常 · ${esc(product.product_status)}</small></div>
      <div class="product-card-badges"><i class="priority ${String(e.priority || 'P2').toLowerCase()}">${esc(e.priority || '-')}</i><span>${formatScore(product.product_score ?? e.score)} 分</span></div>
    </article>`;
  }).join('');
  body.querySelectorAll('.product-task-card[data-product-index]').forEach(card => {
    card.onclick = () => {
      const product = filteredProducts[Number(card.dataset.productIndex)];
      if (product) {
        body.querySelectorAll('.product-task-card[data-product-index]').forEach(row => row.classList.remove('selected'));
        card.classList.add('selected');
        selectProduct(product.key);
      }
    };
  });
  const selectedProduct = filteredProducts.find(product => product.key === state.selectedProductKey);
  if (!selectedProduct) state.selectedProductKey = filteredProducts[0].key;
  selectProduct(state.selectedProductKey);
}

// 选中产品 → 产品摘要 + 单异常展开
function selectProduct(key){
  const productEvents = sortEventsByPriority(
    state.todayData?.events.filter(item => productKey(item) === key) || []);
  if (!productEvents.length) return;
  const main = productEvents[0];
  const productScore = productEvents.find(item => item.product_score != null)?.product_score;
  const productStatus = productStatusForEvents(productEvents);
  const imageUrl = productEvents.find(item => item.image_url)?.image_url || '';
  const qualityEvents = productEvents.filter(item => {
    const quality = item['数据状态'] || {};
    return quality['状态'] && quality['状态'] !== 'COMPLETE';
  });
  const firstQuality = qualityEvents[0]?.['数据状态'];
  const priorityCounts = ['P0', 'P1', 'P2'].map(priority => ({
    priority, count: productEvents.filter(item => item.priority === priority).length,
  })).filter(item => item.count);

  state.selectedProductKey = key;
  if (!productEvents.some(event => event.event_uid === state.selectedEventUid)) {
    state.selectedEventUid = productEvents[0].event_uid;
  }
  const selectedEvent = productEvents.find(event => event.event_uid === state.selectedEventUid) || productEvents[0];
  const adEvent = productEvents.find(event => (event.category || '').includes('交易表现')) || selectedEvent;

  document.getElementById('detailPriority').textContent = selectedEvent.priority || '产品';
  document.getElementById('detailScore').textContent = `产品执行分 ${formatScore(productScore)}`;
  document.getElementById('detailTitle').textContent = main.product_name || main.parent_asin;
  document.getElementById('detailMeta').textContent = `${main.parent_asin} · ${main.parent_sku||'-'} · ${main.shop_account||'-'} · ${main.site||''} · 巡检时间：${main.inspection_time ? main.inspection_time.slice(0, 16) : '未关联'}`;
  document.getElementById('detailScroll').scrollTop = 0;
  document.getElementById('detailScroll').innerHTML = `
    <div class="product-summary-hero">
      <div class="product-summary-image">${productImageHtml(imageUrl)}</div>
      <div class="product-summary-copy">
        <span>父 ASIN：${esc(main.parent_asin || '-')}</span>
        <span class="product-shop">店铺：${esc(main.shop_account || '-')}</span>
      </div>
      <div class="product-summary-facts">
        <span><small>执行分</small><b>${formatScore(productScore)}</b></span>
        <span><small>定位</small><b>${esc(main.tier || '-')}</b></span>
        <span><small>异常</small><b>${productEvents.length} 项</b></span>
      </div>
      <div class="product-summary-status"><span>产品状态</span><b class="status ${statusClass(productStatus)}">${esc(productStatus)}</b></div>
    </div>
    ${firstQuality ? `<div class="data-quality ${['INSUFFICIENT','FAILED','UNKNOWN'].includes(firstQuality['状态']) ? 'critical' : 'partial'}"><b>数据状态：${esc(firstQuality['状态文案'] || '部分数据不足')}</b><span>${qualityEvents.length} 个异常的辅助数据不完整，处理时请注意。</span></div>` : ''}
    <section class="summary-section product-issues-section">
      <div class="summary-section-head"><span>本产品异常</span><div class="summary-issues-tools"><small>${productEvents.length} 项 · ${priorityCounts.map(item => `${item.priority} ${item.count}`).join(' · ')}</small><a class="btn ghost-blue ad-agent-summary-btn" href="${buildAdAgentUrl(adEvent)}" target="_blank" rel="noopener" onclick="markDoingAndOpen('${esc(adEvent.event_uid)}')">点击进入广告决策Agent页面</a></div></div>
      <div class="summary-issue-grid">${productEvents.map(event => `<button class="summary-issue-card ${event.event_uid === selectedEvent.event_uid ? 'primary-issue' : ''}" type="button" data-event-uid="${esc(event.event_uid)}"><span class="priority ${String(event.priority || 'P2').toLowerCase()}">${esc(event.priority || '-')}</span><b>${esc(event.issue || event.category || '异常')}</b><span>${formatScore(event.score)} 分 · 已持续 ${event.days || 0} 天</span></button>`).join('')}</div>
    </section>
    ${anomalyDetailHtml(selectedEvent)}
    <div class="section" style="margin-top:4px"><div class="section-head"><span>执行记录</span><span id="opFormTarget">${esc(selectedEvent.issue || selectedEvent.category || '')}</span></div>
      <div class="section-body op-form">
        <div class="op-row"><label>处理结果</label><select id="opResult"><option value="">请选择</option><option>已按建议执行</option><option>部分执行</option><option>建议不适用</option><option>转人工复核</option></select></div>
        <div class="op-row"><label>实际动作</label><input id="opAction" placeholder="简要描述实际执行了什么"/></div>
        <div class="op-row"><label>复查时间</label><select id="opReview"><option value="3" selected>3天后</option><option value="">不复查</option><option value="7">7天后</option><option value="14">14天后</option></select></div>
        <div class="op-row"><label>备注</label><input id="opNotes" placeholder="补充人工判断或例外原因"/></div>
      </div>
    </div>
  `;
  document.querySelectorAll('.product-task-card').forEach(card => card.classList.toggle('selected', card.dataset.productKey === key));
  bindAnomalyBlockEvents();
  ['rejectBtn','aiBtn','completeBtn'].forEach(id => document.getElementById(id).disabled = false);
  setAiBtnText();
}

// 聚焦某个异常：重绘该产品的唯一展开详情，并绑定底部操作区。
function selectEvent(uid){ focusEvent(uid); }
function focusEvent(uid){
  const e = state.todayData?.events.find(x => x.event_uid === uid);
  if (!e) return;
  state.selectedEventUid = uid;
  selectProduct(productKey(e));
}

// 绑定异常切换和清单勾选。
function bindAnomalyBlockEvents(){
  document.querySelectorAll('#detailScroll .summary-issue-card[data-event-uid]').forEach(card => {
    card.onclick = () => focusEvent(card.dataset.eventUid);
  });
  document.querySelectorAll('#detailScroll .task-check-item').forEach(item => {
    item.onclick = (ev) => {
      ev.preventDefault();
      item.classList.toggle('done');
      const uid = item.dataset.checkUid;
      const items = [...document.querySelectorAll(`.task-check-item[data-check-uid="${CSS.escape(uid)}"]`)];
      const done = items.filter(x => x.classList.contains('done')).length;
      const prog = document.querySelector(`.task-check-progress[data-progress-uid="${CSS.escape(uid)}"]`);
      if (prog) prog.textContent = `${done} / ${items.length} 已完成`;
    };
  });
}

document.getElementById('completeBtn').onclick = async () => {
  if (!state.selectedEventUid) return;
  const days = parseInt(document.getElementById('opReview').value);
  const reviewAt = days ? (() => {
    const d = new Date(); d.setDate(d.getDate() + days);
    return d.toISOString().slice(0,10);
  })() : null;
  await api(`/api/tasks/${state.selectedEventUid}/action`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      userId: state.viewerId, action_type: '完成',
      result: document.getElementById('opResult').value,
      actual_action: document.getElementById('opAction').value,
      review_at: reviewAt,
      notes: document.getElementById('opNotes').value,
    })
  });
  toast('已标记完成');
  await loadToday();
};

document.getElementById('rejectBtn').onclick = async () => {
  if (!state.selectedEventUid) return;
  const notes = prompt('请填写不处理原因');
  if (notes === null) return;
  await api(`/api/tasks/${state.selectedEventUid}/action`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ userId: state.viewerId, action_type: '不处理', notes })
  });
  toast('已标记不处理');
  await loadToday();
};

document.getElementById('aiBtn').onclick = async () => {
  const uid = state.selectedEventUid;
  if (!uid) return;
  const e = state.todayData?.events.find(x => x.event_uid === uid);
  if (!e?.fixture_key){ toast('该事件缺 shop_id 映射，无法调用 AI'); return; }

  const btn = document.getElementById('aiBtn');
  const orig = btn.textContent;

  // 1) 先 GET 查缓存（秒返）
  try {
    const cached = await api(`/api/judge/${encodeURIComponent(e.fixture_key)}`);
    if (cached?.cached) {
      openAiJudgmentDrawer(e, cached);
      return;
    }
  } catch(_){ /* 缓存查询失败不阻塞，继续走 POST */ }

  // 2) 无缓存 → POST 触发 LLM
  btn.disabled = true; btn.textContent = 'AI 分析中… (15-30s)';
  toast('首次分析，正在调用 AI，请稍候…');
  try {
    const r = await api(`/api/judge/${encodeURIComponent(e.fixture_key)}`, {method:'POST'});
    openAiJudgmentDrawer(e, r);
    if (e) e.has_ai_result = true;
    setAiBtnText();
    toast('AI 分析完成，结果已存');
  } catch(err){
    toast('AI 分析失败：' + err.message);
  } finally {
    btn.disabled = false;
    if (btn.textContent.startsWith('AI 分析中')) btn.textContent = orig;
  }
};

function setAiBtnText(){
  const btn = document.getElementById('aiBtn');
  if (!btn) return;
  const e = state.todayData?.events.find(x => x.event_uid === state.selectedEventUid);
  btn.textContent = e?.has_ai_result ? '📄 查看 AI 分析' : '🔮 AI 深度分析';
}

// LLM 判定结果 → 复用抽屉展示
function openAiJudgmentDrawer(e, result){
  const j = result?.judgment || {};
  const pri = j['优先级信息'] || {};
  const details = j['异常明细'] || [];
  const drawer = document.getElementById('historyDrawer');
  document.getElementById('drawerTitle').textContent = `AI 深度分析 · ${e.product_name || e.parent_asin}`;
  document.getElementById('drawerSubtitle').textContent = `${e.parent_asin} · ${e.shop_account || ''} · 模型 ${result?.model || 'deepseek'}`;

  const detailHtml = details.length ? details.map((a, i) => `
    <div style="padding:10px 13px;border-bottom:1px solid #eef1f3;font-size:11px;line-height:1.6">
      <div style="display:flex;justify-content:space-between;margin-bottom:4px">
        <b>#${i+1} ${esc(a['问题点位'] || '-')}</b>
        <span><span class="priority ${(a['该条严重度']||'S2').toLowerCase()}">${esc(a['该条严重度']||'-')}</span> · 分 ${Math.round(a['该条执行分数']||0)}</span>
      </div>
      <div style="color:#4a5768">具体表现：${esc(a['具体表现']||'-')}</div>
      <div style="color:#4a5768">判断依据：${esc(a['判断依据']||'-')}</div>
      <div style="color:#345f86;margin-top:4px">💡 处理建议：${esc(a['处理建议']||'-')}</div>
      ${a['初步原因'] && a['初步原因'] !== '不适用' ? `<div style="color:#7b8794;margin-top:2px">初步原因：${esc(a['初步原因'])}</div>` : ''}
    </div>
  `).join('') : '<div style="padding:20px;text-align:center;color:#8a93a2">AI 未发现具体异常明细</div>';

  document.getElementById('drawerBody').innerHTML = `
    <div class="proof-banner">
      <i>AI</i>
      <div><b>AI 深度分析结果</b><span>基于代码巡检 + 知识库 + 全量事实数据的二次判定</span></div>
    </div>
    <section class="proof-section">
      <div class="proof-section-head"><b>综合结论</b><span>${esc(j['判定时间'] || '')}</span></div>
      <div class="proof-grid">
        <div class="proof-field"><span>执行优先级</span><b><span class="priority ${(pri['执行优先级']||'P2').toLowerCase()}">${esc(pri['执行优先级']||'-')}</span></b></div>
        <div class="proof-field"><span>产品执行分</span><b>${Math.round(pri['产品执行分数']||0)}/100</b></div>
        <div class="proof-field"><span>处理时限</span><b>${esc(pri['处理时限']||'-')}</b></div>
        <div class="proof-field"><span>父卡严重度</span><b>${esc(pri['父卡严重度']||'-')}</b></div>
      </div>
    </section>
    <section class="proof-section">
      <div class="proof-section-head"><b>AI 识别异常明细</b><span>${details.length} 条</span></div>
      ${detailHtml}
    </section>
  `;
  drawer.classList.add('show');
}

// ---- 广告决策 agent 跳转 ----
const AD_AGENT_BASE = 'https://mcp-gateway.example.com/demo/ad-asisitant-agent.html';
function buildAdAgentUrl(e){
  const q = new URLSearchParams({
    shopAccount: e.shop_account || '',
    productName: e.product_name || '',
    parentAsin: e.parent_asin || '',
    parentSellerSku: e.parent_sku || '',
    siteCode: e.site || '',
    userId: String(state.viewerId || ''),
  });
  return AD_AGENT_BASE + '?' + q.toString();
}
// 跳转前把该异常自动标记为"处理中"（仅当前状态是未完成时）
async function markDoingAndOpen(eventUid){
  const e = state.todayData?.events.find(x => x.event_uid === eventUid);
  if (e && e.status === '未完成'){
    try {
      await api(`/api/tasks/${eventUid}/action`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ userId: state.viewerId, action_type: '标记处理中', notes: '跳转广告决策 agent' })
      });
      loadToday();
    } catch(err) { console.warn('mark doing failed', err); }
  }
  return true;
}

document.getElementById('searchInput').oninput = e => { state.filters.q = e.target.value; renderToday(); };
document.getElementById('priorityFilter').onchange = e => { state.filters.priority = e.target.value === 'all' ? '' : e.target.value; renderToday(); };
document.getElementById('statusFilter').onchange = e => { state.filters.status = e.target.value === 'all' ? '' : e.target.value; renderToday(); };
document.getElementById('reloadBtn').onclick = () => loadToday();
