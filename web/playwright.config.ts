import {defineConfig} from '@playwright/test';
const externalServers=process.env.PLAYWRIGHT_EXTERNAL_SERVERS==='1';
const isolatedTestDb=`web/test-results/playwright-${process.pid}.sqlite3`;
const localMockSigningKey=Buffer.from('playwright-local-mock-signing-material-v1-000000000').toString('base64');
export default defineConfig({
  testDir:'./e2e',globalSetup:'./e2e/global-setup.ts',workers:1,
  expect:{timeout:15_000},
  webServer:externalServers?undefined:[
    {command:'C:\\Users\\LX\\anaconda3\\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 5184',
      cwd:'..',url:'http://127.0.0.1:5184/ready',reuseExistingServer:false,
      env:{ROUTING_CONSOLE_DB:isolatedTestDb,WEIMETA_UAT_API_KEY:'',
        WEIMETA_REAL_EXECUTION_ENABLED:'true',WEIMETA_TEST_TRANSPORT:'1',
        ROUTING_CONSOLE_TEST_MODE:'1',UAT_FRONTEND_ORIGINS:'http://127.0.0.1:5184',
        ROUTING_CONSOLE_ALLOWED_ORIGINS:'http://127.0.0.1:5184',
        ROUTING_CONSOLE_ENTERPRISE_SIGNING_KEY:localMockSigningKey,
        UAT_PERSISTENT_CREDENTIAL_DIRECTORY:`web/test-results/credentials-${process.pid}`,
        UAT_LOCAL_API_HOSTS:'127.0.0.1:5184,localhost:5184',
        ROUTING_CONSOLE_ALLOWED_HOSTS:'127.0.0.1:5184,localhost:5184',
        ROUTING_CONSOLE_FRONTEND_DIST:'web/dist'}},
  ],
  use:{baseURL:'http://127.0.0.1:5184',channel:'chrome'},timeout:60_000,
});
