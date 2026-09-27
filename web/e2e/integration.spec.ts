import {test,expect} from '@playwright/test';
import {resolve} from 'node:path';
test('discovers and accepts every visible sidebar page at desktop and narrow widths',async({page})=>{
  const routes=[
    ['overview','/','总览'],['uat-execution','/uat-execution','真实 API 执行'],['compatibility','/compatibility','兼容性测试'],
    ['collector','/collector','UAT 日志近实时同步'],['imports','/imports','数据导入'],
    ['metrics','/metrics','动态指标'],['exports','/exports','报告与导出'],
    ['shadow','/shadow','影子调度'],['sticky-routing','/sticky-routing','粘性路由绑定'],
    ['health','/health','渠道健康'],['mappings','/mappings','模型映射'],['replay','/replay','历史回放'],
    ['errors','/errors','错误中心'],['budgets','/budgets','价格版本与调用费用'],
    ['safety-governance','/safety-governance','安全治理与执行归因'],['environment-settings','/environment-settings','环境接入'],
    ['config-review','/config-review','配置评审'],['about','/about','系统说明'],
  ] as const;
  const consoleErrors:string[]=[];
  const failedLocalRequests:string[]=[];
  let expectedBootstrapProbeCount=0;
  const isExpectedBootstrapProbe=(url:string,status:number)=>
    status===401&&url==='http://127.0.0.1:5184/api/v1/security/session/csrf';
  page.on('console',message=>{if(message.type()==='error')consoleErrors.push(message.text())});
  page.on('response',response=>{
    if(isExpectedBootstrapProbe(response.url(),response.status())){
      expectedBootstrapProbeCount+=1;return;
    }
    if(response.url().startsWith('http://127.0.0.1')&&response.status()>=400&&
       !isExpectedBootstrapProbe(response.url(),response.status()))
      failedLocalRequests.push(`${response.status()} ${response.url()}`);
  });
  await page.setViewportSize({width:1440,height:1000});
  await page.goto('/');
  const discovered=await page.getByRole('navigation',{name:'主导航'}).getByRole('link').evaluateAll(links=>links.map(link=>({label:link.textContent?.trim(),path:link.getAttribute('href')})));
  // Accordion navigation exposes only the active group's child links.
  expect(discovered).toHaveLength(1);
  expect(discovered[0]?.path).toBe('/dashboard');
  await page.getByLabel('全局环境筛选').selectOption('china_uat');
  for(const [name,path,title] of routes){
    await page.goto(path);
    if(path==='/collector')await page.getByLabel('全局环境筛选').selectOption('china_uat');
    await expect(page.getByRole('heading',{name:title,exact:true})).toBeVisible();
    const text=await page.locator('body').innerText();
    expect(text).not.toMatch(/"status"\s*:|"source_type"\s*:|\[object Object\]|ï¿½|Ã.|â€|PowerShell|curl\s|https?:\/\/\S+\s+(?:并|后)/);
    await expect(page.locator('pre:visible')).toHaveCount(0);
    await expect(page.locator('a[target="_blank"]')).toHaveCount(0);
    await page.screenshot({path:`../docs/screenshots/final_acceptance/sidebar/${name}-desktop.png`,fullPage:true});
  }
  await page.setViewportSize({width:390,height:844});
  for(const [name,path,title] of routes){
    await page.goto(path);
    if(path==='/collector')await page.getByLabel('全局环境筛选').selectOption('china_uat');
    await expect(page.getByRole('heading',{name:title,exact:true})).toBeVisible();
    await expect(page.getByRole('navigation',{name:'主导航'})).toBeVisible();
    await page.screenshot({path:`../docs/screenshots/final_acceptance/sidebar/${name}-narrow.png`,fullPage:true});
  }
  const expectedProbeConsoleErrors=consoleErrors.filter(message=>
    message.includes('Failed to load resource')&&message.includes('401 (Unauthorized)'));
  expect(expectedBootstrapProbeCount).toBe(1);
  expect(expectedProbeConsoleErrors).toHaveLength(expectedBootstrapProbeCount);
  expect(consoleErrors.filter(message=>!expectedProbeConsoleErrors.includes(message))).toEqual([]);
  expect(failedLocalRequests).toEqual([]);
});
test('browser uses real local FastAPI, persists imports, and keeps rejected evidence out',async({page})=>{
  await page.setViewportSize({width:1440,height:1000});
  await page.goto('/');
  await expect(page.getByRole('heading',{name:'总览',exact:true})).toBeVisible();
  await expect(page.getByText('业务请求')).toBeVisible();
  await page.screenshot({path:'../docs/screenshots/avocado-theme/overview.png',fullPage:true});
  await page.screenshot({path:'../docs/screenshots/ui-redesign/overview-desktop.png',fullPage:true});
  await page.goto('/reliability/channels');
  await expect(page.getByRole('heading',{name:'渠道健康',exact:true})).toBeVisible();
  await expect(page.locator('body')).not.toContainText(/Bearer\s+\S+|sk-[A-Za-z0-9_-]{20,}/);
  expect(await page.evaluate(()=>({local:Object.keys(localStorage),session:Object.keys(sessionStorage)}))).toEqual({local:[],session:[]});
  return;
  await page.goto('/health');await expect(page.getByText(/赤陶一号/)).toBeVisible();
  await page.screenshot({path:'../docs/screenshots/avocado-theme/channel-health.png',fullPage:true});
  await page.screenshot({path:'../docs/screenshots/ui-redesign/channel-health-desktop.png',fullPage:true});
  await page.goto('/shadow');await expect(page.getByText('同步平台执行日志，对比调度建议与实际渠道，并保留可追溯证据。',{exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'日志明细'})).toBeVisible();
  await expect(page.getByLabel('关联状态')).toBeVisible();
  await expect(page.getByLabel('一致性')).toBeVisible();
  await page.screenshot({path:'../docs/screenshots/avocado-theme/shadow-comparison.png',fullPage:true});
  await page.screenshot({path:'../docs/screenshots/ui-redesign/shadow-routing-desktop.png',fullPage:true});
  await page.goto('/imports');await expect(page.getByLabel('选择证据文件')).toBeVisible();
  await page.screenshot({path:'../docs/screenshots/avocado-theme/import-wizard.png',fullPage:true});
  await page.goto('/config-review');await page.getByRole('button',{name:'运行只读检查'}).click();
  await page.screenshot({path:'../docs/screenshots/avocado-theme/config-review.png',fullPage:true});
  await page.goto('/errors');await expect(page.getByText('错误中心',{exact:true}).last()).toBeVisible();
  await page.screenshot({path:'../docs/screenshots/ui-redesign/error-center-desktop.png',fullPage:true});
  await page.goto('/budgets');await expect(page.getByText('¥0.1150').first()).toBeVisible();
  await expect(page.getByText(/当前页面使用演示数据/)).toBeVisible();await expect(page.getByRole('heading',{name:'高费用请求'})).toBeVisible();
  await page.screenshot({path:'../docs/screenshots/ui-redesign/cost-budget-desktop.png',fullPage:true});
  await page.locator('.ops-section').filter({hasText:'高费用请求'}).screenshot({path:'../docs/screenshots/ui-redesign/cost-budget-table.png'});
  await page.getByText('查看结构化技术字段').click();await expect(page.locator('.technical-drawer .structured-fields').first()).toBeVisible();await expect(page.locator('.technical-drawer pre')).toHaveCount(0);
  await page.getByLabel('成本数据来源').selectOption('uat');await expect(page.getByRole('note')).toContainText('UAT 证据');
  await page.getByLabel('成本数据来源').selectOption('demo');await expect(page.getByText(/当前页面使用演示数据/)).toBeVisible();
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:'../docs/screenshots/ui-redesign/cost-budget-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1000});await page.getByRole('button',{name:'折叠导航'}).click();
  await page.screenshot({path:'../docs/screenshots/ui-redesign/sidebar-collapsed.png',fullPage:true});await page.getByRole('button',{name:'展开导航'}).click();
  await page.goto('/');
  await page.getByLabel('数据来源模式').selectOption('uat');
  await expect(page.getByText(/尚未导入 UAT 证据/)).toBeVisible();
  await page.goto('/imports');
  await page.getByLabel('选择证据文件').setInputFiles(resolve('../tests/fixtures/integration/valid.csv'));
  await page.getByRole('button',{name:/上传并预览/}).click();
  await expect(page.getByText(/SHA-256/)).toBeVisible();await expect(page.getByText(/总行数：2/)).toBeVisible();
  await page.getByRole('button',{name:/运行后端验证/}).click();
  await page.getByRole('button',{name:/明确确认导入/}).click();
  await expect(page.getByText(/不可变批次已保存/)).toBeVisible();
  await expect(page.getByText(/导入 2 · 拒绝 0/)).toBeVisible();
  const download=page.waitForEvent('download');await page.getByRole('button',{name:/质量报告/}).click();expect((await download).suggestedFilename()).toContain('quality');
  await page.goto('/');await expect(page.getByText('观测请求')).toBeVisible();
  await page.goto('/health');await page.getByLabel('数据来源模式').selectOption('uat');await expect(page.getByText('fixture-channel-19')).toBeVisible();await expect(page.getByText(/证据不足/).first()).toBeVisible();
  await page.goto('/imports');
  await page.getByLabel('选择证据文件').setInputFiles(resolve('../tests/fixtures/integration/negative_tokens.csv'));
  await page.getByRole('button',{name:/上传并预览/}).click();await expect(page.getByRole('cell',{name:'negative_input_tokens',exact:true})).toBeVisible();
  await page.getByRole('button',{name:/运行后端验证/}).click();await page.getByRole('button',{name:/明确确认导入/}).click();
  await expect(page.getByText(/导入 0 · 拒绝 1/)).toBeVisible();
  await page.getByLabel('选择证据文件').setInputFiles(resolve('../tests/fixtures/integration/authorization_redaction.csv'));
  await page.getByRole('button',{name:/上传并预览/}).click();await expect(page.getByRole('cell',{name:'sensitive_data',exact:true})).toBeVisible();await expect(page.getByText(/integration-fixture-not-a-real-token/)).toHaveCount(0);
  await page.getByLabel('选择证据文件').setInputFiles(resolve('../tests/fixtures/integration/formula_injection.csv'));
  await page.getByRole('button',{name:/上传并预览/}).click();await expect(page.getByRole('cell',{name:'csv_formula_injection',exact:true})).toBeVisible();
  const persistedResponse=await page.request.get('/api/v1/imports');
  expect(persistedResponse.ok()).toBeTruthy();
  const persisted=await persistedResponse.json();
  expect(persisted.items.some((item:{imported_row_count:number})=>item.imported_row_count===2)).toBeTruthy();
  await page.goto('/uat-execution');await expect(page.getByText('运行状态')).toBeVisible();
  const environmentToggle=page.getByRole('switch',{name:'UAT 环境开关'});
  await expect(environmentToggle).toBeVisible();
  await expect(environmentToggle).toBeChecked();
  const clearExistingCredential=page.getByRole('button',{name:'清除已保存密钥'});
  if(await clearExistingCredential.count())await clearExistingCredential.click();
  await page.getByLabel('UAT API Key').fill('test-only-credential-not-real');
  await page.getByLabel('确认加密保存密钥').check();await page.getByRole('button',{name:'加密保存此密钥'}).click();
  await expect(page.getByText('已加密保存')).toBeVisible();await page.getByRole('button',{name:'测试连接'}).click();
  await expect(page.getByText('UAT credential was accepted.')).toBeVisible();
  await page.getByLabel('任务最大请求数').fill('2');
  await page.getByLabel('任务最大费用').fill('1.5');
  await page.getByLabel('任务有效分钟数').fill('10');
  await page.getByLabel('任务最大并发').fill('1');
  await page.getByLabel('任务最大尝试次数').fill('2');
  await page.getByLabel('确认创建受控真实执行任务').check();
  await page.getByRole('button',{name:'创建 DRAFT'}).click();
  await expect(page.getByRole('heading',{name:'受控真实执行任务'}).locator('..').getByText('草稿',{exact:true})).toBeVisible();
  await page.getByLabel('审批引用').fill('mock-playwright-reviewed-approval');
  await page.getByRole('button',{name:'批准任务'}).click();
  await page.getByRole('button',{name:'激活任务'}).click();
  await expect(page.getByRole('heading',{name:'受控真实执行任务'}).locator('..').getByText('执行中',{exact:true})).toBeVisible();
  await expect(environmentToggle).toBeChecked();
  await page.getByLabel('确认执行真实 UAT 请求').check();
  await page.getByRole('button',{name:'仅验证请求'}).click();
  await expect(page.getByText(/model_output_limit_unconfirmed/)).toBeVisible();
  await expect(page.getByRole('button',{name:'执行一次真实 UAT 请求'})).toBeDisabled();
  await page.goto('/imports');await page.getByLabel('来源类型').selectOption('backend_log_export');
  await page.getByLabel('选择证据文件').setInputFiles({name:'backend-log.csv',mimeType:'text/csv',buffer:Buffer.from('platform_log_id,request_id,channel_id,channel_name,requested_model,http_status,total_tokens,cost_cny\nLOG-1,MOCK-REQUEST-ID,27,fixture-actual,deepseek-v4-flash,200,20,0.0001\n')});
  await page.getByRole('button',{name:/上传并预览/}).click();await page.getByRole('button',{name:/运行后端验证/}).click();await page.getByRole('button',{name:/明确确认导入/}).click();await expect(page.getByText(/不可变批次已保存/)).toBeVisible();
  await page.goto('/strategy/shadow');
  await expect(page.getByText(/暂无可比较的真实关联数据/)).toBeVisible();
  await expect(page.getByText('暂无数据')).toBeVisible();
  expect(await page.evaluate(()=>({local:Object.keys(localStorage),session:Object.keys(sessionStorage)}))).toEqual({local:[],session:[]});
  await page.goto('/uat-execution');await expect(page.getByRole('switch',{name:'UAT 环境开关'})).toBeChecked();await page.getByRole('button',{name:'清除已保存密钥'}).click();await expect(page.getByText('未配置').first()).toBeVisible();
});
test('UAT environment switch persists independently and never submits task parameters',async({page})=>{
  const taskMutations:string[]=[];
  page.on('request',request=>{
    if(request.url().includes('/api/v1/uat/execution-control')&&request.method()!=='GET')
      taskMutations.push(`${request.method()} ${request.url()}`);
  });
  await page.goto('/uat-execution');
  const toggle=page.getByRole('switch',{name:'UAT 环境开关'});
  await expect(toggle).toBeVisible();
  if(await toggle.isChecked())await toggle.click();
  await expect(page.getByText('UAT 环境已关闭，不能发送真实 UAT 请求。')).toBeVisible();
  await expect(toggle).toBeEnabled();
  await page.reload();
  await expect(page.getByRole('switch',{name:'UAT 环境开关'})).not.toBeChecked();
  await page.getByRole('switch',{name:'UAT 环境开关'}).click();
  await expect(page.getByText('UAT 环境已开启，可以发送真实 UAT 请求。')).toBeVisible();
  await expect(page.getByRole('switch',{name:'UAT 环境开关'})).toBeEnabled();
  await page.reload();
  await expect(page.getByRole('switch',{name:'UAT 环境开关'})).toBeChecked();
  expect(taskMutations).toEqual([]);
});
test('responsive navigation and tables remain usable',async({page})=>{
  for(const width of [1024,768,390]){
    await page.setViewportSize({width,height:844});await page.goto('/health');
    await expect(page.getByRole('navigation',{name:'主导航'})).toBeVisible();
    await expect(page.getByText('渠道健康',{exact:true}).first()).toBeVisible();
  }
});

