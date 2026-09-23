import { test, expect } from '@playwright/test';

const sessions = new Map();

async function login(page, password = 'test-admin-only') {
  if (sessions.has(password)) {
    await page.context().addCookies(sessions.get(password));
  } else {
    await page.goto('/settings/login');
    await page.getByLabel('Mot de passe', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Se connecter' }).click();
    await page.waitForURL(password === 'test-admin-only' ? '**/settings' : /\/$/);
    sessions.set(password, (await page.context().cookies()).filter(cookie => cookie.name === 'odin_admin'));
  }
  await page.goto('/');
  await expect(page.locator('#rowCount')).toContainText('65');
}

test('global search, pagination and stable selection', async ({ page }, testInfo) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await login(page);
  await expect(page.locator('#queriesTable tbody tr')).toHaveCount(50);
  await page.screenshot({ path: testInfo.outputPath('dashboard.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.locator('[data-select]').first().check();
  await expect(page.locator('#selectionBar')).toBeVisible();
  await page.getByRole('button', { name: 'Page suivante', exact: true }).click();
  await expect(page.locator('#queriesTable tbody tr')).toHaveCount(15);
  await expect(page.locator('#selectionCount')).toContainText('1');
  await page.locator('#searchInput').fill('needle0064');
  await expect(page.locator('#queriesTable tbody tr')).toHaveCount(1);
  await expect(page.locator('.sql-id')).toHaveText('sql0064');
  await page.reload();
  await expect(page.locator('#searchInput')).toHaveValue('needle0064');
  await page.locator('#searchInput').fill('no-result-unique');
  await expect(page.locator('.empty-state')).toContainText('Aucune requête');
  expect(errors).toEqual([]);
});

test('detail and export sanitize stored HTML', async ({ page }, testInfo) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await login(page);
  await page.goto('/query/1');
  await expect(page.locator('#analysisMarkdownDisplay h2')).toHaveText('Diagnostic');
  const sqlSection = page.locator('section.card').first();
  await expect(sqlSection).toHaveCSS('border-radius', '10px');
  await expect(sqlSection).toHaveCSS('padding-left', '24px');
  await expect(sqlSection).not.toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  await expect(page.locator('.score-inline')).toContainText('0');
  await expect(page.locator('#chatHistory')).toContainText('Diagnostic');
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  await expect(page.locator('[onerror]')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('detail.png'), fullPage: true });
  await page.goto('/export/pdf?ids=1');
  await expect(page.locator('.ai-content h2')).toHaveText('Diagnostic');
  await expect(page.locator('[onerror]')).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  expect(errors).toEqual([]);
});

test('recent performance compares periods and recovers from errors', async ({ page }, testInfo) => {
  await login(page);
  await page.goto('/query/1');
  await expect(page.locator('#performanceStatus')).toContainText('Récente : mesures disponibles');
  const elapsed = page.locator('#performanceRows tr').filter({ hasText: 'Temps moyen (ms)' });
  await expect(elapsed.locator('td').nth(0)).toHaveText('50');
  await expect(elapsed.locator('td').nth(1)).toHaveText('100');
  await expect(elapsed.locator('td').nth(2)).toHaveText('+100 %');
  const response = page.waitForResponse(response => response.url().endsWith('/performance?minutes=60'));
  await page.getByLabel('Période', { exact: true }).selectOption('60');
  expect((await (await response).json()).minutes).toBe(60);
  await expect(page.locator('#performanceSection')).toHaveAttribute('aria-busy', 'false');
  await page.screenshot({ path: testInfo.outputPath('performance.png'), fullPage: true });
  await page.route('**/performance?*', route => route.fulfill({ status: 503, json: { detail: 'Unavailable' } }));
  await page.getByRole('button', { name: 'Actualiser les mesures' }).click();
  await expect(page.locator('#performanceStatus')).toContainText('Mesures indisponibles');
  await expect(page.locator('#performanceRows tr')).toHaveCount(0);
  await page.unroute('**/performance?*');
  await page.getByRole('button', { name: 'Actualiser les mesures' }).click();
  await expect(page.locator('#performanceStatus')).toContainText('mesures disponibles');
  await page.goto('/query/2');
  await expect(page.locator('#performanceStatus')).toContainText('mesures insuffisantes');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test('captured plan comparison escapes text and handles missing history', async ({ page }, testInfo) => {
  await login(page, 'test-viewer-only');
  await page.goto('/query/1');
  await page.locator('#planComparison summary').click();
  await expect(page.locator('#planDiff')).toContainText('-| 0 | TABLE ACCESS FULL ORDERS |');
  await expect(page.locator('#planDiff')).toContainText('+| 0 | INDEX RANGE SCAN ORDERS_ID |');
  await expect(page.locator('#planDiff')).toContainText('<img');
  await expect(page.locator('#planDiff img')).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('plan-comparison.png'), fullPage: true });
  const afterId = await page.locator('#afterPlan').inputValue();
  await page.locator('#beforePlan').selectOption(afterId);
  await expect(page.locator('#planComparisonStatus')).toContainText('Aucune différence');
  await expect(page.locator('#planDiff')).toBeHidden();
  await page.goto('/query/2');
  await page.locator('#planComparison summary').click();
  await expect(page.locator('#planComparisonStatus')).toContainText('Deux captures');
  await expect(page.locator('#beforePlan')).toBeDisabled();
});

test('public reader can reach administrator login', async ({ page }, testInfo) => {
  await page.goto('/');
  await expect(page.locator('#rowCount')).toContainText('65');
  await expect(page.getByRole('link', { name: 'Administration', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Déconnexion', exact: true })).toHaveCount(0);
  await expect(page.locator('#analyzeAll')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('public-dashboard.png'), fullPage: true });
  const response = await page.request.post('/api/analyze/1');
  expect(response.status()).toBe(403);
  await page.getByRole('link', { name: 'Administration', exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/login$/);
  await page.getByLabel('Mot de passe', { exact: true }).fill('test-admin-only');
  await page.getByRole('button', { name: 'Se connecter' }).click();
  await expect(page).toHaveURL(/\/settings$/);
});

test('administration has a visible heading and navigation', async ({ page }, testInfo) => {
  await login(page);
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: 'Administration', exact: true })).toBeVisible();
  await expect(page.locator('.tabs-bar button')).toHaveCount(5);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('administration.png'), fullPage: true });
  await page.locator('.tabs-bar button').nth(1).click();
  await expect(page.locator('#tab-analyse')).toBeVisible();
  await expect(page.locator('#tab-oracle')).toBeHidden();
  await expect(page.locator('#tab-analyse .setting-row')).toHaveCount(5);
  await expect(page.locator('#thinking_budget, #roundsVal, [onclick*="analyzer_ai_mode"]')).toHaveCount(0);
  await expect(page.locator('#ai_model')).toBeVisible();
  await expect(page.locator('#ai_max_tokens')).toBeVisible();
  await expect(page.locator('#plan_truncate')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('native-settings.png'), fullPage: true });
  await page.locator('.tabs-bar button').nth(3).click();
  await expect(page.locator('#promptModeSelect')).toHaveCount(0);
  await expect(page.locator('#systemPromptEditor')).not.toHaveValue('');
  await expect(page.locator('#promptModeLabel')).toContainText('natif');
  await page.locator('.tabs-bar button').first().click();
  await expect(page.locator('#tab-oracle')).toBeVisible();
});

