"""
collector/oracle_collector.py
Polle V$SQL toutes les N secondes et capture les requêtes nouvelles/modifiées.
Récupère aussi le plan d'exécution via DBMS_XPLAN.
"""
import sys
import time
import hashlib
import re
from datetime import datetime
import oracledb
import rich
from rich.console import Console
from rich.table import Table
from rich import print as rprint

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from config import (
    ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD,
    POLL_INTERVAL_SEC, MIN_ELAPSED_MS, IGNORED_SCHEMAS, IGNORE_SYS_QUERIES
)
from db.store import init_db, upsert_query, save_plan, save_bind_values
from collector.connection import connect_oracle

console = Console()

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


def fetch_bind_values(conn, sql_id: str) -> list:
    """Récupère les bind variables depuis V$SQL_BIND_CAPTURE pour un sql_id."""
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
            ORDER BY b.child_number DESC, b.position ASC
        """, sql_id=sql_id)
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
    except Exception as e:
        return []


def get_execution_plan(conn, sql_id: str, child_number: int = 0) -> str:
    """Récupère le plan d'exécution depuis V$SQL_PLAN via DBMS_XPLAN."""
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
        return "\n".join(lines)
    except Exception as e:
        return f"[Plan non disponible: {e}]"


def get_full_sql_text(conn, sql_id: str) -> str:
    """Récupère le texte SQL complet. Tente V$SQL d'abord, puis DBA_HIST_SQLTEXT (AWR) en fallback."""
    # 1. V$SQL (shared pool)
    try:
        cur = conn.cursor()
        cur.prefetchrows = 0
        cur.execute(
            "SELECT sql_fulltext FROM v$sql WHERE sql_id=:sid AND ROWNUM=1",
            sid=sql_id
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            val = row[0]
            text = val.read() if hasattr(val, 'read') else str(val)
            if text and len(text) > 10:
                return text
    except Exception:
        pass
    # 2. DBA_HIST_SQLTEXT (AWR) — SQL évincé du shared pool
    try:
        cur = conn.cursor()
        cur.prefetchrows = 0
        cur.execute(
            "SELECT sql_text FROM dba_hist_sqltext WHERE sql_id=:sid AND ROWNUM=1",
            sid=sql_id
        )
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
    cursor = conn.cursor()
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
    rows = []
    for row in cursor.fetchall():
        d = dict(zip(cols, row))
        sql = (d.get("sql_text") or "").strip()
        if not sql or is_ignored(sql, d.get("parsing_schema_name", ""), d.get("module", "")):
            continue
        d["sql_text"] = sql
        d["sql_hash"] = sql_hash(sql)
        d["schema_name"] = d.pop("parsing_schema_name", "")
        d["source_id"] = str(getattr(conn, "dsn", "") or ORACLE_DSN)
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


def _run_collector():
    from db.store import report_service
    console.rule("[bold blue]🔍 OracleIQ Collector[/bold blue]")

    # Connexion : priorité aux settings en DB, fallback sur les env vars
    from db.store import get_setting as _gs
    dsn  = _gs("oracle_dsn",      ORACLE_DSN)
    user = _gs("oracle_user",     ORACLE_USER)
    pwd  = _gs("oracle_password", ORACLE_PASSWORD)

    console.print(f"[dim]Connexion à {dsn} en tant que {user}...[/dim]")

    try:
        conn = connect_oracle(user=user, password=pwd, dsn=dsn)
        report_service("collector", "connected")
        console.print(f"[green]✓ Connecté à Oracle[/green] | poll toutes les {POLL_INTERVAL_SEC}s\n")
    except Exception as e:
        console.print(f"[red]✗ Erreur connexion Oracle: {e}[/red]")
        sys.exit(1)

    # Backfill des schema_name manquants dans la DB locale
    try:
        from db.store import get_conn as _get_conn
        _db = _get_conn()
        _missing = _db.execute(
            "SELECT id, sql_id FROM queries WHERE schema_name='' OR schema_name IS NULL"
        ).fetchall()
        if _missing:
            console.print(f"[dim]Backfill schema_name pour {len(_missing)} requêtes...[/dim]")
            _updated = 0
            for _db_id, _sql_id in _missing:
                _r = conn.cursor().execute(
                    "SELECT parsing_schema_name FROM v$sql WHERE sql_id=:sid AND ROWNUM=1",
                    sid=_sql_id
                ).fetchone()
                if _r and _r[0]:
                    _db.execute("UPDATE queries SET schema_name=? WHERE id=?", (_r[0], _db_id))
                    _updated += 1
            _db.commit()
            _db.close()
            console.print(f"[green]✓ Backfill: {_updated}/{len(_missing)} schémas récupérés[/green]")
    except Exception as _e:
        console.print(f"[yellow]Backfill schema_name ignoré: {_e}[/yellow]")

    seen_hashes: set[str] = set()
    # Charger les hashes déjà en DB pour éviter de re-capturer à chaque redémarrage
    # MAIS : si une query n'a pas de plan associé, on la retraite quand même
    try:
        _init_conn = __import__('db.store', fromlist=['get_conn']).get_conn()
        _rows = _init_conn.execute(
            """SELECT q.sql_hash FROM queries q
               JOIN execution_plans ep ON ep.query_id = q.id
               GROUP BY q.sql_hash"""
        ).fetchall()
        seen_hashes = {r[0] for r in _rows}
        _init_conn.close()
        console.print(f"[dim]{len(seen_hashes)} hashes chargés depuis la DB (avec plan)[/dim]")
    except Exception:
        pass
    from db.store import get_setting as _gs_epoch
    purge_epoch = _gs_epoch("purge_epoch", "")
    total_captured = 0
    was_paused = False

    def reconnect() -> oracledb.Connection:
        """Ouvre une nouvelle connexion Oracle (paramètres depuis la DB)."""
        from db.store import get_setting as _gs
        _dsn  = _gs("oracle_dsn",      ORACLE_DSN)
        _user = _gs("oracle_user",     ORACLE_USER)
        _pwd  = _gs("oracle_password", ORACLE_PASSWORD)
        console.print(f"[dim]Reconnexion à {_dsn} en tant que {_user}...[/dim]")
        _conn = connect_oracle(user=_user, password=_pwd, dsn=_dsn)
        console.print("[green]✓ Reconnecté à Oracle[/green]")
        return _conn

    try:
        while True:
            try:
                # Vérifier si la collecte est activée
                from db.store import get_setting
                if get_setting("collector_active", "true") != "true":
                    report_service("collector", "paused")
                    if not was_paused:
                        console.print("[dim]⏸ Collecte en pause (réglage web)...[/dim]")
                        was_paused = True
                    time.sleep(5)
                    continue

                # Purge déclenchée depuis l'UI → vider le cache pour re-capturer
                _epoch = get_setting("purge_epoch", "")
                if _epoch != purge_epoch:
                    purge_epoch = _epoch
                    seen_hashes.clear()
                    console.print("[yellow]🗑 Purge détectée — cache de hashes réinitialisé[/yellow]")

                # Reprise après pause → reconnexion Oracle pour éviter connexion expirée
                if was_paused:
                    console.print("[green]▶ Reprise de la collecte — reconnexion Oracle...[/green]")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = reconnect()
                    was_paused = False

                report_service("collector", "collecting", ttl=max(120, POLL_INTERVAL_SEC + 120))
                queries = poll_vsql(conn)
                new_count = 0
                last_signal = time.monotonic()

                for q in queries:
                    if time.monotonic() - last_signal >= 10:
                        report_service("collector", "collecting", ttl=max(120, POLL_INTERVAL_SEC + 120))
                        last_signal = time.monotonic()
                    try:
                        qid = upsert_query(q)
                    except Exception as _eq:
                        print(f"upsert_query failed for {q.get('sql_id')}: {_eq}", flush=True)
                        continue

                    # Récupère le texte complet + plan pour les nouvelles requêtes
                    # OU pour les requêtes qui n'ont pas encore de plan
                    from db.store import get_conn as _gc
                    _chk = _gc()
                    _last_plan = _chk.execute(
                        "SELECT plan_hash_value FROM execution_plans WHERE query_id=? ORDER BY id DESC LIMIT 1", (qid,)
                    ).fetchone()
                    _chk.close()

                    if needs_plan_capture(_last_plan, q.get("plan_hash_value")):
                        # Compléter sql_text tronqué (V$SQL.sql_text = 1000 chars)
                        if len(q.get("sql_text", "")) >= 999:
                            full = get_full_sql_text(conn, q["sql_id"])
                            if full and len(full) > len(q["sql_text"]):
                                q["sql_text"] = full
                                qid = upsert_query(q)  # re-upsert avec texte complet
                        try:
                            plan = get_execution_plan(conn, q["sql_id"], q.get("child_number", 0))
                        except Exception as _ep:
                            plan = f"[Plan non disponible: {_ep}]"
                        if plan and "non disponible" not in plan.lower():
                            save_plan(qid, plan, plan_hash_value=q.get("plan_hash_value"))
                        # Stocker les bind variables capturées
                        try:
                            binds = fetch_bind_values(conn, q["sql_id"])
                            if binds:
                                save_bind_values(qid, binds)
                        except Exception as _eb:
                            pass  # non bloquant
                        seen_hashes.add(q["sql_hash"])
                        new_count += 1
                        total_captured += 1
                    else:
                        # Hash déjà vu — mettre à jour les binds si Oracle en a de nouvelles
                        try:
                            binds = fetch_bind_values(conn, q["sql_id"])
                            if binds:
                                save_bind_values(qid, binds)
                        except Exception:
                            pass
                        # Vérifier quand même si le texte est tronqué en base
                        if len(q.get("sql_text", "")) >= 999:
                            _chk2 = _gc()
                            existing_len = _chk2.execute(
                                "SELECT length(sql_text) FROM queries WHERE id=?", (qid,)
                            ).fetchone()
                            _chk2.close()
                            if existing_len and existing_len[0] <= 1000:
                                full = get_full_sql_text(conn, q["sql_id"])
                                if full and len(full) > len(q.get("sql_text", "")):
                                    q["sql_text"] = full
                                    upsert_query(q)  # met à jour sql_text en base

                ts = datetime.now().strftime("%H:%M:%S")
                status = f"[dim]{ts}[/dim] "
                status += f"[cyan]{len(queries)}[/cyan] requêtes en cache"
                status += f" | [green]+{new_count} nouvelles[/green]"
                status += f" | Total capturé: [bold]{total_captured}[/bold]"
                console.print(status)

                report_service("collector", "waiting", success=True, ttl=max(120, POLL_INTERVAL_SEC + 120))
                time.sleep(POLL_INTERVAL_SEC)

            except Exception as e:
                report_service("collector", "error")
                import traceback
                # Logguer en clair dans le fichier (bypass Rich)
                with open("/tmp/oracleiq_err.log", "a") as _ef:
                    _ef.write(traceback.format_exc() + "\n")
                console.print(f"[yellow]⚠ Erreur collecteur (retry dans 10s): {e}[/yellow]")
                try:
                    conn.close()
                except Exception:
                    pass
                time.sleep(10)
                try:
                    conn = reconnect()
                except Exception:
                    was_paused = True

    except KeyboardInterrupt:
        console.print("\n[bold]Collector arrêté.[/bold]")
        conn.close()


def needs_plan_capture(last_plan, current_hash):
    return last_plan is None or last_plan[0] != current_hash


if __name__ == "__main__":
    run_collector()
