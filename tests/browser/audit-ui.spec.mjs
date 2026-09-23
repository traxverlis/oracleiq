import { test, expect } from '@playwright/test';

let adminCookies;
async function login(page) {
  if (adminCookies) {
    await page.context().addCookies(adminCookies);
    await page.goto('/settings');
    return;
  }
  await page.goto('/settings/login');
  await page.getByLabel('Mot de passe', { exact: true }).fill('test-admin-only');
  await page.getByRole('button', { name: 'Se connecter' }).click();
  await page.waitForURL('**/settings');
  adminCookies = (await page.context().cookies()).filter(cookie => cookie.name === 'odin_admin');
}

for (const failure of [403, 500, 'network']) {
  test(`all settings save paths reject ${failure} and roll back controls`, async ({ page }) => {
    await login(page);
    await page.route('**/api/settings', route => failure === 'network' ? route.abort('failed') :
      route.fulfill({ status: failure, json: { detail: 'Écriture refusée' } }));
    await page.getByRole('button', { name: 'Analyse IA', exact: false }).click();
    const tokens = page.locator('#ai_max_tokens');
    const original = await tokens.inputValue();
    await tokens.selectOption(original === '2048' ? '4096' : '2048');
    await expect(page.locator('#saveMsg')).toContainText('Échec');
    await expect(tokens).toHaveValue(original);
    await page.getByRole('button', { name: 'Connexion', exact: false }).click();
    await page.locator('#oracle_dsn').fill('fixture.invalid:1521/TEST');
    await page.locator('#btnSaveOracle').click();
    await expect(page.locator('#oracleSaveStatus')).toContainText('Échec');
    await expect(page.locator('#btnSaveOracle')).toBeVisible();
    await expect(page.locator('#oracle_dsn')).toHaveValue('fixture.invalid:1521/TEST');
    await page.getByRole('button', { name: 'Outils IA', exact: false }).click();
    const tool = page.locator('#tool-describe_table');
    const wasChecked = await tool.isChecked();
    await tool.click();
    await expect(page.locator('#saveMsg')).toContainText('Échec de l’enregistrement des outils');
    await expect(tool).toBeChecked({ checked: wasChecked });
    const gather = page.locator('#tool-gather_table_stats');
    const gatherChecked = await gather.isChecked();
    await gather.click();
    await expect(page.locator('#saveMsg')).toContainText('Échec de l’enregistrement des statistiques');
    await expect(gather).toBeChecked({ checked: gatherChecked });
    await page.getByRole('button', { name: 'Prompt', exact: false }).click();
    await page.locator('#systemPromptEditor').fill('Brouillon à conserver');
    await page.locator('#tab-prompt').getByRole('button', { name: 'Enregistrer', exact: false }).click();
    await expect(page.locator('#promptSaveResult')).toContainText('Non enregistré');
    await expect(page.locator('#systemPromptEditor')).toHaveValue('Brouillon à conserver');
    await expect(page.locator('#promptHint')).toContainText('Non enregistré');
  });
}

test('settings writes are serialized and prompt success updates saved state', async ({ page }) => {
  await login(page);
  await expect(page.locator('#systemPromptEditor')).toHaveAttribute('maxlength', '20000');
  await expect(page.locator('#plan_truncate option[value="16000"]')).toHaveCount(1);
  expect(await page.locator('#plan_truncate').evaluate(select => Math.max(...[...select.options].map(option => Number(option.value))))).toBe(16000);
  expect(await page.locator('#ai_max_tokens').evaluate(select => Math.max(...[...select.options].map(option => Number(option.value))))).toBe(32000);
  const requests = [];
  let releaseFirst;
  const gate = new Promise(resolve => { releaseFirst = resolve; });
  await page.route('**/api/settings', async route => {
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) await gate;
    await route.fulfill({ json: { ok: true } });
  });
  await page.evaluate(() => { save('ai_max_tokens', 2048); save('ai_max_tokens', 4096); });
  await expect.poll(() => requests.length).toBe(1);
  await expect(page.locator('#ai_max_tokens')).toBeDisabled();
  releaseFirst();
  await expect.poll(() => requests.length).toBe(2);
  await expect(page.locator('#ai_max_tokens')).toHaveValue('4096');
  await expect(page.locator('#ai_max_tokens')).toBeEnabled();
  await page.getByRole('button', { name: 'Prompt', exact: false }).click();
  await page.locator('#systemPromptEditor').fill('Instructions enregistrées');
  await page.locator('#tab-prompt').getByRole('button', { name: 'Enregistrer', exact: false }).click();
  await expect(page.locator('#promptHint')).toContainText('Prompt personnalisé actif');
});

