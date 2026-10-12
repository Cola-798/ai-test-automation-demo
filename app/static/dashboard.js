const $ = (selector) => document.querySelector(selector);
let runHistory = [];
let runFilter = 'all';
let deviceFilter = '';

function showMetric(selector, value) {
  const node = $(selector);
  node.textContent = value;
  node.classList.remove('metric-loading');
  node.removeAttribute('aria-busy');
}

async function loadRun() {
  const params = new URLSearchParams(location.search);
  const requested = params.get("run") || params.get('run_id');
  const runQuery = new URLSearchParams({ representative: 'true' });
  if (requested) runQuery.set('run_id', requested);
  const [response, platformResponse] = await Promise.all([fetch(`/api/runs?${runQuery}`), fetch("/api/device-platforms")]);
  if (!response.ok) throw new Error('无法读取运行记录，请刷新重试');
  const runs = await response.json();
  runHistory = runs;
  renderDevicePicker(runs, platformResponse.ok ? await platformResponse.json() : null);
  deviceFilter = params.get('device') || '';
  $('#device-picker').value = deviceFilter;
  await renderRunSummary(runs);
  const requestedFilter = params.get('filter');
  if (['all', 'passed', 'failed', 'blocked', 'not-run'].includes(requestedFilter)) runFilter = requestedFilter;
  document.querySelectorAll('.run-summary-card').forEach((item) => item.setAttribute('aria-pressed', String(item.dataset.runFilter === runFilter)));
  if (requestedFilter && ['all', 'passed', 'failed', 'blocked', 'not-run'].includes(requestedFilter)) {
    renderExecutionCaseDetails(runFilter).catch(error => { $('#run-message').textContent = `读取用例明细失败：${error.message}`; });
  }
  const requestedRun = runs.find((item) => item.run_id === requested) || null;
  const run = renderRunPicker(requestedRun?.run_id);
  if (!run) { $('#run-message').textContent = '还没有执行记录。先到用例与计划页确认并执行一条计划。'; return; }
  renderRun(run);
}

function matchesRunFilter(run) {
  if (deviceFilter && String(run.device?.udid || '') !== deviceFilter) return false;
  if (runFilter === 'passed') return run.status === 'passed';
  if (runFilter === 'failed') return run.status !== 'passed';
  if (runFilter === 'blocked' || runFilter === 'not-run') return false;
  return true;
}

function matchesDevice(run) {
  return !deviceFilter || String(run.device?.udid || '') === deviceFilter;
}

function representativeRuns(preferredRunId = null) {
  const latestByCase = new Map();
  runHistory.forEach((run) => {
    const key = `${run.case_id || run.run_id}|${run.device?.udid || ''}`;
    const current = latestByCase.get(key);
    if (!current || new Date(run.finished_at).getTime() > new Date(current.finished_at).getTime()) latestByCase.set(key, run);
  });
  const preferred = runHistory.find((run) => run.run_id === preferredRunId);
  if (preferred && matchesRunFilter(preferred)) latestByCase.set(`${preferred.case_id || preferred.run_id}|${preferred.device?.udid || ''}`, preferred);
  return [...latestByCase.values()].sort((left, right) => new Date(right.finished_at).getTime() - new Date(left.finished_at).getTime());
}

function renderRunPicker(preferredRunId) {
  const visibleRuns = representativeRuns(preferredRunId).filter(matchesRunFilter);
  const picker = $('#run-picker');
  picker.innerHTML = visibleRuns.length ? visibleRuns.map(item => `<option value="${escapeHtml(item.run_id)}">${escapeHtml(item.case_id)} · ${item.status === 'passed' ? '通过' : '未通过'} · ${escapeHtml(new Date(item.finished_at).toLocaleString('zh-CN'))}</option>`).join('') : '<option value="">暂无匹配的运行记录</option>';
  const selected = visibleRuns.find(item => item.run_id === preferredRunId) || visibleRuns[0] || null;
  if (selected) picker.value = selected.run_id;
  return selected;
}

async function renderRunSummary() {
  const query = deviceFilter ? `?device_udid=${encodeURIComponent(deviceFilter)}` : '';
  const response = await fetch(`/api/execution/summary${query}`);
  if (!response.ok) throw new Error('无法读取设备用例统计');
  const summary = await response.json();
  showMetric('#summary-total', summary.device_cases);
  showMetric('#summary-passed', summary.passed_cases);
  showMetric('#summary-failed', summary.failed_cases);
  showMetric('#summary-blocked', summary.blocked_cases);
  showMetric('#summary-not-run', summary.not_run_cases);
}

