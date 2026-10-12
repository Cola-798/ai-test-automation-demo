const $ = (selector) => document.querySelector(selector);
let reviewItems = [];
let currentRunId = null;
let searchTimer = null;
let deviceFilter = '';
let activeMetricScope = '';
let detailRequest = 0;
let repairPoll = null;
let repairBusy = false;

const outcomeLabels = {
  PRODUCT_DEFECT: '已确认产品问题', TEST_DATA_ISSUE: '测试数据问题', ORACLE_ISSUE: '验收标准问题',
  AUTOMATION_ISSUE: '自动化问题', ENVIRONMENT_ISSUE: '环境问题', NOT_AN_ISSUE: '非问题', REPAIR_VERIFIED: '修复后通过',
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
  const node = $('#review-notice');
  node.textContent = message; node.dataset.kind = kind; node.hidden = false;
}

function queryString() {
  const params = new URLSearchParams();
  const values = {
    keyword: $('#review-keyword').value.trim(), status: $('#review-status').value,
    observed: $('#review-observed').value, outcome: $('#review-outcome').value,
    device_udid: deviceFilter,
  };
  Object.entries(values).forEach(([key, value]) => { if (value) params.set(key, value); });
  return params.toString();
}

async function loadWorkspace(preferredRunId = currentRunId) {
  await refreshWorkspaceList(preferredRunId);
  const selected = reviewItems.find(item => item.run_id === preferredRunId) || reviewItems[0];
  if (selected) await loadDetail(selected.run_id);
  else showEmpty();
}

async function refreshWorkspaceList(preferredRunId) {
  const [items, metrics] = await Promise.all([api(`/api/reviews?${queryString()}`), api(`/api/reviews/metrics?device_udid=${encodeURIComponent(deviceFilter)}`)]);
  reviewItems = items.filter(item => {
    if (activeMetricScope === 'pending') return item.review_status === 'PENDING';
    if (activeMetricScope === 'execution') return !item.is_stability;
    if (activeMetricScope === 'stability') return item.is_stability;
    return true;
  });
  renderMetrics(metrics);
  renderList(preferredRunId);
}

