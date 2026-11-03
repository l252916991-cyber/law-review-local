import { test, expect, type Page } from '@playwright/test';
import { login } from './helpers';

// Each run uses fresh names: both browser projects share one server and database.
const stamp = () => `${Date.now()}${Math.floor(Math.random() * 1000)}`;

async function newCase(page: Page, title: string) {
  await page.getByRole('button', { name: '＋ 新建案件', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '新建案件' });
  await dialog.getByLabel('案件名称').fill(title);
  await dialog.getByRole('button', { name: '创建案件', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  return Number(new URL(page.url()).searchParams.get('case'));
}

async function registerParties(page: Page, parties: { role: string; name: string }[]) {
  await page.getByRole('button', { name: '利益冲突与隔离', exact: true }).click();
  await page.getByRole('button', { name: /登记当事人|修改当事人/ }).click();
  const dialog = page.getByRole('dialog', { name: '登记当事人' });
  for (const [index, party] of parties.entries()) {
    if (index > 0) await dialog.getByRole('button', { name: '＋ 添加当事人' }).click();
    const group = dialog.getByRole('group', { name: `当事人 ${index + 1}` });
    await group.getByLabel('身份').selectOption({ label: party.role });
    await group.getByLabel('名称').fill(party.name);
  }
  await dialog.getByRole('button', { name: '保存并检查' }).click();
  await expect(dialog).toHaveCount(0);
}

test('a conflict hit locks the case until another partner clears it', async ({ page }) => {
  const id = stamp();
  await login(page);
  const earlier = `E2E 原案 ${id}`;
  await newCase(page, earlier);
  await registerParties(page, [{ role: '对方当事人', name: `乙贸易${id}有限责任公司` }]);
  await expect(page.getByRole('status')).toContainText('未发现冲突');

  await newCase(page, `E2E 新委托 ${id}`);
  await registerParties(page, [{ role: '委托人', name: `乙贸易${id}有限公司` }]);
  await expect(page.getByRole('status')).toContainText('命中待审批');
  await expect(page.locator('.row.hit')).toContainText('直接冲突');
  await expect(page.locator('.row.hit')).toContainText(earlier);
  await expect(page.getByRole('button', { name: '利益冲突待审批' })).toBeVisible();

  // Work is refused while the check is pending.
  await page.getByRole('button', { name: '卷宗目录', exact: true }).click();
  await page.getByLabel('上传卷宗', { exact: true }).setInputFiles({
    name: '冲突期间.txt', mimeType: 'text/plain', buffer: Buffer.from('合成材料，冲突审批前不应导入。'),
  });
  await expect(page.getByRole('alert').filter({ hasText: '合伙人放行前' }).first()).toBeVisible();

  // The partner who ran the check cannot clear it.
  await page.getByRole('button', { name: '利益冲突待审批' }).click();
  await expect(page.getByText('这次检查由你发起，需由另一位合伙人决定。')).toBeVisible();
  await expect(page.getByRole('button', { name: '放行', exact: true })).toBeDisabled();

  await page.getByRole('button', { name: '退出', exact: true }).click();
  await login(page, process.env.LEXVAULT_E2E_PARTNER_TOKEN!);
  await page.getByRole('navigation', { name: '案件列表' }).getByRole('button').filter({ hasText: `E2E 新委托 ${id}` }).click();
  await page.getByRole('button', { name: '利益冲突待审批' }).click();
  await page.getByLabel('决定理由').fill('已取得双方书面同意，并对承办团队设立隔离墙');
  await page.getByRole('button', { name: '放行', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已放行');
  await expect(page.getByText('合成风控合伙人 于', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: '利益冲突待审批' })).toHaveCount(0);

  await page.getByRole('button', { name: '卷宗目录', exact: true }).click();
  await page.getByLabel('上传卷宗', { exact: true }).setInputFiles({
    name: '放行之后.txt', mimeType: 'text/plain', buffer: Buffer.from('合成材料，放行后可以导入。'),
  });
  await expect(page.getByRole('status')).toContainText('导入完成：1 个文件');
});

test('a wall hides the case from an administrator outside it until break-glass', async ({ page }) => {
  const id = stamp();
  const title = `E2E 隔离案件 ${id}`;
  await login(page);
  const caseId = await newCase(page, title);
  await page.getByRole('button', { name: '利益冲突与隔离', exact: true }).click();
  await page.getByRole('button', { name: '设立隔离墙' }).click();
  const dialog = page.getByRole('dialog', { name: '设立隔离墙' });
  await dialog.getByLabel('设墙理由').fill('关联案件对方当事人由本所另案代理');
  await dialog.getByLabel('墙内名单').fill('合成风控合伙人');
  page.once('dialog', confirm => void confirm.accept());
  await dialog.getByRole('button', { name: '保存' }).click();
  const cases = page.getByRole('navigation', { name: '案件列表' });
  await expect(cases.getByRole('button').filter({ hasText: title })).toHaveCount(0);
  expect((await page.request.get(`/api/cases/${caseId}`)).status()).toBe(404);

  await page.getByRole('button', { name: '隔离墙', exact: true }).click();
  const walls = page.getByRole('dialog', { name: '信息隔离墙' });
  const row = walls.locator('article.row').filter({ hasText: `案件 #${caseId}` });
  await expect(row).not.toContainText(title); // titles stay out of the wall list
  await row.getByRole('button', { name: '破窗访问' }).click();
  await walls.getByLabel(`破窗访问案件 #${caseId} 的理由`).fill('客户投诉核查，需要核对材料归档情况');
  await walls.getByRole('button', { name: '确认' }).click();
  await expect(walls.getByRole('status')).toContainText('30 分钟');
  await expect(cases.getByRole('button').filter({ hasText: title })).toHaveCount(1);
  expect((await page.request.get(`/api/cases/${caseId}`)).status()).toBe(200);

  await walls.locator('article.row').filter({ hasText: `案件 #${caseId}` }).getByRole('button', { name: '撤除' }).click();
  await walls.getByLabel(`撤除案件 #${caseId} 隔离墙的理由`).fill('关联案件已结案');
  await walls.getByRole('button', { name: '确认' }).click();
  await expect(walls.getByRole('status')).toContainText('已撤除');
});
