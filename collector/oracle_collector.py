"""
collector/oracle_collector.py
Polle V$SQL toutes les N secondes et capture les requêtes nouvelles/modifiées.
Récupère aussi le plan d'exécution via DBMS_XPLAN.
"""
import sys
import time
import hashlib
import logging
import re
from contextlib import closing
from datetime import datetime, timezone
from rich.console import Console

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from config import (
    POLL_INTERVAL_SEC, MIN_ELAPSED_MS, IGNORED_SCHEMAS, IGNORE_SYS_QUERIES
)
from db.store import init_db, upsert_query, save_plan, save_bind_values
from collector.connection import (
    assert_query_source, connect_oracle, get_oracle_settings,
    is_execution_plan_available, oracle_source_id,
)

console = Console()
logger = logging.getLogger(__name__)
PLAN_REFRESH_SECONDS = 300
BIND_REFRESH_SECONDS = 60
CAPTURE_RETRY_SECONDS = 30


def log_collector_error():
    """Logging must never prevent the recovery loop, even if a handler fails."""
    try:
        logger.exception("Collector failure; retrying")
    except Exception:
        pass


def close_connection(connection):
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass

# Requêtes Oracle à ignorer (internes OracleIQ lui-même)
SELF_MARKERS = ["V$SQL", "DBMS_XPLAN", "EXPLAIN PLAN", "oracleiq", "/* DS_SVC */", "/* SQL Analyze", "/* analyse(", "oracleiq"]

# Modules Oracle à ignorer (source = OracleIQ lui-même ou outils internes)
IGNORED_MODULES = ["oracleiq", ".venv"]


def has_literal_values(sql: str) -> bool:
    """Détecte si le SQL contient des valeurs littérales hardcodées dans WHERE/JOIN/HAVING."""
    if not sql:
        return False
    sql_up = sql.upper().strip()
    # Exclure les DDL
    for ddl in ('CREATE ', 'DROP ', 'ALTER ', 'TRUNCATE ', 'COMMENT '):
        if sql_up.startswith(ddl):
            return False
    # Chercher les patterns suspects en dehors des bind vars
    # = 'texte' ou = 123 ou LIKE 'xxx' ou IN (1,2,...)
    patterns = [
        r"=\s*'[^']+'",           # = 'string'
        r"=\s*\d+",               # = 123
        r"LIKE\s*'[^']+'",        # LIKE 'val%'
        r"IN\s*\([\d\s,]+\)",    # IN (1,2,3)
        r"IN\s*\('[^']*'[^)]*\)", # IN ('a','b')
    ]
    # Exclure les NULL et les bind vars (:param)
    combined = re.compile('|'.join(patterns), re.IGNORECASE)
    matches = combined.findall(sql)
    # Filter out bind-var matches (contains :)
    real = [m for m in matches if ':' not in m]
    return len(real) > 0