async function renderExecutionCaseDetails(filter) {
  const panel = $('#execution-case-panel');
  const params = new URLSearchParams();
  if (deviceFilter) params.set('device_udid', deviceFilter);
  const statuses = { passed: 'PASSED', failed: 'FAILED', blocked: 'BLOCKED', 'not-run': 'NOT_RUN' };
  if (statuses[filter]) params.set('status', statuses[filter]);
  const response = await fetch(`/api/execution/cases?${params}`);
  if (!response.ok) throw new Error('无法读取用例明细');
  const cases = await response.json();
  const labels = { PASSED: '通过', FAILED: '未通过', BLOCKED: '阻塞', NOT_RUN: '未执行' };
  $('#execution-case-title').textContent = `${filter === 'all' ? '全部' : labels[statuses[filter]]}用例明细 · ${cases.length} 条`;
  const exportParams = new URLSearchParams();
  if (deviceFilter) exportParams.set('device_udid', deviceFilter);
  if (statuses[filter]) exportParams.set('status', statuses[filter]);
  $('#execution-case-export').href = `/api/execution/cases.xlsx?${exportParams}`;
  $('#execution-case-list').innerHTML = cases.length ? cases.map(item => `
    <button type="button" class="execution-case-row" data-case-id="${escapeHtml(item.case_id)}" data-run-id="${escapeHtml(item.qualification_run_id || item.first_run_evidence_run_id || '')}" aria-pressed="false">
      <div><strong>${escapeHtml(item.case_id)} · ${escapeHtml(item.title)}</strong><small>${escapeHtml(item.owner_device_name || item.owner_device_udid || '未归属设备')}</small></div>
      <div class="execution-case-reason">${escapeHtml(item.reason)}</div>
      <span class="badge ${item.status === 'PASSED' ? 'ok' : item.status === 'FAILED' ? 'danger' : 'muted'}">${labels[item.status] || item.status}</span>
    </button>`).join('') : '<p class="muted-copy">当前筛选下没有用例。</p>';
  $('#execution-case-list').querySelectorAll('.execution-case-row').forEach((button) => {
    button.addEventListener('click', () => selectExecutionCase(button));
  });
  syncSelectedExecutionCase();
  panel.hidden = false;
}

function syncSelectedExecutionCase() {
  const runId = $('#run-picker').value;
  const selectedRun = runHistory.find(item => item.run_id === runId);
  const selected = selectedRun && [...$('#execution-case-list').querySelectorAll('.execution-case-row')]
    .find(button => button.dataset.caseId === String(selectedRun.case_id || ''));
  $('#execution-case-list').querySelectorAll('.execution-case-row').forEach((button) => {
    const isSelected = button === selected;
    button.classList.toggle('selected', isSelected);
    button.setAttribute('aria-pressed', String(isSelected));
  });
}

