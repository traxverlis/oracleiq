"""
analyzer/copilot_client.py
Client GitHub Copilot — gère l'auth OAuth device flow + refresh token Copilot
Endpoint confirmé : https://api.githubcopilot.com/chat/completions (streaming SSE)
"""
import os
import time
import json
import requests
from pathlib import Path

# GitHub OAuth App (Copilot CLI officielle)
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
TOKEN_CACHE_PATH = Path.home() / ".oracleiq_copilot_token.json"

COPILOT_ENDPOINT = "https://api.githubcopilot.com/chat/completions"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
DEVICE_CODE_URL = "https://github.com/login/device/code"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"


def _load_cache() -> dict:
    if TOKEN_CACHE_PATH.exists():
        try:
            return json.loads(TOKEN_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cache(data: dict):
    TOKEN_CACHE_PATH.write_text(json.dumps(data))
    TOKEN_CACHE_PATH.chmod(0o600)


def get_oauth_token() -> str:
    """Récupère ou renouvelle le token OAuth GitHub via device flow."""
    cache = _load_cache()
    oauth = cache.get("oauth_token", "")
    if oauth:
        # Vérifier que le token est toujours valide
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": f"token {oauth}"}, timeout=30)
        if r.status_code == 200:
            return oauth

    # Device flow
    r = requests.post(DEVICE_CODE_URL,
                      headers={"Accept": "application/json"},
                      json={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    device_code = data["device_code"]
    user_code = data["user_code"]
    interval = data.get("interval", 5)

    print(f"\n🔑 Authentification GitHub Copilot requise")
    print(f"   1. Va sur : https://github.com/login/device")
    print(f"   2. Entre le code : {user_code}")
    print(f"   En attente de l'autorisation...\n")

    # Poll jusqu'à autorisation
    for _ in range(60):
        time.sleep(interval)
        r = requests.post(OAUTH_TOKEN_URL,
                          headers={"Accept": "application/json"},
                          json={
                              "client_id": GITHUB_CLIENT_ID,
                              "device_code": device_code,
                              "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
                          }, timeout=30)
        resp = r.json()
        if "access_token" in resp:
            oauth_token = resp["access_token"]
            cache["oauth_token"] = oauth_token
            cache.pop("copilot_token", None)
            cache.pop("copilot_expires", None)
            _save_cache(cache)
            print("✅ Authentification réussie !")
            return oauth_token
        if resp.get("error") not in ("authorization_pending", "slow_down"):
            raise RuntimeError(f"Erreur OAuth: {resp}")

    raise RuntimeError("Timeout — autorisation non reçue dans les temps")


def get_copilot_token(*, interactive: bool = True, force_refresh: bool = False) -> str:
    """Récupère un token Copilot frais (expire toutes les ~30 min)."""
    cache = _load_cache()
    cop_token = cache.get("copilot_token", "")
    cop_expires = cache.get("copilot_expires", 0)

    # Marge de 2 min avant expiration
    if not force_refresh and cop_token and time.time() < cop_expires - 120:
        return cop_token

    oauth_token = get_oauth_token() if interactive else cache.get("oauth_token")
    if not oauth_token:
        raise RuntimeError("Authentification Copilot requise dans le terminal.")

    r = requests.get(
        COPILOT_TOKEN_URL,
        headers={
            "Authorization": f"token {oauth_token}",
            "Editor-Version": "vscode/1.95.0",
            "Editor-Plugin-Version": "copilot-chat/0.22.0",
            "User-Agent": "GitHubCopilotChat/0.22.0",
        }, timeout=30
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("token", "")
    if not token:
        raise RuntimeError(f"Token Copilot vide: {data}")

    # Extraire expiration depuis le token (format: ...;exp=TIMESTAMP;...)
    import re
    exp_match = re.search(r";exp=(\d+);", token)
    expires = int(exp_match.group(1)) if exp_match else int(time.time()) + 1700

    cache["copilot_token"] = token
    cache["copilot_expires"] = expires
    _save_cache(cache)

    return token


def list_account_models() -> list[dict]:
    for attempt in range(2):
        token = get_copilot_token(interactive=False, force_refresh=attempt > 0)
        response = requests.get(
            "https://api.githubcopilot.com/models",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "Copilot-Integration-Id": "vscode-chat", "Editor-Version": "vscode/1.95.0"},
            timeout=30,
        )
        if response.status_code != 401 or attempt:
            break
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Catalogue Copilot invalide")
    models = {}
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
            continue
        capabilities = entry.get("capabilities") or {}
        policy = entry.get("policy") or {}
        if not isinstance(capabilities, dict) or not isinstance(policy, dict):
            continue
        if capabilities.get("type", "chat") != "chat" or policy.get("state") == "disabled":
            continue
        if entry.get("model_picker_enabled") is False:
            continue
        endpoints = entry.get("supported_endpoints")
        if isinstance(endpoints, list) and "/chat/completions" not in endpoints:
            continue
        name = entry.get("name")
        vendor = entry.get("vendor")
        models[model_id] = {"id": model_id, "name": name[:200] if isinstance(name, str) else model_id,
                            "vendor": vendor[:100] if isinstance(vendor, str) else ""}
    return sorted(models.values(), key=lambda model: (model["vendor"].lower(), model["name"].lower(), model["id"]))


def chat(messages: list, model: str = "claude-sonnet-4.6",
         max_tokens: int = 2000, system: str = None, _retry: int = 0,
         thinking_budget_tokens: int = 1024) -> tuple[str, dict]:
    """Appel chat Copilot non-streaming → retourne (texte, usage_dict).
    thinking_budget_tokens: 0=off, 1024=rapide, 5000=standard, 10000=approfondi
    """
    token = get_copilot_token()

    payload_messages = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    payload_messages.extend(messages)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.95.0",
    }
    payload = {
        "model": model,
        "messages": payload_messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    # Contrôle du raisonnement : budget_tokens limite la profondeur de thinking
    # 0 = désactivé (~8s), 1024 = minimal (~8s), 5000 = standard (~30s), -1 = illimité (~60s+)
    thinking_budget = thinking_budget_tokens
    if thinking_budget == 0:
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    r = requests.post(COPILOT_ENDPOINT, headers=headers,
                      json=payload, timeout=180)  # 3min : plan Oracle peut être volumineux

    if not r.ok:
        # Token expiré ? Vider le cache et réessayer une fois
        if r.status_code in (401, 403) and _retry == 0:
            cache = _load_cache()
            cache.pop("copilot_token", None)
            cache.pop("copilot_expires", None)
            _save_cache(cache)
            return chat(messages, model, max_tokens, system, _retry=1, thinking_budget_tokens=thinking_budget_tokens)
        raise RuntimeError(f"Copilot API error {r.status_code}: {r.text[:300]}")

    data = r.json()
    choices = data.get("choices", [])
    if not choices:
        import logging
        logging.getLogger("oracleiq").error(f"chat() empty choices: {str(data)[:300]}")
        # Retry une fois si choices vide (peut arriver sur surcharge serveur)
        if _retry == 0:
            time.sleep(3)
            return chat(messages, model, max_tokens, system, _retry=1, thinking_budget_tokens=thinking_budget_tokens)
        raise RuntimeError(f"API returned no choices: {str(data)[:200]}")
    content = choices[0].get("message", {}).get("content") or ""
    usage = data.get("usage", {})

    import logging
    logging.getLogger("oracleiq").info(f"chat() model={model} len={len(content)} tokens={usage}")
    return content, usage


# ── Modèles disponibles (vérifiés sur api.githubcopilot.com) ─────────────────
AVAILABLE_MODELS = {
    # Anthropic Claude
    "claude-haiku-4.5":       "Claude Haiku 4.5 — ultra rapide, économique (~3s)",
    "claude-sonnet-4.5":      "Claude Sonnet 4.5 — bon équilibre (~8s)",
    "claude-sonnet-4.6":      "Claude Sonnet 4.6 — recommandé ODIN (~10s) ★",
    "claude-sonnet-5":        "Claude Sonnet 5 — nouvelle génération (~20s)",
    "claude-opus-4.6":        "Claude Opus 4.6 — très puissant (~30s)",
    "claude-opus-4.7":        "Claude Opus 4.7 — très puissant (~30s)",
    "claude-opus-4.8":        "Claude Opus 4.8 — analyses complexes (~30s)",
    "claude-opus-5":          "Claude Opus 5 — meilleur Claude disponible (~40s)",
    # OpenAI GPT
    "gpt-4o-mini":            "GPT-4o mini — rapide, économique (~3s)",
    "gpt-4o":                 "GPT-4o — polyvalent (~8s)",
    "gpt-4o-2024-11-20":      "GPT-4o stable nov.2024 (~8s)",
    "gpt-4.1":                "GPT-4.1 — très fort en SQL/code (~10s)",
    "gpt-4":                  "GPT-4 — classique",
    "gpt-3.5-turbo":          "GPT-3.5 Turbo — économique",
    # Google Gemini (nécessite max_tokens > 1000 — budget raisonnement interne)
    "gemini-3.1-pro-preview":  "Gemini 3.1 Pro — contexte long (~26s)",
}


def chat_with_tools(
    messages: list,
    tools: list,
    model: str = "claude-sonnet-4.6",
    max_tokens: int = 4000,
    system: str = None,
    tool_choice: str = "auto",
    _retry: int = 0,
) -> tuple[str | None, list, dict]:
    """
    Appel Copilot avec native function calling (OpenAI tool_calls format).
    Retourne (text_content_or_none, tool_calls_list, usage_dict).
    tool_calls_list = [{"id": ..., "name": ..., "arguments": {...}}, ...]
    """
    token = get_copilot_token()

    payload_messages = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    payload_messages.extend(messages)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.95.0",
    }
    payload = {
        "model": model,
        "messages": payload_messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": max_tokens,
        "stream": False,
    }

    r = requests.post(COPILOT_ENDPOINT, headers=headers, json=payload, timeout=300)

    if not r.ok:
        if r.status_code in (401, 403) and _retry == 0:
            cache = _load_cache()
            cache.pop("copilot_token", None)
            cache.pop("copilot_expires", None)
            _save_cache(cache)
            return chat_with_tools(messages, tools, model, max_tokens, system, tool_choice, _retry=1)
        raise RuntimeError(f"Copilot API error {r.status_code}: {r.text[:300]}")

    data = r.json()
    choices = data.get("choices", [])
    if not choices:
        if _retry == 0:
            import time as _t
            _t.sleep(3)
            return chat_with_tools(messages, tools, model, max_tokens, system, tool_choice, _retry=1)
        raise RuntimeError(f"API returned no choices: {str(data)[:200]}")

    msg = choices[0].get("message", {})
    text_content = msg.get("content")  # peut être None si le modèle appelle un tool directement
    raw_tool_calls = msg.get("tool_calls") or []
    usage = data.get("usage", {})

    parsed_calls = []
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except Exception:
            args = {}
        parsed_calls.append({
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "arguments": args,
        })

    import logging
    logging.getLogger("oracleiq").info(
        f"chat_with_tools() model={model} text={len(text_content or '')} tools_called={len(parsed_calls)} tokens={usage}"
    )
    return text_content, parsed_calls, usage



    print("Test du client Copilot...")
    result = chat(
        messages=[{"role": "user", "content": "Expert Oracle 19c. Analyse en 2 phrases : SELECT * FROM orders o, customers c WHERE o.customer_id = c.id AND status = 'PENDING'"}],
        model="claude-sonnet-4.5"
    )
    print("\nRéponse:", result)
