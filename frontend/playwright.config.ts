import { defineConfig, devices } from '@playwright/test';
import { randomBytes } from 'node:crypto';

process.env.LEXVAULT_E2E_TOKEN ||= randomBytes(32).toString('hex');
// A second partner: conflict approvals need someone other than the person who ran the check.
process.env.LEXVAULT_E2E_PARTNER_TOKEN ||= randomBytes(32).toString('hex');
export default defineConfig({
  testDir: './e2e', fullyParallel: false, workers: 1, retries: 0, timeout: 60_000,
  use: { baseURL: 'http://127.0.0.1:13000', trace: 'retain-on-failure', screenshot: 'only-on-failure',
    launchOptions: process.env.LEXVAULT_BROWSER_PATH ? { executablePath: process.env.LEXVAULT_BROWSER_PATH } : {},
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 7'] } },
  ],
  webServer: { command: 'uv run --project .. --locked python ../scripts/acceptance/frontend_test_server.py',
    url: 'http://127.0.0.1:13000/api/auth/me', timeout: 60_000, reuseExistingServer: false },
});
