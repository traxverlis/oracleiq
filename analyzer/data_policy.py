"""Outbound AI policy. Character budgets are hard limits, not tokenizer estimates.

Masking is deliberately conservative, not DLP: identifiers, free prose and database
metadata may still be sensitive. Raw values require the exact setting string "true".
"""
import json
import re
import time

MAX_INPUT_CHARS = 400_000
MAX_HISTORY_MESSAGES = 80
MAX_TOOL_RESULT_CHARS = 48_000
MAX_RESPONSE_CHARS = 48_000
MAX_SQL_CHARS = 40_000
MAX_PLAN_CHARS = 40_000
MAX_SYSTEM_CHARS = 20_000
MAX_TOOL_CALLS = 30
MAX_TURNS = 32
ANALYSIS_TIMEOUT_SECONDS = 480
MASK = "[REDACTED]"
LIMITS = {
    "input_chars": MAX_INPUT_CHARS, "history_messages": MAX_HISTORY_MESSAGES,
    "tool_result_chars": MAX_TOOL_RESULT_CHARS, "response_chars_after_reception": MAX_RESPONSE_CHARS,
    "sql_chars": MAX_SQL_CHARS,
    "plan_chars": MAX_PLAN_CHARS, "system_chars": MAX_SYSTEM_CHARS,
    "tool_calls": MAX_TOOL_CALLS, "turns": MAX_TURNS,
    "deadline_seconds": ANALYSIS_TIMEOUT_SECONDS,
    "output_tokens": "advisory only: SDK does not guarantee a hard token cap",
}
WARNINGS = [
    "Masquage conservateur, pas une DLP parfaite : noms d'objets, metadonnees et texte libre peuvent rester sensibles.",
    "Les valeurs masquees limitent le diagnostic de selectivite et des binds.",
    "max_tokens est indicatif ; le SDK ne garantit pas une limite stricte de sortie.",
]


class AIPolicyError(ValueError):
    pass


class AnalysisCancelled(AIPolicyError):
    pass


def require_copilot():
    from config import AI_PROVIDER
    if AI_PROVIDER != "github-copilot":
        raise AIPolicyError("AI_PROVIDER non pris en charge : ODIN accepte uniquement github-copilot.")


def raw_values_enabled():
    from db.store import get_setting
    return get_setting("ai_send_raw_values", "false") == "true"


def check_budget(deadline=None, cancel=None):
    cancelled = cancel and (cancel() if callable(cancel) else cancel.is_set())
    if cancelled:
        raise AnalysisCancelled("Analyse annulee.")
    remaining = ANALYSIS_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise AIPolicyError("Delai global de l'analyse depasse.")
    return remaining