test('chat keyboard and button share a lock and preserve failed text', async ({ page }) => {
  await login(page);
  let calls = 0;
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  await page.route('**/api/queries/1/chat', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    calls++;
    await pending;
    return route.fulfill({ status: 500, json: { detail: 'IA indisponible' } });
  });
  await page.goto('/query/1');
  const input = page.locator('#chatInput');
  await input.fill('  Question conservée  ');
  await input.press('Enter');
  await expect.poll(() => calls).toBe(1);
  await input.press('Enter');
  await page.evaluate(() => sendChat());
  await expect(page.locator('#btnChatSend')).toBeDisabled();
  expect(calls).toBe(1);
  finish();
  await expect(page.locator('#chatStatus')).toContainText('Échec');
  await expect(input).toHaveValue('  Question conservée  ');
  await expect(page.locator('#btnChatSend')).toBeEnabled();
  await expect(page.locator('.chat-user')).toHaveCount(0);
});

test('analysis navigation ignores stale responses and rebuilt controls stay keyboard accessible', async ({ page }) => {
  await login(page);
  await page.goto('/query/1');
  await expect(page.locator('#analysisMarkdownDisplay')).toBeVisible();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let oldStarted = false;
  let oldCompleted = false;
  await page.route('**/api/analyses/901', async route => {
    oldStarted = true;
    await gate;
    await route.fulfill({ json: { raw_response: 'Ancienne réponse', summary: 'Ancienne', perf_score: 10 } }).catch(() => {});
    oldCompleted = true;
  });
  await page.route('**/api/analyses/902', route => route.fulfill({ json: {
    ok: true, raw_response: 'Réponse sélectionnée', summary: 'Sélectionnée', perf_score: 90,
  } }));
  await page.evaluate(() => {
    _analysisHistory = [{ id: 901, analyzed_at: '2026-01-01 10:00:00' }, { id: 902, analyzed_at: '2026-01-02 10:00:00' }];
    _rebuildPills();
    showAnalysis(0);
  });
  await expect.poll(() => oldStarted).toBeTruthy();
  await page.locator('.pill-select').nth(1).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#analysisMarkdownDisplay')).toHaveText('Réponse sélectionnée');
  release();
  await expect.poll(() => oldCompleted).toBeTruthy();
  await expect(page.locator('#analysisMarkdownDisplay')).toHaveText('Réponse sélectionnée');
  page.on('dialog', dialog => dialog.accept());
  await page.locator('.pill-del').nth(1).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.pill-select')).toHaveCount(1);
  await expect(page.locator('.pill-select')).toBeFocused();
});

test('replay renders columns values errors notes and plans as literal text', async ({ page }) => {
  await login(page);
  const literal = '<img src=x onerror="window.injected=true"><b>literal</b>';
  await page.route('**/api/queries/1/replay', route => route.fulfill({ json: {
    ok: true, elapsed_ms: 1, rows_returned: 1, note: literal, exec_error: literal,
    columns: [literal], sample_rows: [{ [literal]: literal }], plan_text: literal,
  } }));
  await page.goto('/query/1');
  await page.locator('#btnReplay').click();
  await expect(page.locator('#replayContent th')).toHaveText(literal);
  await expect(page.locator('#replayContent td')).toHaveText(literal);
  await expect(page.locator('#replayContent .error-text')).toContainText(literal);
  await expect(page.locator('#replayContent pre')).toHaveText(literal);
  await expect(page.locator('#replayContent img, #replayContent b')).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
});

test('dashboard polling preserves nodes and focus for checkboxes and links', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#queriesTable tbody tr')).toHaveCount(50);
  await page.locator('[data-select]').first().focus();
  await page.evaluate(() => { window.focusedCheckbox = document.activeElement; });
  await page.evaluate(() => loadRows());
  expect(await page.evaluate(() => document.activeElement === window.focusedCheckbox)).toBeTruthy();
  await page.locator('[data-select]').first().check();
  await page.locator('.sql-id').first().focus();
  await page.route('**/api/query-page?*', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.items[0].summary = 'Résumé actualisé';
    await route.fulfill({ json: data });
  });
  await page.evaluate(() => loadRows());
  await expect(page.locator('.sql-id').first()).toBeFocused();
  await expect(page.locator('[data-select]').first()).toBeChecked();
  await page.locator('[data-select]').first().focus();
  await page.evaluate(() => { items[0].summary = 'Autre résumé'; renderRows(); });
  await expect(page.locator('[data-select]').first()).toBeFocused();
});