function renderMetrics(metrics) {
  const values = {
    '#review-metric-pending': metrics.pending,
    '#review-metric-execution': metrics.execution_failures,
    '#review-metric-stability': metrics.stability_failures,
  };
  Object.entries(values).forEach(([selector, value]) => {
    const node = $(selector);
    node.textContent = value;
    node.classList.remove('metric-loading');
    node.removeAttribute('aria-busy');
  });
  $('#review-summary').textContent = `${metrics.total} 条最新未通过 · ${metrics.pending} 条待复核`;
  document.querySelectorAll('[data-review-scope]').forEach(button => {
    const selected = button.dataset.reviewScope === activeMetricScope;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
}

function renderList(preferredRunId) {
  const titles = {pending: '待复核用例', execution: '执行未通过', stability: '稳定性未通过'};
  $('#review-list-title').textContent = titles[activeMetricScope] || '最新未通过';
  $('#review-count').textContent = `${reviewItems.length} 条`;
  const target = $('#review-list');
  if (!reviewItems.length) { target.innerHTML = '<div class="review-list-empty">没有匹配的异常结果</div>'; return; }
  target.innerHTML = reviewItems.map(item => `<button type="button" class="review-list-item ${item.run_id === preferredRunId ? 'selected' : ''}" data-run-id="${escapeHtml(item.run_id)}">
    <span class="review-list-status ${item.review_status.toLowerCase()}">${item.review_status === 'CONFIRMED' ? '已复核' : '待复核'}</span>
    <span class="review-list-copy"><strong>${escapeHtml(item.case_id)} · ${escapeHtml(item.case_title)}</strong><small>${item.is_stability ? '稳定性未通过' : '执行未通过'} · ${escapeHtml(item.observed_label)}</small><time>${formatTime(item.run_finished_at || item.created_at)}</time></span>
  </button>`).join('');
  target.querySelectorAll('.review-list-item').forEach(button => button.addEventListener('click', () => loadDetail(button.dataset.runId)));
}

async function loadDetail(runId) {
  const request = ++detailRequest;
  const detail = await api(`/api/reviews/${encodeURIComponent(runId)}`);
  if (request !== detailRequest) return;
  currentRunId = runId;
  history.replaceState(null, '', `/reviews?device=${encodeURIComponent(deviceFilter)}&run=${encodeURIComponent(runId)}`);
  document.querySelectorAll('.review-list-item').forEach(item => item.classList.toggle('selected', item.dataset.runId === runId));
  $('#review-empty').hidden = true;
  $('#review-detail').hidden = false;
  const review = detail.review;
  const source = detail.source_case || {};
  const result = detail.result || {};
  $('#review-detail-title').textContent = `${review.case_id} · ${review.case_title}`;
  $('#review-detail-meta').textContent = `当前结论对应记录 ${review.run_id} · 计划记录 ${review.plan_record_id || '未关联'} · ${formatTime(review.run_finished_at)}${review.plan_record_id ? '' : ' · 缺少对应执行计划，可查看证据和复核，暂不能验证AI修复'}`;
  $('#review-detail-status').textContent = review.review_status === 'CONFIRMED' ? '已复核' : '待复核';
  $('#review-detail-status').className = `badge ${review.review_status === 'CONFIRMED' ? 'ok' : 'muted'}`;
  $('#review-observed-label').textContent = review.observed_label;
  $('#review-message').textContent = result.message || '未记录异常摘要';
  $('#review-outcome-label').textContent = review.review_outcome_label || '待确认';
  $('#review-note-summary').textContent = review.review_note || '尚未填写复核备注';
  $('#review-precondition').textContent = source.precondition || '未填写';
  $('#review-source-steps').textContent = source.steps || '未填写';
  $('#review-expected').textContent = source.expected || '未填写';
  renderOracles(result.oracles || []);
  renderAiEvents(detail.ai || {intervened:false, events:[]});
  renderEvidence(detail.evidence || [], review.run_id);
  renderReruns(detail.reruns || []);
  $('#review-report-link').href = detail.report_url;
  const reviewReportLink = $('#review-review-report-link');
  reviewReportLink.hidden = !detail.review_report_url;
  if (detail.review_report_url) reviewReportLink.href = detail.review_report_url;
  const form = $('#review-form');
  form.elements.review_outcome.value = review.review_outcome || '';
  form.elements.review_note.value = review.review_note || '';
  form.elements.reviewer.value = review.reviewer || 'QA';
  $('#review-rerun').disabled = !review.plan_record_id;
  $('#review-analyze').disabled = repairBusy || !review.plan_record_id;
  renderRepairAnalysis(detail.latest_repair_analysis);
  renderRepairCandidates(detail.repair_candidates || [], detail.execution_plan);
  if ((detail.repair_candidates || []).some(item => item.status === 'activated')) {
    await refreshWorkspaceList(runId);
    if (request !== detailRequest) return;
  }
  clearTimeout(repairPoll);
  if ((detail.repair_candidates || []).some(item => item.status === 'first_run_running' || (item.status === 'awaiting_stability' && item.stability_batch_id))) {
    repairPoll = setTimeout(() => {
      if (currentRunId === runId) loadDetail(runId).catch(error => notice(error.message, 'error'));
    }, 3000);
  }
}

const repairStates = {
  awaiting_first_run: '待验证修复', first_run_running: '正在验证首轮',
  awaiting_stability: '首轮通过，待验证稳定性', failed_first_run: '首轮未通过，正式计划未更新',
  failed_stability: '稳定性未通过，正式计划未更新', activated: '验证通过，已启用新计划',
  stale: '来源已变化，未启用', awaiting_activation: '正在检查启用条件',
};

function renderRepairCandidates(candidates, originalPlan) {
  $('#review-repair-candidates').innerHTML = candidates.map(item => {
    const currentIds = new Set((item.plan.steps || []).map(step => step.step_id));
    const removed = item.analysis.removed_steps || {};
    const omitted = (originalPlan?.steps || []).filter(step => !currentIds.has(step.step_id) && !removed[step.step_id]);
    const retry = !omitted.length && ['awaiting_first_run', 'failed_first_run', 'failed_stability'].includes(item.status);
    const stable = item.status === 'awaiting_stability' && !item.stability_batch_id;
    const rule = item.validation_lane === 'execution'
      ? '完整首轮及原验收通过后，自动启用。'
      : `完整首轮通过后，还需在原设备完成 ${item.required_repetitions} 次稳定性验证，全部通过才启用。`;
    const changes = (item.analysis.changes || []).map(change => {
      const label = value => value?.title || (value === null ? '无' : Array.isArray(value) ? value.join(' → ') : '数据准备设置');
      return `<li><strong>${escapeHtml(change.kind)}</strong>：${escapeHtml(label(change.before))} → ${escapeHtml(label(change.after))}
        <details><summary>查看具体改动</summary><pre class="code-editor">${escapeHtml(JSON.stringify({原内容:change.before, 新内容:change.after}, null, 2))}</pre></details></li>`;
    }).join('');
    const progress = item.repair_progress;
    const progressRows = progress ? [
      ...progress.resolved.map(text => `此前失败、本次通过：${text}`),
      ...progress.new_passes.map(text => `本次新增验证通过：${text}`),
      ...progress.remaining.map(text => `仍需解决：${text}`),
      ...progress.regressions.map(text => `此前通过、本次失败：${text}`),
    ].map(text => `<li>${escapeHtml(text)}</li>`).join('') : '';
    return `<article class="oracle-observation ${item.status === 'activated' ? 'passed' : ''}">
      <strong>候选 v${item.plan.version} · ${escapeHtml(repairStates[item.status] || item.status)}</strong>
      <p>${escapeHtml(item.analysis.summary || '')}</p><p>${escapeHtml(rule)}</p>
      ${item.analysis.verification_goal ? `<p>本轮验证目的：${escapeHtml(item.analysis.verification_goal)}</p>` : ''}
      ${(item.analysis.remaining_issues || []).map(text => `<p>仍待核查：${escapeHtml(text)}</p>`).join('')}
      ${(item.analysis.missing_requirements || []).map(text => `<p>缺少条件：${escapeHtml(text)}</p>`).join('')}
      ${progress ? `<section><strong>实际修复效果</strong><p>${escapeHtml(progress.summary)}</p><p>已执行 ${progress.executed_steps} 步，通过 ${progress.passed_steps} 步；采集 ${progress.screenshots} 张截图。</p><ul>${progressRows}</ul><p>${escapeHtml(progress.message)}</p></section>` : ''}
      ${changes ? `<ul>${changes}</ul>` : ''}${item.error ? `<p>${escapeHtml(item.error)}</p>` : ''}
      ${omitted.length ? `<p>这份旧候选缺少 ${omitted.length} 个原计划步骤，已禁止验证；请重新分析。</p>` : ''}
      <details><summary>查看修复后的步骤</summary><ol>${item.plan.steps.map(step => `<li>${escapeHtml(step.title)}</li>`).join('')}</ol></details>
      ${retry || stable ? `<button class="button secondary" type="button" data-candidate="${escapeHtml(item.candidate_id)}" data-action="${stable ? 'stability' : 'execute'}" ${repairBusy ? 'disabled' : ''}>${stable ? (item.stability_batch_id ? '继续查看验证' : `开始 ${item.required_repetitions} 次稳定性验证`) : '验证修复'}</button>` : ''}
      ${item.first_run_id ? `<a class="button ghost" href="/dashboard?run=${encodeURIComponent(item.first_run_id)}" target="_blank">查看修复首轮报告</a>` : ''}
      ${item.stability_batch_id ? `<a class="button ghost" href="/stability?batch=${encodeURIComponent(item.stability_batch_id)}" target="_blank">查看稳定性进度</a>` : ''}
    </article>`;
  }).join('');
}

function renderRepairAnalysis(result) {
  const analysis = result?.analysis;
  $('#review-repair-analysis').textContent = analysis ? [
      '最近一次分析', analysis.summary, analysis.cannot_repair_reason,
      analysis.analyzed_run_id ? `分析现场：${analysis.analyzed_run_id}${analysis.analyzed_candidate_id ? `（候选 ${analysis.analyzed_candidate_id}）` : ''}` : '',
      ...(analysis.repair_items || []).map(item => `${item.step_id || item.oracle_id || '修复项'}：${item.problem}；证据：${(item.evidence_refs || []).join('、')}；拟改：${item.proposed_change}${item.verification_goal ? `；验证：${item.verification_goal}` : ''}`),
    result.evidence_rerun?.run_id ? `已补跑一次采证：${result.evidence_rerun.run_id}（${result.evidence_rerun.status === 'passed' ? '通过' : '未通过'}）` : '',
    analysis.retryable ? '模型暂不可用；稍后可重新点击分析，原计划和运行结果保留。' : '',
    analysis.verification_goal ? `本轮验证目的：${analysis.verification_goal}` : '',
    ...(analysis.remaining_issues || []).map(text => `仍待核查：${text}`),
    ...(analysis.missing_requirements || []).map(text => `待补充：${text}`),
    ...(result.issues || []),
  ].filter(Boolean).filter((text, i, all) => all.indexOf(text) === i).join('\n') : '';
}

$('#review-analyze').addEventListener('click', async () => {
  if (!currentRunId || repairBusy) return;
  const runId = currentRunId;
  repairBusy = true;
  const button = $('#review-analyze');
  button.disabled = true; button.textContent = '正在分析，必要时补跑采证…';
  try {
    const form = $('#review-form');
    const result = await api(`/api/reviews/${encodeURIComponent(runId)}/analyze`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({reviewer:form.elements.reviewer.value, note:form.elements.review_note.value}),
    });
    if (currentRunId !== runId) return;
    repairBusy = false;
    await loadDetail(runId);
    renderRepairAnalysis(result);
    notice(result.candidate ? '修复草稿已生成，请查看改动，再点击“验证修复”。' : '分析完成，未生成可执行修复；请查看原因。', result.candidate ? 'success' : 'warning');
  } catch (error) { notice(`分析失败：${error.message}`, 'error'); }
  finally { repairBusy = false; button.disabled = !currentRunId; button.textContent = 'AI 分析并生成修复草稿'; }
});

$('#review-repair-candidates').addEventListener('click', async event => {
  const button = event.target.closest('button[data-candidate]');
  if (!button || repairBusy) return;
  const runId = currentRunId;
  repairBusy = true; button.disabled = true;
  try {
    const result = await api(`/api/upgrade-candidates/${encodeURIComponent(button.dataset.candidate)}/${button.dataset.action}`, {method:'POST'});
    notice(button.dataset.action === 'stability' ? '修复计划的稳定性验证已启动，完成后自动判断是否启用。' : (repairStates[result.upgrade_candidate_status] || '验证完成'));
  } catch (error) { notice(`验证未完成：${error.message}`, 'error'); }
  finally {
    repairBusy = false;
    if (currentRunId === runId) await loadDetail(runId).catch(error => notice(error.message, 'error'));
  }
});

function renderOracles(oracles) {
  const target = $('#review-oracles');
  if (!oracles.length) { target.innerHTML = '<p class="muted-copy">本次异常没有生成验收明细，请结合错误摘要和日志判断。</p>'; return; }
  target.innerHTML = oracles.map(item => `<article class="oracle-observation ${item.passed ? 'passed' : 'failed'}"><span>${item.passed ? '通过' : '不一致'}</span><strong>${escapeHtml(item.oracle_id || '验收项')}</strong><p>${escapeHtml(item.message || '无明细')}</p></article>`).join('');
}

function renderEvidence(evidence, runId) {
  const target = $('#review-evidence');
  if (!evidence.length) { target.innerHTML = '<p class="muted-copy">本次运行没有可展示的截图或日志入口。</p>'; return; }
  target.innerHTML = evidence.map(item => item.type === 'screenshot'
    ? `<a href="${escapeHtml(item.url)}" target="_blank" class="review-evidence-card"><img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.label)}"><span>${escapeHtml(item.label)}</span></a>`
    : `<a href="${escapeHtml(item.url)}" target="_blank" class="review-evidence-file"><span>${item.type === 'xml' ? 'XML' : 'LOG'}</span>${escapeHtml(item.label)}</a>`).join('');
}

function renderAiEvents(ai) {
  const target = $('#review-ai-events');
  if (!ai.intervened || !ai.events.length) { target.innerHTML = '<p class="muted-copy">本次运行未发生 AI 介入</p>'; return; }
  target.innerHTML = ai.events.map(event => `<article class="ai-event-item"><span>AI</span><div><strong>${escapeHtml(event.type)}</strong><p>${escapeHtml(event.message || event.proposal?.reason || event.reason || '已记录模型判断')}</p></div></article>`).join('');
}

function renderReruns(reruns) {
  const target = $('#review-reruns');
  if (!reruns.length) { target.innerHTML = '<p class="muted-copy">尚未发起复测</p>'; return; }
  target.innerHTML = reruns.map(item => `<article class="rerun-item"><span class="classification-icon ${item.status === 'passed' ? 'success' : 'danger'}">${item.status === 'passed' ? '✓' : '!'}</span><div><strong>${escapeHtml(item.run_id)}</strong><small>${escapeHtml(item.classification)} · ${formatTime(item.created_at)}</small></div>${item.report_url ? `<a href="${escapeHtml(item.report_url)}" target="_blank">查看报告</a>` : ''}</article>`).join('');
}

function showEmpty() {
  ++detailRequest;
  clearTimeout(repairPoll);
  currentRunId = null;
  $('#review-empty').hidden = false;
  $('#review-detail').hidden = true;
}

$('#review-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!currentRunId) return;
  const button = event.currentTarget.querySelector('button[type=submit]');
  button.disabled = true; button.textContent = '正在保存…';
  try {
    await api(`/api/reviews/${encodeURIComponent(currentRunId)}`, {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        review_outcome: event.currentTarget.elements.review_outcome.value,
        review_note: event.currentTarget.elements.review_note.value,
        reviewer: event.currentTarget.elements.reviewer.value,
      }),
    });
    notice('QA 复核结论已保存，原始执行结果和证据未被修改。', 'success');
    await loadWorkspace(currentRunId);
  } catch (error) { notice(`保存失败：${error.message}`, 'error'); }
  finally { button.disabled = false; button.textContent = '确认复核'; }
});

