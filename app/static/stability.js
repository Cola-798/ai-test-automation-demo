const $ = (selector, root = document) => root.querySelector(selector);
let plans = [];
let batches = [];
let currentBatch = null;
let currentPlanRecordId = null;
let pollTimer = null;
let devicePlatforms = [];
let selectedDeviceUdid = '';
let activeScope = '';
// api('/api/stability/summary') is extended with the selected device below.

const classificationLabels = {
  NORMAL_PASS: '正常通过', AI_REPAIR_SUCCESS: 'AI 修复后通过', AI_VISUAL_ORACLE_PASS: 'AI 视觉验收后通过',
  ENVIRONMENT_RECOVERED_PASS: '环境恢复后通过', ACCEPTANCE_MISMATCH: '验收不一致（待确认）',
  PRODUCT_FAILURE: '历史验收不一致（待确认）',
  AUTOMATION_FAILURE_REPAIRABLE: '定位失败（可修复）', AUTOMATION_FAILURE: '自动化失败',
  ENVIRONMENT_EXCEPTION: '设备或环境异常', TEST_DATA_FAILURE: '测试数据阻塞',
};
const statusLabels = {
  draft: '待执行', running: '执行中', completed: '已完成', stopped: '已停止',
  interrupted: '意外中断', queued: '排队中', cancelled: '已取消',
};

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail || payload));
  return payload;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));
}

function notice(message, kind = 'info') {
  const node = $('#stability-notice');
  node.textContent = message; node.dataset.kind = kind; node.hidden = false;
}

function busy(button, active, label) {
  if (active) button.dataset.label = button.textContent;
  button.disabled = active;
  button.setAttribute('aria-busy', String(active));
  button.textContent = active ? label : button.dataset.label;
}

function selectedPlanIds() {
  return [...document.querySelectorAll('input[name="plan_record_id"]:checked')].map(input => Number(input.value));
}

function updateAttemptEstimate() {
  const selectedCount = selectedPlanIds().length;
  const count = selectedCount * Number($('#batch-form').elements.repetitions.value || 0);
  $('#plan-selection-count').textContent = `已选 ${selectedCount} 条`;
  $('#attempt-total').textContent = `${count} 次`;
}

function setAllPlans(selected) {
  document.querySelectorAll('input[name="plan_record_id"]').forEach(input => { input.checked = selected && !input.disabled; });
  updateAttemptEstimate();
}

function renderPlans() {
  const target = $('#stability-plan-list');
  if (!plans.length) {
    target.innerHTML = '<p class="muted-copy">暂无可执行计划，请先在“用例与计划”中完成 QA 确认。</p>';
    $('#select-all-plans').disabled = true;
    $('#clear-plan-selection').disabled = true;
    updateAttemptEstimate();
    return;
  }
  $('#select-all-plans').disabled = false;
  $('#clear-plan-selection').disabled = false;
  target.innerHTML = plans.map(plan => `<label class="plan-choice-item"><input type="checkbox" name="plan_record_id" value="${plan.record_id}" ${plan.stability_ready === false ? 'disabled' : ''}><span><strong>${escapeHtml(plan.case_id)} · ${escapeHtml(plan.title)}</strong><small>v${plan.version} · ${plan.status === 'validated' ? '真机已验证' : 'QA 已确认'} · ${escapeHtml(plan.compiler_mode)}${plan.stability_ready === false ? ` · ${plan.exemption_reason ? '免稳定性验证' : '前置待准备'}：${escapeHtml(plan.stability_block_reason)}` : ''}</small></span></label>`).join('');
  target.querySelectorAll('input').forEach(input => input.addEventListener('change', updateAttemptEstimate));
}

