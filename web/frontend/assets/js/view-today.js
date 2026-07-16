// ============================================================
// 视图1：今日任务
// ============================================================
async function loadToday(){
  document.getElementById('taskBody').innerHTML = '<tr><td colspan="8" class="loading">加载中…</td></tr>';
  try {
    state.todayData = await api(withTarget('/api/tasks/today'));
    renderToday();
  } catch(e){
    document.getElementById('taskBody').innerHTML = `<tr><td colspan="8" class="loading">加载失败：${esc(e.message)}</td></tr>`;
  }
}

function renderToday(){
  const d = state.todayData; if (!d) return;
  const total = d.total, done = d.done_today;
  document.getElementById('doneNum').textContent = done;
  document.getElementById('totalNum').textContent = total;
  document.getElementById('remainNum').textContent = Math.max(0, total - done);
  document.getElementById('doneTodayNum').textContent = done;
  const pct = total ? Math.round(done/total*100) : 0;
  document.getElementById('goalPct').textContent = `完成 ${pct}%`;
  document.getElementById('goalBar').style.width = pct + '%';

  const p0Und = d.events.filter(e => e.priority==='P0' && e.status !== '已完成').length;
  const waitCnt = d.events.filter(e=>e.status==='待复查').length;
  document.getElementById('queueList').innerHTML = `
    <div class="queue-item ${state.filters.quickMode==='P0'?'active':''}" data-queue="P0"><i class="qdot red"></i><div class="qname">必须立即处理<small>P0 · ${d.p0} 条</small></div><div class="qcount">${d.p0}</div></div>
    <div class="queue-item ${state.filters.quickMode==='P1'?'active':''}" data-queue="P1"><i class="qdot amber"></i><div class="qname">今日重点处理<small>P1 · ${d.p1} 条</small></div><div class="qcount">${d.p1}</div></div>
    <div class="queue-item ${state.filters.quickMode==='P2'?'active':''}" data-queue="P2"><i class="qdot blue"></i><div class="qname">今日常规处理<small>P2 · ${d.p2} 条</small></div><div class="qcount">${d.p2}</div></div>
    <div class="queue-item ${state.filters.quickMode==='wait'?'active':''}" data-queue="wait"><i class="qdot gray"></i><div class="qname">待复查<small>已处理待观察</small></div><div class="qcount">${waitCnt}</div></div>
    <div class="queue-item ${state.filters.quickMode==='all'?'active':''}" data-queue="all"><i class="qdot green"></i><div class="qname">全部任务<small>不筛选</small></div><div class="qcount">${d.total}</div></div>
  `;
  document.querySelectorAll('.queue-item[data-queue]').forEach(el => {
    el.onclick = () => { state.filters.quickMode = el.dataset.queue; renderToday(); };
  });

  const catMap = {};
  d.events.forEach(e => { const c = e.category || '其他'; catMap[c] = (catMap[c]||0)+1; });
  const maxCat = Math.max(1, ...Object.values(catMap));
  document.getElementById('distList').innerHTML = Object.entries(catMap).sort((a,b)=>b[1]-a[1]).map(([k,v]) =>
    `<div class="dist-row"><div class="dist-top"><span>${esc(k)}</span><b>${v}</b></div><div class="mini-bar"><i style="width:${v/maxCat*100}%"></i></div></div>`
  ).join('');

  document.getElementById('summaryCards').innerHTML = `
    <div class="sum-card primary" data-action="all"><div class="sum-label"><span>今日任务进度</span><b class="good-text">${pct}%</b></div><div class="sum-value">${done} / ${total}</div><div class="sum-sub">全部开放任务</div></div>
    <div class="sum-card" data-action="P0"><div class="sum-label"><span>P0 未完成</span><span>立即处理</span></div><div class="sum-value danger-text">${p0Und}</div><div class="sum-sub">${d.events.filter(e=>e.priority==='P0'&&e.days>=3).length} 条 ≥3 天</div></div>
    <div class="sum-card" data-action="P1"><div class="sum-label"><span>P1 数量</span><span>重点处理</span></div><div class="sum-value">${d.p1}</div><div class="sum-sub">影响经营目标</div></div>
    <div class="sum-card" data-action="wait"><div class="sum-label"><span>待复查</span><span>已处理</span></div><div class="sum-value">${waitCnt}</div><div class="sum-sub">到期后重新判定</div></div>
    <div class="sum-card" data-action="P2"><div class="sum-label"><span>P2</span><span>常规</span></div><div class="sum-value">${d.p2}</div><div class="sum-sub">可优化事项</div></div>
  `;
  document.querySelectorAll('.sum-card[data-action]').forEach(el => {
    el.onclick = () => { state.filters.quickMode = el.dataset.action; renderToday(); };
  });

  const q = state.filters.q.toLowerCase(), pf = state.filters.priority, sf = state.filters.status, qm = state.filters.quickMode;
  let rows = d.events.filter(e => {
    if (q && !`${e.parent_asin||''}${e.product_name||''}${e.parent_sku||''}`.toLowerCase().includes(q)) return false;
    if (pf && e.priority !== pf) return false;
    if (sf && e.status !== sf) return false;
    if (qm === 'P0' && e.priority !== 'P0') return false;
    if (qm === 'P1' && e.priority !== 'P1') return false;
    if (qm === 'P2' && e.priority !== 'P2') return false;
    if (qm === 'wait' && e.status !== '待复查') return false;
    return true;
  });
  document.getElementById('taskResultCount').textContent = rows.length;

  const body = document.getElementById('taskBody');
  if (!rows.length){
    body.innerHTML = '<tr class="empty-row"><td colspan="8">当前筛选条件下无任务</td></tr>';
  } else {
    body.innerHTML = rows.map((e, i) => `
      <tr data-uid="${esc(e.event_uid)}" ${e.event_uid===state.selectedEventUid?'class="selected"':''}>
        <td class="rank">${String(i+1).padStart(2,'0')}</td>
        <td><span class="priority ${e.priority.toLowerCase()}">${e.priority}</span></td>
        <td><div class="prod"><div class="thumb">${e.image_url?`<img src="${esc(e.image_url)}" onerror="this.style.display='none'">`:'📦'}</div><div class="prod-txt"><div class="prod-name">${esc(e.product_name || e.parent_asin)}</div><div class="prod-meta">${esc(e.parent_asin)} · ${esc(e.parent_sku||'-')} · ${esc(e.shop_account||'-')}</div></div></div></td>
        <td><div class="score">${Math.round(e.score)}<small>/100</small></div></td>
        <td><span class="tag ${e.severity.toLowerCase()}">${esc(e.issue||e.category)}</span>${e.tier?`<span class="tag t">${e.tier}</span>`:''}${e.days>=3?`<span class="tag">${e.days}天</span>`:''}</td>
        <td class="metric">${esc(e.category||'-')}${e.variant?`<br><span style="color:#7d8797">变体：${esc(e.variant)}</span>`:''}</td>
        <td class="action">${esc(e.category||'-')}</td>
        <td><span class="status ${statusClass(e.status)}">${esc(e.status)}</span></td>
      </tr>
    `).join('');
    body.querySelectorAll('tr[data-uid]').forEach(tr => { tr.onclick = () => selectEvent(tr.dataset.uid); });
  }
}

