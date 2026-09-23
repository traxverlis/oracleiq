'use strict';

const admin = document.body.dataset.admin === 'true';
const selected = new Set();
let analyzing = new Set();
let items = [];
let pages = 1;
let loadVersion = 0;
let searchTimer;
let renderedRows = '';
const params = new URLSearchParams(location.search);
const state = {
  search: params.get('search') || '', schema: params.get('schema') || '',
  critical_only: params.get('critical_only') === 'true', group: params.get('group') === 'true',
  sort: params.get('sort') || 'elapsed', direction: params.get('direction') || 'desc',
  page: Math.max(1, Number(params.get('page')) || 1), page_size: Number(params.get('page_size')) || 50,
  pattern: params.get('pattern') || '', source: params.get('source') || '',
};
const byId = id => document.getElementById(id);
const number = (value, digits = 0) => Number(value ?? 0).toLocaleString('fr-FR', { maximumFractionDigits: digits });

function notify(message, error = false) {
  const notice = byId('notice');
  notice.textContent = message;
  notice.classList.toggle('notice-error', error);
  notice.hidden = !message;
}

function syncControls() {
  byId('searchInput').value = state.search;
  byId('criticalOnly').checked = state.critical_only;
  byId('groupByPattern').checked = state.group;
  byId('sortSelect').value = state.sort;
  byId('pageSize').value = state.page_size;
}

function syncSelection() {
  byId('selectionBar').hidden = selected.size === 0;
  byId('selectionCount').textContent = `${selected.size} sélectionnée(s)`;
  const selectable = items.filter(item => item.variant_count === 1);
  byId('selectAll').checked = selectable.length > 0 && selectable.every(item => selected.has(item.id));
  byId('selectAll').indeterminate = selectable.some(item => selected.has(item.id)) && !byId('selectAll').checked;
}

function renderRows() {
  const body = document.querySelector('#queriesTable tbody');
  const signature = JSON.stringify([items, [...selected], [...analyzing]]);
  if (signature === renderedRows) return;
  renderedRows = signature;
  const active = document.activeElement;
  const focusedRow = active?.closest('#queriesTable tbody tr')?.dataset.id;
  const focusedIndex = focusedRow ? [...active.closest('tr').querySelectorAll('input, a, button')].indexOf(active) : -1;
  body.replaceChildren();
  for (const item of items) {
    const row = document.createElement('tr');
    const severity = ['critical', 'warning', 'ok'].includes(item.severity) ? item.severity : 'pending';
    const grouped = item.variant_count > 1;
    row.dataset.id = item.id;
    row.className = `row-${severity}`;
    const labels = { critical: 'Critique', warning: 'Alerte', ok: 'OK', pending: 'En attente' };
    const score = item.perf_score;
    const scoreClass = score >= 80 ? 'ok' : score >= 50 ? 'warning' : 'critical';
    row.innerHTML = `<td><input type="checkbox" data-select="${item.id}" aria-label="Sélectionner ${escapeHTML(item.sql_id)}" ${selected.has(item.id) ? 'checked' : ''} ${grouped ? 'disabled' : ''}></td>
      <td><span class="badge ${severity}">${labels[severity]}</span></td>
      <td>${score == null ? '—' : `<span class="score-badge score-${scoreClass}">${number(score)}</span>`}</td>
      <td class="sql-cell"><a class="sql-id" href="/query/${item.id}">${escapeHTML(item.sql_id)}</a><code class="sql-preview">${escapeHTML(item.sql_text)}</code><span class="meta-item">${escapeHTML(item.module || '')}</span>${item.plan_change_detected ? '<span class="badge-mini" title="Changement de plan">Plan modifié</span>' : ''}</td>
      <td><span class="schema-tag">${escapeHTML(item.schema_name || '—')}</span></td>
      <td class="num">${number(item.executions)}</td><td class="num">${number(item.elapsed_ms_avg, 1)}</td><td class="num">${number(item.buffer_gets_avg)}</td>
      <td class="summary-cell">${escapeHTML(item.analysis_error || item.summary || '—')}</td>
      <td><div class="row-actions">${grouped ? `<button class="nav-btn" data-action="variants" data-id="${item.id}">${item.variant_count} variantes</button>` : `<a class="nav-btn icon-btn" href="/query/${item.id}" aria-label="Détail ${escapeHTML(item.sql_id)}" title="Détail"><i data-lucide="arrow-up-right"></i></a>${admin ? `<button class="nav-btn icon-btn" data-action="analyze" data-id="${item.id}" title="Analyser" aria-label="Analyser ${escapeHTML(item.sql_id)}" ${analyzing.has(item.id) ? 'disabled' : ''}><i data-lucide="${analyzing.has(item.id) ? 'loader-circle' : 'scan-search'}"></i></button><button class="nav-btn icon-btn" data-action="delete" data-id="${item.id}" title="Supprimer" aria-label="Supprimer ${escapeHTML(item.sql_id)}" ${analyzing.has(item.id) ? 'disabled' : ''}><i data-lucide="trash-2"></i></button>` : ''}`}</div></td>`;
    if (item.analysis_error) row.querySelector('.summary-cell').classList.add('error-text');
    body.appendChild(row);
  }
  if (!items.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 10;
    cell.className = 'empty-state';
    cell.textContent = 'Aucune requête ne correspond aux filtres.';
  }
  syncSelection();
  refreshIcons();
  if (focusedRow) {
    const row = [...body.rows].find(row => row.dataset.id === focusedRow);
    const target = row?.querySelectorAll('input, a, button')[focusedIndex];
    (target && !target.disabled ? target : byId('tableRegion')).focus({ preventScroll: true });
  }
}