function renderScopeSummary(summary) {
  const metrics = {
    '#scope-validatable-cases': summary.validatable_cases,
    '#scope-completed-cases': summary.completed_cases,
    '#scope-stable-cases': summary.stable_cases,
    '#scope-issue-cases': summary.issue_cases,
    '#scope-pass-rate': `${Number(summary.pass_rate || 0).toFixed(1)}%`,
  };
  Object.entries(metrics).forEach(([selector, value]) => {
    const node = $(selector);
    node.textContent = value;
    node.classList.remove('metric-loading');
    node.removeAttribute('aria-busy');
  });
  $('#scope-pass-rate-copy').textContent = `${summary.stable_cases} / ${summary.completed_cases}${summary.exempt_cases ? ` · 另有 ${summary.exempt_cases} 条免跑` : ''}`;
}

const scopeLabels = {
  validatable: '全部可验证用例',
  completed: '已完成稳定性验证',
  stable: '稳定通过用例',
  issues: '验证未完成 / 问题用例',
  pass_rate: '稳定通过率对应的用例',
};

function replaceStabilityScopeUrl(scope = activeScope) {
  const params = new URLSearchParams();
  if (selectedDeviceUdid) params.set('device', selectedDeviceUdid);
  if (scope) params.set('scope', scope);
  history.replaceState(null, '', `/stability${params.size ? `?${params}` : ''}`);
}

