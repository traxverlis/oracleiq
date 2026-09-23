"""
analyzer/ai_analyzer.py
Prend les requêtes non analysées, envoie requête + plan d'exécution à l'IA,
stocke les recommandations.
"""
import sys
import json
import logging
import time
import re
from typing import Literal
from pydantic import BaseModel, Field, ValidationError

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from config import AI_PROVIDER, AI_MODEL, AI_MAX_TOKENS
from db.store import get_unanalyzed, save_analysis
from collector.connection import connect_oracle
from analyzer.copilot_client import chat as copilot_chat
from analyzer.data_policy import (
    AIPolicyError, LIMITS, WARNINGS, MAX_SQL_CHARS, MAX_PLAN_CHARS, MAX_INPUT_CHARS,
    MAX_TOOL_RESULT_CHARS, MAX_RESPONSE_CHARS, MAX_TOOL_CALLS, MAX_TURNS, ANALYSIS_TIMEOUT_SECONDS,
    check_budget, prepare_messages, prepare_request, raw_values_enabled, require_copilot, sanitize_data,
    tool_payload, compact_plan,
)

# ─────────────────────────────────────────────
# Prompt système
# ─────────────────────────────────────────────
from analyzer.oracle_tools import TOOLS_DESCRIPTION

_log = logging.getLogger(__name__)

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

=== IDENTIFICATION ===
- SQL_ID : {sql_id}
- Child number : {child_number}
- PLAN_HASH_VALUE : {plan_hash_value}
- Schéma de parsing : {schema}
- Module : {module}
- Changement de plan détecté : {plan_change}

=== REQUÊTE SQL ===
{sql}

=== STATISTIQUES D'EXÉCUTION ===
- Exécutions : {executions}
- Temps moyen : {elapsed_ms_avg} ms
- CPU moyen : {cpu_ms_avg} ms
- Pic de moyenne observe : {elapsed_ms_max} ms (pas un maximum par execution)
- Buffer gets moy : {buffer_gets_avg}
- Disk reads moy : {disk_reads_avg}
- Lignes retournées moy : {rows_avg}

=== PLAN D'EXÉCUTION ===
{plan}
"""


# ─────────────────────────────────────────────
# Clients IA
# ─────────────────────────────────────────────
class _TemplateValues(dict):
    def __missing__(self, key):
        return "inconnu"


def call_copilot(sql_text: str, context: dict) -> dict:
    """Appel via GitHub Copilot (Claude Sonnet/Opus/Haiku)."""
    prompt = USER_TEMPLATE.format_map(_TemplateValues(
        context, sql=sql_text, plan=context.get("plan_text") or "Non disponible"))
    raw, usage = copilot_chat(
        messages=[{"role": "user", "content": prompt}],
        model=AI_MODEL,
        max_tokens=_get_max_tokens(),
        system=SYSTEM_PROMPT,
    )
    return {"raw": raw, "model": AI_MODEL, "usage": usage}


def call_openai(sql_text: str, context: dict) -> dict:
    raise AIPolicyError("Fournisseur non pris en charge : ODIN accepte uniquement github-copilot.")


def call_anthropic(sql_text: str, context: dict) -> dict:
    raise AIPolicyError("Fournisseur non pris en charge : ODIN accepte uniquement github-copilot.")


class AnalysisResult(BaseModel):
    score: int = Field(ge=0, le=100, strict=True)
    severity: Literal["ok", "warning", "critical"]
    summary: str = Field(min_length=1)
    issues: list[dict] = Field(default_factory=list)
    recommendations: list[dict] = Field(default_factory=list)


def validate_analysis(data):
    try:
        result = AnalysisResult.model_validate(data)
    except ValidationError:
        raise ValueError("Format d'analyse IA invalide.") from None
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

    # Format texte structuré : SCORE: XX / SEVERITY: xxx / SUMMARY: ... (décorations Markdown tolérées)
    prefix = r'(?:^|[/|;][ \t]*)[ \t>#*_-]*'
    sep = r'[*_ \t]*:[*_ \t]*'
    score_m = re.search(prefix + r'SCORE' + sep + r'(\d{1,3})(?![\d.,])', raw, re.IGNORECASE | re.MULTILINE)
    sev_m = re.search(prefix + r'SEVERITY' + sep + r'(ok|warning|critical)\b', raw, re.IGNORECASE | re.MULTILINE)
    sum_m = re.search(prefix + r'SUMMARY' + sep + r'([^\n]+)', raw, re.IGNORECASE | re.MULTILINE)
    if not (score_m and sum_m):
        raise ValueError("Reponse IA incomplete : SCORE et SUMMARY requis")
    score = int(score_m.group(1))
    # Models sometimes localize the label ("MOYENNE"); the documented score bands are authoritative.
    severity = sev_m.group(1).lower() if sev_m else ("ok" if score >= 80 else "warning" if score >= 50 else "critical")
    summary = sum_m.group(1).strip().strip("*_").strip()[:300]

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
    from db.store import get_setting
    from collector.connection import is_execution_plan_available
    plan_text = row.get("plan_text", "") or ""
    plan_available = is_execution_plan_available(plan_text)
    if plan_available:
        plan_text = compact_plan(plan_text, omit_sql=True)
    if len(row.get("sql_text") or "") > MAX_SQL_CHARS:
        raise AIPolicyError("Budget SQL IA depasse ; aucune transmission effectuee.")
    try:
        plan_limit = max(100, min(int(get_setting("plan_truncate", str(MAX_PLAN_CHARS))), MAX_PLAN_CHARS))
    except (ValueError, TypeError):
        plan_limit = MAX_PLAN_CHARS
    if len(plan_text) > plan_limit:
        plan_text = plan_text[:plan_limit] + f"\n[plan tronqué : {len(plan_text) - plan_limit} caractères omis]"
    sql_id = row.get("sql_id", "")
    if not plan_available:
        plan_section = f"""Non disponible.
