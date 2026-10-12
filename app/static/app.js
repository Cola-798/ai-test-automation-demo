const state = { cases: [], selectedCase: null, plan: null, recordId: null };
let savedPlanRecords = [];
const $ = (selector) => document.querySelector(selector);

function flow(id) {
  document.querySelectorAll('.workflow li').forEach(node => node.classList.toggle('current', node.id === id));
}

function busy(button, active, label) {
  if (active) button.dataset.label = button.textContent;
  button.disabled = active;
  button.setAttribute('aria-busy', String(active));
  button.textContent = active ? label : button.dataset.label;
}

function preview(plan) {
  const panel = $('#plan-preview');
  panel.classList.remove('empty-state');
  panel.innerHTML = `<p class="plan-intent">${escapeHtml(plan.intent)}</p>${plan.steps.map((step, index) => `<div class="plan-step"><span>${index + 1}</span><div>${escapeHtml(step.title)}${step.allow_ai_repair ? ' <span class="badge">允许 AI 辅助</span>' : ''}</div></div>`).join('')}<div class="oracle-preview"><strong>验收标准 · ${plan.oracles.length} 项</strong>${plan.oracles.map(item => `<p>${escapeHtml(item.title)}</p>`).join('')}</div>`;
}

function notice(message, kind = "info") {
  const node = $("#notice");
  node.textContent = message;
  node.dataset.kind = kind;
  node.hidden = false;
}

async function loadModelConfig() {
  try {
    const config = await api('/api/model-config');
    const plan = config.plan_model || {};
    const vision = config.vision_model || {};
    $('#plan-model-name').textContent = plan.configured ? plan.model : '尚未选择模型';
    $('#plan-model-status').textContent = plan.gateway_configured
      ? `${plan.gateway_source === 'testai_legacy' ? 'TestAI 中转站' : '独立中转站'} · ${plan.configured ? '结构化解析已启用' : '等待自动识别'}`
      : '未检测到中转站地址和 API Key';
    $('#auto-model-button').disabled = !plan.gateway_configured;
    $('#vision-model-name').textContent = vision.configured ? vision.model : '未配置';
    $('#vision-model-status').textContent = vision.configured
      ? `阿里百炼官方渠道 · 置信度 ≥ ${Number(vision.minimum_confidence).toFixed(2)} 才执行`
      : '视觉兜底未启用';
    $('#vision-model-badge').textContent = vision.configured ? '官方接口已配置' : '未配置';
    $('#vision-model-badge').className = `badge ${vision.configured ? 'ok' : 'danger'}`;
    const ready = plan.configured && vision.configured;
    $('#model-config-summary').textContent = ready ? '两类模型已隔离' : '等待完成配置';
    $('#model-config-summary').className = `badge ${ready ? 'ok' : 'muted'}`;
  } catch (error) {
    $('#model-config-summary').textContent = '读取失败';
    $('#model-config-summary').className = 'badge danger';
  }
}

async function loadDevicePlatforms() {
  try {
    const payload = await api('/api/device-platforms');
    const select = $('#execution-platform');
    payload.platforms.forEach(item => {
      const option = select.querySelector(`option[value="${item.platform}"]`);
      if (!option) return;
      option.disabled = !item.executable;
      option.textContent = `${item.label} · ${item.verified ? '已验证' : item.configured ? '待建立定位库' : '待配置'}`;
    });
    const coverage = payload.locator_coverage || {};
    const android = coverage.android || {configured_targets: 0, total_targets: 0};
    const deviceSelect = $('#execution-device');
    const devices = payload.devices || [];
    const authorizedDevices = devices.filter(item => item.authorized);
    deviceSelect.innerHTML = devices.length
      ? '<option value="">请选择执行设备</option>' + devices.map(item => `<option value="${escapeHtml(item.udid)}" ${item.authorized ? '' : 'disabled'}>${escapeHtml(item.label || item.udid)} · ${escapeHtml(item.status)}</option>`).join('')
      : '<option value="">未检测到 Android 设备</option>';
    deviceSelect.disabled = authorizedDevices.length === 0;
    if (authorizedDevices.length === 1) deviceSelect.value = authorizedDevices[0].udid;
    $('#platform-readiness').textContent = `Android 定位库 ${android.configured_targets}/${android.total_targets}；${devices.length ? `检测到 ${devices.length} 台设备` : '未检测到设备'}，设备授权后才可执行。`;
  } catch (error) {
    $('#platform-readiness').textContent = `设备平台配置读取失败：${error.message}`;
  }
}