async function selectExecutionCase(button) {
  const caseId = button.dataset.caseId;
  const runId = button.dataset.runId;
  $('#execution-case-list').querySelectorAll('.execution-case-row').forEach((item) => {
    const selected = item === button;
    item.classList.toggle('selected', selected);
    item.setAttribute('aria-pressed', String(selected));
  });
  if (!runId) {
    $('#run-picker').innerHTML = '<option value="">该用例暂无执行记录</option>';
    $('#run-status').textContent = '暂无真机首轮执行记录';
    $('#run-dot').classList.remove('amber');
    $('#device-name').textContent = '—';
    $('#device-id').textContent = '—';
    $('#run-message').textContent = `${caseId} 当前没有可展示的真机首轮执行记录。`;
    $('#case-id').textContent = caseId;
    $('#execution-path').textContent = '—';
    $('#classification').textContent = '—';
    $('#finished-at').textContent = '—';
    $('#timeline-title').textContent = `${caseId} · 尚无执行记录`;
    $('#timeline').innerHTML = '<p class="muted-copy">此用例尚无真机执行步骤。</p>';
    $('#report-link').hidden = true;
    $('#report-retry').hidden = true;
    $('#report-status').textContent = '';
    $('#device-screen').textContent = '暂无真机执行证据';
    $('#evidence-caption').textContent = '此用例尚无可展示的运行截图';
    $('#run-review-layer').hidden = true;
    history.replaceState(null, '', `/dashboard?filter=${encodeURIComponent(runFilter)}&device=${encodeURIComponent(deviceFilter)}`);
    document.querySelector('.run-result-hero').scrollIntoView({ behavior: 'smooth', block: 'start' });
    return;
  }

  let run = runHistory.find(item => item.run_id === runId);
  if (!run) {
    const response = await fetch(`/api/runs?representative=true&run_id=${encodeURIComponent(runId)}`);
    if (!response.ok) throw new Error('无法读取该用例的真机运行记录');
    const runs = await response.json();
    run = runs.find(item => item.run_id === runId) || null;
    runHistory = [...runHistory.filter(item => item.run_id !== runId), ...runs];
  }
  if (!run) {
    $('#run-message').textContent = `未找到 ${caseId} 对应的真机运行记录。`;
    return;
  }
  $('#run-picker').value = run.run_id;
  history.replaceState(null, '', `/dashboard?filter=${encodeURIComponent(runFilter)}&device=${encodeURIComponent(deviceFilter)}&run=${encodeURIComponent(run.run_id)}`);
  renderRun(run);
  document.querySelector('.run-result-hero').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderDevicePicker(runs, payload) {
  const select = $('#device-picker');
  const known = new Map();
  (payload?.devices || []).forEach(device => known.set(device.udid, device.label || device.udid));
  runs.forEach(run => { if (run.device?.udid) known.set(run.device.udid, run.device.name ? `${run.device.name} · ${run.device.udid}` : run.device.udid); });
  select.innerHTML = '<option value="">全部设备</option>' + [...known.entries()].map(([id, label]) => `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`).join('');
}

function renderRun(run) {
  $('#run-picker').value = run.run_id;

  const passed = run.status === "passed";
  $("#run-dot").classList.toggle("amber", !passed);
  $("#run-status").textContent = passed ? "执行通过" : "执行未通过";
  $("#run-message").textContent = run.message;
  $("#device-name").textContent = run.device?.name || "移动真机";
  $("#device-id").textContent = run.device?.udid || "—";
  $("#case-id").textContent = run.case_id;
  $("#classification").textContent = run.classification_label || run.classification;
  $("#finished-at").textContent = new Date(run.finished_at).toLocaleString("zh-CN");
  $("#execution-path").textContent = run.execution_mode === "native_evidence_import" ? "原生实测证据" : run.events?.some((event) => event.type.includes("repair")) ? "AI 辅助修复" : "常规自动化";
  $("#timeline-title").textContent = `${run.case_id} · ${run.case_title}`;
  const link = $("#report-link");
  link.href = run.report_url;
  link.hidden = run.report_status === "queued" || run.report_status === "generating";
  $("#report-status").textContent = run.report_status === "queued" || run.report_status === "generating"
    ? "报告后台整理中…" : run.report_status === "failed" ? "报告生成失败，可稍后重试" : "";
  $("#report-retry").hidden = run.report_status !== "failed";
  if (run.report_status === "queued" || run.report_status === "generating") {
    window.setTimeout(() => refreshReportStatus(run.run_id), 1200);
  }
  const reviewLayer = $('#run-review-layer');
  if (run.review) {
    reviewLayer.hidden = false;
    $('#run-observed-result').textContent = run.review.observed_label;
    $('#run-qa-result').textContent = run.review.review_outcome_label || '待确认';
    $('#run-qa-note').textContent = run.review.review_note || '尚未复核';
    $('#run-review-link').href = `/reviews?run=${encodeURIComponent(run.run_id)}`;
  } else {
    reviewLayer.hidden = true;
  }

  const screenshots = (run.events || []).filter((event) => event.screenshot);
  if (screenshots.length) {
    const latest = screenshots[screenshots.length - 1];
    showEvidence(run, latest);
  } else {
    $('#device-screen').textContent = '该次运行未记录截图';
    $('#evidence-caption').textContent = '暂无截图证据';
  }
  $("#timeline").innerHTML = (run.steps || []).map((step, index) => `<button type="button" class="timeline-item" data-index="${index}" aria-pressed="false"><span class="timeline-dot ${step.status === 'passed' ? 'passed' : 'failed'}"></span><span><strong>${escapeHtml(step.title || step.step_id)}</strong><small>${escapeHtml(step.step_id)} · ${escapeHtml(step.message || "执行完成")} · ${step.duration_seconds == null ? "未记录耗时" : `${step.duration_seconds}s`}</small></span><span class="badge ${step.status === "passed" ? "ok" : "danger"}">${step.status === "passed" ? "通过" : "失败"}</span></button>`).join("");
  document.querySelectorAll('.timeline-item').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.timeline-item').forEach(item => { item.classList.remove('selected'); item.setAttribute('aria-pressed', 'false'); });
    button.classList.add('selected'); button.setAttribute('aria-pressed', 'true');
    const step = run.steps[Number(button.dataset.index)];
    const event = screenshots.filter(item => item.step_id === step.step_id
      && item.phase === step.phase && item.step_index === step.step_index).at(-1);
    if (event) showEvidence(run, event);
    else { $('#device-screen').textContent = '此步骤未记录截图'; $('#evidence-caption').textContent = step.title || step.step_id; }
  }));
  if (!$('#execution-case-panel').hidden) syncSelectedExecutionCase();
}

