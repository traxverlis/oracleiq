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
  const localOptions = [...select.children].map(option => option.cloneNode(true));

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
      const selected = select.value;
      select.replaceChildren(...localOptions.map(option => option.cloneNode(true)));
      if (![...select.options].some(option => option.value === selected)) select.add(new Option(selected, selected));
      select.value = selected;
      table.replaceChildren();
      const cell = table.insertRow().insertCell();
      cell.colSpan = 3;
      cell.textContent = 'Aucun catalogue enregistré.';
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

  // ─── Jeton GitHub ──────────────────────────────────────────
  const tokenInput = document.getElementById('githubTokenInput');
  const tokenStatus = document.querySelector('[data-token-status]');
  const tokenTestBtn = document.querySelector('[data-token-test]');
  const tokenSaveBtn = document.querySelector('[data-token-save]');
  const tokenDeleteBtn = document.querySelector('[data-token-delete]');
  let tokenBusy = false;

  function tokenSetStatus(text, error = false) {
    tokenStatus.textContent = text;
    tokenStatus.classList.toggle('error-text', error);
  }

  function tokenSetBusy(state) {
    tokenBusy = state;
    const envManaged = tokenInput.dataset.envManaged === 'true';
    tokenInput.disabled = state || envManaged;
    tokenTestBtn.disabled = state;
    tokenSaveBtn.disabled = state || envManaged;
    tokenDeleteBtn.disabled = state || envManaged;
  }

  async function tokenRefreshStatus() {
    try {
      const info = await requestJSON('/api/settings/github_token');
      tokenInput.dataset.envManaged = info.source === 'env' ? 'true' : 'false';
      if (info.source === 'env') {
        tokenSetStatus('Jeton défini via la variable GITHUB_TOKEN sur le serveur (prioritaire, non modifiable ici).');
      } else if (info.configured) {
        tokenSetStatus('Jeton enregistré via l\u2019administration.');
      } else {
        tokenSetStatus('Aucun jeton configuré.');
      }
      tokenSetBusy(false);
    } catch (error) {
      tokenSetStatus(error.message, true);
    }
  }

  tokenTestBtn.addEventListener('click', async () => {
    if (tokenBusy) return;
    tokenSetBusy(true);
    tokenSetStatus('Test du jeton en cours…');
    try {
      const body = tokenInput.value.trim() ? { token: tokenInput.value.trim() } : {};
      const result = await requestJSON('/api/settings/github_token/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      tokenSetStatus(result.ok ? 'Accès au catalogue confirmé par le SDK Copilot. Test sans enregistrement.' : (result.error || 'Connexion refusée.'), !result.ok);
    } catch (error) {
      tokenSetStatus(error.message, true);
    } finally {
      tokenSetBusy(false);
    }
  });

  tokenSaveBtn.addEventListener('click', async () => {
    if (tokenBusy) return;
    const token = tokenInput.value.trim();
    if (!token) { tokenSetStatus('Saisis un jeton avant d\u2019enregistrer.', true); return; }
    tokenSetBusy(true);
    tokenSetStatus('Enregistrement…');
    try {
      await requestJSON('/api/settings/github_token', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
      });
      tokenInput.value = '';
      tokenSetStatus('Jeton enregistré. Catalogue de modèles réinitialisé.');
      await tokenRefreshStatus();
      load(true);
    } catch (error) {
      tokenSetStatus(error.message, true);
      tokenSetBusy(false);
    }
  });

  tokenDeleteBtn.addEventListener('click', async () => {
    if (tokenBusy) return;
    tokenSetBusy(true);
    tokenSetStatus('Suppression…');
    try {
      await requestJSON('/api/settings/github_token', { method: 'DELETE' });
      tokenInput.value = '';
      tokenSetStatus('Jeton supprimé.');
      await tokenRefreshStatus();
      load();
    } catch (error) {
      tokenSetStatus(error.message, true);
      tokenSetBusy(false);
    }
  });

  tokenRefreshStatus();
})();