$('#auto-model-button').addEventListener('click', async () => {
  const button = $('#auto-model-button');
  const results = $('#model-probe-results');
  busy(button, true, '正在发现与测试…');
  notice('正在读取中转站模型，并测试结构化用例解析能力……');
  try {
    const payload = await api('/api/plan-models/auto-select', { method: 'POST' });
    results.hidden = false;
    results.innerHTML = payload.probes.map(item => `<div class="probe-result"><span class="badge ${item.passed ? 'ok' : 'danger'}">${item.passed ? '通过' : '未通过'}</span><strong>${escapeHtml(item.model)}</strong><small>${escapeHtml(item.message)} · ${item.latency_ms} ms</small></div>`).join('');
    if (!payload.selected_model) throw new Error('候选模型均未通过结构化解析探针');
    notice(`已从 ${payload.candidate_count} 个文本候选中选择 ${payload.selected_model}。`, 'success');
    await loadModelConfig();
  } catch (error) {
    notice(`模型自动识别失败：${error.message}`, 'error');
  } finally {
    busy(button, false);
  }
});

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || payload));
  return payload;
}

async function loadImportedCases() {
  try {
    state.cases = await api('/api/cases');
    $('#case-count').textContent = state.cases.length;
    if (state.cases.length) {
      $('#case-search').disabled = false;
      renderCases();
    }
  } catch (error) {
    notice(`已导入用例读取失败：${error.message}`, 'error');
  }
}

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const button = event.currentTarget.querySelector('button');
  busy(button, true, '正在读取…');
  try {
    state.cases = await api("/api/import", { method: "POST", body: form });
    state.selectedCase = null;
    $('#compile-button').disabled = true;
    $('#case-search').value = '';
    flow('flow-import');
    renderCases();
    $('#case-search').disabled = false;
    $('#case-count').textContent = state.cases.length;
    notice(`已读取 ${state.cases.length} 条用例，请选择一条生成计划。`, "success");
  } catch (error) { notice(error.message, "error"); }
  finally { busy(button, false); }
});

function renderCases() {
  const list = $("#case-list");
  list.classList.remove("empty-state");
  const query = $('#case-search').value.trim().toLowerCase();
  list.innerHTML = state.cases.map((item, index) => ({item, index})).filter(({item}) => `${item.case_id} ${item.title}`.toLowerCase().includes(query)).map(({item, index}) => `
    <button class="case-item ${state.selectedCase === item ? 'selected' : ''}" aria-pressed="${state.selectedCase === item}" type="button" data-index="${index}">
      <span class="case-id">${escapeHtml(item.case_id)}</span>
      <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.priority || "未分级")} · ${escapeHtml(item.case_type || "未分类")}</small></span>
    </button>`).join("");
  list.querySelectorAll(".case-item").forEach((button) => button.addEventListener("click", () => {
    list.querySelectorAll(".case-item").forEach((item) => { item.classList.remove("selected"); item.setAttribute('aria-pressed', 'false'); });
    button.classList.add("selected");
    button.setAttribute('aria-pressed', 'true');
    state.selectedCase = state.cases[Number(button.dataset.index)];
    $("#compile-button").disabled = false;
    flow('flow-plan');
  }));
  if (!list.children.length) list.innerHTML = '<p class="empty-state">没有匹配的用例，请换个关键词。</p>';
}