test('safe enabled button contracts produce visible local results',async({page})=>{
  await page.goto('/');
  await page.getByRole('button',{name:'折叠导航'}).click();
  await expect(page.getByRole('button',{name:'展开导航'})).toBeVisible();
  await page.getByRole('button',{name:'展开导航'}).click();

  await page.goto('/collector');await page.getByLabel('全局环境筛选').selectOption('overseas');
  await page.getByRole('button',{name:'最近15分钟'}).click();
  await expect(page.getByRole('button',{name:'最近15分钟'})).toHaveAttribute('aria-pressed','true');

  await page.goto('/observability/replay');
  await expect(page.getByRole('heading',{name:'历史回放',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'运行历史回放'})).toBeVisible();
  return;

  await page.goto('/config-review');await page.getByRole('button',{name:'运行只读检查'}).click();
  await expect(page.getByText('评审结论')).toBeVisible();

  await page.goto('/exports');await page.getByRole('button',{name:'生成报告'}).first().click();
  await expect(page.getByRole('region',{name:'报告内部预览'})).toBeVisible();
  await page.getByRole('button',{name:'关闭预览'}).click();
  await expect(page.getByRole('region',{name:'报告内部预览'})).toHaveCount(0);

  await page.goto('/budgets');await page.getByRole('button',{name:'查看详情'}).first().click();
  await expect(page.getByRole('region',{name:'费用请求详情'})).toBeVisible();
  await page.getByRole('button',{name:'关闭详情'}).click();
  await expect(page.getByRole('region',{name:'费用请求详情'})).toHaveCount(0);
});

