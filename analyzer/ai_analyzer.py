"""
analyzer/ai_analyzer.py
Prend les requêtes non analysées, envoie requête + plan d'exécution à l'IA,
stocke les recommandations.
"""
import sys
import json
import time
import re
from typing import Literal
from pydantic import BaseModel, Field, ValidationError

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from config import AI_PROVIDER, AI_API_KEY, AI_MODEL, AI_BASE_URL, AI_MAX_TOKENS
from db.store import get_unanalyzed, save_analysis
from collector.connection import connect_oracle
from analyzer.copilot_client import chat as copilot_chat

# ─────────────────────────────────────────────
# Prompt système
# ─────────────────────────────────────────────
from analyzer.oracle_tools import TOOLS_DESCRIPTION, parse_tool_calls, execute_tool

SYSTEM_PROMPT = """Tu es un expert Oracle Database 19c spécialisé en optimisation de performances SQL.
Tu analyses des requêtes SQL et leurs plans d'exécution pour identifier les problèmes et proposer des corrections concrètes.

""" + TOOLS_DESCRIPTION + """

Quand tu as toutes les informations nécessaires (ou d'emblée si aucune info supplémentaire n'est utile),
réponds avec ce format — les 3 premières lignes sont OBLIGATOIRES et doivent rester exactement ainsi :

SCORE: <entier 0-100>
SEVERITY: <ok|warning|critical>
SUMMARY: <résumé en 1-2 phrases>

---

Puis rédige une analyse complète en **Markdown** avec les sections suivantes :

## ⚠️ Problèmes détectés
Pour chaque problème :
- **TYPE** (ex: `FULL_TABLE_SCAN`) · Impact: **critical/high/medium/low**  
  Description du problème

## ✅ Recommandations
1. **[P1] Titre de l'action**  
   Explication détaillée avec SQL si applicable.  
   *Gain estimé : ...*

## 🔍 SQL optimisé *(si applicable)*
```sql
-- version optimisée
```

Score : 100 = parfait, 0 = désastreux. Severity : ok(>=80), warning(50-79), critical(<50).
Types de problèmes : FULL_TABLE_SCAN, MISSING_INDEX, BAD_JOIN_ORDER, CARTESIAN_PRODUCT, STALE_STATS, NON_SARGABLE, EXCESSIVE_BUFFER_GETS, HIGH_DISK_READS, MISSING_BIND_VARS."""

USER_TEMPLATE = """Analyse cette requête Oracle 19c :

=== REQUÊTE SQL ===
{sql}

=== STATISTIQUES D'EXÉCUTION ===
- Exécutions : {executions}
- Temps moyen : {elapsed_ms_avg} ms
- Pic de moyenne observe : {elapsed_ms_max} ms (pas un maximum par execution)
- Buffer gets moy : {buffer_gets_avg}
- Disk reads moy : {disk_reads_avg}
- Lignes retournées moy : {rows_avg}
- Schéma : {schema}

=== PLAN D'EXÉCUTION ===
{plan}
"""


# ─────────────────────────────────────────────
# Clients IA
# ─────────────────────────────────────────────
def call_copilot(sql_text: str, context: dict) -> dict:
    """Appel via GitHub Copilot (Claude Sonnet/Opus/Haiku)."""
    prompt = USER_TEMPLATE.format(
        sql=sql_text,
        plan=context.get("plan_text") or "Non disponible",
        **context
    )
    raw, usage = copilot_chat(
        messages=[{"role": "user", "content": prompt}],
        model=AI_MODEL,
        max_tokens=_get_max_tokens(),
        system=SYSTEM_PROMPT,
    )
    return {"raw": raw, "model": AI_MODEL, "usage": usage}


