'use strict';

(() => {
  const button = document.getElementById('loadAiPreview');
  if (!button) return;
  const status = document.getElementById('aiPreviewStatus');
  const metadata = document.getElementById('aiPreviewMetadata');
  const content = document.getElementById('aiPreviewContent');
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    content.hidden = true;
    content.textContent = '';
    metadata.hidden = true;
    metadata.textContent = '';
    status.className = '';
    status.textContent = 'Construction de l’aperçu local…';
    try {
      const data = await requestJSON(`/api/queries/${Number(document.body.dataset.queryId)}/ai-preview`);
      const system = data.system_prompt ?? data.system;
      if (!['copilot', 'github-copilot'].includes(data.provider) || typeof data.send_raw_values !== 'boolean' ||
          typeof data.masking_applied !== 'boolean' || typeof data.prompt !== 'string' ||
          (system != null && typeof system !== 'string') ||
          (data.tools != null && !Array.isArray(data.tools))) {
        throw new Error('Format d’aperçu inattendu.');
      }
      status.textContent = data.send_raw_values
        ? 'Valeurs brutes — attention, des données sensibles seront transmises à GitHub Copilot. Les résultats des futurs appels d’outils sont absents de cet aperçu.'
        : 'Valeurs masquées — les résultats des futurs appels d’outils sont absents de cet aperçu.';
      status.className = data.send_raw_values ? 'error-text' : '';
      metadata.textContent = `GitHub Copilot · Modèle : ${data.model || 'non précisé'} · Masquage appliqué : ${data.masking_applied ? 'oui' : 'non'} · Limites : ${JSON.stringify(data.limits || {})}.\n${Array.isArray(data.warnings) ? data.warnings.join('\n') : ''}`;
      metadata.hidden = false;
      const sections = [];
      if (system) sections.push(`Instructions système\n${system}`);
      sections.push(`Contexte initial\n${data.prompt}`);
      if (data.tools) sections.push(`Schémas des outils proposés (pas les résultats)\n${JSON.stringify(data.tools, null, 2)}`);
      content.textContent = sections.join('\n\n');
      content.hidden = false;
    } catch (error) {
      status.className = 'error-text';
      status.textContent = `Aperçu indisponible : ${error.message}`;
    } finally {
      button.disabled = false;
    }
  });
})();