async function showScopeCases(scope, {scroll = true} = {}) {
  activeScope = scope;
  const panel = $('#stability-case-list');
  panel.setAttribute('aria-busy', 'true');
  const queryScope = scope === 'pass_rate' ? 'stable' : scope;
  const rows = await api(`/api/stability/cases?scope=${encodeURIComponent(queryScope)}&device_udid=${encodeURIComponent(selectedDeviceUdid)}`);
  $('#scope-list-title').textContent = scopeLabels[scope] || '稳定性用例清单';
  $('#scope-list-count').textContent = `${rows.length} 条`;
  $('#scope-export-link').href = `/api/stability/cases/export.csv?scope=${encodeURIComponent(queryScope)}&device_udid=${encodeURIComponent(selectedDeviceUdid)}`;
  document.querySelectorAll('.scope-card[data-scope]').forEach(card => {
    const selected = card.dataset.scope === scope;
    card.classList.toggle('selected', selected);
    card.setAttribute('aria-pressed', String(selected));
  });
  const body = $('#scope-case-table-body');
  body.innerHTML = rows.length ? rows.map(row => {
    const status = row.exempt ? '免稳定性验证' : row.stable ? '稳定通过' : row.complete ? '已完成待复核' : row.entered ? '验证未完成' : '待开始稳定性验证';
    const tone = row.stable ? 'ok' : row.complete || !row.entered ? 'warning' : 'danger';
    const rounds = row.exempt ? `实跑 ${row.completed_attempts} 次` : row.entered ? `${row.completed_attempts} / ${row.required_attempts}` : '轮次待设定';
    const passes = row.exempt ? '沿用首轮结论' : row.entered ? `验收通过 ${row.acceptance_passes} 次` : '尚未开始';
    const issues = row.exempt ? row.exemption_reason : row.issue_categories.length ? row.issue_categories.join('、') : '—';
    const action = row.latest_run_id ? `<a href="/dashboard?run=${encodeURIComponent(row.latest_run_id)}">查看报告</a>` : '—';
    return `<tr><td><strong>${escapeHtml(row.case_id)}</strong><small>${escapeHtml(row.title)}</small></td><td><span class="badge ${tone}">${status}</span></td><td>${rounds}<small>${passes}</small></td><td>${escapeHtml(issues)}</td><td>${escapeHtml(row.latest_batch_name || '尚未建立批次')}</td><td>${action}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="table-empty">当前分类暂无用例</td></tr>';
  panel.hidden = false;
  panel.setAttribute('aria-busy', 'false');
  replaceStabilityScopeUrl(scope);
  if (scroll) panel.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function planForRecord(recordId) {
  return plans.find(plan => Number(plan.record_id) === Number(recordId));
}

function batchCaseLabel(batch, planRecordId = null) {
  if (planRecordId) {
    const plan = planForRecord(planRecordId);
    return plan ? `${plan.case_id} · ${plan.title}` : '未找到关联用例';
  }
  const linkedPlans = batch.plan_record_ids.map(planForRecord).filter(Boolean);
  if (!linkedPlans.length) return '未找到关联用例';
  const first = linkedPlans[0];
  const suffix = linkedPlans.length > 1 ? ` 等 ${linkedPlans.length} 条` : '';
  return `${first.case_id} · ${first.title}${suffix}`;
}

function verifiedCaseOptions() {
  const seen = new Set();
  const options = [];
  batches.forEach(batch => {
    if (selectedDeviceUdid && batch.device_udid && batch.device_udid !== selectedDeviceUdid) return;
    if (batch.status !== 'running' && Number(batch.metrics?.finished_attempts || 0) <= 0) return;
    batch.plan_record_ids.forEach(recordId => {
      const plan = planForRecord(recordId);
      if (!plan || seen.has(plan.case_id)) return;
      seen.add(plan.case_id);
      options.push({batch, plan, value: `${batch.batch_id}|${recordId}`});
    });
  });
  return options;
}

function renderBatchPicker() {
  const query = ($('#batch-case-search')?.value || '').trim().toLowerCase();
  const options = verifiedCaseOptions().filter(item => `${item.plan.case_id} ${item.plan.title}`.toLowerCase().includes(query));
  $('#batch-picker').innerHTML = options.length
    ? options.map(item => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.plan.case_id)} · ${escapeHtml(item.plan.title)}</option>`).join('')
    : '<option value="">无匹配验证记录</option>';
  if (currentBatch && currentPlanRecordId) $('#batch-picker').value = `${currentBatch.batch_id}|${currentPlanRecordId}`;
}

function rate(value) { return `${Number(value || 0).toFixed(1)}%`; }

function calculateAttemptMetrics(attempts) {
  const completed = attempts.filter(item => item.status === 'completed');
  const classifications = completed.map(item => item.classification).filter(Boolean);
  const automationCompletedSet = new Set(['NORMAL_PASS','AI_REPAIR_SUCCESS','AI_VISUAL_ORACLE_PASS','ENVIRONMENT_RECOVERED_PASS','PRODUCT_FAILURE','ACCEPTANCE_MISMATCH']);
  const productPassSet = new Set(['NORMAL_PASS','AI_REPAIR_SUCCESS','AI_VISUAL_ORACLE_PASS','ENVIRONMENT_RECOVERED_PASS']);
  const automationCompleted = classifications.filter(value => automationCompletedSet.has(value)).length;
  const productPasses = classifications.filter(value => productPassSet.has(value)).length;
  const durations = completed.map(item => Number(item.duration_seconds)).filter(Number.isFinite);
  const aiInterventions = completed.reduce((sum, item) => sum + Number(item.ai_interventions || 0), 0);
  const aiSucceeded = completed.reduce((sum, item) => sum + Number(item.ai_repairs_succeeded || 0), 0);
  const steps = completed.reduce((sum, item) => sum + Number(item.step_count || 0), 0);
  const aiSteps = completed.reduce((sum, item) => sum + Number(item.ai_step_count || 0), 0);
  const percent = (numerator, denominator) => denominator ? numerator / denominator * 100 : 0;
  return {
    total_attempts: attempts.length, finished_attempts: completed.length,
    automation_completion_rate: percent(automationCompleted, completed.length),
    product_pass_rate: percent(productPasses, automationCompleted),
    average_duration_seconds: durations.length ? (durations.reduce((a,b) => a+b, 0) / durations.length).toFixed(2) : 0,
    deterministic_action_rate: percent(steps - aiSteps, steps),
    locator_failures: completed.reduce((sum, item) => sum + Number(item.locator_failures || 0), 0),
    ai_interventions: aiInterventions, ai_repairs_succeeded: aiSucceeded,
    ai_repair_success_rate: percent(aiSucceeded, aiInterventions),
    automation_failures: classifications.filter(value => ['AUTOMATION_FAILURE','AUTOMATION_FAILURE_REPAIRABLE'].includes(value)).length,
    environment_failures: classifications.filter(value => value === 'ENVIRONMENT_EXCEPTION').length,
  };
}

function renderBatch(batch, planRecordId = null) {
  currentBatch = batch;
  currentPlanRecordId = planRecordId == null ? null : Number(planRecordId);
  const batchIndex = batches.findIndex(item => item.batch_id === batch.batch_id);
  if (batchIndex >= 0) batches[batchIndex] = {...batches[batchIndex], ...batch};
  else batches.unshift(batch);
  const visibleAttempts = currentPlanRecordId
    ? batch.attempts.filter(item => Number(item.plan_record_id) === currentPlanRecordId)
    : batch.attempts;
  const metrics = currentPlanRecordId ? calculateAttemptMetrics(visibleAttempts) : batch.metrics;
  const finished = metrics.finished_attempts;
  const total = metrics.total_attempts;
  const progress = total ? finished / total * 100 : 0;
  $('#batch-overview-title').textContent = batchCaseLabel(batch, currentPlanRecordId);
  const batchCategory = batch.category === 'experiment' ? '单轮实验（不计入稳定性）' : batch.category === 'repair' ? 'AI 修复专项' : '正常稳定性';
  $('#batch-meta').textContent = `Android · ${batch.device_udid || '未记录设备'} · ${batchCategory} · ${currentPlanRecordId ? '单条用例' : `${batch.plan_record_ids.length} 条计划`} × ${batch.repetitions} 次${batch.error_message ? ` · ${batch.error_message}` : ''}`;
  $('#batch-status').textContent = statusLabels[batch.status] || batch.status;
  $('#batch-status').className = `badge ${batch.status === 'completed' ? 'ok' : batch.status === 'running' ? '' : 'muted'}`;
  $('#batch-progress').style.width = `${progress}%`;
  $('#batch-progress-label').textContent = `${finished} / ${total}`;
  $('#metric-automation').textContent = rate(metrics.automation_completion_rate);
  $('#metric-product').textContent = rate(metrics.product_pass_rate);
  $('#metric-duration').textContent = `${metrics.average_duration_seconds || 0}s`;
  $('#metric-deterministic').textContent = rate(metrics.deterministic_action_rate);
  $('#metric-locator').textContent = metrics.locator_failures;
  $('#metric-ai').textContent = rate(metrics.ai_repair_success_rate);
  $('#metric-ai-count').textContent = `${metrics.ai_interventions} 次介入 · ${metrics.ai_repairs_succeeded} 次成功`;
  $('#metric-automation-fail').textContent = metrics.automation_failures;
  $('#metric-environment').textContent = metrics.environment_failures;
  $('#attempt-count').textContent = `${total} 条`;
  renderAttempts(visibleAttempts);

  const start = $('#start-batch-button');
  start.disabled = batch.status !== 'draft';
  start.hidden = batch.status !== 'draft';
  $('#stop-batch-button').hidden = batch.status !== 'running';
  const terminal = ['completed', 'stopped', 'interrupted'].includes(batch.status);
  $('#batch-report-link').hidden = !terminal;
  $('#batch-json-link').hidden = false;
  $('#batch-report-link').href = batch.report_url;
  $('#batch-json-link').href = batch.json_url;
  renderBatchPicker();
  schedulePoll(batch.status === 'running');
}

function renderAttempts(attempts) {
  const body = $('#attempt-table-body');
  if (!attempts.length) { body.innerHTML = '<tr><td colspan="7" class="table-empty">该批次没有执行任务</td></tr>'; return; }
  body.innerHTML = attempts.map(item => {
    const evidence = item.run_id ? `<a class="evidence-link" href="/dashboard?run=${encodeURIComponent(item.run_id)}"><span aria-hidden="true">↗</span>查看报告</a>` : '—';
    const successful = ['NORMAL_PASS', 'AI_REPAIR_SUCCESS', 'AI_VISUAL_ORACLE_PASS', 'ENVIRONMENT_RECOVERED_PASS'].includes(item.classification);
    const failed = ['PRODUCT_FAILURE', 'AUTOMATION_FAILURE', 'ENVIRONMENT_EXCEPTION'].includes(item.classification);
    const tone = successful ? 'success' : failed ? 'danger' : 'warning';
    const glyph = successful ? '✓' : failed ? '!' : '•';
    const classification = classificationLabels[item.classification] || item.classification || '—';
    return `<tr><td>${item.sequence_no}</td><td><strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(item.case_title)}</small></td><td>第 ${item.iteration} 次</td><td><span class="attempt-status ${escapeHtml(item.status)}">${statusLabels[item.status] || item.status}</span></td><td><span class="classification-icon ${tone}" aria-hidden="true">${glyph}</span>${escapeHtml(classification)}</td><td>${item.duration_seconds == null ? '—' : `${item.duration_seconds}s`}</td><td>${evidence}</td></tr>`;
  }).join('');
}

function schedulePoll(active) {
  clearTimeout(pollTimer);
  if (active && currentBatch) pollTimer = setTimeout(() => loadBatch(currentBatch.batch_id, currentPlanRecordId), 2000);
}

async function loadBatch(batchId, planRecordId = null) {
  const batch = await api(`/api/stability/batches/${encodeURIComponent(batchId)}`);
  renderBatch(batch, planRecordId);
}

function selectInitialCase(requestedBatchId, requestedPlanRecordId) {
  const options = verifiedCaseOptions();
  if (requestedPlanRecordId) {
    const requestedPlan = planForRecord(requestedPlanRecordId);
    const latest = options.find(item => item.plan.case_id === requestedPlan?.case_id);
    if (latest) return latest;
  }
  const requested = batches.find(batch => batch.batch_id === requestedBatchId);
  if (requested) {
    const firstRecordId = requested.plan_record_ids[0];
    const requestedPlan = planForRecord(firstRecordId);
    return options.find(item => item.plan.case_id === requestedPlan?.case_id)
      || {batch: requested, plan: requestedPlan};
  }
  return options[0] || null;
}

async function initialize() {
  const platformPayload = await api('/api/device-platforms');
  devicePlatforms = platformPayload.platforms || [];
  const devices = platformPayload.devices || [];
  const deviceSelect = $('#batch-form').elements.device_udid;
  deviceSelect.innerHTML = '<option value="">请选择设备</option>' + devices.map(item => `<option value="${escapeHtml(item.udid)}">${escapeHtml(item.label || item.udid)}</option>`).join('');
  const liveDevice = devices.find(item => item.status === 'device') || devices[0];
  if (liveDevice) { deviceSelect.value = liveDevice.udid; selectedDeviceUdid = liveDevice.udid; }
  const platformSelect = $('#batch-form').elements.platform;
  devicePlatforms.forEach(item => {
    const option = platformSelect.querySelector(`option[value="${item.platform}"]`);
    if (!option) return;
    option.disabled = !item.executable;
    option.textContent = `${item.label}${item.verified ? ' · 已验证' : item.configured ? ' · 待建立定位库' : ' · 待配置'}`;
  });
  let scopeSummary;
  selectedDeviceUdid = new URLSearchParams(location.search).get('device') || selectedDeviceUdid;
  if (selectedDeviceUdid) deviceSelect.value = selectedDeviceUdid;
  const deviceQuery = selectedDeviceUdid ? `?device_udid=${encodeURIComponent(selectedDeviceUdid)}` : '';
  [plans, batches, scopeSummary] = await Promise.all([api('/api/stability/plans' + deviceQuery), api('/api/stability/batches'), api('/api/stability/summary' + deviceQuery)]);
  renderScopeSummary(scopeSummary);
  $('#stability-summary').textContent = `${batches.length} 个历史批次`;
  renderPlans(); renderBatchPicker(); updateAttemptEstimate();
  const params = new URLSearchParams(location.search);
  if (['validatable', 'completed', 'stable', 'issues', 'pass_rate'].includes(params.get('scope'))) {
    await showScopeCases(params.get('scope'), {scroll: false});
  }
  const selected = selectInitialCase(params.get('batch'), params.get('plan'));
  if (selected) await loadBatch(selected.batch.batch_id, selected.plan?.record_id);
}

$('#batch-form').elements.category.addEventListener('change', event => {
  const repair = event.target.value === 'repair';
  $('#category-note').innerHTML = repair
    ? '<strong>AI 修复专项</strong><span>用于验证受控定位失败和修复精度，不计入正常回归的 AI 介入率。</span>'
    : '<strong>正常稳定性</strong><span>用于衡量日常回归表现，受控 AI 修复用例不要混入本组。</span>';
});
$('#batch-form').elements.repetitions.addEventListener('input', updateAttemptEstimate);
$('#select-all-plans').addEventListener('click', () => setAllPlans(true));
$('#clear-plan-selection').addEventListener('click', () => setAllPlans(false));
document.querySelectorAll('.scope-card[data-scope]').forEach(card => card.addEventListener('click', () => showScopeCases(card.dataset.scope).catch(error => notice(`读取用例清单失败：${error.message}`, 'error'))));

$('#batch-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  const ids = selectedPlanIds();
  if (!ids.length) { notice('请至少选择一条已确认计划。', 'warning'); return; }
  busy(button, true, '正在创建…');
  try {
    const batch = await api('/api/stability/batches', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        name: event.currentTarget.elements.name.value.trim(),
        platform: event.currentTarget.elements.platform.value,
        category: event.currentTarget.elements.category.value,
        device_udid: event.currentTarget.elements.device_udid.value || null,
        repetitions: Number(event.currentTarget.elements.repetitions.value),
        plan_record_ids: ids,
      }),
    });
    batches = await api('/api/stability/batches');
    history.replaceState(null, '', `/stability?batch=${encodeURIComponent(batch.batch_id)}`);
    renderBatch(batch, null); notice('验证批次已创建。确认范围后可开始执行。', 'success');
  } catch (error) { notice(`创建失败：${error.message}`, 'error'); }
  finally { busy(button, false); }
});

