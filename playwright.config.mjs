import { defineConfig, devices } from '@playwright/test';

const python = process.env.ODIN_TEST_PYTHON ||
  (process.platform === 'win32' ? '.venv\\Scripts\\python.exe' : '.venv/bin/python');
const port = process.env.ODIN_TEST_PORT || '8099';
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  use: { baseURL, screenshot: 'only-on-failure' },
  webServer: {
    command: `"${python}" tests/browser_server.py`,
    url: `${baseURL}/settings/login`,
    env: { PYTHONUTF8: '1', ODIN_TEST_PORT: port },
    reuseExistingServer: false,
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile', use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium' } },
  ],
});