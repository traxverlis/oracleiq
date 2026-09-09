'use strict';

(() => {
  const queryId = Number(document.body.dataset.queryId);
  const element = id => document.getElementById(id);
  const format = (value, digits = 1) => value == null ? '—' : Number(value).toLocaleString('fr-FR', { maximumFractionDigits: digits });
  const date = value => new Date(value * 1000).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' });
  let performanceVersion = 0;
  let planVersion = 0;

  async function loadPerformance() {
    const version = ++performanceVersion;
    const status = element('performanceStatus');
    const rows = element('performanceRows');
    element('performanceSection').setAttribute('aria-busy', 'true');
    element('refreshPerformance').disabled = true;
    status.textContent = 'Chargement des mesures…';
    status.classList.remove('error-text');
    try {
      const data = await requestJSON(`/api/queries/${queryId}/performance?minutes=${element('performancePeriod').value}`, { signal: AbortSignal.timeout(10000) });
      if (version !== performanceVersion) return;
      rows.replaceChildren();
      const { previous, current, variations } = data;
      const metrics = [
        ['Temps moyen (ms)', 'elapsed_ms_avg'], ['CPU moyen (ms)', 'cpu_ms_avg'],
        ['Buffer gets / exécution', 'buffer_gets_avg'], ['Lectures disque / exécution', 'disk_reads_avg'],
        ['Lignes / exécution', 'rows_avg'], ['Exécutions observées', 'executions'],
        ['Temps cumulé observé (ms)', 'elapsed_ms_total'], ['Couverture (%)', 'coverage_percent'],
      ];
      for (const [label, key] of metrics) {
        const row = document.createElement('tr');
        const heading = document.createElement('th');
        heading.scope = 'row';
        heading.textContent = label;
        row.appendChild(heading);
        for (const period of [previous, current]) {
          const cell = document.createElement('td');
          cell.textContent = period.state === 'insufficient' && key !== 'coverage_percent' ? '—' : format(period[key]);
          row.appendChild(cell);
        }
        const variation = document.createElement('td');
        variation.textContent = variations[key] == null ? '—' : `${variations[key] > 0 ? '+' : ''}${format(variations[key])} %`;
        row.appendChild(variation);
        rows.appendChild(row);
      }
      for (const [name, period] of [['previous', previous], ['current', current]]) {
        element(`${name}PeriodDates`).textContent = `${date(period.start)} – ${date(period.end)}`;
      }
      const stateLabels = { insufficient: 'mesures insuffisantes', idle: 'aucune exécution sur les intervalles observés', measured: 'mesures disponibles' };
      status.textContent = `Récente : ${stateLabels[current.state]}. Précédente : ${stateLabels[previous.state]}. Comparaison sur les intervalles observés, sans extrapolation.`;
      const reasons = { gap: 'trous de collecte', cursor_change: 'changements de curseur', plan_change: 'changements de plan', reset: 'compteurs remis à zéro', in_flight: 'activité sans nouvelle exécution comptée' };
      const exclusions = Object.entries(reasons).map(([key, label]) => {
        const count = previous.excluded[key] + current.excluded[key];
        return count ? `${count} ${label}` : null;
      }).filter(Boolean);
      element('performanceExclusions').textContent = exclusions.length ? `Intervalles exclus : ${exclusions.join(', ')}.` : '';
    } catch {
      if (version !== performanceVersion) return;
      rows.replaceChildren();
      element('previousPeriodDates').textContent = '';
      element('currentPeriodDates').textContent = '';
      element('performanceExclusions').textContent = '';
      status.textContent = 'Mesures indisponibles. Vérifiez la connexion et réessayez.';
      status.classList.add('error-text');
    } finally {
      if (version === performanceVersion) {
        element('performanceSection').setAttribute('aria-busy', 'false');
        element('refreshPerformance').disabled = false;
      }
    }
  }

  async function loadPlans(reset = false) {
    const version = ++planVersion;
    const before = element('beforePlan');
    const after = element('afterPlan');
    const status = element('planComparisonStatus');
    const output = element('planDiff');
    const params = !reset && before.value && after.value ? `?${new URLSearchParams({ before_id: before.value, after_id: after.value })}` : '';
    element('refreshPlanComparison').disabled = true;
    status.textContent = 'Chargement des plans…';
    status.classList.remove('error-text');
    output.hidden = true;
    try {
      const data = await requestJSON(`/api/queries/${queryId}/plan-comparison${params}`, { signal: AbortSignal.timeout(10000) });
      if (version !== planVersion) return;
      for (const select of [before, after]) {
        select.replaceChildren(...data.plans.map(plan => new Option(`#${plan.id} · ${plan.captured_at} UTC · hash ${plan.plan_hash_value ?? 'inconnu'}`, plan.id)));
        select.disabled = data.plans.length < 2;
      }
      output.replaceChildren();
      if (!data.before || !data.after) {
        status.textContent = 'Deux captures de plan sont nécessaires à la comparaison.';
        return;
      }
      before.value = data.before.id;
      after.value = data.after.id;
      status.textContent = data.diff.length ? 'Différences textuelles des captures.' : 'Aucune différence textuelle entre ces captures.';
      if (data.truncated) status.textContent += ' Comparaison tronquée aux 1 000 premières lignes et 100 000 caractères par plan.';
      for (const line of data.diff) {
        const span = document.createElement('span');
        span.textContent = `${line}\n`;
        span.className = line.startsWith('+') ? 'diff-added' : line.startsWith('-') ? 'diff-removed' : '';
        output.appendChild(span);
      }
      output.hidden = data.diff.length === 0;
    } catch {
      if (version !== planVersion) return;
      status.textContent = 'Comparaison indisponible. Actualisez les captures.';
      status.classList.add('error-text');
    } finally {
      if (version === planVersion) element('refreshPlanComparison').disabled = false;
    }
  }

  element('performancePeriod').addEventListener('change', loadPerformance);
  element('refreshPerformance').addEventListener('click', loadPerformance);
  element('planComparison').addEventListener('toggle', () => { if (element('planComparison').open) loadPlans(); });
  element('beforePlan').addEventListener('change', () => loadPlans());
  element('afterPlan').addEventListener('change', () => loadPlans());
  element('refreshPlanComparison').addEventListener('click', () => loadPlans(true));
  loadPerformance();
})();