async function loadRows() {
  const version = ++loadVersion;
  byId('tableRegion').setAttribute('aria-busy', 'true');
  try {
    const result = await requestJSON(`/api/query-page?${new URLSearchParams(state)}`);
    if (version !== loadVersion) return;
    items = result.items;
    pages = result.pages;
    state.page = result.page;
    history.replaceState(null, '', `/?${new URLSearchParams(state)}`);
    const schemaSelect = byId('schemaFilter');
    if (JSON.stringify([...schemaSelect.options].slice(1).map(option => option.value)) !== JSON.stringify(result.schemas)) {
      schemaSelect.replaceChildren(new Option('Tous les schémas', ''), ...result.schemas.map(schema => new Option(schema, schema)));
    }
    schemaSelect.value = state.schema;
    byId('rowCount').textContent = `${number(result.total)} ${state.group ? 'patterns' : 'requêtes'}`;
    byId('pageLabel').textContent = `${state.page} / ${pages}`;
    byId('previousPage').disabled = state.page <= 1;
    byId('nextPage').disabled = state.page >= pages;
    document.querySelectorAll('[data-sort]').forEach(button => button.closest('th').setAttribute('aria-sort', button.dataset.sort === state.sort ? (state.direction === 'asc' ? 'ascending' : 'descending') : 'none'));
    renderRows();
    byId('updatedAt').textContent = `Mis à jour à ${new Date().toLocaleTimeString('fr-FR')}`;
  } catch (error) {
    if (version === loadVersion) notify(error.message, true);
  } finally {
    if (version === loadVersion) byId('tableRegion').setAttribute('aria-busy', 'false');
  }
}

async function refreshStats() {
  const stats = await requestJSON('/api/stats');
  for (const [id, value] of [['totalQueries', stats.total_queries], ['analyzedQueries', stats.analyzed], ['criticalQueries', stats.critical]]) byId(id).textContent = number(value);
  byId('avgScoreVal').textContent = stats.avg_score == null ? '—' : number(stats.avg_score, 1);
  byId('statusCollectorNav').textContent = stats.collector_active ? 'Collecte activée' : 'Collecte en pause';
  byId('statusModeNav').textContent = stats.analyzer_mode === 'auto' ? 'Analyse auto' : 'Analyse manuelle';
  analyzing = new Set(stats.analyzing_ids);
  return stats;
}

async function analyzeIds(ids) {
  let queued = 0;
  try {
    for (const id of ids) { await requestJSON(`/api/analyze/${id}`, { method: 'POST' }); queued++; }
    notify(`${queued} analyse(s) ajoutée(s) à la file.`);
  } catch (error) { notify(`${queued} ajoutée(s). ${error.message}`, true); }
  await refreshStats();
  renderRows();
}