test('long markdown tables binds and touch keyboard copy fit the viewport', async ({ page }, testInfo) => {
  await login(page);
  await page.route('**/api/queries/1/binds', route => route.fulfill({ json: {
    captured: true, note: 'Capture locale', binds: [{
      bind_name: ':very_long_bind_name'.repeat(20), datatype: 'VARCHAR2'.repeat(30),
      value: 'long-value'.repeat(100), last_captured: '2026-01-01 10:00:00',
    }],
  } }));
  await page.goto('/query/1');
  await expect(page.locator('#analysisMarkdownDisplay')).toBeVisible();
  await page.evaluate(() => {
    const markdown = '| ' + 'Column | '.repeat(14) + '\n|' + '---|'.repeat(14) + '\n|' + 'long-value '.repeat(12) + '|' + 'value|'.repeat(13);
    renderMarkdown(markdown, 'analysisMarkdownDisplay');
    appendMessage('assistant', markdown + '\n\n```sql\nselect 1 from dual;\n```', false);
  });
  await expect(page.locator('#bindsContent')).toContainText('long-value');
  await expect(page.locator('#analysisMarkdownDisplay .content-table-wrap')).toBeVisible();
  const copy = page.locator('#chatHistory .copy-code-btn').last();
  await expect(copy).toHaveCSS('opacity', '1');
  await copy.focus();
  await expect(copy).toBeFocused();
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {
    configurable: true, value: { writeText: async text => { window.copiedCode = text; } },
  }));
  await page.keyboard.press('Enter');
  await expect.poll(() => page.evaluate(() => window.copiedCode)).toContain('select 1 from dual;');
  if (testInfo.project.name === 'mobile') {
    await copy.tap();
    await expect(copy).toHaveAttribute('aria-label', 'Code copié');
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(await page.locator('.bind-item').evaluate(el => el.scrollWidth <= el.clientWidth)).toBeTruthy();
});

function alertItem(id, acknowledged = false) {
  return {
    id, kind: 'regression', severity: 'warning', title: `<b>Alerte ${id}</b>`,
    message: '<img src=x onerror="window.injected=true">', query_id: 1,
    created_at: '2026-01-01 10:00:00', acknowledged_at: acknowledged ? '2026-01-02 10:00:00' : null,
    resolved_at: null,
  };
}

