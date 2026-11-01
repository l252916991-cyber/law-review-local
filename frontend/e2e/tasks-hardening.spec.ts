import { test, expect } from '@playwright/test';
import { login } from './helpers';

test('task assignment only offers members who can read and edit the case', async ({ page }, info) => {
  await login(page);
  const suffix = `${info.project.name}-${Date.now()}`;
  const created = await page.request.post('/api/cases', { data: { title: `任务负责人验收 ${suffix}` } });
  expect(created.status()).toBe(201);
  const caseRow = await created.json();
  const names = { eligible: `可承接助理 ${suffix}`, outsider: `未分案成员 ${suffix}`, viewer: `只读成员 ${suffix}` };
  const memberIds: Record<string, number> = {};
  for (const key of ['eligible', 'outsider', 'viewer'] as const) {
    const response = await page.request.post('/api/members', { data: {
      name: names[key], permissions: ['view', 'edit'],
      assignments: key === 'outsider' ? [] : [{ case_id: caseRow.id, role: key === 'viewer' ? 'viewer' : 'assistant' }],
    } });
    expect(response.status()).toBe(201);
    memberIds[key] = (await response.json()).member.id;
  }
  await page.goto(`/?case=${caseRow.id}&view=tasks`);
  const select = page.locator('select[name="assignee_member_id"]');
  await expect(select.locator('option').filter({ hasText: names.eligible })).toHaveCount(1);
  await expect(select.locator('option').filter({ hasText: names.outsider })).toHaveCount(0);
  await expect(select.locator('option').filter({ hasText: names.viewer })).toHaveCount(0);
  await select.selectOption(String(memberIds.eligible));
  const title = `可处理的核证任务 ${suffix}`;
  await page.locator('input[name="title"]').fill(title);
  await page.getByRole('button', { name: '创建任务', exact: true }).click();
  await expect(page.locator('article.row').filter({ hasText: title })).toContainText(names.eligible);
  // Direct API writes must enforce the same rule as the picker.
  const denied = await page.request.post(`/api/cases/${caseRow.id}/tasks`, { data: {
    title: '绕过候选列表的分派', assignee_member_id: memberIds.outsider,
  } });
  expect(denied.status()).toBe(409);
});

test('stale task action sends its read version, reports conflict and refreshes', async ({ page }, info) => {
  await login(page);
  const suffix = `${info.project.name}-${Date.now()}`;
  const created = await page.request.post('/api/cases', { data: { title: `任务并发验收 ${suffix}` } });
  expect(created.status()).toBe(201);
  const caseRow = await created.json();
  const response = await page.request.post(`/api/cases/${caseRow.id}/tasks`, { data: { title: `任务版本验收 ${suffix}` } });
  expect(response.status()).toBe(201);
  const task = (await response.json()).task;
  await page.goto(`/?case=${caseRow.id}&view=tasks`);
  const row = page.locator('article.row').filter({ hasText: task.title });
  await expect(row).toContainText('待处理');
  // Another operator changes the deadline while this page still has version 1.
  const changed = await page.request.patch(`/api/tasks/${task.id}`, { data: {
    action: 'due', due_at: '2027-01-15', expected_version: task.version,
  } });
  expect(changed.status()).toBe(200);
  const latest = (await changed.json()).task;
  const conflictPromise = page.waitForResponse(result =>
    new URL(result.url()).pathname === `/api/tasks/${task.id}` && result.request().method() === 'PATCH');
  page.once('dialog', dialog => dialog.accept());
  await row.getByRole('button', { name: '取消', exact: true }).click();
  const conflict = await conflictPromise;
  expect(conflict.request().postDataJSON().expected_version).toBe(task.version);
  expect(conflict.status()).toBe(409);
  await expect(page.getByRole('alert').filter({ hasText: '任务已被其他人修改' })).toBeVisible();
  await expect(row).toContainText('2027-01-15');
  await expect(row).toContainText('待处理');
  const retryPromise = page.waitForResponse(result =>
    new URL(result.url()).pathname === `/api/tasks/${task.id}` && result.request().method() === 'PATCH');
  page.once('dialog', dialog => dialog.accept());
  await row.getByRole('button', { name: '取消', exact: true }).click();
  const retried = await retryPromise;
  expect(retried.request().postDataJSON().expected_version).toBe(latest.version);
  expect(retried.status()).toBe(200);
  await expect(row).toContainText('已取消');
});

