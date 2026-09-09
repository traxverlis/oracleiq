'use strict';

(() => {
  const labels = {
    unknown: 'Non observé', stale: 'Sans signal récent', stopped: 'Arrêté',
    error: 'Erreur signalée', starting: 'Démarrage', connecting: 'Connexion en cours',
    connected: 'Connecté', collecting: 'Collecte en cours', waiting: 'En attente',
    paused: 'En pause', manual: 'Mode manuel', analyzing: 'Analyse en cours',
  };
  const element = id => document.getElementById(id);
  const section = document.querySelector('.service-health');
  const refresh = element('refreshHealth');
  let loading = false;
  const dateLabel = value => {
    if (value == null) return 'Aucun';
    const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value.replace(' ', 'T') + 'Z');
    return Number.isNaN(date.getTime()) ? 'Non disponible' : date.toLocaleString('fr-FR');
  };

  async function loadHealth() {
    if (loading) return;
    loading = true;
    refresh.disabled = true;
    section.setAttribute('aria-busy', 'true');
    try {
      const health = await requestJSON('/api/health', { signal: AbortSignal.timeout(8000) });
      for (const name of ['collector', 'analyzer']) {
        const service = health.services[name];
        element(`${name}State`).textContent = labels[service.state] || 'Non observé';
        element(`${name}State`).dataset.state = service.state;
        element(`${name}Signal`).textContent = `Dernier signal : ${dateLabel(service.updated_at)}`;
      }
      element('collectorSuccess').textContent = dateLabel(health.services.collector.last_success);
      element('lastAnalysis').textContent = dateLabel(health.last_analysis);
      element('analysisQueue').textContent = health.queue_size;
      element('analysisFailures').textContent = health.failed_queries;
      element('collectorSetting').textContent = health.collector_enabled ? 'Collecte autorisée' : 'Pause demandée';
      element('analyzerSetting').textContent = health.automatic_analysis ? 'Mode automatique demandé' : 'Mode manuel demandé';
      element('healthUpdated').textContent = new Date().toLocaleTimeString('fr-FR');
      element('healthError').hidden = true;
    } catch {
      element('healthError').textContent = 'État indisponible. Vérifiez la connexion et votre session.';
      element('healthError').hidden = false;
      element('healthUpdated').textContent = '';
      for (const name of ['collector', 'analyzer']) {
        element(`${name}State`).textContent = 'Non disponible';
        element(`${name}State`).dataset.state = 'unknown';
      }
    } finally {
      loading = false;
      refresh.disabled = false;
      section.setAttribute('aria-busy', 'false');
    }
  }

  refresh.addEventListener('click', loadHealth);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadHealth(); });
  setInterval(() => { if (!document.hidden) loadHealth(); }, 15000);
  loadHealth();
})();