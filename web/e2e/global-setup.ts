import {mkdirSync} from 'node:fs';import {resolve} from 'node:path';
export default async()=>{
  if(process.env.PLAYWRIGHT_EXTERNAL_SERVERS==='1')return;
  // Playwright starts managed web servers before global setup. A per-run DB is
  // configured in playwright.config.ts so Windows never tries to unlink an
  // SQLite file already opened by FastAPI.
  mkdirSync(resolve('test-results'),{recursive:true});
};