test('alerts history pagination acknowledgement failure and resolution are distinct', async ({ page }) => {
  await login(page);
  let acknowledged = false;
  let failAck = true;
  await page.route('**/api/alerts?*', route => {
    const url = new URL(route.request().url());
    const history = url.searchParams.get('include_acknowledged') === 'true';
    const offset = Number(url.searchParams.get('offset'));
    const items = history ? Array.from({ length: 51 }, (_, i) => alertItem(i + 1, i > 0 || acknowledged)) :
      acknowledged ? [] : [alertItem(1)];
    return route.fulfill({ json: { items: items.slice(offset, offset + 50), total: items.length, unacknowledged: acknowledged ? 0 : 1 } });
  });
  await page.route('**/api/alerts/1/acknowledge', route => {
    if (failAck) return route.fulfill({ status: 403, json: { detail: 'Session expirée' } });
    acknowledged = true;
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/');
  await expect(page.locator('#alertsList')).toContainText('<b>Alerte 1</b>');
  await expect(page.locator('#alertsList b, #alertsList img')).toHaveCount(0);
  await expect(page.locator('#alertsList')).toContainText('Suspicion');
  await page.getByRole('button', { name: 'Historique des alertes', exact: true }).click();
  await expect(page.locator('#alertsList > li')).toHaveCount(50);
  await page.getByRole('button', { name: 'Alertes suivantes', exact: true }).click();
  await expect(page.locator('#alertsList > li')).toHaveCount(1);
  await expect(page.locator('#alertsList')).toContainText('Alerte 51');
  await page.getByRole('button', { name: 'Alertes précédentes', exact: true }).click();
  await page.getByRole('button', { name: 'Acquitter : <b>Alerte 1</b>', exact: true }).click();
  await expect(page.locator('#alertsStatus')).toContainText('Échec de l’acquittement');
  failAck = false;
  await page.getByRole('button', { name: 'Acquitter : <b>Alerte 1</b>', exact: true }).click();
  const first = page.locator('#alertsList > li').first();
  await expect(first.locator('.alert-ack')).toContainText('Acquittée');
  await expect(first.locator('.alert-resolution')).toHaveText('Non résolue');
  await expect(first.getByRole('button')).toHaveCount(0);
});

test('reader alerts are read-only and empty/error states are explicit', async ({ page }) => {
  let state = 'item';
  await page.route('**/api/alerts?*', route => state === 'error' ?
    route.fulfill({ status: 500, json: { detail: 'Service indisponible' } }) :
    route.fulfill({ json: { items: state === 'item' ? [alertItem(1)] : [], total: state === 'item' ? 1 : 0, unacknowledged: state === 'item' ? 1 : 0 } }));
  await page.goto('/');
  await expect(page.locator('#alertsList > li')).toHaveCount(1);
  await expect(page.getByRole('button', { name: /^Acquitter/ })).toHaveCount(0);
  state = 'empty';
  await page.locator('#refreshAlerts').click();
  await expect(page.locator('#alertsStatus')).toContainText('Aucune alerte');
  state = 'error';
  await page.locator('#refreshAlerts').click();
  await expect(page.locator('#alertsStatus')).toContainText('Alertes indisponibles');
  await page.goto('/query/1');
  await expect(page.locator('#loadAiPreview')).toHaveCount(0);
});

test('privacy defaults to masked, opt-in rolls back, preview is local and literal', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Analyse IA', exact: false }).click();
  await expect(page.getByLabel('Envoyer les valeurs brutes à GitHub Copilot', { exact: true })).not.toBeChecked();
  await expect(page.locator('#privacyWarning')).toContainText('Attention');
  await page.route('**/api/settings', route => route.fulfill({ status: 403, json: { detail: 'Refusé' } }));
  await page.locator('#ai_send_raw_values').click();
  await expect(page.locator('#saveMsg')).toContainText('Échec');
  await expect(page.locator('#ai_send_raw_values')).not.toBeChecked();
  let raw = false;
  let fail = false;
  let previewCalls = 0;
  await page.route('**/api/queries/1/ai-preview', route => {
    previewCalls++;
    return fail ? route.fulfill({ status: 500, json: { detail: 'Aperçu en erreur' } }) :
      route.fulfill({ json: {
        provider: raw ? 'copilot' : 'github-copilot', model: 'fixture-model', send_raw_values: raw, masking_applied: !raw,
        prompt: `<b>literal</b>\n${raw ? 'example' : '[MASKED]'}`,
        ...(raw ? {} : { system: 'Instructions <b>système</b>' }),
        ...(raw ? {} : { tools: [{ name: 'fixture_tool', description: '<img src=x> schéma' }] }),
        limits: { input_chars: 12000 }, warnings: ['<img src=x> Outils futurs exclus'],
      } });
  });
  await page.goto('/query/1');
  expect(previewCalls).toBe(0);
  await page.locator('#loadAiPreview').click();
  await expect(page.locator('#aiPreviewStatus')).toContainText('Valeurs masquées');
  await expect(page.locator('#aiPreviewContent')).toContainText('[MASKED]');
  await expect(page.locator('#aiPreviewContent')).toContainText('Instructions <b>système</b>');
  await expect(page.locator('#aiPreviewContent')).toContainText('Schémas des outils proposés (pas les résultats)');
  await expect(page.locator('#aiPreviewContent')).toContainText('fixture_tool');
  await expect(page.locator('#aiPreviewContent img')).toHaveCount(0);
  await expect(page.locator('#aiPreviewContent b')).toHaveCount(0);
  await expect(page.locator('#aiPreviewMetadata')).toContainText('fixture-model');
  await expect(page.locator('#aiPreviewMetadata')).toContainText('<img src=x>');
  await expect(page.locator('#aiPreviewMetadata img')).toHaveCount(0);
  raw = true;
  await page.locator('#loadAiPreview').click();
  await expect(page.locator('#aiPreviewStatus')).toContainText('Valeurs brutes');
  await expect(page.locator('#aiPreviewStatus')).toContainText('futurs appels d’outils');
  await expect(page.locator('#aiPreviewContent')).toContainText('example');
  await expect(page.locator('#aiPreviewContent')).not.toContainText('Instructions système');
  fail = true;
  await page.locator('#loadAiPreview').click();
  await expect(page.locator('#aiPreviewStatus')).toContainText('Aperçu indisponible');
  await expect(page.locator('#aiPreviewContent')).toBeHidden();
  await expect(page.locator('#aiPreviewContent')).toBeEmpty();
  await expect(page.locator('#aiPreviewMetadata')).toBeHidden();
});

test('explicit reanalysis posts before SSE while reconnect never queues a new job', async ({ page }) => {
  await login(page);
  await page.addInitScript(() => {
    window.eventStreams = [];
    window.EventSource = class {
      constructor(url) { this.url = url; window.eventStreams.push(this); }
      close() {}
    };
  });
  let calls = 0;
  let fail = true;
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/analyze/1', async route => {
    calls++;
    if (fail) return route.fulfill({ status: 503, json: { detail: 'File indisponible' } });
    await gate;
    return route.fulfill({ json: { ok: true, queued: true } });
  });
  await page.goto('/query/1');
  await page.locator('#btnAnalyze').click();
  await expect(page.locator('#analyzeStatus')).toContainText('Échec du lancement');
  await expect(page.locator('#btnAnalyze')).toBeEnabled();
  expect(await page.evaluate(() => window.eventStreams.length)).toBe(0);
  fail = false;
  await page.evaluate(() => { reanalyzeNative(false); });
  await expect.poll(() => calls).toBe(2);
  await expect(page.locator('#btnAnalyze')).toBeDisabled();
  expect(await page.evaluate(() => window.eventStreams.length)).toBe(0);
  release();
  await expect.poll(() => page.evaluate(() => window.eventStreams.length)).toBe(1);
  expect(await page.evaluate(() => window.eventStreams[0].url)).toBe('/api/analyze/1/stream');
  await page.evaluate(() => {
    window.eventStreams[0].onmessage({ data: JSON.stringify({ type: 'error', message: 'Fixture arrêtée' }) });
    reanalyzeNative(true);
  });
  expect(await page.evaluate(() => window.eventStreams.length)).toBe(2);
  expect(calls).toBe(2);
});