$('#review-rerun').addEventListener('click', async event => {
  if (!currentRunId) return;
  const button = event.currentTarget;
  button.disabled = true; button.textContent = '正在复测…';
  try {
    const query = deviceFilter ? `?device_udid=${encodeURIComponent(deviceFilter)}` : '';
    const result = await api(`/api/reviews/${encodeURIComponent(currentRunId)}/rerun${query}`, {method:'POST'});
    notice(`复测完成：${result.status === 'passed' ? '通过' : '未通过'}，已关联运行 ${result.run_id}。`, result.status === 'passed' ? 'success' : 'warning');
    await loadWorkspace(result.status === 'passed' ? null : result.run_id);
  } catch (error) { notice(`复测失败：${error.message}`, 'error'); }
  finally { button.disabled = false; button.textContent = '按原计划重跑'; }
});

['review-status','review-observed','review-outcome'].forEach(id => $(`#${id}`).addEventListener('change', () => loadWorkspace()));
document.querySelectorAll('[data-review-scope]').forEach(button => button.addEventListener('click', () => {
  activeMetricScope = activeMetricScope === button.dataset.reviewScope ? '' : button.dataset.reviewScope;
  loadWorkspace().catch(error => notice(`读取复核中心失败：${error.message}`, 'error'));
}));
$('#review-device').addEventListener('change', event => {
  deviceFilter = event.target.value;
  history.replaceState(null, '', `/reviews?device=${encodeURIComponent(deviceFilter)}`);
  loadWorkspace().catch(error => notice(`读取复核中心失败：${error.message}`, 'error'));
});
$('#review-keyword').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadWorkspace(), 220); });

function formatTime(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN');
}

const requestedRun = new URLSearchParams(location.search).get('run');
deviceFilter = new URLSearchParams(location.search).get('device') || '';
api('/api/device-platforms').then(payload => {
  const select = $('#review-device');
  const devices = payload.devices || [];
  select.innerHTML = '<option value="">全部设备</option>' + devices.map(item => `<option value="${escapeHtml(item.udid)}">${escapeHtml(item.label || item.udid)}</option>`).join('');
  select.value = deviceFilter;
}).catch(() => {});
loadWorkspace(requestedRun).catch(error => notice(`读取复核中心失败：${error.message}`, 'error'));
