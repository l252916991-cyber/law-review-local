import { test, expect } from '@playwright/test';
import { login, demoCase } from './helpers';

test('failed node diagnostics stay inspectable without displaying hidden reasoning', async ({ page }) => {
  await login(page); await demoCase(page);
  await page.getByRole('button', { name: '阅卷任务', exact: true }).click();
  const failed = page.locator('.row').filter({ hasText: '合成失败节点验收' });
  await failed.getByRole('button', { name: '查看轨迹' }).click();
  const trace = page.getByRole('region', { name: '执行轨迹' });
  await expect(trace).toContainText('证据 Agent');
  await trace.locator('summary').click();
  await expect(trace).toContainText('synthetic_node_failure');
  await expect(page.getByText('NEVER_RENDER_PRIVATE_REASONING', { exact: false })).toHaveCount(0);
});

test('resume a real checkpoint and compare both runtimes without model calls', async ({ page }, info) => {
  await login(page); await demoCase(page);
  await page.getByRole('button', { name: '阅卷任务', exact: true }).click();
  const interrupted = page.locator('.row').filter({ hasText: `合成断点恢复验收 ${info.project.name}` });
  await interrupted.getByRole('button', { name: '恢复运行' }).click();
  await expect(page.getByRole('region', { name: '审校结果' })).toBeVisible();
  await expect(page.getByRole('region', { name: '执行轨迹' })).toContainText('completed');
  await page.getByLabel('使用模型', { exact: true }).uncheck();
  await page.getByLabel('审校问题').fill('张某如何参与年化12%固定回报宣传？');
  await page.getByRole('button', { name: '分析方式对比', exact: true }).click();
  await expect(page.getByRole('heading', { name: '运行方式对比', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: /^native ·/ })).toBeVisible();
  await expect(page.getByRole('heading', { name: /^langgraph ·/ })).toBeVisible();
});

test('background agent tool loop, fallback, trace and usage stay in the selected case', async ({ page }) => {
  await login(page); await demoCase(page);
  await page.getByRole('button', { name: '阅卷任务', exact: true }).click();
  await page.getByLabel('审校问题').fill('回退验证：张某如何参与年化12%固定回报宣传？');
  await page.getByRole('button', { name: '运行审校 →' }).click();
  const result = page.getByRole('region', { name: '审校结果' });
  await expect(result).toContainText('请律师复核', { timeout: 30_000 });
  await expect(result).toContainText('回退模型：fallback-model');
  const trace = page.getByRole('region', { name: '执行轨迹' });
  await expect(trace).toContainText('completed');
  await expect(trace).toContainText('审校 Agent');
  await trace.locator('details').last().locator('summary').click();
  await expect(trace).toContainText('get_page');
  await expect(trace).toContainText('fallback-model');
  await page.getByRole('button', { name: '用量与费用', exact: true }).click();
  const usage = page.getByRole('region', { name: '模型用量与估算费用' });
  await expect(usage).toContainText('按模型');
  await expect(usage).toContainText('fallback-model');
  await expect(usage).toContainText('未配置成本');
  // API result and rendered counts describe the same case and call ledger.
  const cases = await (await page.request.get('/api/cases')).json();
  const demo = cases.find((row: { title: string }) => row.title.includes('（演示）'));
  const metrics = await (await page.request.get(`/api/cases/${demo.id}/platform-metrics`)).json();
  expect(metrics.llm_usage.calls).toBeGreaterThan(1);
  await expect(usage.locator('.metrics article').first().locator('strong')).toHaveText(String(metrics.llm_usage.calls));
  await page.screenshot({ path: test.info().outputPath('metrics.png'), fullPage: true });
  const empty = await (await page.request.post('/api/cases', { data: { title: `E2E 无调用 ${Date.now()}` } })).json();
  await page.reload();
  await page.getByRole('navigation', { name: '案件列表' }).getByRole('button').filter({ hasText: empty.title }).click();
  await page.getByRole('button', { name: '用量与费用', exact: true }).click();
  await expect(page.getByRole('region', { name: '模型用量与估算费用' }).locator('.metrics article').first().locator('strong')).toHaveText('0');
  await expect(page.getByText('fallback-model', { exact: true })).toHaveCount(0);
});