function selectEvent(uid){
  const e = state.todayData?.events.find(x => x.event_uid === uid);
  if (!e) return;
  state.selectedEventUid = uid;
  document.querySelectorAll('#taskBody tr').forEach(tr => tr.classList.toggle('selected', tr.dataset.uid === uid));

  document.getElementById('detailPriority').textContent = e.priority;
  document.getElementById('detailScore').textContent = `执行分 ${Math.round(e.score)}`;
  document.getElementById('detailTitle').textContent = e.product_name || e.parent_asin;
  document.getElementById('detailMeta').textContent = `${e.parent_asin} · ${e.parent_sku||'-'} · ${e.shop_account||'-'} · ${e.site||''}`;

  const basis = e.judge_basis || {};
  const basisTxt = basis['命中依据'] || basis['判定过程'] || JSON.stringify(basis);

  // 交易表现类异常 → 显示"广告决策"跳转按钮
  const 是交易表现 = (e.category || '').includes('交易表现');
  const adBtn = 是交易表现 ? `<a class="btn ghost-blue" style="margin-left:8px;text-decoration:none;font-weight:700;padding:4px 10px;font-size:11px" href="${buildAdAgentUrl(e)}" target="_blank" rel="noopener" onclick="markDoingAndOpen('${esc(e.event_uid)}')" title="打开广告辅助决策 agent（自动标记为处理中）">🎯 广告决策</a>` : '';

  document.getElementById('detailScroll').innerHTML = `
    <div class="decision">
      <div class="decision-top"><span class="decision-label">异常判定</span><span>${adBtn}<span class="risk-badge" style="margin-left:6px">${e.priority} · ${e.priority==='P0'?'立即处理':e.priority==='P1'?'今日处理':'常规处理'}</span></span></div>
      <h3>${esc(e.issue)} · ${esc(e.category)}</h3>
      <p>${esc(basisTxt)}</p>
    </div>
    <div class="section">
      <div class="section-head"><span>为什么排在前面</span><span>判定链路</span></div>
      <div class="section-body why">
        <div class="why-item"><b class="danger-text">${e.severity}</b><span>异常严重度</span></div>
        <div class="why-item"><b>${e.tier||'-'}</b><span>产品定位</span></div>
        <div class="why-item"><b>${e.days} 天</b><span>持续时间</span></div>
        <div class="why-item"><b>${Math.round(e.score)}</b><span>执行分</span></div>
      </div>
    </div>
    <div class="section">
      <div class="section-head"><span>事件信息</span><span>event_uid</span></div>
      <div class="section-body evidence">
        <div class="e-row"><span>首次命中</span><b>${fmtDate(e.first_seen)}</b></div>
        <div class="e-row"><span>最近命中</span><b>${fmtDate(e.last_seen)}</b></div>
        <div class="e-row"><span>作用层级</span><b>${esc(e.scope||'-')}</b></div>
        <div class="e-row"><span>命中变体</span><b>${esc(e.variant||'-')} ${e.variant_importance?'· '+esc(e.variant_importance):''}</b></div>
        <div class="e-row"><span>event_uid</span><b style="font-family:monospace;font-size:10px">${esc(e.event_uid)}</b></div>
      </div>
    </div>
    ${e.latest_action ? `
    <div class="section">
      <div class="section-head"><span>最近处理记录</span><span>${fmtDate(e.latest_action.created_at)}</span></div>
      <div class="section-body evidence">
        <div class="e-row"><span>动作</span><b>${esc(e.latest_action.action_type)}</b></div>
        ${e.latest_action.result?`<div class="e-row"><span>结果</span><b>${esc(e.latest_action.result)}</b></div>`:''}
        ${e.latest_action.actual_action?`<div class="e-row"><span>实际动作</span><b>${esc(e.latest_action.actual_action)}</b></div>`:''}
        ${e.latest_action.notes?`<div class="e-row"><span>备注</span><b>${esc(e.latest_action.notes)}</b></div>`:''}
      </div>
    </div>` : ''}
    <div class="section">
      <div class="section-head"><span>执行记录（完成时写入）</span><span>闭环必填</span></div>
      <div class="section-body op-form">
        <div class="op-row"><label>处理结果</label><select id="opResult"><option value="">请选择</option><option>已按建议执行</option><option>部分执行</option><option>建议不适用</option><option>转人工复核</option></select></div>
        <div class="op-row"><label>实际动作</label><input id="opAction" placeholder="简要描述实际执行了什么"/></div>
        <div class="op-row"><label>复查时间</label><select id="opReview"><option value="">不复查</option><option value="3">3天后</option><option value="7">7天后</option><option value="14">14天后</option></select></div>
        <div class="op-row"><label>备注</label><input id="opNotes" placeholder="补充人工判断或例外原因"/></div>
      </div>
    </div>
  `;
  ['rejectBtn','aiBtn','completeBtn'].forEach(id => document.getElementById(id).disabled = false);
  setAiBtnText();  // 根据 has_ai_result 更新按钮文本
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
  const first = state.todayData?.events[0];
  if (first) selectEvent(first.event_uid);
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
  } catch(_){ /* 缓存查询失败也不阻塞，继续走 POST */ }

  // 2) 无缓存 → POST 触发 LLM
  btn.disabled = true; btn.textContent = 'AI 分析中… (15-30s)';
  toast('首次分析，正在调用 AI，请稍候…');
  try {
    const r = await api(`/api/judge/${encodeURIComponent(e.fixture_key)}`, {method:'POST'});
    openAiJudgmentDrawer(e, r);
    // 标记事件已分析（避免刷新前状态不同步）
    if (e) e.has_ai_result = true;
    // 更新按钮文本
    setAiBtnText();
    toast('AI 分析完成，结果已存');
  } catch(err){
    toast('AI 分析失败：' + err.message);
  } finally {
    btn.disabled = false;
    if (btn.textContent.startsWith('AI 分析中')) btn.textContent = orig;
  }
};

// 按钮文本随当前事件的 has_ai_result 状态变化
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
// 跳转前把该异常自动标记为"处理中"（仅当前状态是新发现/待处理时）
async function markDoingAndOpen(eventUid){
  const e = state.todayData?.events.find(x => x.event_uid === eventUid);
  if (e && (e.status === '新发现' || e.status === '待处理')){
    try {
      await api(`/api/tasks/${eventUid}/action`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ userId: state.viewerId, action_type: '标记处理中', notes: '跳转广告决策 agent' })
      });
      loadToday();  // 刷新状态
    } catch(err) { console.warn('mark doing failed', err); }
  }
  return true;  // 不阻止跳转
}

document.getElementById('searchInput').oninput = e => { state.filters.q = e.target.value; renderToday(); };
document.getElementById('priorityFilter').onchange = e => { state.filters.priority = e.target.value === 'all' ? '' : e.target.value; renderToday(); };
document.getElementById('statusFilter').onchange = e => { state.filters.status = e.target.value === 'all' ? '' : e.target.value; renderToday(); };
document.getElementById('reloadBtn').onclick = () => loadToday();

