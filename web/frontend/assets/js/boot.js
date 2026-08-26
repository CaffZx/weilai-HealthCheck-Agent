// ============================================================
// 启动
// ============================================================
const initialHash = location.hash.replace('#','');
if (['all','review','history','dashboard','health','today'].includes(initialHash) && initialHash !== 'today') {
  state.view = initialHash;
  document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.view === state.view));
  document.getElementById('view' + state.view.charAt(0).toUpperCase() + state.view.slice(1)).classList.add('active');
}
initGate();
