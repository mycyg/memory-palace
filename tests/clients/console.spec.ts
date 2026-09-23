import { test, expect } from '../../console/node_modules/@playwright/test/index.mjs';

test.beforeEach(async ({ page }) => {
  await page.goto('/#token=test-console-local');
  await expect(page.getByRole('heading', { name: '概览' })).toBeVisible();
});

test('source to record and task checkpoint remain traceable', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.locator('nav').getByRole('button', { name: '来源' }).click();
  const title = '界面来源 ' + Date.now();
  await page.getByLabel('标题').fill(title);
  await page.getByLabel('内容').fill('A verified local migration receipt.');
  await page.getByRole('button', { name: '保存来源' }).click();
  await expect(page.locator('.list-item').filter({ hasText: title })).toBeVisible();
  await page.locator('.list-item').filter({ hasText: title }).click();
  const detail = page.getByRole('dialog', { name: '详情' });
  await expect(detail).toContainText('A verified local migration receipt.');
  await detail.getByRole('button', { name: /mem_/ }).click();
  await expect(detail).toContainText('来源');
  await detail.getByRole('button', { name: '关闭' }).click();

  await page.locator('nav').getByRole('button', { name: '任务续接' }).click();
  await page.getByLabel('任务或会话 ID').fill('browser-task-' + Date.now());
  await page.getByLabel('已确认进展').fill('Source receipt confirmed.');
  await page.getByLabel('下次入口').fill('Review pending job.');
  await page.getByRole('button', { name: '保存检查点' }).click();
  await expect(page.locator('.record-list')).toContainText('Review pending job.');
  await page.screenshot({ path: 'test-results/continuation.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('generic sections, recall, and mobile layout', async ({ page }) => {
  for (const view of ['记录', '事件族', '召回', '提醒', '作业', '设置']) {
    await page.locator('nav').getByRole('button', { name: view, exact: true }).click();
    await expect(page.getByRole('heading', { name: view, exact: true, level: 1 })).toBeVisible();
  }
  await page.locator('nav').getByRole('button', { name: '召回' }).click();
  await page.getByLabel('召回问题').fill('数据库迁移');
  await page.getByRole('button', { name: '召回', exact: true }).last().click();
  await expect(page.locator('.context-output')).toContainText('迁移');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: 'test-results/mobile.png', fullPage: true });
});

test('explicit reminder can be canceled with its delivery outcome', async ({ page }) => {
  await page.locator('nav').getByRole('button', { name: '提醒' }).click();
  await page.getByLabel('到期时间').fill('2030-09-09T10:00');
  await page.getByRole('button', { name: '安排提醒' }).click();
  await expect(page.locator('.family').filter({ hasText: 'scheduled' }).first()).toBeVisible();
  await page.route('**/v1/reminders/schedule_*', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch();
    await route.fulfill({ response, json: {
      ...await response.json(), reconciliation_required: true,
      deliveries: [{ id: 'delivery-fixture', outcome: 'possibly_sent' }],
    } });
  });
  await page.locator('.family').filter({ hasText: 'scheduled' }).first().getByRole('button', { name: '取消' }).click();
  await expect(page.locator('.family').filter({ hasText: 'canceled' }).first()).toBeVisible();
  await expect(page.getByRole('status')).toContainText('可能已发送；请按投递 ID 核对回执');
});