test('deselecting all Oracle tools persists after reload', async ({ page }) => {
  await login(page);
  const original = await (await page.request.get('/api/settings')).json();
  const saveSelection = async (action, expected) => {
    const response = page.waitForResponse(response => response.url().endsWith('/api/settings') &&
      response.request().method() === 'POST');
    await action();
    expect((await response).ok()).toBeTruthy();
    const settings = await (await page.request.get('/api/settings')).json();
    expect(settings.tools_enabled).toBe(expected);
  };
  try {
    expect((await page.request.post('/api/settings', {
      data: { tools_enabled: '', gather_stats_enabled: false },
    })).ok()).toBeTruthy();
    await page.goto('/settings');
    await page.locator('.tabs-bar button').nth(2).click();
    await expect(page.locator('#toolsGrid input:checked')).toHaveCount(14);
    await saveSelection(() => page.getByRole('button', { name: 'Tout désélectionner' }).click(), 'none');
    await page.reload();
    await page.locator('.tabs-bar button').nth(2).click();
    await expect(page.locator('#toolsGrid input:checked')).toHaveCount(0);
    await expect(page.locator('#toolsGrid .tool-card.active')).toHaveCount(0);
    await saveSelection(() => page.locator('#tool-describe_table').check(), 'describe_table');
    await saveSelection(() => page.locator('#tool-describe_table').uncheck(), 'none');
    await page.reload();
    await page.locator('.tabs-bar button').nth(2).click();
    await expect(page.locator('#toolsGrid input:checked')).toHaveCount(0);
  } finally {
    expect((await page.request.post('/api/settings', { data: {
      tools_enabled: original.tools_enabled, gather_stats_enabled: original.gather_stats_enabled,
    } })).ok()).toBeTruthy();
  }
});

