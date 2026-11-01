import { test, expect } from '@playwright/test';
import { login } from './helpers';

test('partially failed batch refreshes accepted documents and stops polling', async ({ page }) => {
  await login(page);
  const response = await page.request.post('/api/cases', { data: { title: `批量终态验收 ${Date.now()}` } });
  expect(response.status()).toBe(201);
  const created = await response.json();
  // The queue itself has offline backend tests. This page regression supplies
  // its real terminal contract while importing the accepted file through HTTP.
  await page.route(`**/api/cases/${created.id}/batch-import`, async route => {
    const accepted = await page.request.post(`/api/cases/${created.id}/documents`, {
      multipart: { files: { name: '成功导入的材料.txt', mimeType: 'text/plain', buffer: Buffer.from('合成成功材料') } },
    });
    expect(accepted.status()).toBe(201);
    await route.fulfill({ status: 202, json: { batch_id: 987654 } });
  });
  let polls = 0;
  await page.route('**/api/batch-imports/987654', async route => {
    polls++;
    await route.fulfill({ json: {
      status: 'completed_with_errors', total_files: 2, processed_files: 2,
      successful_files: 1, failed_files: 1, progress_percent: 100,
      error_log: ['失败扫描件.pdf：无法解析合成样例'],
    } });
  });
  await page.goto(`/?case=${created.id}&view=documents`);
  await page.getByText('批量导入（需队列服务）', { exact: true }).click();
  await page.clock.install();
  await page.getByLabel('批量文件', { exact: true }).setInputFiles([
    { name: '成功导入的材料.txt', mimeType: 'text/plain', buffer: Buffer.from('合成成功材料') },
    { name: '失败扫描件.pdf', mimeType: 'application/pdf', buffer: Buffer.from('synthetic invalid PDF') },
  ]);
  await expect(page.getByText(/导入完成（部分失败） · 2\/2 个文件/)).toBeVisible();
  await expect(page.getByRole('button', { name: '成功导入的材料.txt', exact: true })).toBeVisible();
  await expect(page.getByText('失败扫描件.pdf：无法解析合成样例', { exact: true })).toBeVisible();
  await page.clock.fastForward(5000);
  expect(polls).toBe(1);
});