$('#report-retry').addEventListener('click', async () => {
  const runId = $('#run-picker').value;
  const response = await fetch(`/api/artifact-jobs/report/${encodeURIComponent(runId)}/retry`, { method: 'POST' });
  if (!response.ok) return;
  const run = runHistory.find(item => item.run_id === runId);
  if (run) {
    run.report_status = 'queued';
    renderRun(run);
  }
});

async function refreshReportStatus(runId) {
  if ($('#run-picker').value !== runId) return;
  try {
    const response = await fetch(`/api/artifact-jobs/report/${encodeURIComponent(runId)}`);
    if (!response.ok) return;
    const job = await response.json();
    const run = runHistory.find(item => item.run_id === runId);
    if (!run) return;
    run.report_status = job.status === 'completed' ? 'completed'
      : job.status === 'failed' ? 'failed' : job.status === 'running' ? 'generating' : 'queued';
    renderRun(run);
  } catch {
    // Report rendering is ancillary; leave the execution result visible if status lookup is unavailable.
  }
}

function showEvidence(run, event) {
  const image = document.createElement('img');
  image.src = String(event.screenshot).startsWith('/')
    ? event.screenshot : `/runs/${encodeURIComponent(run.run_id)}/${event.screenshot}`;
  image.alt = `${event.phase || 'execution'} ${event.step_id || event.oracle_id || 'evidence'} 执行证据`;
  $('#device-screen').replaceChildren(image);
  $('#evidence-caption').textContent = `${event.phase || '执行'} · ${event.step_id || event.oracle_id || '验收'} · ${event.event_id || '已记录截图'}`;
}

$('#run-picker').addEventListener('change', (event) => {
  const run = runHistory.find(item => item.run_id === event.target.value);
  if (!run) return;
  history.replaceState(null, '', `/dashboard?filter=${encodeURIComponent(runFilter)}&device=${encodeURIComponent(deviceFilter)}&run=${encodeURIComponent(run.run_id)}`);
  renderRun(run);
});

document.querySelectorAll('.run-summary-card').forEach((button) => button.addEventListener('click', () => {
  runFilter = button.dataset.runFilter;
  document.querySelectorAll('.run-summary-card').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
  const currentRunId = $('#run-picker').value;
  const run = renderRunPicker(currentRunId);
  renderExecutionCaseDetails(runFilter).catch(error => { $('#run-message').textContent = `读取用例明细失败：${error.message}`; });
  if (!run) return;
  history.replaceState(null, '', `/dashboard?filter=${encodeURIComponent(runFilter)}&device=${encodeURIComponent(deviceFilter)}&run=${encodeURIComponent(run.run_id)}`);
  renderRun(run);
}));

$('#device-picker').addEventListener('change', (event) => {
  deviceFilter = event.target.value;
  renderRunSummary().catch((error) => { $('#run-message').textContent = `读取设备统计失败：${error.message}`; });
  if (!$('#execution-case-panel').hidden) renderExecutionCaseDetails(runFilter).catch((error) => { $('#run-message').textContent = `读取用例明细失败：${error.message}`; });
  const run = renderRunPicker(null);
  history.replaceState(null, '', `/dashboard?filter=${encodeURIComponent(runFilter)}&device=${encodeURIComponent(deviceFilter)}${run ? `&run=${encodeURIComponent(run.run_id)}` : ''}`);
  if (run) renderRun(run);
});

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
}

loadRun().catch((error) => { $("#run-message").textContent = `读取执行结果失败：${error.message}`; });