$("#compile-button").addEventListener("click", async () => {
  if (!state.selectedCase) return;
  busy($('#compile-button'), true, 'AI 正在解析…');
  notice("正在编译执行计划……");
  try {
    const data = await api("/api/compile", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.selectedCase) });
    state.plan = data.plan;
    state.recordId = null;
    preview(data.plan);
    flow('flow-confirm');
    $("#plan-editor").value = JSON.stringify(data.plan, null, 2);
    renderValidation(data.validation);
    $("#save-button").disabled = false;
    $("#confirm-button").disabled = true;
    notice(`计划已生成，编译模式：${data.plan.compiler_mode}。请先审核。`, data.plan.compiler_mode === "ai" ? "success" : "warning");
  } catch (error) { notice(error.message, "error"); }
  finally { busy($('#compile-button'), false); }
});

$("#save-button").addEventListener("click", async () => {
  try {
    const plan = JSON.parse($("#plan-editor").value);
    preview(plan);
    const record = await api("/api/plans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(plan) });
    state.recordId = record.record_id;
    renderValidation(record.validation);
    $("#confirm-button").disabled = !record.validation.valid;
    notice(record.validation.valid ? "草稿已保存并通过结构校验，可以由 QA 确认；定位仍需真机验证。" : "草稿已保存，但结构校验未通过。", record.validation.valid ? "success" : "warning");
    loadPlans();
  } catch (error) { notice(`保存失败：${error.message}`, "error"); }
});

$("#confirm-button").addEventListener("click", async () => {
  if (!state.recordId) return;
  const qaName = $("#qa-name").value.trim();
  if (!qaName) return notice("请填写 QA 确认人。", "error");
  try {
    const record = await api(`/api/plans/${state.recordId}/confirm?qa_name=${encodeURIComponent(qaName)}`, { method: "POST" });
    state.plan = record.plan;
    $("#plan-editor").value = JSON.stringify(record.plan, null, 2);
    $("#confirm-button").disabled = true;
    notice("计划已经 QA 确认，可进入执行队列。", "success");
    flow('flow-execute');
    loadPlans();
  } catch (error) { notice(`确认失败：${error.message}`, "error"); }
});

function renderValidation(validation) {
  const badge = $("#validation-badge");
  badge.textContent = validation.valid ? "结构校验通过" : `${validation.issues.length} 个结构问题`;
  badge.className = `badge ${validation.valid ? "ok" : "danger"}`;
  $("#validation-list").innerHTML = validation.issues.map((issue) => `<p><strong>${escapeHtml(issue.path)}</strong> ${escapeHtml(issue.message)}</p>`).join("");
}

async function loadPlans() {
  try {
    const plans = await api("/api/plans");
    savedPlanRecords = plans.filter(record => !record.plan.source.case_id.startsWith('CLEANUP-') && record.plan.status !== 'candidate');
    const latestPlans = latestPlanRecords(savedPlanRecords);
    const category = $('#saved-plan-category');
    const selectedCategory = category.value;
    const categories = [...new Set(latestPlans.map(record => record.plan.source.case_type || '未分类'))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
    category.innerHTML = '<option value="">全部分类</option>' + categories.map(item => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join('');
    category.value = categories.includes(selectedCategory) ? selectedCategory : '';
    renderSavedPlans();
  } catch (error) { notice(error.message, "error"); }
}

function latestPlanRecords(records) {
  const latestByCase = new Map();
  records.forEach((record) => {
    const caseId = record.plan.source.case_id;
    const current = latestByCase.get(caseId);
    if (!current || record.plan.version > current.plan.version || (record.plan.version === current.plan.version && record.record_id > current.record_id)) {
      latestByCase.set(caseId, record);
    }
  });
  return [...latestByCase.values()].sort((left, right) => right.record_id - left.record_id);
}

function renderSavedPlans() {
  const target = $("#saved-plans");
  const query = $('#saved-plan-search').value.trim().toLowerCase();
  const category = $('#saved-plan-category').value;
  const statusText = { draft: "草稿", confirmed: "QA 已确认", validated: "真机已验证", candidate: "修复候选验证中" };
  const latestPlans = latestPlanRecords(savedPlanRecords);
  const filtered = latestPlans.filter((record) => {
    const source = record.plan.source;
    const type = source.case_type || '未分类';
    const searchable = `${source.case_id} ${source.title} ${source.priority || ''} ${type}`.toLowerCase();
    return (!query || searchable.includes(query)) && (!category || type === category);
  });
  target.classList.toggle("empty-state", filtered.length === 0);
  $('#saved-plan-count').textContent = `${filtered.length}/${latestPlans.length} 用例 · ${savedPlanRecords.length} 版本`;
  $('#saved-plan-count').classList.remove('metric-loading');
  $('#saved-plan-count').removeAttribute('aria-busy');
  target.innerHTML = filtered.length ? filtered.map((record) => { const stateMode = record.plan.state_contract?.mode || 'legacy_unmanaged'; const stateLabel = stateMode === 'legacy_unmanaged' ? '旧版状态管理' : `状态管理：${stateMode}`; return `<article class="saved-plan"><span class="badge ${record.plan.status !== "draft" ? "ok" : "muted"}">${statusText[record.plan.status] || record.plan.status}</span><div class="saved-plan-copy"><strong>${escapeHtml(record.plan.source.case_id)} · ${escapeHtml(record.plan.source.title)}</strong><small>${escapeHtml(record.plan.source.case_type || '未分类')} · 最新 v${record.plan.version} · ${escapeHtml(record.plan.compiler_mode)} · ${escapeHtml(stateLabel)} · ${record.validation.valid ? "结构校验通过" : "结构校验失败"}</small></div><button class="button secondary execute-button" type="button" data-record-id="${record.record_id}" ${record.plan.status === "draft" ? "disabled" : ""}>执行真机测试</button></article>`; }).join("") : (savedPlanRecords.length ? "没有匹配的计划，请调整检索条件。" : "暂无计划");
  target.querySelectorAll(".execute-button").forEach((button) => button.addEventListener("click", () => executePlan(button)));
}

async function executePlan(button) {
  const platform = $('#execution-platform').value;
  const device = $('#execution-device').value;
  if (!device) {
    notice('请先选择执行设备，避免多台手机的测试记录混淆。', 'warning');
    return;
  }
  busy(button, true, '真机执行中…');
  notice('pytest 正在调度 Appium 执行 Android 真机计划，请保持设备解锁……');
  try {
    const query = new URLSearchParams({platform});
    if (device) query.set('device_udid', device);
    const result = await api(`/api/plans/${button.dataset.recordId}/execute?${query.toString()}`, { method: "POST" });
    notice(`执行完成：${result.classification}。正在打开执行看板。`, result.status === "passed" ? "success" : "warning");
    window.location.href = `/dashboard?run=${encodeURIComponent(result.run_id)}`;
  } catch (error) {
    busy(button, false);
    notice(`执行失败：${error.message}`, "error");
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
}

$("#refresh-button").addEventListener("click", loadPlans);
$('#saved-plan-search').addEventListener('input', renderSavedPlans);
$('#saved-plan-category').addEventListener('change', renderSavedPlans);
$('#case-search').addEventListener('input', renderCases);
$('#plan-editor').addEventListener('input', () => {
  state.recordId = null;
  $('#confirm-button').disabled = true;
  $('#validation-badge').textContent = '已修改 · 请重新保存';
  $('#validation-badge').className = 'badge muted';
});
loadPlans();
loadImportedCases();
loadModelConfig();
loadDevicePlatforms();