def normalize_sql(sql: str) -> str:
    """Normalise le SQL pour le hachage (ignore espaces, casse, littéraux)."""
    sql = sql.upper().strip()
    # Remplace les littéraux numériques et strings
    sql = re.sub(r"'[^']*'", "'?'", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    # Normalise les espaces
    sql = re.sub(r"\s+", " ", sql)
    return sql


def sql_hash(sql: str) -> str:
    return hashlib.md5(normalize_sql(sql).encode()).hexdigest()


def is_ignored(sql: str, schema: str, module: str = "") -> bool:
    if IGNORE_SYS_QUERIES and schema in IGNORED_SCHEMAS:
        return True
    # Exclure les requêtes générées par OracleIQ lui-même
    mod = (module or "").lower()
    for m in IGNORED_MODULES:
        if m in mod:
            return True
    sql_up = sql.upper()
    for marker in SELF_MARKERS:
        if marker.upper() in sql_up:
            return True
    return False


def fetch_bind_values(conn, sql_id: str, child_number=None) -> list:
    """Récupère les bind variables depuis V$SQL_BIND_CAPTURE pour un sql_id."""
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                b.name           AS bind_name,
                b.position       AS position,
                b.datatype_string AS datatype,
                b.value_string   AS value,
                TO_CHAR(b.last_captured, 'YYYY-MM-DD HH24:MI:SS') AS last_captured
            FROM v$sql_bind_capture b
            WHERE b.sql_id = :sql_id
              AND (:child_no IS NULL OR b.child_number = :child_no)
            ORDER BY b.child_number DESC, b.position ASC
        """, sql_id=sql_id, child_no=child_number)
        cols = [d[0].lower() for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        # Dédupliquer par nom (garder valeur la plus récente non-NULL en priorité)
        seen = {}
        for r in rows:
            name = r.get("bind_name") or ""
            if not name:
                continue
            if name not in seen or (r.get("value") and not seen[name].get("value")):
                seen[name] = r
        return list(seen.values())
    except Exception:
        return []
    finally:
        close_connection(cursor)


def get_execution_plan(conn, sql_id: str, child_number: int = 0) -> str:
    """Récupère le plan d'exécution depuis V$SQL_PLAN via DBMS_XPLAN."""
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM TABLE(
                DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, :child_no, 'ALLSTATS LAST +PEEKED_BINDS')
            )
        """, sql_id=sql_id, child_no=child_number)
        lines = []
        for row in cursor.fetchall():
            val = row[0]
            # CLOB Oracle : appeler read() si nécessaire
            if hasattr(val, 'read'):
                val = val.read()
            if val:
                lines.append(str(val))
        plan = "\n".join(lines)
        return plan if is_execution_plan_available(plan) else ""
    except Exception:
        return ""
    finally:
        close_connection(cursor)


def get_full_sql_text(conn, sql_id: str) -> str:
    """Récupère le texte SQL complet. Tente V$SQL d'abord, puis DBA_HIST_SQLTEXT (AWR) en fallback."""
    for query in (
        "SELECT sql_fulltext FROM v$sql WHERE sql_id=:sid AND ROWNUM=1",
        "SELECT sql_text FROM dba_hist_sqltext WHERE sql_id=:sid AND ROWNUM=1",
    ):
        try:
            with closing(conn.cursor()) as cur:
                cur.prefetchrows = 0
                cur.execute(query, sid=sql_id)
                row = cur.fetchone()
                if row and row[0] is not None:
                    val = row[0]
                    text = val.read() if hasattr(val, 'read') else str(val)
                    if text and len(text) > 10:
                        return text
        except Exception:
            pass
    return ""


def poll_vsql(conn) -> list[dict]:
    """Lit V$SQL et retourne les requêtes à traiter."""
    source_id = oracle_source_id(conn)
    assert_query_source({"source_id": source_id}, conn)
    with closing(conn.cursor()) as cursor:
        cursor.execute("""
        SELECT
            sql_id,
            sql_text,
            parsing_schema_name,
            module,
            executions,
            ROUND(elapsed_time / GREATEST(executions, 1) / 1000, 2) AS elapsed_ms_avg,
            ROUND(elapsed_time / 1000, 2)                            AS elapsed_ms_total,
            ROUND(elapsed_time / GREATEST(executions, 1) / 1000, 2) AS elapsed_ms_max,
            ROUND(cpu_time / GREATEST(executions, 1) / 1000, 2)     AS cpu_ms_avg,
            ROUND(buffer_gets / GREATEST(executions, 1), 0)         AS buffer_gets_avg,
            ROUND(disk_reads / GREATEST(executions, 1), 0)          AS disk_reads_avg,
            ROUND(rows_processed / GREATEST(executions, 1), 2)      AS rows_avg,
            child_number,
            plan_hash_value,
            first_load_time || ':' || RAWTOHEX(child_address) AS cursor_generation,
            elapsed_time AS elapsed_us,
            cpu_time AS cpu_us,
            buffer_gets,
            disk_reads,
            rows_processed
        FROM v$sql
        WHERE executions > 0
          AND elapsed_time / GREATEST(executions, 1) / 1000 >= :min_ms
          AND (module IS NULL OR module NOT IN ('DBMS_SCHEDULER'))
          AND (module IS NULL OR LOWER(module) NOT LIKE '%oracleiq%'
                              AND LOWER(module) NOT LIKE '%.venv%')
        ORDER BY elapsed_time / GREATEST(executions, 1) DESC
        FETCH FIRST 200 ROWS ONLY
        """, min_ms=MIN_ELAPSED_MS)

        cols = [d[0].lower() for d in cursor.description]
        records = cursor.fetchall()
    rows = []
    for row in records:
        d = dict(zip(cols, row))
        sql = (d.get("sql_text") or "").strip()
        if not sql or is_ignored(sql, d.get("parsing_schema_name", ""), d.get("module", "")):
            continue
        d["sql_text"] = sql
        d["sql_hash"] = sql_hash(sql)
        d["schema_name"] = d.pop("parsing_schema_name", "")
        d["source_id"] = source_id
        rows.append(d)
    return rows


