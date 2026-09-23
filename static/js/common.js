'use strict';

function escapeHTML(value) {
  const element = document.createElement('span');
  element.textContent = String(value ?? '');
  return element.innerHTML.replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function safeMarkdown(value) {
  return DOMPurify.sanitize(marked.parse(String(value ?? ''), { gfm: true, breaks: true }), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form', 'input', 'button', 'iframe'],
    FORBID_ATTR: ['style'],
  });
}

function refreshIcons() {
  if (window.lucide) lucide.createIcons();
}

function wrapMarkdownTables(container) {
  container.querySelectorAll('table').forEach(table => {
    if (table.parentElement.classList.contains('content-table-wrap')) return;
    const wrap = document.createElement('div');
    wrap.className = 'content-table-wrap';
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'region');
    wrap.setAttribute('aria-label', 'Tableau — défilement horizontal');
    table.replaceWith(wrap);
    wrap.append(table);
  });
}

async function copyCodeText(button, text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.append(textarea);
      try {
        textarea.focus();
        textarea.select();
        if (!document.execCommand('copy')) throw new Error('Copie indisponible');
      } finally {
        textarea.remove();
        button.focus({ preventScroll: true });
      }
    }
    button.textContent = '✅';
    button.setAttribute('aria-label', 'Code copié');
  } catch {
    button.textContent = '❌';
    button.setAttribute('aria-label', 'Copie impossible — réessayer');
  }
  setTimeout(() => {
    button.textContent = '📋';
    button.setAttribute('aria-label', 'Copier le code');
  }, 2000);
}

async function requestJSON(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('Reponse du serveur illisible.'); }
  if (!response.ok || data.ok === false) {
    const detail = typeof data.detail === 'string' ? data.detail : data.error;
    throw new Error(detail || `La demande a echoue (${response.status}).`);
  }
  return data;
}

document.addEventListener('DOMContentLoaded', refreshIcons);