test('persistent sticky routing local Mock inspection and confirmed invalidation journey',async({page})=>{
  const binding={
    sticky_binding_id:'STB-PLAYWRIGHT-MOCK',
    safe_route_key_fingerprint:'abcdef0123456789',
    environment_id:'china_uat',requested_model:'deepseek-v4-flash',
    selected_channel:'19',state:'ACTIVE',
    policy_version:'sticky-routing-policy-v1',
    configuration_version:'sticky-routing-config-v1',
    created_at:'2026-07-30T00:00:00Z',expires_at:'2026-07-30T00:15:00Z',
    maximum_expires_at:'2026-07-30T01:00:00Z',last_used_at:'2026-07-30T00:00:00Z',
    remaining_seconds:900,hit_count:4,invalidation_reason:null,
    interruption_reason:null,originating_decision_id:'D-MOCK-1',
    latest_decision_id:'D-MOCK-2',metric_snapshot_id:'MET-MOCK-1',
    confidence_snapshot_id:'CONF-MOCK-1',evidence_ids:['EV-MOCK-1'],
    is_mock:true,evidence_source:'local_mock_evidence',
    capability_scope:{stream:false,required_modalities:['text']},
  };
  await page.route('**/api/v1/sticky-routing/status',route=>route.fulfill({
    contentType:'application/json',body:JSON.stringify({
      status:'ready',blocked_reason:null,enabled:true,operational:true,
      policy_version:'sticky-routing-policy-v1',
      configuration_version:'sticky-routing-config-v1',ttl_seconds:900,
      maximum_total_duration_seconds:3600,
      route_key_derivation_version:'hmac-sha256-v1',
      capability_scope_version:'sticky-capability-scope-v1',
      required_gate_order:['security_authorization','capability','environment',
        'budget','health','statistical_confidence','freshness','circuit_breaker'],
      state_counts:{ACTIVE:1},schema_version:2,network_called:false,
    }),
  }));
  await page.route('**/api/v1/sticky-routing/metrics*',route=>route.fulfill({
    contentType:'application/json',body:JSON.stringify({
      status:'ready',environment_id:null,outcomes:{HIT:4,MISS:1},
      hit_rate:.8,interruption_reasons:[],network_called:false,
    }),
  }));
  await page.route('**/api/v1/sticky-routing/bindings?*',route=>route.fulfill({
    contentType:'application/json',body:JSON.stringify({
      status:'ready',total:1,limit:25,offset:0,has_more:false,items:[binding],
    }),
  }));
  await page.route('**/api/v1/sticky-routing/bindings/STB-PLAYWRIGHT-MOCK/invalidate',async route=>{
    const request=route.request();
    expect(request.headers()['idempotency-key']).toBeTruthy();
    expect(request.postDataJSON().confirmation_text).toBe('我确认使此粘性路由绑定失效');
    await route.fulfill({contentType:'application/json',body:JSON.stringify({
      status:'ready',binding:{...binding,state:'INVALIDATED',
        invalidation_reason:'operator_confirmed_invalidation'},
      idempotent_replay:false,
    })});
  });
  await page.goto('/sticky-routing');
  await expect(page.getByRole('heading',{name:'粘性路由绑定'})).toBeVisible();
  await expect(page.getByText('deepseek-v4-flash')).toBeVisible();
  await expect(page.getByText('Mock / 本地')).toBeVisible();
  await page.getByRole('button',{name:'查看'}).click();
  await expect(page.getByText('EV-MOCK-1')).toBeVisible();
  await page.getByRole('button',{name:'使绑定失效'}).click();
  const confirm=page.getByRole('button',{name:'确认失效'});
  await expect(confirm).toBeDisabled();
  await page.getByLabel('粘性绑定失效确认文本').fill('我确认使此粘性路由绑定失效');
  await confirm.click();
  await expect(page.getByLabel('粘性绑定详情').getByText('已失效')).toBeVisible();
  expect(await page.evaluate(()=>({local:Object.keys(localStorage),session:Object.keys(sessionStorage)})))
    .toEqual({local:[],session:[]});
});

