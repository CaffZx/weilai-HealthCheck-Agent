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

  const effectBtn = (r, val, label) => {
    const cls = val==='变好'?'good':val==='变差'?'bad':'watch';
    const active = r.effect === val;
    return `<button class="effect-btn ${cls} ${active?'active':''}" onclick="setReviewEffect('${esc(r.event_uid)}', '${val}')">${label}</button>`;
  };
  const conclBtn = (r, val, label) => {
    return `<button class="concl-btn" onclick="setReviewConclusion('${esc(r.event_uid)}', '${val}', ${r.effect ? 'null' : "'${val}'"})">${label}</button>`;
  };

  document.getElementById('reviewBody').innerHTML = d.records.length ? d.records.map(r => `
    <tr data-uid="${esc(r.event_uid)}">
      <td><div class="mini-product"><b>${esc(r['父ASIN']||'-')}</b><span>${esc(r['店铺账号']||'-')}</span></div></td>
      <td>${esc(r['问题点位']||'-')}</td>
      <td>${esc(r.actual_action||r.action_type||'-')}<div style="color:#8a93a2;font-size:10px;margin-top:3px">${esc(r.notes||'')}</div></td>
      <td>${fmtDate(r.review_at)}</td>
      <td>
        <div class="review-effect-btns">
          ${effectBtn(r, '变好', '✓ 变好')}
          ${effectBtn(r, '待观察', '⋯ 待观察')}
          ${effectBtn(r, '变差', '✗ 变差')}
        </div>
      </td>
      <td>
        <div class="review-concl-btns">
          <button class="concl-btn" onclick="setReviewConclusion('${esc(r.event_uid)}', '保留动作')" title="不再干预，本次调整已生效">保留</button>
          <button class="concl-btn" onclick="setReviewConclusion('${esc(r.event_uid)}', '继续观察')" title="样本不足，暂不结论">观察</button>
          <button class="concl-btn danger" onclick="setReviewConclusion('${esc(r.event_uid)}', '二次调整')" title="效果不佳，重新拉回今日任务池">二次调整</button>
        </div>
      </td>
    </tr>
  `).join('') : '<tr class="empty-row"><td colspan="6">当前区间无到期复查记录</td></tr>';
}

// 复盘效果标记（变好/变差/待观察）
async function setReviewEffect(eventUid, effect){
  try {
    await api(`/api/review/${encodeURIComponent(eventUid)}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ userId: state.viewerId, effect }),
    });
    toast(`已标记：${effect}`);
    await loadReview();
  } catch(e){ toast('标记失败：' + e.message); }
}

// 复盘结论（保留/继续观察/二次调整）
async function setReviewConclusion(eventUid, conclusion){
  // 结论必须先标效果
  const rec = state.reviewData?.records.find(r => r.event_uid === eventUid);
  let effect = rec?.effect;
  if (!effect){
    effect = prompt('先记录本次调整效果（变好 / 变差 / 待观察）：');
    if (!['变好','变差','待观察'].includes(effect)) { toast('取消'); return; }
  }
  let notes = null;
  if (conclusion === '二次调整'){
    notes = prompt('二次调整原因（选填）：') || null;
  }
  try {
    await api(`/api/review/${encodeURIComponent(eventUid)}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ userId: state.viewerId, effect, conclusion, notes }),
    });
    toast(`已记录：${conclusion}${conclusion === '二次调整' ? '（事件已拉回任务池）' : ''}`);
    // 二次调整会新增待复查 action，可能会改今日任务池，一并刷新
    state.todayData = null;
    await loadReview();
  } catch(e){ toast('记录失败：' + e.message); }
}

document.getElementById('reviewPeriod').onchange = () => loadReview();
document.getElementById('reviewReloadBtn').onclick = () => loadReview();
