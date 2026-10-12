const $ = (selector, root = document) => root.querySelector(selector);

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail || payload));
  return payload;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));
}

function notify(message, kind = 'info') {
  const node = $('#model-notice');
  node.textContent = message;
  node.dataset.kind = kind;
  node.hidden = false;
}

function busy(button, active, label) {
  if (active) button.dataset.label = button.textContent;
  button.disabled = active;
  button.setAttribute('aria-busy', String(active));
  button.textContent = active ? label : button.dataset.label;
}

function payloadFor(form, requireModel = true) {
  const manual = $('[data-field="manual_model"]', form);
  const model = manual.hidden
    ? form.elements.model_choice.value.trim()
    : form.elements.custom_model.value.trim();
  if (requireModel && !model) throw new Error('请先选择模型，或手动输入模型名称');
  return {
    base_url: form.elements.base_url.value.trim(),
    api_key: form.elements.api_key.value.trim(),
    model,
  };
}

function renderModelOptions(form, models, selected = '') {
  const select = form.elements.model_choice;
  const uniqueModels = [...new Set(models.filter(Boolean))];
  if (selected && !uniqueModels.includes(selected)) uniqueModels.unshift(selected);
  select.innerHTML = [
    '<option value="">请选择模型</option>',
    ...uniqueModels.map(model => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`),
  ].join('');
  select.value = selected && uniqueModels.includes(selected) ? selected : '';
}

function renderRole(form, config) {
  form.elements.base_url.value = config.base_url || '';
  renderModelOptions(form, config.model ? [config.model] : [], config.model || '');
  const status = $('[data-field="status"]', form);
  status.textContent = config.configured ? '已启用' : (config.api_key_configured ? '待选择模型' : '未配置');
  status.className = `badge ${config.configured ? 'ok' : 'muted'}`;
  $('[data-field="key_hint"]', form).textContent = config.api_key_configured ? '已保存密钥 · 留空保持不变' : '尚未保存密钥';
}

async function loadSettings() {
  const config = await api('/api/model-config');
  renderRole($('form[data-role="plan"]'), config.plan_model || {});
  renderRole($('form[data-role="vision"]'), config.vision_model || {});
  const ready = config.plan_model?.configured && config.vision_model?.configured;
  $('#workbench-summary').textContent = ready ? '两类模型已配置' : '还有配置未完成';
}

document.querySelectorAll('.secret-toggle').forEach(button => button.addEventListener('click', () => {
  const input = button.parentElement.querySelector('input');
  const showing = input.type === 'text';
  input.type = showing ? 'password' : 'text';
  button.textContent = showing ? '显示' : '隐藏';
}));

document.querySelectorAll('.model-editor').forEach(form => {
  const role = form.dataset.role;
  const probe = $('[data-field="probe"]', form);
  const manualWrap = $('[data-field="manual_model"]', form);
  const manualButton = $('.manual-model-button', form);

  manualButton.addEventListener('click', () => {
    const enteringManually = manualWrap.hidden;
    manualWrap.hidden = !enteringManually;
    form.elements.model_choice.disabled = enteringManually;
    form.elements.model_choice.required = !enteringManually;
    form.elements.custom_model.required = enteringManually;
    if (enteringManually) {
      form.elements.custom_model.value = form.elements.model_choice.value;
      form.elements.custom_model.focus();
    } else if (form.elements.custom_model.value.trim()) {
      const value = form.elements.custom_model.value.trim();
      renderModelOptions(form, [...form.elements.model_choice.options].map(option => option.value), value);
    }
    manualButton.textContent = enteringManually ? '返回模型列表选择' : '没有找到？手动输入模型名';
  });

  $('.discover-button', form).addEventListener('click', async event => {
    const button = event.currentTarget;
    busy(button, true, '正在发现…');
    try {
      const result = await api(`/api/model-settings/${role}/discover`, {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payloadFor(form, false)),
      });
      const current = manualWrap.hidden ? form.elements.model_choice.value : form.elements.custom_model.value.trim();
      manualWrap.hidden = true;
      form.elements.model_choice.disabled = false;
      form.elements.model_choice.required = true;
      form.elements.custom_model.required = false;
      manualButton.textContent = '没有找到？手动输入模型名';
      renderModelOptions(form, result.models, result.models.includes(current) ? current : '');
      if (!form.elements.model_choice.value) form.elements.model_choice.value = result.models[0] || '';
      notify(`已发现 ${result.count} 个模型，请从下拉列表中手动选择。`, 'success');
    } catch (error) {
      notify(`发现模型失败：${error.message}`, 'error');
    } finally { busy(button, false); }
  });

  $('.probe-button', form).addEventListener('click', async event => {
    const button = event.currentTarget;
    busy(button, true, role === 'plan' ? '正在测试解析…' : '正在测试图片…');
    probe.hidden = true;
    try {
      const result = await api(`/api/model-settings/${role}/probe`, {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payloadFor(form)),
      });
      probe.hidden = false;
      probe.className = `probe-feedback ${result.passed ? 'passed' : 'failed'}`;
      probe.innerHTML = `<strong>${result.passed ? '能力测试通过' : '能力测试未通过'}</strong><span>${escapeHtml(result.message)} · ${result.latency_ms} ms</span>`;
      notify(result.passed ? '模型能力测试通过，可以保存配置。' : '模型未通过能力测试，请更换模型或检查接口。', result.passed ? 'success' : 'warning');
    } catch (error) {
      notify(`能力测试失败：${error.message}`, 'error');
    } finally { busy(button, false); }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('.save-model-button', form);
    busy(button, true, '正在保存…');
    try {
      await api(`/api/model-settings/${role}`, {
        method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payloadFor(form)),
      });
      form.elements.api_key.value = '';
      await loadSettings();
      notify(`${role === 'plan' ? '用例解析' : '视觉兜底'}模型配置已加密保存并立即生效。`, 'success');
    } catch (error) {
      notify(`保存失败：${error.message}`, 'error');
    } finally { busy(button, false); }
  });
});

loadSettings().catch(error => notify(`读取模型配置失败：${error.message}`, 'error'));