def call_openai(sql_text: str, context: dict) -> dict:
    from openai import OpenAI
    client = OpenAI(
        api_key=AI_API_KEY,
        base_url=AI_BASE_URL if AI_BASE_URL else None
    )
    prompt = USER_TEMPLATE.format(
        sql=sql_text,
        plan=context.get("plan_text") or "Non disponible",
        **context
    )
    resp = client.chat.completions.create(
        model=AI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=_get_max_tokens(),
    )
    raw = resp.choices[0].message.content
    return {"raw": raw, "model": AI_MODEL}


def call_anthropic(sql_text: str, context: dict) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=AI_API_KEY)
    prompt = USER_TEMPLATE.format(
        sql=sql_text,
        plan=context.get("plan_text") or "Non disponible",
        **context
    )
    resp = client.messages.create(
        model=AI_MODEL,
        max_tokens=_get_max_tokens(),
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text
    return {"raw": raw, "model": AI_MODEL}


class AnalysisResult(BaseModel):
    score: int = Field(ge=0, le=100, strict=True)
    severity: Literal["ok", "warning", "critical"]
    summary: str = Field(min_length=1)
    issues: list[dict] = Field(default_factory=list)
    recommendations: list[dict] = Field(default_factory=list)


def validate_analysis(data):
    result = AnalysisResult.model_validate(data)
    if not result.summary.strip():
        raise ValueError("Resume IA vide")
    return result.model_dump()


def parse_ai_response(raw: str) -> dict:
    """Parse la réponse de l'IA — format texte structuré ou JSON."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Reponse IA vide")
    # Tentative JSON d'abord
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and "score" in data:
            return validate_analysis(data)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict) and "score" in data:
                return validate_analysis(data)
        except json.JSONDecodeError:
            pass

    # Format texte structuré : SCORE: XX / SEVERITY: xxx / SUMMARY: ...
    score_m = re.search(r'^SCORE:[ \t]*(\d+)[ \t]*$', raw, re.IGNORECASE | re.MULTILINE)
    sev_m = re.search(r'^SEVERITY:[ \t]*(ok|warning|critical)[ \t]*$', raw, re.IGNORECASE | re.MULTILINE)
    sum_m = re.search(r'^SUMMARY:[ \t]*([^\n]+)', raw, re.IGNORECASE | re.MULTILINE)
    if not all((score_m, sev_m, sum_m)):
        raise ValueError("Reponse IA incomplete : SCORE, SEVERITY et SUMMARY requis")
    score = int(score_m.group(1))
    severity = sev_m.group(1).lower()
    summary = sum_m.group(1).strip()[:300]

    # Extraire problèmes
    issues = []
    for m in re.finditer(
        r'TYPE:\s*([A-Z_]+).*?IMPACT:\s*(\w+).*?\|(.+?)(?=\n|$)', raw, re.IGNORECASE
    ):
        issues.append({"type": m.group(1), "impact": m.group(2).lower(),
                        "description": m.group(3).strip()})

    # Extraire recommandations
    recos = []
    for i, m in enumerate(re.finditer(
        r'\d+\.\s*\[P(\d)\]\s*(.+?)\u2192(.+?)(?=\n\d+\.|$)', raw, re.IGNORECASE | re.DOTALL
    ), 1):
        recos.append({"priority": int(m.group(1)), "action": m.group(2).strip(),
                      "detail": m.group(3).strip()[:400], "expected_gain": ""})

    return validate_analysis({
        "score": score,
        "severity": severity,
        "summary": summary[:300],
        "issues": issues,
        "recommendations": recos,
    })


def _build_initial_prompt(row: dict) -> str:
    plan_text = row.get("plan_text", "") or ""
    sql_id = row.get("sql_id", "")
    if not plan_text or "non disponible" in plan_text.lower() or "plan non disponible" in plan_text.lower():
        plan_section = f"""Non disponible.
⚠️ Aucun plan d'exécution en base. Tu DOIS appeler `explain_plan({sql_id})` comme première action
pour générer un plan via EXPLAIN PLAN FOR. Cette opération est sécurisée (pas d'exécution réelle)."""
    else:
        plan_section = plan_text
    return USER_TEMPLATE.format(
        sql=row["sql_text"],
        plan=plan_section,
        executions=row.get("executions", 1),
        elapsed_ms_avg=row.get("elapsed_ms_avg", 0),
        elapsed_ms_max=row.get("elapsed_ms_max", 0),
        buffer_gets_avg=row.get("buffer_gets_avg", 0),
        disk_reads_avg=row.get("disk_reads_avg", 0),
        rows_avg=row.get("rows_avg", 0),
        schema=row.get("schema_name", "UNKNOWN"),
    )


def analyze_query(row: dict, oracle_conn=None) -> dict:
    """Analyse une requête. Délègue au mode natif si configuré, sinon boucle classic."""
    from db.store import get_setting
    ANALYZER_AI_MODE = get_setting("analyzer_ai_mode", "classic")  # classic | agentic | native
    if ANALYZER_AI_MODE == "native":
        return analyze_query_native(row, oracle_conn=oracle_conn)
    return _analyze_classic(row, oracle_conn=oracle_conn)


def _get_max_tokens() -> int:
    """Lit ai_max_tokens depuis les settings DB (dynamique) avec fallback sur config.py."""
    from db.store import get_setting
    try:
        return int(get_setting("ai_max_tokens", str(AI_MAX_TOKENS)))
    except (ValueError, TypeError):
        return AI_MAX_TOKENS


def _analyze_classic(row: dict, oracle_conn=None) -> dict:
    """Analyse avec boucle agentique maison (classic ou agentic prompt)."""
    from db.store import get_setting
    messages = [{"role": "user", "content": _build_initial_prompt(row)}]
    all_raw_parts = []
    total_usage = {}
    model_used = AI_MODEL

    # Settings configurables
    try:
        MAX_TOOL_ROUNDS = max(1, min(int(get_setting("tool_rounds", "20")), 50))
    except ValueError:
        MAX_TOOL_ROUNDS = 20
    ANALYZER_AI_MODE = get_setting("analyzer_ai_mode", "classic")  # classic | agentic

    # Adapter le prompt système selon le mode
    system_prompt = SYSTEM_PROMPT
    if ANALYZER_AI_MODE == "agentic":
        system_prompt = SYSTEM_PROMPT + f"""

## Mode AGENTIC activé
Tu peux utiliser jusqu'à {MAX_TOOL_ROUNDS} rounds d'outils. Utilise-les librement pour collecter TOUTES
les informations dont tu as besoin avant de rédiger l'analyse finale.
Quand tu as terminé tes investigations, rédige l'analyse complète avec SCORE: / SEVERITY: / SUMMARY:.
"""

    for round_idx in range(MAX_TOOL_ROUNDS + 1):
        # Appel IA
        if AI_PROVIDER in ("github-copilot", "copilot"):
            raw, usage = copilot_chat(
                messages=messages,
                model=AI_MODEL,
                max_tokens=_get_max_tokens(),
                system=system_prompt,
            )
        elif AI_PROVIDER == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=AI_API_KEY)
            resp = client.messages.create(
                model=AI_MODEL, max_tokens=_get_max_tokens(), system=system_prompt, messages=messages
            )
            raw, usage = resp.content[0].text, {}
        else:
            from openai import OpenAI
            client = OpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL or None)
            resp = client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                temperature=0.1, max_tokens=_get_max_tokens(),
            )
            raw, usage = resp.choices[0].message.content, {}

        all_raw_parts.append(raw)
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v

        # En mode agentic : autoriser l'IA à appeler plusieurs tools par round (pas de limite sur le nombre)
        # En mode classic : parser les tools normalement
        tool_calls = parse_tool_calls(raw) if oracle_conn else []

        # Mode agentic : si le modèle n'a pas encore rédigé SCORE/SEVERITY, on laisse tourner
        if ANALYZER_AI_MODE == "agentic" and round_idx < MAX_TOOL_ROUNDS:
            has_final = bool(parse_tool_calls.__module__) and "SCORE:" in raw
            # Si pas de tools ET réponse finale détectée → on s'arrête
            if not tool_calls and ("SCORE:" in raw or "score:" in raw.lower()):
                break
            # Si le modèle veut encore des tools → on continue
            if tool_calls:
                pass  # continuer la boucle
            elif round_idx == 0:
                # Première réponse sans tool ni SCORE → forcer une conclusion
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "Rédige maintenant l'analyse finale en commençant par SCORE: / SEVERITY: / SUMMARY:"})
                continue
        else:
            # Mode classic : stopper si pas de tools
            if not tool_calls:
                # Vérifier qu'on a bien une analyse finale (SCORE présent)
                # Si pas de SCORE et pas de tools → le modèle divague, forcer une conclusion
                has_final = bool(re.search(r'SCORE:\s*\d+', raw, re.IGNORECASE))
                if has_final:
                    break
                # Pas de SCORE → forcer une conclusion
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "Rédige MAINTENANT l'analyse finale complète en commençant OBLIGATOIREMENT par SCORE: / SEVERITY: / SUMMARY:"})
                if AI_PROVIDER in ("github-copilot", "copilot"):
                    raw, usage = copilot_chat(messages=messages, model=AI_MODEL, max_tokens=_get_max_tokens(), system=system_prompt)
                elif AI_PROVIDER == "anthropic":
                    import anthropic
                    client = anthropic.Anthropic(api_key=AI_API_KEY)
                    resp = client.messages.create(model=AI_MODEL, max_tokens=_get_max_tokens(), system=system_prompt, messages=messages)
                    raw, usage = resp.content[0].text, {}
                else:
                    from openai import OpenAI
                    client = OpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL or None)
                    resp = client.chat.completions.create(model=AI_MODEL, messages=[{"role":"system","content":system_prompt}]+messages, temperature=0.1, max_tokens=_get_max_tokens())
                    raw, usage = resp.choices[0].message.content, {}
                all_raw_parts.append(raw)
                for k, v in (usage or {}).items():
                    if isinstance(v, (int, float)):
                        total_usage[k] = total_usage.get(k, 0) + v
                break
            # Dernier round : exécuter les tools puis forcer une analyse finale
            if round_idx == MAX_TOOL_ROUNDS:
                messages.append({"role": "assistant", "content": raw})
                tool_results = []
                for name, args in tool_calls:
                    result = execute_tool(oracle_conn, name, args)
                    tool_results.append(f"### Résultat : {name}({', '.join(args)})\n```json\n{json.dumps(result, default=str, ensure_ascii=True, indent=2)}\n```")
                feedback = "Voici les résultats des outils Oracle que tu as demandés :\n\n" + "\n\n".join(tool_results)
                feedback += "\n\nTu as atteint le nombre maximum de rounds. Rédige MAINTENANT l'analyse finale complète en commençant obligatoirement par SCORE: / SEVERITY: / SUMMARY:"
                messages.append({"role": "user", "content": feedback})
                # Un dernier appel pour obtenir l'analyse finale
                if AI_PROVIDER in ("github-copilot", "copilot"):
                    raw, usage = copilot_chat(messages=messages, model=AI_MODEL, max_tokens=_get_max_tokens(), system=system_prompt)
                elif AI_PROVIDER == "anthropic":
                    import anthropic
                    client = anthropic.Anthropic(api_key=AI_API_KEY)
                    resp = client.messages.create(model=AI_MODEL, max_tokens=_get_max_tokens(), system=system_prompt, messages=messages)
                    raw, usage = resp.content[0].text, {}
                else:
                    from openai import OpenAI
                    client = OpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL or None)
                    resp = client.chat.completions.create(model=AI_MODEL, messages=[{"role":"system","content":system_prompt}]+messages, temperature=0.1, max_tokens=_get_max_tokens())
                    raw, usage = resp.choices[0].message.content, {}
                all_raw_parts.append(raw)
                for k, v in (usage or {}).items():
                    if isinstance(v, (int, float)):
                        total_usage[k] = total_usage.get(k, 0) + v
                break

        # Ajouter la réponse de l'IA dans le fil de conversation
        messages.append({"role": "assistant", "content": raw})

        # Exécuter les outils et construire le message de résultats
        tool_results = []
        for name, args in tool_calls:
            result = execute_tool(oracle_conn, name, args)
            tool_results.append(f"### Résultat : {name}({', '.join(args)})\n```json\n{json.dumps(result, default=str, ensure_ascii=True, indent=2)}\n```")

        feedback = "Voici les résultats des outils Oracle que tu as demandés :\n\n" + "\n\n".join(tool_results)
        # Ne demander la conclusion que si on approche la fin, sinon laisser le modèle continuer
        rounds_left = MAX_TOOL_ROUNDS - round_idx
        if rounds_left <= 2:
            feedback += "\n\nTu disposes encore de peu de rounds. Si tu as suffisamment d'informations, rédige l'analyse finale maintenant en commençant par SCORE: / SEVERITY: / SUMMARY:"
        messages.append({"role": "user", "content": feedback})

    # La dernière partie de raw contient l'analyse finale
    final_raw = raw
    full_raw = "\n\n---\n\n".join(all_raw_parts)
    parsed = parse_ai_response(final_raw)
    return {
        "model": model_used,
        "score": parsed.get("score", 50),
        "severity": parsed.get("severity", "warning"),
        "summary": parsed.get("summary", ""),
        "issues": parsed.get("issues", []),
        "recommendations": parsed.get("recommendations", []),
        "raw": full_raw,
        "usage": total_usage,
    }