byId('filters').addEventListener('submit', event => event.preventDefault());
byId('searchInput').addEventListener('input', event => {
  state.search = event.target.value;
  state.page = 1;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadRows, 300);
});
for (const [id, key] of [['schemaFilter', 'schema'], ['criticalOnly', 'critical_only'], ['groupByPattern', 'group'], ['sortSelect', 'sort'], ['pageSize', 'page_size']]) {
  byId(id).addEventListener('change', event => {
    state[key] = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
    if (key === 'sort') state.direction = state.sort === 'score' ? 'asc' : 'desc';
    state.page = 1;
    loadRows();
  });
}
byId('resetFilters').addEventListener('click', () => {
  Object.assign(state, { search: '', schema: '', critical_only: false, group: false, pattern: '', source: '', page: 1 });
  syncControls(); loadRows();
});
for (const [id, delta] of [['previousPage', -1], ['nextPage', 1]]) byId(id).addEventListener('click', () => { state.page += delta; loadRows(); });
document.querySelectorAll('[data-sort]').forEach(button => button.addEventListener('click', () => {
  state.direction = state.sort === button.dataset.sort && state.direction === 'desc' ? 'asc' : 'desc';
  state.sort = button.dataset.sort;
  state.page = 1;
  byId('sortSelect').value = state.sort;
  loadRows();
}));
byId('selectAll').addEventListener('change', event => {
  items.filter(item => item.variant_count === 1).forEach(item => event.target.checked ? selected.add(item.id) : selected.delete(item.id));
  renderRows();
});
byId('queriesTable').addEventListener('change', event => {
  if (!event.target.dataset.select) return;
  const id = Number(event.target.dataset.select);
  event.target.checked ? selected.add(id) : selected.delete(id);
  syncSelection();
});
byId('queriesTable').addEventListener('click', async event => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const id = Number(button.dataset.id);
  button.disabled = true;
  try {
    if (button.dataset.action === 'analyze') await analyzeIds([id]);
    if (button.dataset.action === 'delete' && confirm('Supprimer cette requête et toutes ses données ?')) {
      await requestJSON(`/api/queries/${id}`, { method: 'DELETE' });
      selected.delete(id); await refreshStats(); await loadRows();
    }
    if (button.dataset.action === 'variants') {
      const item = items.find(item => item.id === id);
      Object.assign(state, { group: false, pattern: item.sql_hash, schema: item.schema_name || '', source: item.source_id, page: 1 });
      syncControls(); await loadRows();
    }
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
});
byId('clearSelection').addEventListener('click', () => { selected.clear(); renderRows(); });
byId('exportSelection').addEventListener('click', () => {
  if (selected.size) window.open(`/export/pdf?ids=${[...selected].join(',')}`, '_blank', 'noopener');
});
byId('analyzeSelection')?.addEventListener('click', async event => {
  event.currentTarget.disabled = true;
  try { await analyzeIds([...selected]); } catch (error) { notify(error.message, true); }
  finally { byId('analyzeSelection').disabled = false; }
});
byId('analyzeAll')?.addEventListener('click', async () => {
  byId('analyzeAll').disabled = true;
  try {
    const result = await requestJSON('/api/analyze/all', { method: 'POST' });
    notify(`${result.queued} analyse(s) ajoutée(s).${result.remaining ? ` ${result.remaining} restantes : relancez après ce lot.` : ''}`);
    await refreshStats(); renderRows();
  } catch (error) { notify(error.message, true); }
  finally { byId('analyzeAll').disabled = false; }
});
byId('openPurge')?.addEventListener('click', () => byId('purgeDialog').showModal());
byId('confirmPurge')?.addEventListener('click', async () => {
  byId('confirmPurge').disabled = true;
  try {
    await requestJSON('/api/queries', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope: byId('purgeScope').value }) });
    byId('purgeDialog').close(); selected.clear(); notify('Suppression terminée.');
    await refreshStats(); await loadRows();
  } catch (error) { notify(error.message, true); byId('purgeDialog').close(); }
  finally { byId('confirmPurge').disabled = false; }
});

async function poll() {
  try { if (!document.hidden) { await refreshStats(); await loadRows(); } }
  catch (error) { notify(error.message, true); }
  finally { setTimeout(poll, analyzing.size ? 5000 : 30000); }
}
syncControls();
poll();