test('analysis provenance distinguishes current old and unknown capture on history navigation', async ({ page }) => {
  await login(page);
  await page.goto('/query/1');
  await expect(page.locator('#analysisMarkdownDisplay')).toBeVisible();
  const currentPlan = Number(await page.locator('body').getAttribute('data-plan-id'));
  expect(currentPlan).toBeGreaterThan(0);
  const analyses = {
    991: { plan_id: currentPlan, raw_response: 'Diagnostic actuel', perf_score: 90 },
    992: { plan_id: currentPlan + 100, raw_response: 'Diagnostic ancien', perf_score: 80 },
    993: { raw_response: 'Diagnostic sans provenance', perf_score: 70 },
  };
  await page.route(/\/api\/analyses\/99[123]$/, route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1);
    return route.fulfill({ json: analyses[id] });
  });
  await page.evaluate(current => {
    _analysisHistory = [
      { id: 992, plan_id: current + 100, analyzed_at: '2026-01-03 10:00:00' },
      { id: 991, plan_id: current, analyzed_at: '2026-01-02 10:00:00' },
      { id: 993, analyzed_at: '2026-01-01 10:00:00' },
    ];
    _rebuildPills();
    showAnalysis(0);
  }, currentPlan);
  await expect(page.locator('#analysisMarkdownDisplay')).toHaveText('Diagnostic ancien');
  await expect(page.locator('#analysisPlanStatus')).toContainText('ancienne capture');
  await expect(page.locator('#analysisPlanStatus')).toContainText('À réactualiser');
  await expect(page.locator('#analysisPlanStatus')).toHaveClass('notice notice-error');
  await page.locator('.pill-select').nth(1).click();
  await expect(page.locator('#analysisPlanStatus')).toHaveText(`Analyse liée à la capture actuelle #${currentPlan}.`);
  await expect(page.locator('#analysisPlanStatus')).not.toHaveClass(/notice-error/);
  await page.locator('.pill-select').nth(2).click();
  await expect(page.locator('#analysisPlanStatus')).toContainText('Provenance de l’analyse non renseignée');
  await expect(page.locator('#analysisPlanStatus')).toContainText('à réactualiser');
});