⚠️ Aucun plan d'exécution valide en base. Un EXPLAIN PLAN est seulement estime.
Utilise explain_plan avec sql_id={sql_id} et child_number={row.get('child_number', 'inconnu')} si autorise.
Il necessite les privileges de parsing et une PLAN_TABLE ; il ne mesure pas une execution."""
    else:
        plan_section = plan_text
    return USER_TEMPLATE.format(
        sql=row["sql_text"],
        plan=plan_section,
        sql_id=sql_id or "inconnu",
        child_number=row.get("child_number", "inconnu"),
        plan_hash_value=row.get("plan_hash_value") or "inconnu",
        module=row.get("module") or "inconnu",
        plan_change="oui" if row.get("plan_change_detected") else "non",
        executions=row.get("executions", 1),
        elapsed_ms_avg=row.get("elapsed_ms_avg", 0),
        cpu_ms_avg=row.get("cpu_ms_avg", 0),
        elapsed_ms_max=row.get("elapsed_ms_max", 0),
        buffer_gets_avg=row.get("buffer_gets_avg", 0),
        disk_reads_avg=row.get("disk_reads_avg", 0),
        rows_avg=row.get("rows_avg", 0),
        schema=row.get("schema_name", "UNKNOWN"),
    )


def build_ai_preview(row: dict) -> dict:
    """Initial masked user/system/tool-schema payload, never future tool results."""
    from db.store import get_setting
    from analyzer.oracle_tools import SYSTEM_NATIVE_ANALYZE, get_tools_schema_filtered
    require_copilot()
    raw = raw_values_enabled()
    custom_system = get_setting("system_prompt", "")
    system = custom_system.strip() if custom_system and custom_system.strip() else SYSTEM_NATIVE_ANALYZE
    messages, system, tools = prepare_request(
        [{"role": "user", "content": _build_initial_prompt(row)}],
        system, get_tools_schema_filtered(), raw=raw)
    context = {"system": system, "prompt": messages[0]["content"], "tools": tools}
    return {"provider": "github-copilot", "model": get_setting("ai_model", AI_MODEL),
            "send_raw_values": raw, "masking_applied": not raw,
            "ai_send_raw_values": raw, "context": context,
            "prompt": messages[0]["content"], "system": system, "system_prompt": system, "tools": tools,
            "limits": dict(LIMITS), "warnings": list(WARNINGS) + [
                "Apercu initial seulement : les resultats futurs des outils ne sont pas inclus. "
                "Le SDK ajoute des instructions de transport et un objectif indicatif de tokens."]}


def analyze_query(row: dict, oracle_conn=None, **kwargs) -> dict:
    """Analyse une requête avec les outils natifs, quel que soit l'ancien réglage."""
    return analyze_query_native(row, oracle_conn=oracle_conn, **kwargs)