test('assistant can read reviewer return reasons and retain their full history after restarting', async ({ page, browser }, info) => {
  await login(page);
  const suffix = `${info.project.name}-${Date.now()}`;
  const created = await page.request.post('/api/cases', { data: { title: `任务退回说明验收 ${suffix}` } });
  expect(created.status()).toBe(201);
  const caseRow = await created.json();
  const assistantResponse = await page.request.post('/api/members', { data: {
    name: `补证助理 ${suffix}`, permissions: ['view', 'edit'], assignments: [{ case_id: caseRow.id, role: 'assistant' }],
  } });
  expect(assistantResponse.status()).toBe(201);
  const assistant = await assistantResponse.json();
  const reviewerResponse = await page.request.post('/api/members', { data: {
    name: `独立复核 ${suffix}`, permissions: ['view', 'approve'], assignments: [{ case_id: caseRow.id, role: 'reviewer' }],
  } });
  expect(reviewerResponse.status()).toBe(201);
  const reviewer = await reviewerResponse.json();
  const createdTask = await page.request.post(`/api/cases/${caseRow.id}/tasks`, { data: {
    title: `补证需看退回原因 ${suffix}`, assignee_member_id: assistant.member.id,
  } });
  expect(createdTask.status()).toBe(201);
  const task = (await createdTask.json()).task;
  const assistantContext = await browser.newContext({ baseURL: new URL(page.url()).origin });
  const reviewerContext = await browser.newContext({ baseURL: new URL(page.url()).origin });
  try {
    const worker = await assistantContext.newPage();
    await worker.goto('/');
    await worker.getByLabel('访问令牌').fill(assistant.token);
    await worker.getByRole('button', { name: '登录工作空间', exact: true }).click();
    await expect(worker.getByRole('navigation', { name: '案件列表' })).toBeVisible();
    await worker.goto(`/?case=${caseRow.id}&view=tasks`);
    const row = worker.locator('article.row').filter({ hasText: task.title });
    await row.getByRole('button', { name: '开始', exact: true }).click();
    await expect(row).toContainText('进行中');
    await row.getByRole('button', { name: '提交复核', exact: true }).click();
    await expect(row).toContainText('待复核');
    expect((await reviewerContext.request.post('/api/auth/session', { data: { token: reviewer.token } })).status()).toBe(200);
    const reason = '签字页不清楚，请补拍完整签字及日期';
    const returned = await reviewerContext.request.patch(`/api/tasks/${task.id}`, { data: { action: 'return', note: reason } });
    expect(returned.status()).toBe(200);
    await worker.reload();
    await expect(row).toContainText(`最近退回说明：${reason}`);
    const history = row.locator('details');
    await history.locator('summary').click();
    await expect(history.locator('li')).toHaveCount(4);
    await expect(history.getByText(reason, { exact: true })).toBeVisible();
    await expect(history).toContainText(reviewer.member.name);
    await row.getByRole('button', { name: '开始', exact: true }).click();
    await expect(row).toContainText('进行中');
    await expect(history.locator('li')).toHaveCount(5);
    await expect(history.getByText(reason, { exact: true })).toBeVisible();
    await expect(history).toContainText('提交复核');
    await expect(row).toContainText(`最近退回说明：${reason}`);
    await worker.screenshot({ path: info.outputPath('assistant-task-return-history.png'), fullPage: true });
  } finally {
    await assistantContext.close();
    await reviewerContext.close();
  }
});
