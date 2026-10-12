const $ = selector => document.querySelector(selector);
let searchTimer = null;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));
}

function formatTime(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN');
}

async function api(url) {
  const response = await fetch(url);
  const payload = await response.json().catch(() => ([]));
  if (!response.ok) throw new Error('历史记录读取失败');
  return payload;
}

async function loadHistory() {
  const params = new URLSearchParams();
  const keyword = $('#history-keyword').value.trim();
  const device = $('#history-device').value;
  if (keyword) params.set('keyword', keyword);
  if (device) params.set('device_udid', device);
  const items = await api(`/api/reviews/history?${params}`);
  $('#history-summary').textContent = `${items.length} 条历史失败记录`;
  $('#history-count').textContent = `${items.length} 条`;
  $('#history-count').classList.remove('metric-loading');
  $('#history-count').removeAttribute('aria-busy');
  $('#history-list').innerHTML = items.length ? items.map(item => `<tr>
    <td>${escapeHtml(formatTime(item.run_finished_at || item.created_at))}</td>
    <td><strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(item.case_title)}</small></td>
    <td>${escapeHtml(item.device_udid || '未知设备')}</td>
    <td>${item.is_stability ? '稳定性验证' : '普通执行'}</td>
    <td>${escapeHtml(item.observed_label)}</td>
    <td><a href="${escapeHtml(item.report_url)}" target="_blank">查看报告</a></td>
  </tr>`).join('') : '<tr><td colspan="6" class="table-empty">没有匹配的历史失败记录</td></tr>';
}

$('#history-keyword').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadHistory(), 220);
});
$('#history-device').addEventListener('change', loadHistory);

api('/api/device-platforms').then(payload => {
  const devices = payload.devices || [];
  $('#history-device').innerHTML = '<option value="">全部设备</option>' + devices.map(item => `<option value="${escapeHtml(item.udid)}">${escapeHtml(item.label || item.udid)}</option>`).join('');
}).catch(() => {});
loadHistory().catch(error => { $('#history-list').innerHTML = `<tr><td colspan="6" class="table-empty">${escapeHtml(error.message)}</td></tr>`; });