$('#batch-picker').addEventListener('change', async event => {
  if (!event.target.value) return;
  const [batchId, planRecordId] = event.target.value.split('|');
  history.replaceState(null, '', `/stability?batch=${encodeURIComponent(batchId)}&plan=${encodeURIComponent(planRecordId)}`);
  await loadBatch(batchId, Number(planRecordId));
});

$('#batch-case-search').addEventListener('input', renderBatchPicker);
$('#batch-form').elements.device_udid.addEventListener('change', async event => {
  selectedDeviceUdid = event.target.value;
  replaceStabilityScopeUrl();
  plans = await api(`/api/stability/plans?device_udid=${encodeURIComponent(selectedDeviceUdid)}`);
  renderPlans();
  renderBatchPicker();
  const summary = await api(`/api/stability/summary?device_udid=${encodeURIComponent(selectedDeviceUdid)}`);
  renderScopeSummary(summary);
  if (activeScope) await showScopeCases(activeScope, {scroll: false});
});

$('#start-batch-button').addEventListener('click', async event => {
  if (!currentBatch) return;
  busy(event.currentTarget, true, '正在启动…');
  try {
    const batch = await api(`/api/stability/batches/${encodeURIComponent(currentBatch.batch_id)}/start`, {method:'POST'});
    renderBatch(batch, currentPlanRecordId);
    if (batch.error_message && batch.status !== 'running' && batch.status !== 'completed') {
      notice(`启动失败：${batch.error_message}`, 'error');
    } else {
      notice('批次已启动，将按顺序使用真机执行。', 'success');
    }
  } catch (error) { notice(`启动失败：${error.message}`, 'error'); }
  finally { busy(event.currentTarget, false); }
});

$('#stop-batch-button').addEventListener('click', async () => {
  if (!currentBatch) return;
  try {
    const batch = await api(`/api/stability/batches/${encodeURIComponent(currentBatch.batch_id)}/stop`, {method:'POST'});
    renderBatch(batch, currentPlanRecordId); notice('已请求安全停止，当前单次执行完成后不再启动下一次。', 'warning');
  } catch (error) { notice(`停止失败：${error.message}`, 'error'); }
});

initialize().catch(error => notice(`读取稳定性工作台失败：${error.message}`, 'error'));
