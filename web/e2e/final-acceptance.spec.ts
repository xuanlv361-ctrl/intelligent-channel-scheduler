import {test,expect} from '@playwright/test';
import {mkdirSync} from 'node:fs';
import {resolve} from 'node:path';

const screenshots=resolve('../docs/screenshots/final_acceptance');

test.beforeAll(async()=>{
  mkdirSync(screenshots,{recursive:true});
});

const pages=[
  ['overview','/'],['uat-execution','/uat-execution'],['imports','/imports'],
  ['collector','/collector'],['metrics','/metrics'],['safety-governance','/safety-governance'],['health','/health'],['shadow','/shadow'],
  ['replay','/replay'],['errors','/errors'],
  ['budgets','/budgets'],['mappings','/mappings'],['compatibility','/compatibility'],
  ['config-review','/config-review'],['exports','/exports'],
  ['environment-settings','/environment-settings'],
] as const;

test('critical pages render against local Mock backend at desktop and tablet widths',async({page})=>{
  test.setTimeout(180_000);
  for(const width of [1440,820]){
    await page.setViewportSize({width,height:960});
    for(const [name,path] of pages){
      await page.goto(path);
      if(path==='/collector'){
        await page.locator('select').filter({has:page.locator('option[value="china_uat"]')}).selectOption('china_uat');
      }
      await expect(page.locator('main h1:visible').last()).toBeVisible();
      await expect(page.locator('body')).not.toContainText(/Bearer\s+\S+|sk-[A-Za-z0-9_-]{20,}/);
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(2);
      if(width===1440)await page.screenshot({path:resolve(screenshots,`${name}-desktop.png`),fullPage:true});
    }
  }
});

test('retired Bug URL redirects to the error-center action',async({page})=>{
  await page.goto('/bugs');
  await expect(page).toHaveURL(/\/errors\?tab=bug-report$/);
  await expect(page.getByRole('heading',{name:'错误中心',exact:true})).toBeVisible();
});

test('global search preserves environment and shows a truthful local empty state',async({page})=>{
  test.setTimeout(30_000);
  await page.goto('/');
  const environment=page.locator('select').filter({has:page.locator('option[value="overseas"]')});
  await environment.selectOption('overseas');
  await page.locator('form[role="search"] input').fill('NO-SUCH-LOCAL-EVIDENCE');
  await page.locator('form[role="search"] button[type="submit"]').click();
  await expect(page).toHaveURL(/\/search\?q=NO-SUCH-LOCAL-EVIDENCE/);
  await expect(page.locator('.page-title h1')).toHaveText('全局搜索');
  await expect(page.locator('.search-provenance')).toContainText('海外站');
  await expect(page.locator('.state.empty')).toContainText('没有匹配证据');
  await page.screenshot({path:resolve(screenshots,'global-search-empty.png'),fullPage:true});
});
