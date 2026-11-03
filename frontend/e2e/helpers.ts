import { expect, type Page } from '@playwright/test';
export async function login(page: Page, token = process.env.LEXVAULT_E2E_TOKEN!) {
  await page.goto('/');
  await page.getByLabel('访问令牌').fill(token);
  await page.getByRole('button', { name: '登录工作空间' }).click();
  await expect(page.getByRole('navigation', { name: '案件列表' })).toBeVisible();
}
export async function demoCase(page: Page) {
  await page.getByRole('navigation', { name: '案件列表' }).getByRole('button').filter({ hasText: '（演示）' }).first().click();
}