def run_collector():
    from db.store import report_service
    init_db()
    report_service("collector", "connecting")
    try:
        _run_collector()
    except KeyboardInterrupt:
        report_service("collector", "stopped")
    except BaseException:
        report_service("collector", "error")
        raise
    else:
        report_service("collector", "stopped")


def backfill_schema_names(conn):
    """Only backfill rows belonging to this source, never unidentified legacy rows."""
    from db.store import get_conn

    with closing(get_conn()) as db:
        with closing(db.execute(
            "SELECT id, sql_id, child_number, source_id FROM queries "
            "WHERE (schema_name='' OR schema_name IS NULL) AND source_id=?",
            (oracle_source_id(conn),),
        )) as cursor:
            missing = cursor.fetchall()
        for query_id, sql_id, child_number, source_id in missing:
            assert_query_source({"source_id": source_id}, conn)
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    "SELECT parsing_schema_name FROM v$sql WHERE sql_id=:sid "
                    "AND child_number=:child_no AND ROWNUM=1",
                    sid=sql_id, child_no=child_number,
                )
                row = cursor.fetchone()
            if row and row[0]:
                with closing(db.execute(
                    "UPDATE queries SET schema_name=? WHERE id=?", (row[0], query_id)
                )):
                    pass
        db.commit()


def needs_plan_capture(last_plan, current_hash):
    return (
        last_plan is None or last_plan[0] != current_hash
        or (len(last_plan) > 1 and not is_execution_plan_available(last_plan[1]))
    )


def refresh_query_details(conn, query, query_id, state, now):
    """Refresh runtime evidence independently of plan identity; throttle failed captures."""
    from db.store import get_conn

    assert_query_source(query, conn)
    plan_key = (query.get("plan_hash_value"), query.get("child_number", 0),
                query.get("cursor_generation"))
    if not state:
        with closing(get_conn()) as db:
            with closing(db.execute(
                "SELECT plan_hash_value, plan_text, captured_at FROM execution_plans "
                "WHERE query_id=? ORDER BY id DESC LIMIT 1", (query_id,),
            )) as cursor:
                last_plan = cursor.fetchone()
        state["plan_key"] = None
        state["plan_success"] = float("-inf")
        if not needs_plan_capture(last_plan, plan_key[0]):
            state["plan_key"] = plan_key
            try:
                captured = datetime.fromisoformat(last_plan[2])
                # SQLite CURRENT_TIMESTAMP is UTC.
                captured = captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured
                age = max(0, datetime.now(timezone.utc).timestamp() - captured.timestamp())
                state["plan_success"] = now - age
            except (ValueError, TypeError, IndexError):
                pass

    changed = state["plan_key"] != plan_key
    due = changed or now - state["plan_success"] >= PLAN_REFRESH_SECONDS
    retry_due = (
        state.get("plan_attempt_key") != plan_key
        or now - state.get("plan_attempt", float("-inf")) >= CAPTURE_RETRY_SECONDS
    )
    captured = False
    if due and retry_due:
        state["plan_attempt_key"] = plan_key
        state["plan_attempt"] = now
        plan = get_execution_plan(conn, query["sql_id"], query.get("child_number", 0))
        reported_hash = re.search(r"^Plan hash value:\s*(\d+)", plan or "", re.MULTILINE | re.IGNORECASE)
        hash_matches = (
            reported_hash is None or plan_key[0] is None
            or int(reported_hash.group(1)) == plan_key[0]
        )
        if is_execution_plan_available(plan) and hash_matches:
            save_plan(query_id, plan, plan_hash_value=query.get("plan_hash_value"))
            state["plan_key"] = plan_key
            state["plan_success"] = now
            captured = True

    if state.get("bind_key") != plan_key or (
        now - state.get("bind_attempt", float("-inf")) >= BIND_REFRESH_SECONDS
    ):
        state["bind_attempt"] = now
        state["bind_key"] = plan_key
        binds = fetch_bind_values(conn, query["sql_id"], query.get("child_number", 0))
        if binds:
            save_bind_values(query_id, binds)

    if len(query.get("sql_text", "")) >= 999 and (
        now - state.get("text_attempt", float("-inf")) >= PLAN_REFRESH_SECONDS
    ):
        state["text_attempt"] = now
        full = get_full_sql_text(conn, query["sql_id"])
        if full and len(full) > len(query["sql_text"]):
            query["sql_text"] = full
            upsert_query(query)
    return captured


