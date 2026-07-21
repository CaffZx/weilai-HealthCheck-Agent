var state = {
  viewerId: null, viewerName: '', targetId: null,
  role: 'operator', reports: [], users: [],
  todayData: null, allData: null, reviewData: null, healthData: null,
  historyData: null, dashboardData: null, historyRange: 'all', dashRange: '7',
  selectedEventUid: null, selectedProductKey: null,
  filters: { q:'', priority:'', status:'', sort:'priority', quickMode:'all', assign:'' },
  assignMode: false, assignSelected: new Set(),
  view: 'today',
};
var LS_KEY = 'hcAgent.viewerId';

async function api(path, opts={}){
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 1700);
}
function esc(s){ return String(s??'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtDate(iso){ if(!iso) return '-'; return String(iso).slice(0,10); }
function statusClass(s){
  if (s === '处理中') return 'doing';
  if (s === '已完成' || s === '已解决') return 'done';
  if (s === '已关闭') return 'closed';
  if (s === '待复查') return 'wait';
  return 'pending';
}
function withTarget(url){
  const sep = url.includes('?') ? '&' : '?';
  let q = `userId=${state.viewerId}`;
  if (state.targetId && state.targetId !== state.viewerId) q += `&targetId=${state.targetId}`;
  return url + sep + q;
}

// ============================================================
// 视图切换
// ============================================================
document.querySelectorAll('.nav-item').forEach(item => { item.onclick = () => switchView(item.dataset.view); });
function switchView(v){
  state.view = v;
  document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.view === v));
  document.getElementById('view' + v.charAt(0).toUpperCase() + v.slice(1)).classList.add('active');
  location.hash = v === 'today' ? '' : v;
  if (!state.viewerId) return;
  if (v === 'today' && !state.todayData) loadToday();
  if (v === 'all') loadAll();
  if (v === 'review') loadReview();
  if (v === 'history') loadHistory();
  if (v === 'dashboard') loadDashboard();
  if (v === 'health') loadHealth();
}

// ============================================================
// 登录
// ============================================================
async function initGate(){
  const saved = Number(new URLSearchParams(location.search).get('userId') || localStorage.getItem(LS_KEY));
  const users = await api('/api/users').catch(() => []);
  state.users = users;
  const sel = document.getElementById('userGateSelect');
  sel.innerHTML = users.map(u => `<option value="${u.user_id}">${esc(u.user_name)} (${u.asin_count} 个 ASIN)</option>`).join('');
  if (saved && users.some(u => u.user_id === saved)){
    await enterAs(saved);
  } else {
    document.getElementById('userGate').classList.add('show');
  }
}
document.getElementById('userGateConfirm').onclick = async () => {
  const uid = Number(document.getElementById('userGateSelect').value);
  if (!uid) return;
  await enterAs(uid);
  document.getElementById('userGate').classList.remove('show');
};

async function enterAs(uid){
  state.viewerId = uid;
  localStorage.setItem(LS_KEY, uid);
  const info = await api(`/api/reports?userId=${uid}`);
  state.viewerName = info.self?.user_name || `用户${uid}`;
  state.role = info.role;
  state.reports = info.reports || [];
  state.targetId = uid;

  document.getElementById('topUser').style.display = 'flex';
  document.getElementById('viewerName').textContent = `${state.viewerName} (${state.role === 'manager' ? '主管' : '运营'})`;
  document.getElementById('topAvatar').textContent = state.viewerName.slice(-1);

  if (state.reports.length){
    const ts = document.getElementById('targetSelect');
    ts.style.display = '';
    ts.innerHTML = `<option value="${uid}">查看自己</option>` +
      state.reports.map(r => `<option value="${r.id}">下属：${esc(r.user_name)}</option>`).join('');
    ts.onchange = () => {
      state.targetId = Number(ts.value);
      state.todayData = state.allData = state.reviewData = null;
      state.selectedEventUid = state.selectedProductKey = null;
      loadCurrentView();
    };
  }

  const sideDate = document.getElementById('sideDate');
  if (sideDate) sideDate.textContent = new Date().toLocaleDateString('zh-CN', {year:'numeric', month:'long', day:'numeric', weekday:'long'});
  document.getElementById('freshInfo').textContent = `数据更新：${new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'})}`;

  loadCurrentView();
}
function loadCurrentView(){
  if (state.view === 'today') loadToday();
  else if (state.view === 'all') loadAll();
  else if (state.view === 'review') loadReview();
  else if (state.view === 'history') loadHistory();
  else if (state.view === 'dashboard') loadDashboard();
  else if (state.view === 'health') loadHealth();
}