test('account models refresh preserves selection and cached catalog', async ({ page }, testInfo) => {
  await login(page);
  let catalog = { provider: 'github-copilot', models: [], updated_at: null };
  let fail = false;
  let saves = 0;
  await page.route('**/api/models', route => route.fulfill({ json: catalog }));
  await page.route('**/api/models/refresh', route => {
    if (fail) return route.fulfill({ status: 502, json: { detail: 'Copilot indisponible' } });
    catalog = { provider: 'github-copilot', updated_at: 1700000000, models: [
      { id: 'new-model', name: 'Nouveau modèle', vendor: 'Vendor' },
      { id: 'other-model', name: '<img src=x onerror="window.injected=true">', vendor: 'Vendor' },
    ] };
    return route.fulfill({ json: catalog });
  });
  await page.route('**/api/settings', route => {
    if (route.request().method() === 'POST') saves++;
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/settings');
  const selected = await page.locator('#ai_model').inputValue();
  await page.locator('.tabs-bar button').nth(4).click();
  await page.getByRole('button', { name: 'Actualiser les modèles Copilot' }).click();
  await expect(page.locator('#accountModels tr')).toHaveCount(2);
  await expect(page.locator('#ai_model')).toHaveValue(selected);
  await expect(page.locator('#tab-models [data-models-status]')).toContainText('choix conservé');
  await expect(page.locator('#accountModels img')).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  expect(saves).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('models.png'), fullPage: true });
  fail = true;
  await page.getByRole('button', { name: 'Actualiser les modèles Copilot' }).click();
  await expect(page.locator('#tab-models [data-models-status]')).toContainText('Copilot indisponible');
  await expect(page.locator('#accountModels tr')).toHaveCount(2);
  await expect(page.locator('#ai_model')).toHaveValue(selected);
  await page.reload();
  await page.locator('.tabs-bar button').nth(1).click();
  await expect(page.locator('#tab-analyse [data-models-status]')).toContainText('2 modèles');
  await expect(page.locator('#ai_model option[value="new-model"]')).toHaveCount(1);
  await page.locator('#ai_model').selectOption('new-model');
  await expect.poll(() => saves).toBe(1);
});

test('Copilot token testing saving deletion and env priority', async ({ page }, testInfo) => {
  await login(page);
  let source = null;
  let saves = 0;
  let refreshes = 0;
  let catalog = { provider: 'github-copilot', models: [], updated_at: null };
  await page.route('**/api/settings/github_token', route => {
    const method = route.request().method();
    if (method === 'POST') {
      expect(route.request().postDataJSON().token).toBe('github_pat_browser_fixture');
      source = 'file';
      saves++;
    }
    if (method === 'DELETE') {
      source = null;
      catalog = { ...catalog, models: [], updated_at: null };
    }
    return route.fulfill({ json: { ok: true, configured: source !== null, source } });
  });
  await page.route('**/api/settings/github_token/test', route => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/models', route => route.fulfill({ json: catalog }));
  await page.route('**/api/models/refresh', route => {
    refreshes++;
    catalog = { ...catalog, updated_at: 1700000000, models: [{ id: 'fixture', name: 'Fixture', vendor: '' }] };
    return route.fulfill({ json: catalog });
  });
  await page.goto('/settings');
  await page.locator('.tabs-bar button').nth(4).click();
  await page.getByText('Comment créer ce jeton ?', { exact: true }).click();
  await expect(page.locator('.help-steps')).toContainText('Copilot Requests');
  const input = page.getByLabel('Jeton GitHub', { exact: true });
  await expect(input).toHaveAttribute('type', 'password');
  await input.fill('github_pat_browser_fixture');
  await page.locator('[data-token-test]').click();
  await expect(page.locator('[data-token-status]')).toContainText('Test sans enregistrement');
  expect(saves).toBe(0);
  await page.locator('[data-token-save]').click();
  await expect(input).toHaveValue('');
  await expect(page.locator('#accountModels')).toContainText('Fixture');
  expect(saves).toBe(1);
  await page.locator('[data-token-delete]').click();
  await expect(page.locator('[data-token-status]')).toContainText('Aucun jeton configuré');
  await expect(page.locator('#accountModels')).toContainText('Aucun catalogue enregistré');
  expect(refreshes).toBe(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('copilot-token.png'), fullPage: true });
  source = 'env';
  await page.reload();
  await page.locator('.tabs-bar button').nth(4).click();
  await expect(input).toBeDisabled();
  await expect(page.locator('[data-token-save]')).toBeDisabled();
  await expect(page.locator('[data-token-delete]')).toBeDisabled();
  await expect(page.locator('[data-token-test]')).toBeEnabled();
});