def _get_max_tokens() -> int:
    """Lit ai_max_tokens depuis les settings DB (dynamique) avec fallback sur config.py."""
    from db.store import get_setting
    try:
        return max(1, min(int(get_setting("ai_max_tokens", str(AI_MAX_TOKENS))), 32000))
    except (ValueError, TypeError):
        return AI_MAX_TOKENS


def analyze_query_native(row: dict, oracle_conn=None, on_event=None, cancel=None) -> dict:
    """Shared bounded orchestration for automatic, HTTP and SSE analysis.

    Events are dictionaries: thinking(text), tool_start(tool,args),
    tool_result(tool,result,ok,ms), complete(result). Callback data is masked by
    default; cancellation is checked between every turn/tool and after SDK calls.
    """
    from analyzer.copilot_client import chat_with_tools
    from analyzer.oracle_tools import execute_tool_native
    require_copilot()
    if oracle_conn is not None:
        from collector.connection import assert_query_source
        assert_query_source(row, oracle_conn)
    deadline = time.monotonic() + ANALYSIS_TIMEOUT_SECONDS
    total_usage: dict = {}
    all_parts: list[str] = []
    preview = build_ai_preview(row)
    model_used = preview["model"]
    system_native = preview["system"]
    tools_schema = preview["tools"]
    messages = [{"role": "user", "content": preview["prompt"]}]
    trace = []
    tool_calls_count = 0
    force_final = False

    def emit(event):
        if on_event:
            on_event(sanitize_data(event, raw=raw_values_enabled()))

    for turn in range(MAX_TURNS):
        check_budget(deadline, cancel)
        messages, safe_system = prepare_messages(messages, system_native)
        text, tool_calls, usage = chat_with_tools(
            messages=messages, tools=[] if force_final else tools_schema, model=model_used,
            max_tokens=_get_max_tokens(), system=safe_system,
            tool_choice="none" if force_final else "auto", deadline=deadline, cancel=cancel,
        )
        check_budget(deadline, cancel)
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v
        if text:
            if len(text) > MAX_RESPONSE_CHARS:
                raise AIPolicyError("Budget reponse IA depasse.")
            all_parts.append(text)
            emit({"type": "thinking", "text": text})
        if not tool_calls:
            try:
                parsed = parse_ai_response(text or "")
            except (ValueError, ValidationError):
                header = [line.strip()[:120] for line in (text or "").splitlines() if line.strip()][:3]
                _log.warning("Reponse IA non conforme (%d caracteres, final=%s) ; debut : %r",
                             len(text or ""), force_final, header)
                if force_final:
                    raise ValueError("Reponse IA finale invalide.") from None
                messages.append({"role": "assistant", "content": text or ""})
                messages.append({"role": "user", "content":
                                 "Redige l'analyse finale en commencant exactement par ces trois lignes :\n"
                                 "SCORE: <entier 0-100>\nSEVERITY: <ok|warning|critical, en anglais>\n"
                                 "SUMMARY: <une ligne>\n"
                                 "Indique explicitement les limites des donnees disponibles."})
                force_final = True
                continue
            break
        if force_final:
            raise AIPolicyError("Appel outil inattendu pendant la conclusion.")
        if tool_calls_count + len(tool_calls) > MAX_TOOL_CALLS:
            raise AIPolicyError("Budget appels outils IA depasse.")
        messages.append({"role": "assistant", "content": text, "tool_calls": [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
            for tc in tool_calls]})
        for tc in tool_calls:
            check_budget(deadline, cancel)
            tool_calls_count += 1
            t0 = time.monotonic()
            emit({"type": "tool_start", "tool": tc["name"], "args": tc["arguments"]})
            if oracle_conn:
                kwargs = dict(tc["arguments"])
                if tc["name"] in {"explain_plan", "bind_captures"} and kwargs.get("sql_id") == row.get("sql_id"):
                    kwargs["child_number"] = row.get("child_number")
                result = execute_tool_native(oracle_conn, tc["name"], kwargs,
                                             deadline=deadline, cancel=cancel)
            else:
                result = {"error": "Connexion Oracle non disponible pour cet outil."}
            check_budget(deadline, cancel)
            result = sanitize_data(result, raw=raw_values_enabled())
            payload = tool_payload(result)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            has_error = "error" in result
            event = {"type": "tool_result", "tool": tc["name"], "args": tc["arguments"],
                     "result": result, "ok": not has_error, "ms": elapsed_ms}
            emit(event)
            trace.append(sanitize_data({
                "t": time.strftime("%H:%M:%S"), "tool": tc["name"], "args": tc["arguments"],
                "ok": not has_error, "error": "Outil indisponible" if has_error else None,
                "ms": elapsed_ms}, raw=raw_values_enabled()))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": payload})
        if tool_calls_count == MAX_TOOL_CALLS:
            force_final = True
            messages.append({"role": "user", "content": "Limite outils atteinte. Conclus en indiquant cette limite."})
        else:
            used = len(json.dumps(messages, default=str, ensure_ascii=False)) * 100 // MAX_INPUT_CHARS
            messages.append({"role": "user", "content": (
                f"Budget ODIN restant : {MAX_TOOL_CALLS - tool_calls_count} appels d'outils, "
                f"environ {int(deadline - time.monotonic())} s, contexte utilise {used} %. "
                + ("Contexte presque plein : conclus maintenant." if used >= 75 else
                   "Appelle en un seul tour les outils encore necessaires, puis conclus."))})
    else:
        raise AIPolicyError("Budget tours IA depasse.")
    result = {
        "model": model_used,
        "score": parsed.get("score", 50),
        "severity": parsed.get("severity", "warning"),
        "summary": parsed.get("summary", ""),
        "issues": parsed.get("issues", []),
        "trace": trace,
        "recommendations": parsed.get("recommendations", []),
        "raw": "\n\n---\n\n".join(all_parts),
        "usage": total_usage,
    }
    emit({"type": "complete", "result": result})
    return result


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
    require_copilot()
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
            # Double vérification : ne pas relancer si déjà en queue ou déjà analysé
            from db.store import analyzing_queue_add, analyzing_queue_remove, get_conn as _gc2
            _chk = _gc2()
            already_queued  = _chk.execute("SELECT 1 FROM analyzing_queue WHERE query_id=?", (row["id"],)).fetchone()
            already_analyzed = _chk.execute("SELECT 1 FROM queries WHERE id=? AND analyzed=1", (row["id"],)).fetchone()
            _chk.close()
            if already_queued or already_analyzed:
                console.print(f"  [dim]Skip #{row['id']} (déjà en cours ou analysé)[/dim]")
                continue
            console.print(f"  → [dim]Requete #{row['id']}[/dim]")
            oracle_conn = None
            claimed = False
            try:
                if not analyzing_queue_add(row["id"]):
                    continue
                claimed = True
                report_service("analyzer", "analyzing", ttl=1800)
                from db.store import get_query_detail
                from collector.connection import get_oracle_settings, assert_query_source
                detail = get_query_detail(row["id"])
                if not detail.get("query"):
                    raise ValueError("Requete disparue avant analyse.")
                row = dict(detail["query"])
                row["plan_text"] = (detail.get("plan") or {}).get("plan_text")
                settings = get_oracle_settings()
                oracle_conn = connect_oracle(user=settings["oracle_user"],
                                             password=settings["oracle_password"],
                                             dsn=settings["oracle_dsn"])
                assert_query_source(row, oracle_conn)
                analysis = analyze_query(row, oracle_conn=oracle_conn)
                current = save_analysis(row["id"], analysis, expected_plan_id=row.get("plan_id"))
                if current is False:
                    report_service("analyzer", "waiting")
                    console.print("    [yellow]Analyse archivee mais devenue obsolete ; reanalyse necessaire.[/yellow]")
                    continue
                report_service("analyzer", "waiting", success=True)

                color = {"ok": "green", "warning": "yellow", "critical": "red"}.get(
                    analysis["severity"], "white"
                )
                console.print(
                    f"    [{color}]Score: {analysis['score']}/100 "
                    f"| {analysis['severity'].upper()}[/{color}] "
                    f"— {analysis['summary'][:100]}"
                )
            except Exception:
                from db.store import save_analysis_error
                current_error = save_analysis_error(
                    row["id"], "Analyse echouee. Consultez les journaux puis relancez.",
                    expected_plan_id=row.get("plan_id"))
                if current_error is False:
                    report_service("analyzer", "waiting")
                    console.print("    [yellow]Erreur d'analyse obsolete ignoree ; reanalyse necessaire.[/yellow]")
                else:
                    report_service("analyzer", "error")
                    console.print("    [red]Analyse echouee ; details sensibles non affiches.[/red]")
            finally:
                if claimed:
                    analyzing_queue_remove(row["id"])
                if oracle_conn:
                    try:
                        oracle_conn.close()
                    except Exception:
                        pass

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
