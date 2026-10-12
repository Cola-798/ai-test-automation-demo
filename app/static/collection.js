/* All AI/source text is rendered as text, never executable HTML. */
(() => {
  const $ = id => document.getElementById(id);
  const readOnly = $('main-content').dataset.readOnly === 'true';
  const actions={tap:'点击',input:'输入',swipe:'滑动',back:'返回',wait:'等待',stop:'停止'};
  const states = {running:'运行中',waiting_input:'等待外部条件',completed:'范围采集完成',partial:'部分完成',stopped:'已停止',failed:'未完成',interrupted:'已中断'};
  let timer, pendingRequest, library, currentAppId, apps=[], activeTab="pages", selectedKey=null, libraryRequest=0;
  async function api(path, options) {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查采集参数');
    return data;
  }
  function notice(error) { $('collection-notice').hidden=false; $('collection-notice').textContent=error.message; }
  function option(select,value,label) { const item=document.createElement('option');item.value=value;item.textContent=label;select.append(item); }
  function paragraph(parent,text) { const p=document.createElement('p');p.textContent=text;parent.append(p); }
  function renderDevices(devices) {
    const select=$('collection-device'),selected=select.value;select.replaceChildren();
    const online=devices.filter(device=>device.status==='device');
    if(!online.length)option(select,'','未发现已授权的 Android 手机');
    online.forEach(device=>option(select,device.udid,device.label || device.udid));
    if(online.some(device=>device.udid===selected))select.value=selected;
  }
  $('collection-device-refresh').onclick=async()=>{
    $('collection-device-refresh').disabled=true;
    try{const data=await api('/api/device-platforms');renderDevices(data.devices);}catch(error){notice(error);}finally{updateActionScope();}
  };

  async function refresh() {
    clearTimeout(timer);
    const selected=$('collection-tasks').value;
    const tasks=await api('/api/collection/tasks');$('collection-tasks').replaceChildren();
    tasks.forEach(t=>option($('collection-tasks'),t.task_id,`${apps.find(a=>a.app_package===t.app_package)?.name || t.app_package} · ${states[t.status] || t.status} · ${t.task_id.slice(0,8)}`));
    if (tasks.some(t=>t.task_id===selected)) $('collection-tasks').value=selected;
    await showTask();
  }
  async function showTask() {
    clearTimeout(timer);const id=$('collection-tasks').value;if(!id)return;
    const t=await api(`/api/collection/tasks/${id}`);
    $('collection-status').textContent=`${apps.find(a=>a.app_package===t.app_package)?.name || t.app_package} · ${states[t.status]} · ${t.steps} 个操作 · ${t.elapsed_seconds} 秒 · ${t.message}`;
    $('collection-stop').disabled=readOnly || t.status!=='running';
    $('collection-resume').disabled=readOnly || !['waiting_input','partial','stopped','interrupted','failed'].includes(t.status);
    for(const name of ['readiness','missing','controls','pages','routes','objects']) $('collection-'+name).replaceChildren();
    t.readiness.forEach(r=>{paragraph($('collection-readiness'),`${r.case_id} · ${r.condition}：${r.state}。${r.reason}`);if(r.xml){const a=document.createElement('a');a.href=`/api/collection/tasks/${id}/files/${r.xml}`;a.textContent='查看前置核验结构';a.target='_blank';a.rel='noopener';$('collection-readiness').append(a);}if(r.screenshot){const a=document.createElement('a');a.href=`/api/collection/tasks/${id}/files/${r.screenshot}`;a.textContent='查看前置核验截图';a.target='_blank';a.rel='noopener';$('collection-readiness').append(a);}});
    if(!t.readiness.length)paragraph($('collection-readiness'),'尚无用例条件的现场核验记录。');
    t.missing.forEach(text=>{const li=document.createElement('li');li.textContent=text;$('collection-missing').append(li);});
    const labels={locator_verified:'定位已核验',candidate:'待核验',conflict:'与现有定位冲突，未覆盖'};
    t.controls.slice(-80).forEach(c=>paragraph($('collection-controls'),`${c.target_key} · ${c.description} · ${labels[c.status]} · 匹配 ${c.matched_count} 个 · ${c.effect_verified?'操作效果已核验':'操作效果未核验'}`));
    t.routes.slice(-40).forEach(r=>paragraph($('collection-routes'),`${actions[r.action.type] || r.action.type} · ${r.verified?'观察到预期变化':'未证实预期变化'} · ${r.from.slice(0,8)} → ${r.to.slice(0,8)}`));
    t.objects.forEach(o=>paragraph($('collection-objects'),`本轮已映射对象：${o.name}`));
    t.pages.slice(-30).forEach(p=>{
      const div=document.createElement('div');paragraph(div,`${p.name}：${p.summary}`);
      if(p.screenshot){const img=document.createElement('img');img.src=`/api/collection/tasks/${id}/files/${p.screenshot}`;img.alt=p.name;img.style.maxWidth='240px';img.loading='lazy';div.append(img);}
      const a=document.createElement('a');a.href=`/api/collection/tasks/${id}/files/${p.xml}`;a.textContent='查看页面结构';a.target='_blank';a.rel='noopener';div.append(a);$('collection-pages').append(div);
    });
    if(t.status==='running') timer=setTimeout(()=>showTask().catch(notice),2000);
  }
  $('collection-form').addEventListener('submit',async event=>{
    event.preventDefault();if($('collection-app-picker').value!==currentAppId || readOnly)return;$('collection-start').disabled=true;$('collection-start').setAttribute('aria-busy','true');$('collection-start-label').textContent='正在创建采集任务…';
    try {
      const body={request_id:pendingRequest || crypto.randomUUID(),device_udid:$('collection-device').value,app_id:$('collection-app-picker').value,case_ids:Array.from($('collection-cases').selectedOptions,o=>o.value),scope_text:$('collection-scope').value,authorized_effects:$('collection-create').checked?['create']:[]};
      pendingRequest=body.request_id;
      const task=await api('/api/collection/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});pendingRequest=null;await refresh();$('collection-tasks').value=task.task_id;await showTask();
    }catch(error){notice(error);}finally{$('collection-start').removeAttribute('aria-busy');$('collection-start-label').textContent='核验并补齐前置条件';updateActionScope();}
  });
  $('collection-form').addEventListener('change',()=>{pendingRequest=null;});
  $('collection-refresh').onclick=async()=>{try{const devices=await api('/api/device-platforms');renderDevices(devices.devices);await refresh();}catch(error){notice(error);}};
  $('collection-tasks').onchange=()=>showTask().catch(notice);
  for(const action of ['stop','resume'])$('collection-'+action).onclick=async()=>{
    try{await api(`/api/collection/tasks/${$('collection-tasks').value}/${action}`,{method:'POST'});await refresh();}catch(error){notice(error);}
  };
  const strategyNames={text:'文本定位',resource_id:'控件 ID',accessibility_id:'无障碍描述',xpath:'页面结构定位',android_uiautomator:'Android 选择器',normalized_coordinate:'坐标定位'};
  function evidenceUrl(ref) { return '/api/collection/existing-files/'+ref.split('/').map(encodeURIComponent).join('/')+'?app_id='+encodeURIComponent($('collection-app-picker').value); }
  function link(parent,text,url) { const a=document.createElement('a');a.href=url;a.textContent=text;a.className='button secondary';a.target='_blank';a.rel='noopener';parent.append(a); }
  function setFilters() {
    const select=$('collection-filter');select.replaceChildren();
    const values=activeTab==='cases'?{all:'全部准备状态',已有资料:'已有资料',缺定位资料:'缺定位资料',缺页面关联证据:'缺页面关联证据',待整理计划:'待整理计划',需要外部条件:'需要外部条件'}:activeTab==='pages'?{all:'全部页面',both:'截图与结构齐全',image:'仅截图',xml:'仅页面结构'}:{all:'全部定位方式',...strategyNames};
    Object.entries(values).forEach(([value,label])=>option(select,value,label));
  }
  function updateActionScope() {
    const same=$('collection-app-picker').value===currentAppId;
    const loaded=library?.app_id===$('collection-app-picker').value;
    const current=apps.find(a=>a.app_id===currentAppId);
    $('collection-action-scope').textContent=same?`当前服务采集 App：${current?.name || ''}。执行前会重新检查手机现场。`:`当前浏览其他 App 的资料。此服务的真机采集仍属于${current?.name || '当前 App'}，切回后才能开始采集。`;
    Array.from($('collection-form').elements).forEach(element=>element.disabled=readOnly || !same || !loaded || (element.id==='collection-start' && element.getAttribute('aria-busy')==='true'));
  }
  function showPreview(item) {
    const preview=$('collection-existing-preview'),links=$('collection-preview-links');preview.replaceChildren();links.replaceChildren();
    if(!item){$('collection-preview-title').textContent='没有匹配的资料';$('collection-existing-state').textContent='调整搜索关键词或筛选条件，再选择资料。';paragraph(preview,activeTab==='cases'?'可搜索用例编号、标题或前置条件。':'可搜索页面名称、控件描述或定位内容。');return;}
    if(activeTab==='pages') {
      $('collection-preview-title').textContent=item.title || item.name;
      $('collection-existing-state').textContent=`${item.name} · ${item.state}`;
      if(item.xml)link(links,'页面结构',evidenceUrl(item.xml));
      if(item.screenshot){link(links,'查看原图',evidenceUrl(item.screenshot));const img=document.createElement('img');img.src=evidenceUrl(item.screenshot);img.alt=item.title || item.name;preview.append(img);}
      else paragraph(preview,'这份资料仅保存页面结构，可通过上方按钮查看。');
      if(item.related_targets?.length)paragraph(preview,`关联 ${item.related_targets.length} 个控件目标`);
    } else if(activeTab==='cases') {
      showCasePreparation(item,preview);
    } else {
      $('collection-preview-title').textContent=item.description;
      $('collection-existing-state').textContent=`${item.target_key} · 已有定位资料，执行前仍需现场核验`;
      const div=document.createElement('div');div.className='locator-details';
      paragraph(div,'定位配置用于查找控件，不代表该控件的业务操作已经通过。');
      const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='查看具体定位策略';details.append(summary);
      (item.android_locators || []).forEach(locator=>{const h=document.createElement('h3');h.textContent=strategyNames[locator.strategy] || locator.strategy;const pre=document.createElement('pre');pre.textContent=locator.value;details.append(h,pre);});div.append(details);
      const page=library.existing_pages.find(p=>p.related_targets?.includes(item.target_key));
      if(page){const button=document.createElement('button');button.className='button secondary';button.type='button';button.textContent='查看关联页面';button.onclick=()=>{activeTab='pages';selectedKey=page.page_id;$('collection-search').value='';setFilters();renderLibrary();};div.append(button);}
      else paragraph(div,'当前独立页面资料中没有对应引用；执行报告中的证据仍可在执行页查看。');
      preview.append(div);
    }
  }
  function showCasePreparation(item,preview) {
    $('collection-preview-title').textContent=`${item.case_id} · ${item.title}`;
    $('collection-existing-state').textContent=`${item.state} · ${item.live_state}`;
    const detail=document.createElement('div');detail.className='preparation-details';
    const section=(title,texts)=>{const block=document.createElement('section'),heading=document.createElement('h3');heading.textContent=title;block.append(heading);texts.forEach(text=>paragraph(block,text));detail.append(block);return block;};
    section('原文前置条件',[item.source.precondition || '原文未填写前置条件，不自行增加条件。']);
    section('原文步骤与预期结果',[`步骤：${item.source.steps || '原文未填写'}`,`结果：${item.source.expected || '原文未填写'}`]);
    if(item.plan_version){const names={draft:'草稿',confirmed:'已确认',validated:'已通过执行验证',candidate:'修复候选'};section('关联计划',[`版本 ${item.plan_version} · ${names[item.plan_status] || item.plan_status}`,...item.declared_conditions.map(c=>`计划声明的准备条件（参考）：${c}`)]);}
    const available=section('已有资料',[`${item.targets.length} 个定位目标 · ${item.snapshot_targets.length} 个计划快照定位 · ${item.pages.length} 份关联页面证据`,'关联来自当前原文对应计划或当前首轮记录，不证明现场数据仍存在。']);
    const refs=document.createElement('details'),summary=document.createElement('summary');summary.textContent='查看控件和页面证据';refs.append(summary);
    item.targets.forEach(target=>{const button=document.createElement('button');button.type='button';button.className='preparation-reference';button.textContent=`${target.description} · ${target.target_key}`;button.onclick=()=>{activeTab='controls';selectedKey=target.target_key;$('collection-search').value='';setFilters();renderLibrary();};refs.append(button);});
    item.snapshot_targets.forEach(key=>paragraph(refs,`${key}：定位保存在计划快照中。`));
    item.pages.forEach(page=>{const row=document.createElement('div');row.className='preparation-evidence';paragraph(row,page.title);if(page.xml)link(row,'页面结构',evidenceUrl(page.xml));if(page.screenshot)link(row,'截图',evidenceUrl(page.screenshot));refs.append(row);});available.append(refs);
    section('资料缺口',item.gaps.length?item.gaps:['按计划引用未发现定位缺口；完整性和现场状态仍需核验。']);
    section('下一步',item.next_steps);
    section('现场状态',[item.live_state+'：账号、权限、测试数据及控件当前可用性尚未检查。']);
    preview.append(detail);
  }
  function renderLibrary() {
    if(!library)return;
    document.querySelectorAll('[data-library-tab]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.libraryTab===activeTab)));
    const query=$('collection-search').value.trim().toLowerCase(),filter=$('collection-filter').value;
    const all=activeTab==='cases'?library.preparations:activeTab==='pages'?library.existing_pages:library.existing_targets;
    const items=all.filter(item=>{
      const text=activeTab==='cases'?[item.case_id,item.title,item.source.precondition,item.state].join(' '):activeTab==='pages'?[item.name,item.title,...(item.related_targets || [])].join(' '):[item.target_key,item.description,...(item.android_locators || []).map(l=>l.value)].join(' ');
      if(query && !text.toLowerCase().includes(query))return false;
      if(filter==='all')return true;
      if(activeTab==='cases')return item.state===filter;
      if(activeTab==='controls')return (item.android_locators || []).some(l=>l.strategy===filter);
      return filter==='both'?item.xml && item.screenshot:filter==='image'?item.screenshot && !item.xml:item.xml && !item.screenshot;
    });
    const key=item=>activeTab==='cases'?item.case_id:activeTab==='pages'?item.page_id:item.target_key;
    if(!items.some(item=>key(item)===selectedKey))selectedKey=items.length?key(items[0]):null;
    $('collection-result-count').textContent=`显示 ${items.length} / ${all.length} ${activeTab==='cases'?'条用例':activeTab==='pages'?'份页面资料':'个控件目标'}`;
    const list=$('collection-library-list');list.replaceChildren();
    items.forEach(item=>{const button=document.createElement('button');button.type='button';button.className='collection-library-item';button.setAttribute('aria-pressed',String(key(item)===selectedKey));const title=document.createElement('strong');title.textContent=activeTab==='cases'?`${item.case_id} · ${item.title}`:activeTab==='pages'?(item.title || item.name):item.description;const small=document.createElement('small');small.textContent=activeTab==='cases'?`${item.state} · ${item.live_state}`:activeTab==='pages'?`${item.name} · ${item.xml?'结构':''}${item.xml && item.screenshot?' / ':''}${item.screenshot?'截图':''}`:item.target_key;button.append(title,small);button.onclick=()=>{selectedKey=key(item);renderLibrary();};list.append(button);});
    if(!items.length){const p=document.createElement('p');p.className='collection-empty';p.textContent='没有匹配的资料。试试减少关键词或选择全部类型。';list.append(p);}
    showPreview(items.find(item=>key(item)===selectedKey));
  }
  async function loadLibrary() {
    const request=++libraryRequest,appId=$('collection-app-picker').value;
    $('collection-app').textContent='正在加载所选 App 的资料…';$('collection-result-count').textContent='正在读取资料…';$('collection-browse-label').textContent=`正在浏览：${apps.find(a=>a.app_id===appId)?.name || '所选 App'}（读取中）`;
    library=null;updateActionScope();$('collection-cases').replaceChildren();$('collection-library-list').replaceChildren();showPreview(null);$('collection-preview-title').textContent='正在加载资料…';
    const [data,preparations]=await Promise.all([api('/api/collection/pages?app_id='+encodeURIComponent(appId)),api('/api/collection/preparations?app_id='+encodeURIComponent(appId))]);
    if(request!==libraryRequest)return;
    library={...data,preparations:preparations.cases};selectedKey=null;pendingRequest=null;
    library.preparations.forEach(c=>option($('collection-cases'),c.case_id,`${c.case_id} · ${c.title}`));
    updateActionScope();
    const app=apps.find(a=>a.app_id===appId);
    $('collection-app').textContent=`${app?.name || data.app_package} · ${data.existing_pages.length} 份页面资料 · ${data.existing_targets.length} 个控件目标`;
    $('collection-browse-label').textContent=`正在浏览：${app?.name || data.app_package} · ${library.preparations.length} 条用例`;$('collection-execute-label').textContent=`当前采集目标：${apps.find(a=>a.app_id===currentAppId)?.name || '当前 App'}${readOnly?'（只读服务，不能操作手机）':'（本服务；实际采集需连接手机）'}`;
    renderLibrary();
  }
  $('collection-app-picker').onchange=()=>loadLibrary().catch(notice);
  $('collection-search').oninput=()=>renderLibrary();$('collection-filter').onchange=()=>renderLibrary();
  document.querySelectorAll('[data-library-tab]').forEach(button=>button.onclick=()=>{activeTab=button.dataset.libraryTab;selectedKey=null;setFilters();renderLibrary();});
  Promise.all([api('/api/device-platforms'),api('/api/collection/apps')]).then(async([devices,registry])=>{
    renderDevices(devices.devices);
    apps=registry.apps;currentAppId=registry.current_app_id;
    apps.forEach(app=>option($('collection-app-picker'),app.app_id,app.name));$('collection-app-picker').value=currentAppId;
    setFilters();await loadLibrary();await refresh();
  }).catch(notice);
})();