test('service health handles stale signals and request failures', async ({ page }) => {
  await login(page);
  let unavailable = false;
  const health = {
    services: {
      collector: { state: 'stale', updated_at: 100, last_success: 90 },
      analyzer: { state: 'manual', updated_at: 100, last_success: null },
    },
    queue_size: 2, failed_queries: 1, last_analysis: null,
    collector_enabled: true, automatic_analysis: false,
  };
  await page.route('**/api/health', route => route.fulfill(unavailable
    ? { status: 503, json: { detail: 'Unavailable' } }
    : { json: health }));
  await page.goto('/settings');
  await expect(page.locator('#collectorState')).toHaveText('Sans signal récent');
  await expect(page.locator('#analyzerState')).toHaveText('Mode manuel');
  await expect(page.locator('#analysisQueue')).toHaveText('2');
  await expect(page.locator('#analysisFailures')).toHaveText('1');
  await page.locator('#oracle_user').fill('unsaved-user');
  unavailable = true;
  await page.getByRole('button', { name: 'Actualiser les services' }).click();
  await expect(page.locator('#healthError')).toBeVisible();
  await expect(page.locator('#collectorState')).toHaveText('Non disponible');
  unavailable = false;
  health.services.collector.state = 'collecting';
  await page.getByRole('button', { name: 'Actualiser les services' }).click();
  await expect(page.locator('#collectorState')).toHaveText('Collecte en cours');
  await expect(page.locator('#healthError')).toBeHidden();
  await expect(page.locator('#oracle_user')).toHaveValue('unsaved-user');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test('Oracle test does not imply saved credentials', async ({ page }) => {
  await login(page);
  await page.goto('/settings');
  await page.locator('#oracle_password').fill('fixture-password');
  await page.route('**/api/settings/test_oracle', route => route.fulfill({
    json: { ok: true, version: '19', user: 'fixture', dsn: 'fixture' },
  }));
  await page.getByRole('button', { name: 'Tester', exact: false }).click();
  await expect(page.locator('#oracleTestResult')).toContainText('Modifications non enregistrées');
  await page.route('**/api/settings', route => route.fulfill({
    status: 403, json: { detail: 'Authentification requise' },
  }));
  await page.locator('#btnSaveOracle').click();
  await expect(page.locator('#oracleSaveStatus')).toContainText('Échec');
  await expect(page.locator('#btnSaveOracle')).toBeVisible();
  await page.route('**/api/settings', route => route.fulfill({ json: { ok: true } }));
  await page.locator('#btnSaveOracle').click();
  await expect(page.locator('#oracleSaveStatus')).toContainText('Enregistré');
  await expect(page.locator('#btnSaveOracle')).toBeHidden();
});

test('viewer cannot trigger privileged operations', async ({ page }) => {
  await login(page, 'test-viewer-only');
  await expect(page.locator('#analyzeAll')).toHaveCount(0);
  await page.goto('/query/1');
  await expect(page.locator('#btnAnalyze')).toBeHidden();
  await expect(page.locator('#btnLoadBinds')).toBeHidden();
  await expect(page.locator('#bindsContent')).toContainText('historique local');
  const response = await page.request.post('/api/analyze/1');
  expect(response.status()).toBe(403);
});