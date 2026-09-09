import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8099', screenshot: 'only-on-failure' },
  webServer: {
    command: `${process.env.ODIN_TEST_PYTHON || '.venv/bin/python'} tests/browser_server.py`,
    url: 'http://127.0.0.1:8099/settings/login',
    reuseExistingServer: false,
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile', use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium' } },
  ],
});