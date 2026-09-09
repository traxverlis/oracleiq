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