def analyze_query_native(row: dict, oracle_conn=None) -> dict:
    """
    Analyse avec vrai function calling natif (OpenAI tool_calls format).
    Le modèle s'arrête quand il est satisfait — pas de limite arbitraire de rounds.
    Compatible : GitHub Copilot (OpenAI-compatible endpoint).
    """
    from analyzer.copilot_client import chat_with_tools
    from analyzer.oracle_tools import execute_tool_native, get_tools_schema_filtered, SYSTEM_NATIVE_ANALYZE
    import json
    import logging

    log = logging.getLogger("oracleiq")
    model_used = AI_MODEL
    total_usage: dict = {}
    all_parts: list[str] = []

    # Prompt système : custom si configuré, sinon natif par défaut
    from db.store import get_setting
    custom_prompt = get_setting("system_prompt", "")
    system_native = custom_prompt.strip() if custom_prompt and custom_prompt.strip() else SYSTEM_NATIVE_ANALYZE

    # Outils filtrés selon les settings
    tools_schema = get_tools_schema_filtered()

    messages: list[dict] = [{"role": "user", "content": _build_initial_prompt(row)}]
    trace: list[dict] = []  # log des événements pour débogage

    # Sécurité : max 30 appels d'outils au total pour éviter les boucles infinies
    MAX_TOOL_CALLS = 30
    tool_calls_count = 0

    while True:
        text, tool_calls, usage = chat_with_tools(
            messages=messages,
            tools=tools_schema,
            model=model_used,
            max_tokens=_get_max_tokens(),
            system=system_native,
        )
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v

        if text:
            all_parts.append(text)

        # Plus d'appels d'outils ou limite atteinte → l'IA a conclu
        if not tool_calls:
            has_score = text and ("SCORE:" in text or "score:" in text.lower())
            if not has_score and len(all_parts) <= 3:
                # Texte préliminaire sans tool_calls ni analyse — forcer tool_choice=required
                log.warning(f"native: réponse préliminaire sans tools ni SCORE, forcçage tool_choice=required")
                messages.append({"role": "assistant", "content": text or ""})
                messages.append({"role": "user", "content": "Tu dois appeler au moins un outil Oracle pour collecter les informations nécessaires avant de rédiger l'analyse."})
                text, tool_calls, usage2 = chat_with_tools(
                    messages=messages, tools=tools_schema,
                    model=model_used, max_tokens=_get_max_tokens(), system=system_native,
                    tool_choice="required",
                )
                for k, v in (usage2 or {}).items():
                    if isinstance(v, (int, float)):
                        total_usage[k] = total_usage.get(k, 0) + v
                if text:
                    all_parts.append(text)
                if not tool_calls:
                    # Toujours rien — conclusion forcée sur le SQL brut
                    messages.append({"role": "assistant", "content": text or ""})
                    messages.append({"role": "user", "content": "Rédige MAINTENANT l'analyse finale avec SCORE: / SEVERITY: / SUMMARY: basée sur le SQL et le plan fournis."})
                    text3, _, usage3 = chat_with_tools(messages=messages, tools=[], model=model_used, max_tokens=_get_max_tokens(), system=system_native)
                    if text3:
                        all_parts.append(text3)
                    for k, v in (usage3 or {}).items():
                        if isinstance(v, (int, float)):
                            total_usage[k] = total_usage.get(k, 0) + v
                    break
                # tool_calls dispo — continuer la boucle normale
            else:
                break

        if tool_calls_count + len(tool_calls) > MAX_TOOL_CALLS:
            log.warning(f"native: limite de {MAX_TOOL_CALLS} appels d'outils atteinte, forçage de la conclusion")
            # Ajouter réponse partielle et forcer conclusion
            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                        for tc in tool_calls
                    ]
                })
                for tc in tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps({"error": "Limite d'appels atteinte."})
                    })
            messages.append({"role": "user", "content": "Tu as collecté suffisamment d'informations. Rédige MAINTENANT l'analyse finale complète avec SCORE: / SEVERITY: / SUMMARY:"})
            text2, _, usage2 = chat_with_tools(
                messages=messages, tools=[], model=model_used, max_tokens=_get_max_tokens(), system=system_native
            )
            if text2:
                all_parts.append(text2)
            for k, v in (usage2 or {}).items():
                if isinstance(v, (int, float)):
                    total_usage[k] = total_usage.get(k, 0) + v
            break

        # Ajouter la réponse de l'IA avec les tool_calls
        assistant_msg: dict = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    }
                }
                for tc in tool_calls
            ]
        }
        messages.append(assistant_msg)

        # Exécuter chaque outil et ajouter le résultat
        for tc in tool_calls:
            tool_calls_count += 1
            import time as _time
            t0 = _time.monotonic()
            if oracle_conn:
                result = execute_tool_native(oracle_conn, tc["name"], tc["arguments"])
            else:
                result = {"error": "Connexion Oracle non disponible pour cet outil."}
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            has_error = "error" in result
            log.info(f"native tool: {tc['name']}({tc['arguments']}) → {str(result)[:80]}")
            trace.append({
                "t": _time.strftime("%H:%M:%S"),
                "tool": tc["name"],
                "args": tc["arguments"],
                "ok": not has_error,
                "error": result.get("error") if has_error else None,
                "ms": elapsed_ms,
                "rows": len(result) if isinstance(result, list) else (len(result.get("rows", [])) if isinstance(result, dict) and "rows" in result else None),
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, default=str, ensure_ascii=True),
            })

    final_raw = all_parts[-1] if all_parts else ""
    full_raw = "\n\n---\n\n".join(all_parts)
    parsed = parse_ai_response(final_raw)
    return {
        "model": model_used,
        "score": parsed.get("score", 50),
        "severity": parsed.get("severity", "warning"),
        "summary": parsed.get("summary", ""),
        "issues": parsed.get("issues", []),
        "trace": trace,
        "recommendations": parsed.get("recommendations", []),
        "raw": full_raw,
        "usage": total_usage,
    }