test('evidence editing, approval attribution, safe annotations, graph and export', async ({ page }) => {
  await login(page); await demoCase(page);
  await page.getByRole('button', { name: '证据关系', exact: true }).click();
  await page.getByRole('button', { name: '＋ 添加证据' }).click();
  const dialog = page.getByRole('dialog', { name: '添加证据' });
  const title = `合成证据 ${Date.now()}`;
  await dialog.getByLabel('证据标题').fill(title);
  await dialog.getByLabel('待证事实').fill('宣传内容需要核验');
  await dialog.getByLabel('状态', { exact: true }).selectOption('已确认');
  await dialog.getByLabel('来源卷宗').selectOption({ index: 1 });
  await dialog.getByLabel('原文引用').fill('年化12%固定回报');
  await dialog.getByRole('button', { name: '保存证据' }).click();
  const card = page.locator('.evidence-grid article').filter({ has: page.getByRole('heading', { name: title, exact: true }) });
  await expect(card).toContainText('确认人：合成页面验证');
  await card.getByRole('button', { name: '批注', exact: true }).click();
  const annotation = page.getByRole('dialog', { name: '证据批注' });
  const content = '<img src=x onerror="window.__lexvaultXss=1">';
  await annotation.getByLabel('批注内容').fill(content);
  await annotation.getByRole('button', { name: '添加批注' }).click();
  await expect(annotation).toContainText(content);
  expect(await page.evaluate('typeof window.__lexvaultXss')).toBe('undefined');
  await annotation.getByRole('button', { name: '切换处理状态' }).click();
  await expect(annotation).toContainText('已处理');
  await annotation.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('img', { name: '证据关联图' })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('evidence.png'), fullPage: true });
  await page.getByRole('button', { name: '成果导出', exact: true }).click();
  const [download] = await Promise.all([
    page.waitForEvent('download'), page.getByRole('link', { name: '下载预览包' }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.zip$/);
  await page.getByRole('button', { name: '模型设置', exact: true }).click();
  await expect(page.getByLabel('模型名称')).toHaveValue('stub-chat');
});

test('missing statute corpus remains explicit in the specialist trace', async ({ page }) => {
  await login(page); await demoCase(page);
  await page.getByRole('button', { name: '阅卷任务', exact: true }).click();
  await page.getByLabel('使用模型', { exact: true }).uncheck();
  await page.getByLabel('审校问题').fill('请核验刑法第999999条的法律规定，缺少依据时不要作法律结论。');
  await page.getByRole('button', { name: '运行审校 →' }).click();
  const trace = page.getByRole('region', { name: '执行轨迹' });
  await expect(trace.locator('header .badge').first()).toHaveText('completed', { timeout: 30_000 });
  await trace.locator('details').filter({ hasText: '法条核验 Agent' }).locator('summary').click();
  await expect(trace).toContainText('未配置法条语料，无法核验法律依据');
});

test('administrator creates a member and assigns a case role', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: '成员与权限', exact: true }).click();
  await page.getByRole('button', { name: '添加成员' }).click();
  const dialog = page.getByRole('dialog', { name: '添加成员' });
  const name = `合成成员 ${Date.now()}`;
  await dialog.getByLabel('姓名 / 登录标识').fill(name);
  await dialog.getByRole('button', { name: '创建成员并生成令牌' }).click();
  const tokenDialog = page.getByRole('dialog', { name: '访问令牌（只显示一次）' });
  await expect(tokenDialog).toBeVisible();
  await expect(tokenDialog.locator('.token')).not.toHaveText('');
  await tokenDialog.getByRole('button', { name: '我已保存' }).click();
  await expect(page.getByText(name, { exact: false }).first()).toBeVisible();

  await page.getByRole('button', { name: '案件总览', exact: true }).click();
  const panel = page.locator('section.card').filter({
    has: page.getByRole('heading', { name: '案件分工', exact: true }),
  });
  await panel.locator('select[name="member_id"]').selectOption({ label: name });
  await panel.locator('select[name="role"]').selectOption('reviewer');
  await panel.getByRole('button', { name: '分配' }).click();
  await expect(panel).toContainText(name);
  await expect(panel).toContainText('复核律师');
  await page.screenshot({ path: test.info().outputPath('members.png'), fullPage: true });
});

test('owner creates a review task and cancels it', async ({ page }) => {
  await login(page); await demoCase(page);
  await page.getByRole('button', { name: '任务', exact: true }).click();
  const title = `合成任务 ${Date.now()}`;
  await page.locator('input[name="title"]').fill(title);
  await page.locator('select[name="kind"]').selectOption('核证');
  await page.locator('input[name="due_at"]').fill('2026-12-31');
  await page.getByRole('button', { name: '创建任务' }).click();
  const row = page.locator('article.row').filter({ hasText: title });
  await expect(row).toContainText('待处理');
  await expect(row).toContainText('2026-12-31');
  page.once('dialog', (dialog) => dialog.accept());
  await row.getByRole('button', { name: '取消' }).click();
  await expect(row).toContainText('已取消');
  await page.screenshot({ path: test.info().outputPath('tasks.png'), fullPage: true });
});

test('report approval unlocks formal delivery', async ({ page }) => {
  await login(page); await demoCase(page);
  await page.getByRole('button', { name: '成果导出', exact: true }).click();
  await page.getByRole('button', { name: '新建报告草稿' }).click();
  const row = page.locator('article.row').filter({ hasText: /^v\d+ / }).first();
  await expect(row).toContainText('草稿');
  await row.getByRole('button', { name: '提交复核' }).click();
  await expect(row).toContainText('待复核');
  await row.getByRole('button', { name: '批准' }).click();
  await expect(row).toContainText('已批准');
  await expect(page.getByText('可正式交付')).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('reports.png'), fullPage: true });
});
