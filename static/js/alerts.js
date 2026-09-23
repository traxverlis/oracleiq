'use strict';

(() => {
  const get = id => document.getElementById(id);
  const isAdmin = document.body.dataset.admin === 'true';
  const limit = 50;
  let offset = 0;
  let includeAcknowledged = false;
  let version = 0;
  let signature = '';
  let timer;
  let acknowledging = false;
  const text = (parent, tag, value, className = '') => {
    const node = document.createElement(tag);
    node.textContent = String(value ?? '');
    node.className = className;
    parent.append(node);
    return node;
  };
  const date = value => {
    if (!value) return '';
    const parsed = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value.replace(' ', 'T') + 'Z');
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('fr-FR');
  };

  function render(items) {
    const nextSignature = JSON.stringify(items);
    if (signature === nextSignature) return;
    signature = nextSignature;
    const focused = document.activeElement?.closest('[data-alert-id]');
    const focusedId = focused?.dataset.alertId;
    const focusedTag = document.activeElement?.tagName;
    get('alertsList').replaceChildren(...items.map(item => {
      const entry = document.createElement('li');
      entry.dataset.alertId = item.id;
      const heading = text(entry, 'h3', item.title);
      const badge = text(heading, 'span', item.severity === 'critical' ? 'Critique' : 'Avertissement', 'badge');
      badge.classList.add(item.severity === 'critical' ? 'critical' : 'warning');
      text(entry, 'p', item.kind === 'regression' ? 'Suspicion de régression' : 'État du service', 'meta-item');
      text(entry, 'p', item.message);
      text(entry, 'p', `Créée : ${date(item.created_at)}`, 'meta-item');
      text(entry, 'p', item.acknowledged_at ? `Acquittée : ${date(item.acknowledged_at)}` : 'Non acquittée', 'alert-ack');
      text(entry, 'p', item.resolved_at ? `Résolue : ${date(item.resolved_at)}` : 'Non résolue', 'alert-resolution');
      if (Number.isSafeInteger(item.query_id) && item.query_id > 0) {
        text(entry, 'a', `Voir la requête #${item.query_id}`, 'nav-btn').href = `/query/${item.query_id}`;
      }
      if (isAdmin && !item.acknowledged_at) {
        const button = text(entry, 'button', 'Acquitter', 'nav-btn');
        button.type = 'button';
        button.setAttribute('aria-label', `Acquitter : ${item.title}`);
        button.addEventListener('click', () => acknowledge(item.id, button));
      }
      return entry;
    }));
    if (focusedId) {
      const row = [...get('alertsList').children].find(node => node.dataset.alertId === focusedId);
      (row?.querySelector(focusedTag === 'A' ? 'a' : 'button') || get('alertsHistory')).focus({ preventScroll: true });
    }
  }

  async function load() {
    const current = ++version;
    get('alertsList').setAttribute('aria-busy', 'true');
    get('refreshAlerts').disabled = true;
    try {
      const data = await requestJSON(`/api/alerts?${new URLSearchParams({ limit, offset, include_acknowledged: includeAcknowledged })}`);
      if (current !== version) return;
      if (offset && offset >= data.total) {
        offset = Math.max(0, Math.floor((data.total - 1) / limit) * limit);
        return load();
      }
      get('alertsCount').textContent = `— ${data.unacknowledged} non acquittée(s)`;
      render(data.items);
      get('alertsStatus').className = '';
      get('alertsStatus').textContent = data.items.length ? (includeAcknowledged ? 'Historique complet des alertes.' : 'Alertes non acquittées.') : (includeAcknowledged ? 'Aucune alerte dans l’historique.' : 'Aucune alerte non acquittée.');
      get('alertsPage').textContent = `${Math.floor(offset / limit) + 1} / ${Math.max(1, Math.ceil(data.total / limit))} — ${data.total} alerte(s)`;
      get('alertsPrevious').disabled = offset === 0;
      get('alertsNext').disabled = offset + limit >= data.total;
    } catch (error) {
      if (current !== version) return;
      get('alertsStatus').className = 'error-text';
      get('alertsStatus').textContent = `Alertes indisponibles : ${error.message} Les données affichées peuvent être anciennes. Réessayez avec Actualiser les alertes.`;
    } finally {
      if (current === version) {
        get('alertsList').setAttribute('aria-busy', 'false');
        get('refreshAlerts').disabled = false;
      }
    }
  }

  async function acknowledge(id, button) {
    if (acknowledging) return;
    acknowledging = true;
    button.disabled = true;
    try {
      await requestJSON(`/api/alerts/${encodeURIComponent(id)}/acknowledge`, { method: 'POST' });
      await load();
    } catch (error) {
      get('alertsStatus').className = 'error-text';
      get('alertsStatus').textContent = `Échec de l’acquittement : ${error.message}`;
    } finally {
      button.disabled = false;
      acknowledging = false;
    }
  }

  get('alertsHistory').addEventListener('click', () => {
    includeAcknowledged = !includeAcknowledged;
    offset = 0;
    get('alertsHistory').setAttribute('aria-pressed', String(includeAcknowledged));
    get('alertsHistory').textContent = includeAcknowledged ? 'Afficher les alertes non acquittées' : 'Historique des alertes';
    load();
  });
  get('refreshAlerts').addEventListener('click', load);
  get('alertsPrevious').addEventListener('click', () => { offset = Math.max(0, offset - limit); load(); });
  get('alertsNext').addEventListener('click', () => { offset += limit; load(); });
  async function poll() {
    if (!document.hidden && !acknowledging) await load();
    timer = setTimeout(poll, 30000);
  }
  window.addEventListener('pagehide', () => clearTimeout(timer));
  poll();
})();