def run_analyzer(once: bool = False, batch_size: int = 10):
    from db.store import init_db, report_service
    init_db()
    report_service("analyzer", "starting")
    try:
        _run_analyzer(once=once, batch_size=batch_size)
    except KeyboardInterrupt:
        report_service("analyzer", "stopped")
    except BaseException:
        report_service("analyzer", "error")
        raise
    else:
        report_service("analyzer", "stopped")


def _run_analyzer(once: bool = False, batch_size: int = 10):
    from rich.console import Console
    from rich.progress import track
    console = Console()

    console.rule("[bold magenta]🧠 OracleIQ AI Analyzer[/bold magenta]")
    console.print(f"[dim]Provider: {AI_PROVIDER} | Model: {AI_MODEL}[/dim]\n")

    from db.store import report_service

    while True:
        # Respecter le mode manuel : ne pas analyser automatiquement
        from db.store import get_setting
        if get_setting("analyzer_mode", "manual") == "manual":
            report_service("analyzer", "manual")
            if once:
                break
            time.sleep(5)
            continue

        pending = get_unanalyzed(limit=batch_size)
        if not pending:
            report_service("analyzer", "waiting")
            if once:
                console.print("[dim]Aucune requête en attente d'analyse.[/dim]")
                break
            time.sleep(10)
            continue

        console.print(f"[cyan]{len(pending)} requête(s) à analyser...[/cyan]")

        for row in pending:
            short_sql = row["sql_text"][:80].replace("\n", " ")
            # Double vérification : ne pas relancer si déjà en queue ou déjà analysé
            from db.store import analyzing_queue_add, analyzing_queue_remove, get_conn as _gc2
            _chk = _gc2()
            already_queued  = _chk.execute("SELECT 1 FROM analyzing_queue WHERE query_id=?", (row["id"],)).fetchone()
            already_analyzed = _chk.execute("SELECT 1 FROM queries WHERE id=? AND analyzed=1", (row["id"],)).fetchone()
            _chk.close()
            if already_queued or already_analyzed:
                console.print(f"  [dim]Skip #{row['id']} (déjà en cours ou analysé)[/dim]")
                continue
            console.print(f"  → [dim]{short_sql}...[/dim]")
            try:
                if not analyzing_queue_add(row["id"]):
                    continue
                report_service("analyzer", "analyzing", ttl=1800)
                # Connexion Oracle pour les tools IA
                oracle_conn = None
                try:
                    import oracledb
                    from config import ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD
                    from db.store import get_setting as _gs
                    _dsn  = _gs("oracle_dsn",  ORACLE_DSN)
                    _user = _gs("oracle_user", ORACLE_USER)
                    _pwd  = _gs("oracle_password", ORACLE_PASSWORD)
                    oracle_conn = connect_oracle(user=_user, password=_pwd, dsn=_dsn)
                except Exception as oe:
                    console.print(f"    [yellow]Oracle non disponible pour tools: {oe}[/yellow]")
                try:
                    analysis = analyze_query(row, oracle_conn=oracle_conn)
                    save_analysis(row["id"], analysis)
                    report_service("analyzer", "waiting", success=True)
                finally:
                    analyzing_queue_remove(row["id"])
                    if oracle_conn:
                        try: oracle_conn.close()
                        except: pass

                color = {"ok": "green", "warning": "yellow", "critical": "red"}.get(
                    analysis["severity"], "white"
                )
                console.print(
                    f"    [{color}]Score: {analysis['score']}/100 "
                    f"| {analysis['severity'].upper()}[/{color}] "
                    f"— {analysis['summary'][:100]}"
                )
            except Exception as e:
                report_service("analyzer", "error")
                from db.store import save_analysis_error
                save_analysis_error(row["id"], "Analyse echouee. Consultez les journaux puis relancez.")
                console.print(f"    [red]Erreur analyse: {e}[/red]")

            time.sleep(0.5)  # Rate limit

        if once:
            break
        time.sleep(15)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Analyser une fois puis quitter")
    parser.add_argument("--batch", type=int, default=10)
    args = parser.parse_args()
    run_analyzer(once=args.once, batch_size=args.batch)
