'use strict';

(() => {
  const select = document.getElementById('ai_model');
  const buttons = [...document.querySelectorAll('[data-refresh-models]')];
  const statuses = [...document.querySelectorAll('[data-models-status]')];
  const table = document.getElementById('accountModels');
  let busy = false;
  let supported = true;
  const current = select.dataset.currentModel;
  if (![...select.options].some(option => option.value === current)) select.add(new Option(current, current));
  select.value = current;

  function status(text, error = false) {
    for (const target of statuses) {
      target.textContent = text;
      target.classList.toggle('error-text', error);
    }
  }

  function render(catalog) {
    supported = catalog.provider === 'github-copilot';
    if (!supported) {
      status('Actualisation disponible pour GitHub Copilot uniquement.');
      return;
    }
    if (catalog.updated_at == null) {
      status('Catalogue du compte non actualisé. Liste locale dans le sélecteur.');
      return;
    }
    const selected = select.value;
    const listed = catalog.models.some(model => model.id === selected);
    const options = catalog.models.map(model => new Option(`${model.name} (${model.id})`, model.id));
    if (!listed) options.unshift(new Option(`${selected} (sélection actuelle, non listé)`, selected));
    select.replaceChildren(...options);
    select.value = selected;
    table.replaceChildren();
    for (const model of catalog.models) {
      const row = document.createElement('tr');
      for (const value of [model.name, model.id, model.vendor || '—']) {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.appendChild(cell);
      }
      table.appendChild(row);
    }
    if (!catalog.models.length) {
      const row = table.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 3;
      cell.textContent = 'Aucun modèle compatible renvoyé par le compte.';
    }
    const updated = new Date(catalog.updated_at * 1000).toLocaleString('fr-FR');
    status(`${catalog.models.length} modèles · Actualisé le ${updated}.${listed ? '' : ' Modèle sélectionné absent du catalogue, choix conservé.'}`);
  }

  async function load(refresh = false) {
    if (busy) return;
    busy = true;
    for (const button of buttons) button.disabled = true;
    status(refresh ? 'Actualisation du catalogue Copilot…' : 'Chargement du catalogue enregistré…');
    try {
      const catalog = await requestJSON(refresh ? '/api/models/refresh' : '/api/models', {
        method: refresh ? 'POST' : 'GET', signal: AbortSignal.timeout(125000),
      });
      render(catalog);
    } catch (error) {
      status(`${error.message} Liste et sélection conservées.`, true);
    } finally {
      busy = false;
      for (const button of buttons) button.disabled = !supported;
    }
  }

  for (const button of buttons) button.addEventListener('click', () => load(true));
  load();
})();