"""
db/store.py — SQLite local : stockage requêtes, plans, analyses IA
"""
import sqlite3
import json
import time
import hashlib
from datetime import datetime
from pathlib import Path
from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    conn = get_conn()
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS queries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        sql_id          TEXT NOT NULL,          -- identifiant Oracle V$SQL
        sql_text        TEXT NOT NULL,
        sql_hash        TEXT NOT NULL,          -- hash pour dédoublonner
        schema_name     TEXT,
        module          TEXT,                   -- appli source (V$SQL.MODULE)
        first_seen      DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_seen       DATETIME DEFAULT CURRENT_TIMESTAMP,
        executions      INTEGER DEFAULT 1,
        elapsed_ms_avg  REAL DEFAULT 0,
        elapsed_ms_max  REAL DEFAULT 0,
        elapsed_ms_total REAL DEFAULT 0,
        cpu_ms_avg      REAL DEFAULT 0,
        buffer_gets_avg REAL DEFAULT 0,
        disk_reads_avg  REAL DEFAULT 0,
        rows_avg        REAL DEFAULT 0,
        perf_score      INTEGER DEFAULT NULL,   -- 0-100, calculé par IA
        analyzed        INTEGER DEFAULT 0       -- 0=non 1=oui
    );

    CREATE TABLE IF NOT EXISTS execution_plans (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id    INTEGER REFERENCES queries(id),
        captured_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        plan_text   TEXT NOT NULL,              -- DBMS_XPLAN.DISPLAY brut
        plan_json   TEXT                        -- version parsée JSON
    );

    CREATE TABLE IF NOT EXISTS ai_analyses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id        INTEGER REFERENCES queries(id),
        analyzed_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
        model_used      TEXT,
        perf_score      INTEGER,                -- 0-100 (100=parfait)
        severity        TEXT,                   -- ok | warning | critical
        summary         TEXT,
        issues          TEXT,                   -- JSON array
        recommendations TEXT,                   -- JSON array
        raw_response    TEXT                    -- réponse IA brute
    );

    CREATE INDEX IF NOT EXISTS idx_queries_hash ON queries(sql_hash);
    CREATE INDEX IF NOT EXISTS idx_queries_score ON queries(perf_score);
    CREATE INDEX IF NOT EXISTS idx_queries_elapsed ON queries(elapsed_ms_avg DESC);
    CREATE TABLE IF NOT EXISTS query_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id INTEGER REFERENCES queries(id),
        captured_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        elapsed_ms_avg REAL,
        executions INTEGER,
        buffer_gets_avg REAL,
        disk_reads_avg REAL
    );
    CREATE TABLE IF NOT EXISTS analyzing_queue (
        query_id  INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS query_chats (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id   INTEGER REFERENCES queries(id) ON DELETE CASCADE,
        role       TEXT NOT NULL,   -- 'user' | 'assistant'
        content    TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_query_chats_qid ON query_chats(query_id);
    CREATE TABLE IF NOT EXISTS bind_values (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id     INTEGER REFERENCES queries(id) ON DELETE CASCADE,
        captured_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        bind_name    TEXT NOT NULL,
        position     INTEGER,
        datatype     TEXT,
        value_string TEXT,
        last_captured_oracle TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_bind_values_qid ON bind_values(query_id);
    CREATE TABLE IF NOT EXISTS performance_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
        observed_at REAL NOT NULL,
        cursor_generation TEXT NOT NULL,
        plan_hash_value INTEGER NOT NULL,
        executions INTEGER NOT NULL,
        elapsed_us INTEGER NOT NULL,
        cpu_us INTEGER NOT NULL,
        buffer_gets INTEGER NOT NULL,
        disk_reads INTEGER NOT NULL,
        rows_processed INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_performance_query_time
        ON performance_samples(query_id, observed_at);
    """)
    # Migration : ajouter tokens_in / tokens_out si absents
    cols = [r[1] for r in conn.execute('PRAGMA table_info(ai_analyses)').fetchall()]
    if 'tokens_in' not in cols:
        conn.execute('ALTER TABLE ai_analyses ADD COLUMN tokens_in INTEGER DEFAULT 0')
    if 'tokens_out' not in cols:
        conn.execute('ALTER TABLE ai_analyses ADD COLUMN tokens_out INTEGER DEFAULT 0')
    if 'trace' not in cols:
        conn.execute('ALTER TABLE ai_analyses ADD COLUMN trace TEXT')
    # Migration : ajouter plan_change_detected si absent
    qcols = [r[1] for r in conn.execute('PRAGMA table_info(queries)').fetchall()]
    if 'plan_change_detected' not in qcols:
        conn.execute('ALTER TABLE queries ADD COLUMN plan_change_detected INTEGER DEFAULT 0')
    if 'has_literal_values' not in qcols:
        conn.execute('ALTER TABLE queries ADD COLUMN has_literal_values INTEGER DEFAULT 0')
    if 'severity' not in qcols:
        conn.execute('ALTER TABLE queries ADD COLUMN severity TEXT')
    for name, definition in (
        ("source_id", "TEXT NOT NULL DEFAULT ''"),
        ("child_number", "INTEGER NOT NULL DEFAULT 0"),
        ("plan_hash_value", "INTEGER"),
        ("analysis_error", "TEXT"),
    ):
        if name not in qcols:
            conn.execute(f"ALTER TABLE queries ADD COLUMN {name} {definition}")
    pcols = [row[1] for row in conn.execute("PRAGMA table_info(execution_plans)")]
    if "plan_hash_value" not in pcols:
        conn.execute("ALTER TABLE execution_plans ADD COLUMN plan_hash_value INTEGER")
    conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_queries_identity
            ON queries(source_id, schema_name, sql_id, child_number) WHERE source_id <> '';
        CREATE INDEX IF NOT EXISTS idx_analyses_query ON ai_analyses(query_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_plans_query ON execution_plans(query_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_snapshots_query ON query_snapshots(query_id, captured_at);
    """)
    _ensure_settings(conn)
    conn.commit()
    conn.close()
    print(f"[DB] Initialisée → {DB_PATH}")


def upsert_query(row: dict) -> int:
    """Insère ou met à jour une requête. Retourne l'id local."""
    conn = get_conn()
    c = conn.cursor()

    source_id = row.get("source_id", "")
    identity = (row.get("sql_id", ""), row.get("schema_name", ""), row.get("child_number", 0))
    c.execute("BEGIN IMMEDIATE")
    existing = c.execute(
        "SELECT id, executions FROM queries WHERE sql_id=? AND COALESCE(schema_name, '')=? "
        "AND child_number=? AND source_id=?",
        (*identity, source_id),
    ).fetchone()
    if existing is None and source_id:
        existing = c.execute(
            "SELECT id, executions FROM queries WHERE sql_id=? AND COALESCE(schema_name, '')=? "
            "AND child_number=? AND source_id='' ORDER BY id LIMIT 1", identity,
        ).fetchone()

    now = datetime.utcnow().isoformat()

    # Detect literal values in SQL
    from collector.oracle_collector import has_literal_values as _has_lv
    literal_flag = 1 if _has_lv(row.get("sql_text", "")) else 0

    if existing:
        qid = existing["id"]
        # Mettre à jour sql_text si la version actuelle est tronquée (<=1000 chars)
        # et que la nouvelle version est plus longue
        existing_text = c.execute("SELECT sql_text FROM queries WHERE id=?", (qid,)).fetchone()[0] or ""
        new_text = row.get("sql_text", "")
        update_sql_text = len(new_text) > len(existing_text)
        if update_sql_text:
            c.execute("UPDATE queries SET sql_text=?, has_literal_values=? WHERE id=?",
                      (new_text, literal_flag, qid))
        c.execute("""
            UPDATE queries SET
                source_id       = ?,
                sql_hash        = ?,
                module          = ?,
                last_seen       = ?,
                executions      = ?,
                elapsed_ms_avg  = ?,
                elapsed_ms_max  = MAX(elapsed_ms_max, ?),
                elapsed_ms_total= ?,
                cpu_ms_avg      = ?,
                buffer_gets_avg = ?,
                disk_reads_avg  = ?,
                rows_avg        = ?,
                analyzed        = CASE WHEN analyzed = 1 THEN 1 ELSE 0 END
            WHERE id = ?
        """, (
            source_id, row["sql_hash"], row.get("module", ""),
            now,
            row.get("executions", 1),
            row.get("elapsed_ms_avg", 0),
            row.get("elapsed_ms_max", 0),
            row.get("elapsed_ms_total", 0),
            row.get("cpu_ms_avg", 0),
            row.get("buffer_gets_avg", 0),
            row.get("disk_reads_avg", 0),
            row.get("rows_avg", 0),
            qid,
        ))
    else:
        c.execute("""
            INSERT INTO queries
                (sql_id, sql_text, sql_hash, schema_name, module,
                 executions, elapsed_ms_avg, elapsed_ms_max, elapsed_ms_total,
                  cpu_ms_avg, buffer_gets_avg, disk_reads_avg, rows_avg, has_literal_values,
                  source_id, child_number)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row.get("sql_id", ""),
            row["sql_text"],
            row["sql_hash"],
            row.get("schema_name", ""),
            row.get("module", ""),
            row.get("executions", 1),
            row.get("elapsed_ms_avg", 0),
            row.get("elapsed_ms_max", 0),
            row.get("elapsed_ms_total", 0),
            row.get("cpu_ms_avg", 0),
            row.get("buffer_gets_avg", 0),
            row.get("disk_reads_avg", 0),
            row.get("rows_avg", 0),
            literal_flag,
            source_id,
            row.get("child_number", 0),
        ))
        qid = c.lastrowid

    # Save snapshot if last one is older than 10 minutes
    last_snap = c.execute(
        "SELECT MAX(captured_at) FROM query_snapshots WHERE query_id=?", (qid,)
    ).fetchone()[0]
    should_snap = True
    if last_snap:
        from datetime import timedelta
        try:
            last_dt = datetime.fromisoformat(last_snap)
            if (datetime.utcnow() - last_dt).total_seconds() < 600:
                should_snap = False
        except Exception:
            pass
    if should_snap:
        c.execute("""
            INSERT INTO query_snapshots (query_id, elapsed_ms_avg, executions, buffer_gets_avg, disk_reads_avg)
            VALUES (?, ?, ?, ?, ?)
        """, (
            qid,
            row.get("elapsed_ms_avg", 0),
            row.get("executions", 1),
            row.get("buffer_gets_avg", 0),
            row.get("disk_reads_avg", 0),
        ))

    sample_fields = ("cursor_generation", "plan_hash_value", "executions", "elapsed_us",
                     "cpu_us", "buffer_gets", "disk_reads", "rows_processed")
    if all(row.get(field) is not None for field in sample_fields) and row["cursor_generation"]:
        observed_at = time.time()
        previous = c.execute(
            "SELECT observed_at FROM performance_samples WHERE query_id=? ORDER BY observed_at DESC LIMIT 1", (qid,)
        ).fetchone()
        if previous is None or observed_at - previous[0] >= 60:
            c.execute(
                "INSERT INTO performance_samples (query_id, observed_at, cursor_generation, plan_hash_value, "
                "executions, elapsed_us, cpu_us, buffer_gets, disk_reads, rows_processed) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (qid, observed_at, *(row[field] for field in sample_fields)),
            )
            c.execute("DELETE FROM performance_samples WHERE query_id=? AND observed_at < ?", (qid, observed_at - 7 * 86400))

    conn.commit()
    conn.close()
    return qid


def save_plan(query_id: int, plan_text: str, plan_json: dict = None, plan_hash_value: int = None):
    conn = get_conn()
    import re
    if plan_hash_value is None:
        match = re.search(r"Plan hash value:\s*(\d+)", plan_text or "", re.IGNORECASE)
        plan_hash_value = int(match.group(1)) if match else None
    last_plan = conn.execute(
        "SELECT plan_hash_value FROM execution_plans WHERE query_id=? AND plan_hash_value IS NOT NULL ORDER BY id DESC LIMIT 1",
        (query_id,)
    ).fetchone()
    plan_change = bool(last_plan and plan_hash_value is not None and last_plan[0] != plan_hash_value)
    conn.execute(
        "INSERT INTO execution_plans (query_id, plan_text, plan_json, plan_hash_value) VALUES (?,?,?,?)",
        (query_id, plan_text, json.dumps(plan_json) if plan_json else None, plan_hash_value)
    )
    if plan_hash_value is not None:
        conn.execute("UPDATE queries SET plan_hash_value=? WHERE id=?", (plan_hash_value, query_id))
    if plan_change:
        conn.execute(
            "UPDATE queries SET plan_change_detected=1, analyzed=0, analysis_error=NULL WHERE id=?",
            (query_id,)
        )
    conn.commit()
    conn.close()


def save_analysis(query_id: int, analysis: dict):
    """Enregistre une analyse IA et met à jour le score/severité de la requête."""
    usage = analysis.get("usage") or {}
    tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    tokens_out = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    score = analysis.get("score")
    severity = analysis.get("severity") or "warning"

    conn = get_conn()
    conn.execute("""
        INSERT INTO ai_analyses
            (query_id, model_used, perf_score, severity, summary,
             issues, recommendations, raw_response, tokens_in, tokens_out, trace)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        query_id,
        analysis.get("model", ""),
        score,
        severity,
        analysis.get("summary", ""),
        json.dumps(analysis.get("issues", []), ensure_ascii=False),
        json.dumps(analysis.get("recommendations", []), ensure_ascii=False),
        analysis.get("raw", ""),
        tokens_in,
        tokens_out,
        json.dumps(analysis.get("trace", []), ensure_ascii=False),
    ))
    conn.execute(
        "UPDATE queries SET analyzed = 1, perf_score = ?, severity = ?, analysis_error=NULL WHERE id = ?",
        (score, severity, query_id)
    )
    conn.commit()
    conn.close()


def save_bind_values(query_id: int, binds: list):
    """Sauvegarde les bind variables capturées depuis Oracle.
    Remplace les valeurs existantes pour ce query_id (upsert par nom de bind)."""
    if not binds:
        return
    conn = get_conn()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for b in binds:
        name = b.get("bind_name") or b.get("name") or ""
        if not name:
            continue
        existing = conn.execute(
            "SELECT id FROM bind_values WHERE query_id=? AND bind_name=?",
            (query_id, name)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE bind_values
                SET value_string=?, datatype=?, last_captured_oracle=?, captured_at=?
                WHERE id=?
            """, (
                b.get("value") or b.get("value_string"),
                b.get("datatype") or b.get("datatype_string"),
                str(b.get("last_captured") or "") or None,
                now,
                existing[0]
            ))
        else:
            conn.execute("""
                INSERT INTO bind_values (query_id, bind_name, position, datatype, value_string, last_captured_oracle, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                query_id,
                name,
                b.get("position"),
                b.get("datatype") or b.get("datatype_string"),
                b.get("value") or b.get("value_string"),
                str(b.get("last_captured") or "") or None,
                now
            ))
    conn.commit()
    conn.close()


def get_bind_values(query_id: int) -> list:
    """Récupère les bind variables stockées en SQLite pour une requête."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT bind_name, position, datatype, value_string, last_captured_oracle, captured_at
        FROM bind_values WHERE query_id=?
        ORDER BY position ASC
    """, (query_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


    usage = analysis.get("usage") or {}
    tokens_in  = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    conn = get_conn()
    conn.execute("""
        INSERT INTO ai_analyses
            (query_id, model_used, perf_score, severity,
             summary, issues, recommendations, raw_response, tokens_in, tokens_out, trace)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        query_id,
        analysis.get("model"),
        analysis.get("score"),
        analysis.get("severity"),
        analysis.get("summary"),
        json.dumps(analysis.get("issues", []), ensure_ascii=False),
        json.dumps(analysis.get("recommendations", []), ensure_ascii=False),
        analysis.get("raw"),
        tokens_in,
        tokens_out,
        json.dumps(analysis.get("trace", []), ensure_ascii=False) if analysis.get("trace") else None,
    ))
    conn.execute(
        "UPDATE queries SET perf_score=?, severity=?, analyzed=1 WHERE id=?",
        (analysis.get("score"), analysis.get("severity"), query_id)
    )
    conn.commit()
    conn.close()


# ─── Settings ───────────────────────────────────────────────
def _ensure_settings(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('analyzer_mode','manual')")
    conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('tool_rounds','3')")
    conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('analyzer_ai_mode','classic')")
    conn.commit()


def chat_add_message(query_id: int, role: str, content: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO query_chats (query_id, role, content) VALUES (?, ?, ?)",
        (query_id, role, content)
    )
    msg_id = cur.lastrowid
    conn.commit()
    conn.close()
    return msg_id


def chat_get_messages(query_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, content, created_at FROM query_chats WHERE query_id=? ORDER BY id ASC",
        (query_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def chat_clear(query_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM query_chats WHERE query_id=?", (query_id,))
    conn.commit()
    conn.close()


def analyzing_queue_add(query_id: int):
    conn = get_conn()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM analyzing_queue WHERE datetime(started_at) < datetime('now', '-2 hours')")
            if conn.execute("SELECT 1 FROM analyzing_queue WHERE query_id=?", (query_id,)).fetchone():
                return False
            if conn.execute("SELECT COUNT(*) FROM analyzing_queue").fetchone()[0] >= 32:
                raise ValueError("File d'analyse pleine (32 taches maximum)")
            conn.execute("INSERT INTO analyzing_queue(query_id, started_at) VALUES (?, CURRENT_TIMESTAMP)", (query_id,))
            conn.execute("UPDATE queries SET analysis_error=NULL WHERE id=?", (query_id,))
        return True
    finally:
        conn.close()


def save_analysis_error(query_id: int, message: str):
    conn = get_conn()
    try:
        with conn:
            conn.execute("UPDATE queries SET analysis_error=? WHERE id=?", (message[:500], query_id))
    finally:
        conn.close()


def analyzing_queue_remove(query_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM analyzing_queue WHERE query_id=?", (query_id,))
    conn.commit()
    conn.close()


def analyzing_queue_get() -> list[int]:
    conn = get_conn()
    rows = conn.execute("SELECT query_id FROM analyzing_queue WHERE datetime(started_at) >= datetime('now', '-2 hours')").fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default

def report_service(name, state, *, success=False, ttl=120):
    if name not in {"collector", "analyzer"}:
        raise ValueError("Unknown service")
    now = time.time()
    conn = get_conn()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM settings WHERE key=?", (f"service_{name}",)).fetchone()
            previous = json.loads(row[0]) if row else {}
            status = {"state": state, "updated_at": now, "expires_at": now + ttl,
                      "last_success": now if success else previous.get("last_success")}
            conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                         (f"service_{name}", json.dumps(status)))
    finally:
        conn.close()


def get_service_health():
    services = {}
    for name in ("collector", "analyzer"):
        status = json.loads(get_setting(f"service_{name}", "{}"))
        if not status:
            status = {"state": "unknown", "updated_at": None, "last_success": None}
        elif status["state"] not in {"stopped", "error"} and status["expires_at"] < time.time():
            status["state"] = "stale"
        services[name] = status
    return services


def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()


def delete_query_data(query_id: int):
    conn = get_conn()
    try:
        with conn:
            for table in ("ai_analyses", "execution_plans", "query_snapshots", "query_chats", "bind_values", "analyzing_queue"):
                conn.execute(f"DELETE FROM {table} WHERE query_id=?", (query_id,))
            conn.execute("DELETE FROM queries WHERE id=?", (query_id,))
    finally:
        conn.close()


def get_query_stats():
    conn = get_conn()
    try:
        return dict(conn.execute("""
            SELECT COUNT(*) AS total_queries,
                   COALESCE(SUM(analyzed=1), 0) AS analyzed,
                   COALESCE(SUM(severity='critical'), 0) AS critical,
                   COALESCE(SUM(severity='warning'), 0) AS warning,
                   COALESCE(SUM(severity='ok'), 0) AS ok,
                   ROUND(AVG(perf_score), 1) AS avg_score,
                   MAX(elapsed_ms_avg) AS slowest_ms
            FROM queries
        """).fetchone())
    finally:
        conn.close()


def purge_data(scope: str = "analyzed") -> dict:
    """Purge les donnees locales.

    scope :
      - 'all'      : toutes les requetes, plans, binds, analyses et chats
      - 'analyzed' : uniquement les requetes deja analysees (et leurs donnees liees)
      - 'analyses' : uniquement les analyses IA, les requetes sont conservees
    """
    if scope not in ("all", "analyzed", "analyses"):
        raise ValueError(f"scope invalide: {scope}")

    child_tables = ("ai_analyses", "execution_plans", "query_snapshots",
                    "query_chats", "bind_values", "analyzing_queue")
    conn = get_conn()
    c = conn.cursor()
    counts = {"scope": scope}

    if scope == "analyses":
        counts["queries"] = 0
        counts["analyses"] = c.execute("SELECT COUNT(*) FROM ai_analyses").fetchone()[0]
        c.execute("DELETE FROM ai_analyses")
        c.execute("DELETE FROM query_chats")
        c.execute("DELETE FROM analyzing_queue")
        c.execute("UPDATE queries SET analyzed = 0, perf_score = NULL, severity = NULL")
    elif scope == "all":
        counts["queries"] = c.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
        counts["analyses"] = c.execute("SELECT COUNT(*) FROM ai_analyses").fetchone()[0]
        for table in child_tables:
            c.execute(f"DELETE FROM {table}")
        c.execute("DELETE FROM queries")
    else:
        sub = "(SELECT id FROM queries WHERE analyzed = 1)"
        counts["queries"] = c.execute("SELECT COUNT(*) FROM queries WHERE analyzed = 1").fetchone()[0]
        counts["analyses"] = c.execute(
            f"SELECT COUNT(*) FROM ai_analyses WHERE query_id IN {sub}"
        ).fetchone()[0]
        for table in child_tables:
            c.execute(f"DELETE FROM {table} WHERE query_id IN {sub}")
        c.execute("DELETE FROM queries WHERE analyzed = 1")

    conn.commit()
    conn.close()
    # Signale au collecteur de vider son cache de hashes en memoire
    set_setting("purge_epoch", str(int(time.time())))
    return counts


def get_unanalyzed(limit=20):
    conn = get_conn()
    rows = conn.execute("""
                SELECT q.*, ep.plan_text
        FROM queries q
                LEFT JOIN execution_plans ep ON ep.id = (
                        SELECT id FROM execution_plans WHERE query_id=q.id ORDER BY id DESC LIMIT 1
                )
        WHERE q.analyzed = 0
                    AND q.analysis_error IS NULL
          -- Exclure les requêtes déjà en cours d'analyse (queue)
          AND q.id NOT IN (SELECT query_id FROM analyzing_queue)
        ORDER BY q.elapsed_ms_avg DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_queries(limit=100, order="elapsed_ms_avg DESC"):
    allowed = {"elapsed_ms_avg DESC", "executions DESC", "buffer_gets_avg DESC", "disk_reads_avg DESC", "perf_score ASC", "last_seen DESC"}
    order = order if order in allowed else "elapsed_ms_avg DESC"
    limit = max(1, min(int(limit), 200))
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT q.*, a.severity, a.summary, a.perf_score as ai_score
        FROM queries q
        LEFT JOIN ai_analyses a ON a.id = (
            SELECT id FROM ai_analyses
            WHERE query_id = q.id
            ORDER BY id DESC LIMIT 1
        )
        ORDER BY q.{order}
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_query_page(search="", schema="", critical_only=False, group=False, sort="elapsed",
                   direction="desc", page=1, page_size=50, pattern="", source=""):
    page_size = max(1, min(int(page_size), 100))
    predicates, params = [], []
    if search:
        predicates.append("(instr(lower(q.sql_text), lower(?)) > 0 OR instr(lower(COALESCE(q.schema_name,'')), lower(?)) > 0 OR instr(lower(COALESCE(q.module,'')), lower(?)) > 0 OR instr(lower(q.sql_id), lower(?)) > 0)")
        params.extend([search] * 4)
    for column, value in (("schema_name", schema), ("sql_hash", pattern), ("source_id", source)):
        if value:
            predicates.append(f"q.{column}=?")
            params.append(value)
    if critical_only:
        predicates.append("q.severity='critical'")
    where = " AND ".join(predicates) if predicates else "1=1"
    base = f"""
        WITH filtered AS (
            SELECT q.*, a.summary FROM queries q
            LEFT JOIN ai_analyses a ON a.id=(SELECT id FROM ai_analyses WHERE query_id=q.id ORDER BY id DESC LIMIT 1)
            WHERE {where}
        ), ranked AS (
            SELECT filtered.*,
                ROW_NUMBER() OVER (PARTITION BY source_id, schema_name, sql_hash ORDER BY elapsed_ms_avg DESC, id) AS rank,
                COUNT(*) OVER pattern_window AS variant_count,
                SUM(executions) OVER pattern_window AS group_executions,
                SUM(elapsed_ms_avg * executions) OVER pattern_window / MAX(SUM(executions) OVER pattern_window, 1) AS group_elapsed,
                SUM(buffer_gets_avg * executions) OVER pattern_window / MAX(SUM(executions) OVER pattern_window, 1) AS group_buffer,
                MIN(perf_score) OVER pattern_window AS group_score,
                MAX(CASE severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 WHEN 'ok' THEN 1 ELSE 0 END) OVER pattern_window AS group_severity,
                MAX(last_seen) OVER pattern_window AS group_last_seen
            FROM filtered WINDOW pattern_window AS (PARTITION BY source_id, schema_name, sql_hash)
        )
    """
    eligible = "rank=1" if group else "1=1"
    order_columns = {
        "elapsed": "group_elapsed" if group else "elapsed_ms_avg",
        "executions": "group_executions" if group else "executions",
        "score": "group_score" if group else "perf_score",
        "buffer": "group_buffer" if group else "buffer_gets_avg",
        "schema": "schema_name", "last_seen": "group_last_seen" if group else "last_seen",
        "severity": "group_severity" if group else "CASE severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 WHEN 'ok' THEN 1 ELSE 0 END",
    }
    order = order_columns.get(sort, order_columns["elapsed"])
    direction_sql = "ASC" if direction == "asc" else "DESC"
    conn = get_conn()
    try:
        total = conn.execute(base + f"SELECT COUNT(*) FROM ranked WHERE {eligible}", params).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(int(page), pages))
        rows = conn.execute(base + f"SELECT * FROM ranked WHERE {eligible} ORDER BY {order} {direction_sql} NULLS LAST, id LIMIT ? OFFSET ?",
                            [*params, page_size, (page - 1) * page_size]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            if group:
                item.update(executions=item["group_executions"], elapsed_ms_avg=item["group_elapsed"],
                            buffer_gets_avg=item["group_buffer"], perf_score=item["group_score"],
                            severity={3: "critical", 2: "warning", 1: "ok", 0: None}[item["group_severity"]],
                            last_seen=item["group_last_seen"])
                if item["variant_count"] > 1:
                    item["summary"] = None
            else:
                item["variant_count"] = 1
            items.append(item)
        schemas = [row[0] for row in conn.execute("SELECT DISTINCT schema_name FROM queries WHERE COALESCE(schema_name,'')<>'' ORDER BY schema_name")]
        return {"items": items, "total": total, "page": page, "pages": pages, "page_size": page_size, "schemas": schemas}
    finally:
        conn.close()


def get_query_detail(query_id: int):
    conn = get_conn()
    q = conn.execute("SELECT * FROM queries WHERE id=?", (query_id,)).fetchone()
    # Chercher le plan par query_id d'abord, puis par sql_id (en cas de re-capture)
    plan = conn.execute(
        "SELECT * FROM execution_plans WHERE query_id=? ORDER BY id DESC LIMIT 1",
        (query_id,)
    ).fetchone()
    # Dernière analyse
    analysis = conn.execute(
        "SELECT * FROM ai_analyses WHERE query_id=? ORDER BY id DESC LIMIT 1",
        (query_id,)
    ).fetchone()
    # Historique complet (pour comparaison)
    history = conn.execute(
        "SELECT id, analyzed_at, model_used, perf_score, severity, summary, tokens_in, tokens_out FROM ai_analyses WHERE query_id=? ORDER BY id DESC",
        (query_id,)
    ).fetchall()
    conn.close()
    return {
        "query": dict(q) if q else None,
        "plan": dict(plan) if plan else None,
        "analysis": dict(analysis) if analysis else None,
        "history": [dict(h) for h in history],
    }