def mask_sql(text, mask_numbers=True, *, prose=False):
    """Small Oracle lexer: comments, ordinary/N strings, q/nq literals and numbers.

    Quoted identifiers are retained. Unterminated literals/comments consume the rest
    rather than disclosing truncated values. This is not a SQL authorization parser.
    """
    result, i = [], 0
    while i < len(text):
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            result.append("-- " + MASK)
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            result.append("/* " + MASK + " */")
            continue
        boundary = i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$#")
        q = re.match(r"(?i)(?:n?q)'(.)", text[i:]) if boundary else None
        if q:
            opening = q.group(1)
            closing = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opening, opening)
            end = text.find(closing + "'", i + q.end())
            i = len(text) if end < 0 else end + 2
            result.append("'" + MASK + "'")
            continue
        if text[i] == "'":
            end, closed = i + 1, False
            while end < len(text):
                if text[end] == "'":
                    if end + 1 < len(text) and text[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    closed = True
                    break
                end += 1
            window = text[max(0, i - 16):i]
            literal = not prose or boundary or re.search(
                r"(?i)(?:^|[^\w$#])(?:DATE|TIMESTAMP|INTERVAL|SELECT|WHERE|THEN|ELSE|WHEN|AND|OR|ON|AS|BY|RETURNING|VALUES)$",
                window)
            # French elisions (n'est, N'invente) are not national literals: require N'...' closed before a non-letter.
            if not literal and closed and re.search(r"(?:^|[^\w$#])N$", window, re.I) and \
                    not (end < len(text) and (text[end].isalnum() or text[end] in "_$#")):
                literal = True
            if literal:
                result.append("'" + MASK + "'")
                i = end
                continue
        if text[i] == '"':
            end = i + 1
            while end < len(text):
                if text[end] == '"':
                    if end + 1 < len(text) and text[end + 1] == '"':
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            result.append(text[i:end])
            i = end
            continue
        if mask_numbers and (text[i].isdigit() or (text[i] == "." and i + 1 < len(text) and text[i + 1].isdigit())):
            previous = text[i - 1] if i else ""
            number = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[fFdD]?", text[i:])
            if number and (not previous or not (previous.isalnum() or previous in "_$#:")):
                result.append("0")
                i += number.end()
                continue
        result.append(text[i])
        i += 1
    return "".join(result)


def sanitize_text(text, raw=False):
    # Structured tool results are sanitized before serialization, including binds.
    try:
        value = json.loads(text)
        if isinstance(value, (dict, list)):
            return json.dumps(sanitize_data(value, raw=raw), ensure_ascii=False, default=str)
    except (ValueError, TypeError):
        pass
    if raw:
        return text
    text = re.sub(r"(?ims)^\s*Peeked\s+Binds.*?(?=^(?:Predicate Information|Outline Data|Note|Query Block Name)|\Z)",
                  "[bind details redacted]\n", text)
    text = re.sub(r"(?im)(\b(?:bind_?value|value_string|value)\s*[=:]\s*)[^\n,]*",
                  r"\1" + MASK, text)
    text = mask_sql(text, mask_numbers=False, prose=True)
    # Keep full multiline SQL segments together: q-quotes/comments may span lines.
    sections = re.split(r"(?m)(^===.*?===\s*$)", text)
    output = []
    for section in sections:
        has_sql = bool(re.search(r"\b(SELECT|WITH|INSERT|UPDATE|DELETE|MERGE|WHERE|HAVING|VALUES)\b|\b(?:filter|access)\s*\(",
                                 section, re.I))
        direct_sql = bool(re.match(r"\s*(?:SELECT|WITH|INSERT|UPDATE|DELETE|MERGE)\b", section, re.I))
        if "Predicate Information" in section:
            lines = section.splitlines(keepends=True)
            start = next(i for i, line in enumerate(lines) if "Predicate Information" in line)
            output.append(sanitize_text("".join(lines[:start])) +
                          mask_sql("".join(lines[start:])))
        else:
            plan_header = re.search(r"(?m)^\s*\|\s*Id\s*\|\s*Operation\b", section)
            if plan_header:
                output.append(mask_sql(section[:plan_header.start()], mask_numbers=has_sql, prose=not direct_sql) +
                              mask_sql(section[plan_header.start():], mask_numbers=False, prose=True))
            else:
                output.append(mask_sql(section, mask_numbers=has_sql, prose=not direct_sql))
    return "".join(output)


_DRIVER_DETAILS = re.compile(r"(?i)\b(?:ORA|PLS|TNS|DPI|DPY|SP2)-\d+|traceback|exception")


def sanitize_data(value, raw=False, key=""):
    lowered = key.lower()
    if lowered == "error" and value:
        # ODIN's own messages guide the model; Oracle/driver details stay local.
        if not isinstance(value, str) or _DRIVER_DETAILS.search(value):
            return "Diagnostic indisponible (details Oracle masques)."
        return value if raw else sanitize_text(value)
    if raw:
        if isinstance(value, dict):
            return {str(k): sanitize_data(v, raw=True, key=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [sanitize_data(v, raw=True) for v in value]
        return value
    if lowered in {"value", "value_string", "bind_value", "peeked_binds", "password",
                   "token", "secret", "low_value", "high_value", "sample_values"}:
        return MASK
    if lowered == "rows" and isinstance(value, list):
        return [{str(k): MASK for k in row} if isinstance(row, dict) else MASK for row in value]
    if lowered in {"binds", "bind_values"} and isinstance(value, dict):
        return {str(k): MASK for k in value}
    if isinstance(value, dict):
        return {str(k): sanitize_data(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_data(v) for v in value]
    if isinstance(value, str):
        if lowered in {"sql", "sql_text", "sql_fulltext", "sql_preview", "search_condition",
                       "data_default", "default_value", "predicate", "filter_predicates",
                       "access_predicates", "definition", "query"}:
            return mask_sql(value)
        return sanitize_text(value)
    return value


_PLAN_GRID_HEADER = re.compile(r"^\s*\|\s*Id\s*\|\s*Operation\b")


def compact_plan(text, omit_sql=False):
    """Remove DBMS_XPLAN column padding (about half the size); operation indentation is kept."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("Plan hash value")), None)
    if omit_sql and start and lines[0].startswith("SQL_ID"):
        lines = [lines[0], "[texte SQL omis : identique au SQL de la requete analysee]", ""] + lines[start:]
    output, operation = [], None
    for line in lines:
        line = line.rstrip()
        if len(line) > 1 and line.startswith("|") and line.endswith("|"):
            cells = line[1:-1].split("|")
            if _PLAN_GRID_HEADER.match(line):
                operation = next(i for i, cell in enumerate(cells) if cell.strip() == "Operation")
            line = "|" + "|".join(cell.rstrip() if i == operation else cell.strip()
                                   for i, cell in enumerate(cells)) + "|"
        elif len(line) > 10 and set(line.strip()) == {"-"}:
            line = "-" * 10
        output.append(line)
    return "\n".join(output)


def _shorten(value, chars, items):
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + f"\n[... {len(value) - chars} caracteres tronques]"
    if isinstance(value, dict):
        return {k: _shorten(v, chars, items) for k, v in value.items()}
    if isinstance(value, list):
        kept = [_shorten(v, chars, items) for v in value[:items]]
        return kept + ([f"[... {len(value) - items} elements tronques]"] if len(value) > items else [])
    return value


def tool_payload(result):
    """Serialize an already sanitized tool result within the per-result budget."""
    full = payload = json.dumps(result, default=str, ensure_ascii=False)
    chars, items = MAX_TOOL_RESULT_CHARS, 1000
    # Shorten the longest fields first: a blind prefix of the JSON is double-escaped and hard to read.
    while len(payload) > MAX_TOOL_RESULT_CHARS and chars > 500:
        chars, items = int(chars * 0.8), max(10, int(items * 0.8))
        payload = json.dumps({
            "truncated": True, "original_chars": len(full),
            "warning": "Resultat tronque : champs longs raccourcis et signales par un marqueur [...]. "
                       "Cibler un objet, une partie ou une periode plus restreint si la suite est necessaire.",
            "result": _shorten(result, chars, items)}, default=str, ensure_ascii=False)
    cut = MAX_TOOL_RESULT_CHARS - 600
    while len(payload) > MAX_TOOL_RESULT_CHARS:
        # Last resort (many small fields). Escaping can grow the partial text, hence the shrinking loop.
        payload = json.dumps({
            "truncated": True, "original_chars": len(full),
            "warning": "Resultat tronque : cibler un objet ou une periode plus restreint si la suite est necessaire.",
            "partial": full[:cut]}, ensure_ascii=False)
        cut = int(cut * 0.8)
    return payload


def prepare_messages(messages, system=None, raw=None):
    """Single outbound policy used by every chat route, independent of callers."""
    require_copilot()
    raw = raw_values_enabled() if raw is None else raw
    if len(messages) > MAX_HISTORY_MESSAGES:
        raise AIPolicyError("Budget historique IA depasse.")
    if len(system or "") > MAX_SYSTEM_CHARS:
        raise AIPolicyError("Budget prompt systeme IA depasse.")
    if len(json.dumps(messages, default=str)) + len(system or "") > MAX_INPUT_CHARS:
        raise AIPolicyError("Budget entree IA depasse.")
    clean = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content", ""), (str, type(None))):
            raise AIPolicyError("Format message IA non pris en charge.")
        if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name"}:
            raise AIPolicyError("Champs message IA non pris en charge.")
        entry = dict(message)
        if isinstance(entry.get("content"), str):
            entry["content"] = sanitize_text(entry["content"], raw)
        if "tool_calls" in entry:
            entry["tool_calls"] = sanitize_data(entry["tool_calls"], raw)
        if entry.get("role") == "tool" and len(entry.get("content") or "") > MAX_TOOL_RESULT_CHARS:
            raise AIPolicyError("Budget resultat outil IA depasse.")
        clean.append(entry)
    clean_system = sanitize_text(system, raw) if system else system
    if len(json.dumps(clean, ensure_ascii=False)) + len(clean_system or "") > MAX_INPUT_CHARS:
        raise AIPolicyError("Budget entree IA depasse apres masquage.")
    return clean, clean_system


def prepare_request(messages, system=None, tools=None, raw=None):
    """Bound the full SDK payload, reserving space for transport instructions."""
    require_copilot()
    raw = raw_values_enabled() if raw is None else raw
    messages, system = prepare_messages(messages, system, raw=raw)
    tools = sanitize_data(tools or [], raw=raw)
    if len(json.dumps(messages, ensure_ascii=False)) + len(system or "") + len(json.dumps(tools)) + 1000 > MAX_INPUT_CHARS:
        raise AIPolicyError("Budget entree IA depasse (schemas outils inclus).")
    return messages, system, tools