def _run_collector():
    from db.store import get_setting, report_service
    console.rule("[bold blue]🔍 OracleIQ Collector[/bold blue]")
    conn = None
    settings = None
    capture_states = {}
    purge_epoch = get_setting("purge_epoch", "")
    total_captured = 0
    try:
        while True:
            try:
                desired_settings = get_oracle_settings()
                if settings != desired_settings:
                    close_connection(conn)
                    conn = None
                    settings = desired_settings
                    capture_states.clear()

                if get_setting("collector_active", "true") != "true":
                    report_service("collector", "paused")
                    close_connection(conn)
                    conn = None
                    time.sleep(5)
                    continue

                epoch = get_setting("purge_epoch", "")
                if epoch != purge_epoch:
                    purge_epoch = epoch
                    capture_states.clear()

                if conn is None:
                    report_service("collector", "connecting")
                    conn = connect_oracle(
                        user=settings["oracle_user"], password=settings["oracle_password"],
                        dsn=settings["oracle_dsn"],
                    )
                    report_service("collector", "connected")
                    try:
                        backfill_schema_names(conn)
                    except Exception:
                        log_collector_error()

                report_service("collector", "collecting", ttl=max(120, POLL_INTERVAL_SEC + 120))
                queries = poll_vsql(conn)
                captured_count = 0
                last_signal = time.monotonic()
                for query in queries:
                    if time.monotonic() - last_signal >= 10:
                        report_service("collector", "collecting", ttl=max(120, POLL_INTERVAL_SEC + 120))
                        last_signal = time.monotonic()
                    assert_query_source(query, conn)
                    query_id = upsert_query(query)
                    state = capture_states.setdefault(query_id, {})
                    now = time.monotonic()
                    if refresh_query_details(
                        conn, query, query_id, state, now,
                    ):
                        captured_count += 1
                    state["last_seen"] = now

                now = time.monotonic()
                capture_states = {
                    key: value for key, value in capture_states.items()
                    if now - value.get("last_seen", now) < 2 * PLAN_REFRESH_SECONDS
                }
                total_captured += captured_count
                console.print(
                    f"[dim]{datetime.now():%H:%M:%S}[/dim] "
                    f"[cyan]{len(queries)}[/cyan] requêtes en cache"
                    f" | [green]+{captured_count} plans[/green] | Total: {total_captured}"
                )
                report_service("collector", "waiting", success=True, ttl=max(120, POLL_INTERVAL_SEC + 120))
                time.sleep(POLL_INTERVAL_SEC)
            except Exception:
                log_collector_error()
                close_connection(conn)
                conn = None
                try:
                    report_service("collector", "error")
                except Exception:
                    log_collector_error()
                time.sleep(10)
    except KeyboardInterrupt:
        console.print("\n[bold]Collector arrêté.[/bold]")
    finally:
        close_connection(conn)


if __name__ == "__main__":
    run_collector()