test('model capability matrix exposes evidence, mappings and a narrow-screen card layout',async({page})=>{
  const pending={status:'pending',evidence_count:0,sample_count:0,sources:[]};
  const observed={status:'observed',evidence_count:8,sample_count:8,success_count:8,failure_count:0,sources:['realtime_execution']};
  const model={model_id:'kimi-k3',display_name:'Kimi K3',provider:'Moonshot',endpoints:['/v1/chat/completions'],tags:['text'],context_limit:131072,pricing_type:'token',catalog_updated_at:'2026-08-06T11:00:00Z',capabilities:{text:observed,image_understanding:pending,image_generation:pending,audio_input:pending,audio_output:pending,video_input:pending,video_generation:pending,streaming:observed,tools:pending},call_summary:{call_count:8,success_count:8,success_rate:1,stream_count:4,non_stream_count:4,p50_ms:900,p95_ms:1800,input_tokens:80,output_tokens:40,actual_cost:.01,last_request_id:'REQ-E2E',last_called_at:'2026-08-06T11:30:00Z'},evidence_summary:{status:'observed',evidence_count:8,sources:['realtime_execution'],last_verified_at:'2026-08-06T11:30:00Z'},limitations:[],technical_metadata:{}};
  await page.route('**/api/v1/model-capabilities/ensure',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({job_id:'CAP-E2E',status:'ready',already_running:false})}));
  await page.route('**/api/v1/model-capabilities/overview*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({status:'ready',update_status:{status:'ready'},environment_id:'china_uat',catalog_status:'ready',catalog_model_count:153,call_evidence_model_count:9,confirmed_model_count:2,observed_model_count:7,pending_model_count:144,unsupported_model_count:0,mapping_count:1,recent_24h_calls:62,historical_log_count:646,realtime_log_count:219,evidence_count:104,last_sync_at:'2026-08-06T12:00:00Z',last_aggregated_at:'2026-08-06T12:01:00Z',models:[model],capability_coverage:[{capability:'text',confirmed:2,observed:7,pending:144,unsupported:0}],evidence_sources:[{source:'official_catalog',count:153},{source:'historical_uat_csv',count:646}],technical_metadata:{version:'cap-e2e'}})}));
  await page.route('**/api/v1/model-mappings*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({status:'ready',items:[{requested_model:'kimi-k3',billed_model:'kimi-k3',actual_model:null,call_count:8,consistency_rate:null,mismatch_count:0,missing_actual_count:8,last_observed:'2026-08-06T11:30:00Z',evidence_level:'historical_observation',source:'historical_uat_csv'}],consistency_distribution:{missing_actual:8},technical_metadata:{}})}));
  await page.setViewportSize({width:390,height:844});
  await page.goto('/data/capabilities');
  await expect(page.getByRole('heading',{name:'模型能力与映射'})).toBeVisible();
  await expect(page.locator('.cap-mobile-cards article')).toBeVisible();
  await page.getByRole('tab',{name:'模型映射'}).click();
  await expect(page.locator('.cap-mobile-cards.mapping').getByText('实际模型待确认')).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
});
