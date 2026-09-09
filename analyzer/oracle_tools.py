"""
analyzer/oracle_tools.py
Outils Oracle read-only disponibles pour l'IA pendant l'analyse.
AUCUNE modification de données — uniquement des SELECT.
"""
import re


# ─────────────────────────────────────────────
# Sécurité : whitelist des requêtes autorisées
# ─────────────────────────────────────────────
_FORBIDDEN = re.compile(
    r'\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|EXECUTE|CALL|PRAGMA)\b',
    re.IGNORECASE
)

def _safe_name(name: str) -> str:
    """Valide qu'un nom de table/schéma ne contient que des caractères légaux."""
    name = name.strip().strip('"\'')
    if not re.match(r'^[A-Za-z0-9_$#.]+$', name):
        raise ValueError(f"Nom invalide : {name!r}")
    return name


def _query(conn, sql: str, params: dict | None = None) -> list[dict]:
    """Exécute une requête SELECT read-only et retourne une liste de dicts."""
    if _FORBIDDEN.search(sql):
        raise PermissionError("Requête non autorisée (écriture interdite)")
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [description[0].lower() for description in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchmany(500)]


# ─────────────────────────────────────────────
# Outils disponibles
# ─────────────────────────────────────────────

def table_stats(conn, table_name: str, schema: str = "") -> dict:
    """Statistiques générales sur une table (lignes, taille, dernière analyse)."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None

    where = "TABLE_NAME = :tbl"
    params = {"tbl": t}
    if s:
        where += " AND OWNER = :sch"
        params["sch"] = s

    rows = _query(conn, f"""
        SELECT OWNER, TABLE_NAME,
               NUM_ROWS, BLOCKS, AVG_ROW_LEN,
               LAST_ANALYZED,
               ROUND(BLOCKS * 8 / 1024, 2) AS size_mb
        FROM ALL_TAB_STATISTICS
        WHERE {where}
        ORDER BY LAST_ANALYZED DESC NULLS LAST
        FETCH FIRST 5 ROWS ONLY
    """, params)

    if not rows:
        return {"error": f"Table {t} introuvable ou stats non disponibles"}
    return {"table_stats": rows}


def index_list(conn, table_name: str, schema: str = "") -> dict:
    """Liste des index sur une table avec leurs colonnes."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None

    where = "i.TABLE_NAME = :tbl"
    params = {"tbl": t}
    if s:
        where += " AND i.TABLE_OWNER = :sch"
        params["sch"] = s

    rows = _query(conn, f"""
        SELECT i.INDEX_NAME, i.UNIQUENESS, i.STATUS, i.VISIBILITY,
               i.NUM_ROWS, i.LAST_ANALYZED,
               LISTAGG(c.COLUMN_NAME || '(' || c.COLUMN_POSITION || ')', ', ')
                   WITHIN GROUP (ORDER BY c.COLUMN_POSITION) AS columns
        FROM ALL_INDEXES i
        JOIN ALL_IND_COLUMNS c ON c.INDEX_NAME = i.INDEX_NAME AND c.TABLE_OWNER = i.TABLE_OWNER
        WHERE {where}
        GROUP BY i.INDEX_NAME, i.UNIQUENESS, i.STATUS, i.VISIBILITY, i.NUM_ROWS, i.LAST_ANALYZED
        ORDER BY i.INDEX_NAME
        FETCH FIRST 20 ROWS ONLY
    """, params)

    if not rows:
        return {"info": f"Aucun index trouvé sur {t}"}
    return {"indexes": rows}


def column_stats(conn, table_name: str, schema: str = "") -> dict:
    """Statistiques des colonnes (cardinalité, nulls, dernière analyse)."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None

    where = "c.TABLE_NAME = :tbl"
    params = {"tbl": t}
    if s:
        where += " AND c.OWNER = :sch"
        params["sch"] = s

    rows = _query(conn, f"""
        SELECT c.COLUMN_NAME, c.DATA_TYPE, c.NULLABLE,
               c.NUM_DISTINCT, c.NUM_NULLS,
               c.AVG_COL_LEN, c.LAST_ANALYZED,
               CASE WHEN i.COLUMN_NAME IS NOT NULL THEN 'YES' ELSE 'NO' END AS has_index
        FROM ALL_TAB_COL_STATISTICS c
        LEFT JOIN (
            SELECT DISTINCT ic.TABLE_OWNER, ic.TABLE_NAME, ic.COLUMN_NAME
            FROM ALL_IND_COLUMNS ic
        ) i ON i.TABLE_NAME = c.TABLE_NAME AND i.TABLE_OWNER = c.OWNER
            AND i.COLUMN_NAME = c.COLUMN_NAME
        WHERE {where}
        ORDER BY c.COLUMN_ID
        FETCH FIRST 30 ROWS ONLY
    """, params)

    if not rows:
        return {"info": f"Aucune stat de colonne trouvée pour {t}"}
    return {"column_stats": rows}


def table_constraints(conn, table_name: str, schema: str = "") -> dict:
    """Contraintes de la table (PK, FK, UNIQUE, CHECK)."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None

    where = "c.TABLE_NAME = :tbl"
    params = {"tbl": t}
    if s:
        where += " AND c.OWNER = :sch"
        params["sch"] = s

    rows = _query(conn, f"""
        SELECT c.CONSTRAINT_NAME, c.CONSTRAINT_TYPE, c.STATUS, c.VALIDATED,
               LISTAGG(cc.COLUMN_NAME, ', ') WITHIN GROUP (ORDER BY cc.POSITION) AS columns,
               c.R_OWNER || '.' || c.R_CONSTRAINT_NAME AS references
        FROM ALL_CONSTRAINTS c
        JOIN ALL_CONS_COLUMNS cc ON cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME AND cc.OWNER = c.OWNER
        WHERE {where}
          AND c.CONSTRAINT_TYPE IN ('P', 'U', 'R', 'C')
        GROUP BY c.CONSTRAINT_NAME, c.CONSTRAINT_TYPE, c.STATUS, c.VALIDATED, c.R_OWNER, c.R_CONSTRAINT_NAME
        ORDER BY c.CONSTRAINT_TYPE
        FETCH FIRST 20 ROWS ONLY
    """, params)

    if not rows:
        return {"info": f"Aucune contrainte trouvée pour {t}"}
    return {"constraints": rows}


def sql_plan_history(conn, sql_id: str) -> dict:
    """Stats en temps réel depuis V$SQL (depuis dernier chargement en shared pool)."""
    # sql_id Oracle est en minuscules — ne pas upper()
    sid = sql_id.strip().strip("'\"")
    if not re.match(r'^[a-zA-Z0-9]+$', sid):
        return {"error": f"sql_id invalide : {sid!r}"}
    rows = _query(conn, """
        SELECT SQL_ID, PLAN_HASH_VALUE,
               EXECUTIONS,
               ROUND(ELAPSED_TIME / GREATEST(EXECUTIONS, 1) / 1000, 2) AS avg_elapsed_ms,
               ROUND(ELAPSED_TIME / 1000000, 2)                         AS total_elapsed_sec,
               ROUND(BUFFER_GETS / GREATEST(EXECUTIONS, 1), 0)          AS avg_buffer_gets,
               ROUND(DISK_READS  / GREATEST(EXECUTIONS, 1), 0)          AS avg_disk_reads,
               LAST_ACTIVE_TIME,
               FIRST_LOAD_TIME
        FROM V$SQL
        WHERE SQL_ID = :sid
        ORDER BY LAST_ACTIVE_TIME DESC
        FETCH FIRST 5 ROWS ONLY
    """, {"sid": sid})

    if not rows:
        return {"info": f"Requête {sid} non trouvée dans V$SQL (peut avoir été évincée du shared pool)"}
    return {"vsql_stats": rows}


def awr_sql_stats(conn, sql_id: str, days: str = "7") -> dict:
    """Stats AWR d'une requête sur les N derniers jours (snapshots horaires DBA_HIST_SQLSTAT).
    Donne les exécutions réelles par période avec les plans utilisés."""
    # sql_id Oracle est en minuscules — ne pas upper()
    sid = sql_id.strip().strip("'\"")
    if not re.match(r'^[a-zA-Z0-9]+$', sid):
        return {"error": f"sql_id invalide : {sid!r}"}
    try:
        d = max(1, min(int(days), 30))
    except ValueError:
        d = 7

    rows = _query(conn, """
        SELECT
            TO_CHAR(sn.begin_interval_time, 'YYYY-MM-DD HH24:MI') AS period_start,
            TO_CHAR(sn.end_interval_time,   'YYYY-MM-DD HH24:MI') AS period_end,
            s.plan_hash_value,
            s.executions_delta                                      AS executions,
            ROUND(s.elapsed_time_delta / GREATEST(s.executions_delta,1) / 1000, 2) AS avg_elapsed_ms,
            ROUND(s.elapsed_time_delta / 1000000, 2)               AS total_elapsed_sec,
            ROUND(s.buffer_gets_delta  / GREATEST(s.executions_delta,1), 0) AS avg_buffer_gets,
            ROUND(s.disk_reads_delta   / GREATEST(s.executions_delta,1), 0) AS avg_disk_reads,
            ROUND(s.rows_processed_delta / GREATEST(s.executions_delta,1), 2) AS avg_rows
        FROM DBA_HIST_SQLSTAT s
        JOIN DBA_HIST_SNAPSHOT sn ON sn.snap_id = s.snap_id AND sn.dbid = s.dbid
        WHERE s.sql_id = :sid
          AND sn.begin_interval_time >= SYSDATE - :d
          AND s.executions_delta > 0
        ORDER BY sn.begin_interval_time DESC
        FETCH FIRST 48 ROWS ONLY
    """, {"sid": sid, "d": d})

    if not rows:
        return {"info": f"Aucune donnée AWR pour {sid} sur les {d} derniers jours"}

    total_exec    = sum(r.get("executions") or 0 for r in rows)
    total_elapsed = sum(r.get("total_elapsed_sec") or 0 for r in rows)
    avg_ms        = round(total_elapsed * 1000 / total_exec, 2) if total_exec else 0
    plan_hashes   = list({r.get("plan_hash_value") for r in rows})

    return {
        "awr_summary": {
            "sql_id":               sid,
            "period_days":          d,
            "total_executions":     total_exec,
            "total_elapsed_sec":    round(total_elapsed, 2),
            "avg_elapsed_ms":       avg_ms,
            "distinct_plan_hashes": plan_hashes,
            "snapshots_count":      len(rows),
        },
        "awr_by_snapshot": rows[:20],
    }


def awr_top_sql(conn, days: str = "1", limit: str = "10") -> dict:
    """Top requêtes les plus coûteuses d'après AWR sur les N derniers jours."""
    try:
        d   = max(1, min(int(days),  30))
        lim = max(1, min(int(limit), 50))
    except ValueError:
        d, lim = 1, 10

    rows = _query(conn, """
        SELECT
            s.sql_id,
            MAX(SUBSTR(DBMS_LOB.SUBSTR(t.sql_text, 120, 1), 1, 120)) AS sql_preview,
            SUM(s.executions_delta)                                 AS total_executions,
            ROUND(SUM(s.elapsed_time_delta) / 1000000, 2)          AS total_elapsed_sec,
            ROUND(SUM(s.elapsed_time_delta)
                  / GREATEST(SUM(s.executions_delta),1) / 1000, 2) AS avg_elapsed_ms,
            ROUND(SUM(s.buffer_gets_delta)
                  / GREATEST(SUM(s.executions_delta),1), 0)         AS avg_buffer_gets,
            ROUND(SUM(s.disk_reads_delta)
                  / GREATEST(SUM(s.executions_delta),1), 0)         AS avg_disk_reads
        FROM DBA_HIST_SQLSTAT s
        JOIN DBA_HIST_SNAPSHOT sn ON sn.snap_id = s.snap_id AND sn.dbid = s.dbid
        LEFT JOIN DBA_HIST_SQLTEXT t ON t.sql_id = s.sql_id AND t.dbid = s.dbid
        WHERE sn.begin_interval_time >= SYSDATE - :d
          AND s.executions_delta > 0
        GROUP BY s.sql_id
        ORDER BY SUM(s.elapsed_time_delta) DESC
        FETCH FIRST :lim ROWS ONLY
    """, {"d": d, "lim": lim})

    if not rows:
        return {"info": f"Aucune donnée AWR sur les {d} derniers jours"}
    return {"top_sql": rows}


def related_views(conn, table_name: str, schema: str = "") -> dict:
    """Vues qui référencent cette table."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None

    where = "d.REFERENCED_NAME = :tbl AND d.REFERENCED_TYPE = 'TABLE'"
    params = {"tbl": t}
    if s:
        where += " AND d.REFERENCED_OWNER = :sch"
        params["sch"] = s

    rows = _query(conn, f"""
        SELECT DISTINCT d.OWNER, d.NAME, d.TYPE
        FROM ALL_DEPENDENCIES d
        WHERE {where}
          AND d.TYPE = 'VIEW'
        FETCH FIRST 10 ROWS ONLY
    """, params)

    if not rows:
        return {"info": f"Aucune vue ne référence {t}"}
    return {"views": rows}


def describe_table(conn, table_name: str, schema: str = "") -> dict:
    """Description complète d'une table : colonnes, types, contraintes, index, taille."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None

    where_cols = "c.TABLE_NAME = :tbl"
    where_idx  = "i.TABLE_NAME = :tbl"
    where_cst  = "cn.TABLE_NAME = :tbl"
    params = {"tbl": t}
    if s:
        where_cols += " AND c.OWNER = :sch"
        where_idx  += " AND i.TABLE_OWNER = :sch"
        where_cst  += " AND cn.OWNER = :sch"
        params["sch"] = s

    columns = _query(conn, f"""
        SELECT
            c.COLUMN_ID, c.COLUMN_NAME,
            c.DATA_TYPE
                || CASE
                    WHEN c.DATA_TYPE IN ('VARCHAR2','CHAR','NVARCHAR2','NCHAR')
                    THEN '(' || c.DATA_LENGTH || ')'
                    WHEN c.DATA_TYPE = 'NUMBER' AND c.DATA_PRECISION IS NOT NULL
                    THEN '(' || c.DATA_PRECISION ||
                         CASE WHEN c.DATA_SCALE > 0 THEN ',' || c.DATA_SCALE ELSE '' END || ')'
                    ELSE '' END AS data_type_full,
            c.NULLABLE, c.DATA_DEFAULT,
            cs.NUM_DISTINCT, cs.NUM_NULLS, cs.LAST_ANALYZED,
            CASE WHEN ic.COLUMN_NAME IS NOT NULL THEN 'YES' ELSE 'NO' END AS indexed
        FROM ALL_TAB_COLUMNS c
        LEFT JOIN ALL_TAB_COL_STATISTICS cs
            ON cs.TABLE_NAME = c.TABLE_NAME AND cs.OWNER = c.OWNER AND cs.COLUMN_NAME = c.COLUMN_NAME
        LEFT JOIN (
            SELECT DISTINCT TABLE_OWNER, TABLE_NAME, COLUMN_NAME FROM ALL_IND_COLUMNS
        ) ic ON ic.TABLE_NAME = c.TABLE_NAME AND ic.TABLE_OWNER = c.OWNER AND ic.COLUMN_NAME = c.COLUMN_NAME
        WHERE {where_cols}
        ORDER BY c.COLUMN_ID
    """, params)

    indexes = _query(conn, f"""
        SELECT i.INDEX_NAME, i.UNIQUENESS, i.STATUS, i.INDEX_TYPE,
               LISTAGG(ic.COLUMN_NAME || CASE ic.DESCEND WHEN 'DESC' THEN ' DESC' ELSE '' END, ', ')
                   WITHIN GROUP (ORDER BY ic.COLUMN_POSITION) AS columns
        FROM ALL_INDEXES i
        JOIN ALL_IND_COLUMNS ic ON ic.INDEX_NAME = i.INDEX_NAME AND ic.TABLE_OWNER = i.TABLE_OWNER
        WHERE {where_idx}
        GROUP BY i.INDEX_NAME, i.UNIQUENESS, i.STATUS, i.INDEX_TYPE
        ORDER BY i.UNIQUENESS DESC, i.INDEX_NAME
    """, params)

    constraints = _query(conn, f"""
        SELECT cn.CONSTRAINT_NAME,
               DECODE(cn.CONSTRAINT_TYPE,'P','PRIMARY KEY','U','UNIQUE','R','FOREIGN KEY','C','CHECK',cn.CONSTRAINT_TYPE) AS type,
               cn.STATUS, cn.VALIDATED,
               LISTAGG(cc.COLUMN_NAME, ', ') WITHIN GROUP (ORDER BY cc.POSITION) AS columns,
               cn.R_OWNER || '.' || cn.R_CONSTRAINT_NAME AS references
        FROM ALL_CONSTRAINTS cn
        JOIN ALL_CONS_COLUMNS cc ON cc.CONSTRAINT_NAME = cn.CONSTRAINT_NAME AND cc.OWNER = cn.OWNER
        WHERE {where_cst} AND cn.CONSTRAINT_TYPE IN ('P','U','R','C')
        GROUP BY cn.CONSTRAINT_NAME, cn.CONSTRAINT_TYPE, cn.STATUS, cn.VALIDATED, cn.R_OWNER, cn.R_CONSTRAINT_NAME
        ORDER BY cn.CONSTRAINT_TYPE
    """, params)

    stats = _query(conn, f"""
        SELECT NUM_ROWS, BLOCKS, ROUND(BLOCKS*8/1024,2) AS size_mb, LAST_ANALYZED
        FROM ALL_TAB_STATISTICS
        WHERE TABLE_NAME = :tbl {'AND OWNER = :sch' if s else ''}
        ORDER BY LAST_ANALYZED DESC NULLS LAST FETCH FIRST 1 ROW ONLY
    """, params)

    if not columns:
        return {"error": f"Table {t} introuvable ou droits insuffisants"}
    return {
        "table": f"{s + '.' if s else ''}{t}",
        "stats": stats[0] if stats else {},
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "note": f"{len(columns)} colonnes, {len(indexes)} index, {len(constraints)} contraintes"
    }


def describe_object(conn, object_name: str, schema: str = "") -> dict:
    """Description complète de n'importe quel objet Oracle : table, vue, procédure, fonction,
    package, trigger, séquence, synonyme, type, index, etc."""
    n = _safe_name(object_name)
    s = _safe_name(schema) if schema else None
    params = {"obj": n}
    where_owner = " AND OWNER = :sch" if s else ""
    if s:
        params["sch"] = s

    # 1. Détecter le type de l'objet
    obj_info = _query(conn, f"""
        SELECT OBJECT_NAME, OBJECT_TYPE, OWNER, STATUS, CREATED, LAST_DDL_TIME
        FROM ALL_OBJECTS
        WHERE OBJECT_NAME = :obj {where_owner}
          AND OBJECT_TYPE NOT IN ('PACKAGE BODY')  -- on liste le PACKAGE, pas le body
        ORDER BY OBJECT_TYPE
        FETCH FIRST 5 ROWS ONLY
    """, params)

    if not obj_info:
        return {"error": f"Objet '{n}' introuvable (vérifiez le nom et le schéma)"}

    result = {"objects": obj_info, "details": {}}

    for obj in obj_info:
        obj_type = (obj.get("object_type") or "").upper()
        owner    = obj.get("owner") or s or ""
        p        = {"obj": n, "sch": owner}

        if obj_type == "TABLE":
            result["details"][obj_type] = describe_table(conn, n, owner)

        elif obj_type == "VIEW":
            # Colonnes de la vue
            cols = _query(conn, """
                SELECT COLUMN_NAME, DATA_TYPE, COLUMN_ID, NULLABLE
                FROM ALL_TAB_COLUMNS
                WHERE TABLE_NAME = :obj AND OWNER = :sch
                ORDER BY COLUMN_ID
            """, p)
            # Texte de la vue via DBMS_METADATA (ALL_VIEWS.TEXT est de type LONG, incompatible)
            view_text = ""
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT DBMS_METADATA.GET_DDL('VIEW', :obj, :sch) FROM DUAL", p
                )
                row = cur.fetchone()
                if row and row[0]:
                    view_text = str(row[0])[:3000]
            except Exception:
                view_text = "(texte non disponible)"
            result["details"][obj_type] = {"columns": cols, "view_text": view_text}

        elif obj_type in ("PROCEDURE", "FUNCTION"):
            src = _query(conn, """
                SELECT LINE, TEXT FROM ALL_SOURCE
                WHERE NAME = :obj AND OWNER = :sch AND TYPE = :typ
                ORDER BY LINE
                FETCH FIRST 200 ROWS ONLY
            """, {**p, "typ": obj_type})
            args = _query(conn, """
                SELECT ARGUMENT_NAME, POSITION, IN_OUT, DATA_TYPE, DEFAULTED
                FROM ALL_ARGUMENTS
                WHERE OBJECT_NAME = :obj AND OWNER = :sch
                ORDER BY POSITION
            """, p)
            result["details"][obj_type] = {
                "arguments": args,
                "source_lines": len(src),
                "source": "".join(r.get("text","") for r in src)
            }

        elif obj_type in ("PACKAGE", "PACKAGE BODY"):
            src = _query(conn, """
                SELECT LINE, TEXT FROM ALL_SOURCE
                WHERE NAME = :obj AND OWNER = :sch AND TYPE = 'PACKAGE'
                ORDER BY LINE FETCH FIRST 300 ROWS ONLY
            """, p)
            result["details"][obj_type] = {
                "source_lines": len(src),
                "source": "".join(r.get("text","") for r in src)
            }

        elif obj_type == "TRIGGER":
            rows = _query(conn, """
                SELECT TRIGGER_NAME, TRIGGER_TYPE, TRIGGERING_EVENT,
                       TABLE_OWNER, TABLE_NAME, STATUS,
                       SUBSTR(TRIGGER_BODY, 1, 2000) AS body
                FROM ALL_TRIGGERS
                WHERE TRIGGER_NAME = :obj AND OWNER = :sch
            """, p)
            result["details"][obj_type] = {"trigger": rows}

        elif obj_type == "SEQUENCE":
            rows = _query(conn, """
                SELECT SEQUENCE_NAME, MIN_VALUE, MAX_VALUE, INCREMENT_BY,
                       CYCLE_FLAG, ORDER_FLAG, CACHE_SIZE, LAST_NUMBER
                FROM ALL_SEQUENCES
                WHERE SEQUENCE_NAME = :obj AND SEQUENCE_OWNER = :sch
            """, p)
            result["details"][obj_type] = {"sequence": rows}

        elif obj_type == "SYNONYM":
            rows = _query(conn, """
                SELECT SYNONYM_NAME, TABLE_OWNER, TABLE_NAME, DB_LINK
                FROM ALL_SYNONYMS
                WHERE SYNONYM_NAME = :obj AND OWNER = :sch
            """, p)
            result["details"][obj_type] = {"synonym": rows}

        elif obj_type == "INDEX":
            rows = _query(conn, """
                SELECT i.INDEX_NAME, i.TABLE_OWNER, i.TABLE_NAME,
                       i.INDEX_TYPE, i.UNIQUENESS, i.STATUS, i.VISIBILITY,
                       i.NUM_ROWS, i.LAST_ANALYZED,
                       LISTAGG(ic.COLUMN_NAME ||
                           CASE ic.DESCEND WHEN 'DESC' THEN ' DESC' ELSE '' END, ', ')
                           WITHIN GROUP (ORDER BY ic.COLUMN_POSITION) AS columns
                FROM ALL_INDEXES i
                JOIN ALL_IND_COLUMNS ic ON ic.INDEX_NAME = i.INDEX_NAME AND ic.TABLE_OWNER = i.TABLE_OWNER
                WHERE i.INDEX_NAME = :obj AND i.TABLE_OWNER = :sch
                GROUP BY i.INDEX_NAME, i.TABLE_OWNER, i.TABLE_NAME,
                         i.INDEX_TYPE, i.UNIQUENESS, i.STATUS, i.VISIBILITY,
                         i.NUM_ROWS, i.LAST_ANALYZED
            """, p)
            result["details"][obj_type] = {"index": rows}

        elif obj_type == "TYPE":
            src = _query(conn, """
                SELECT LINE, TEXT FROM ALL_SOURCE
                WHERE NAME = :obj AND OWNER = :sch AND TYPE = 'TYPE'
                ORDER BY LINE FETCH FIRST 100 ROWS ONLY
            """, p)
            result["details"][obj_type] = {
                "source": "".join(r.get("text","") for r in src)
            }

        else:
            # Fallback : juste les infos ALL_OBJECTS
            result["details"][obj_type] = {"info": obj}

    return result



    """Description complète d'une table : colonnes, types, contraintes, index, taille."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None

    where_cols  = "c.TABLE_NAME = :tbl"
    where_idx   = "i.TABLE_NAME = :tbl"
    where_cst   = "cn.TABLE_NAME = :tbl"
    params = {"tbl": t}
    if s:
        where_cols += " AND c.OWNER = :sch"
        where_idx  += " AND i.TABLE_OWNER = :sch"
        where_cst  += " AND cn.OWNER = :sch"
        params["sch"] = s

    # Colonnes complètes
    columns = _query(conn, f"""
        SELECT
            c.COLUMN_ID,
            c.COLUMN_NAME,
            c.DATA_TYPE
                || CASE
                    WHEN c.DATA_TYPE IN ('VARCHAR2','CHAR','NVARCHAR2','NCHAR')
                    THEN '(' || c.DATA_LENGTH || ')'
                    WHEN c.DATA_TYPE = 'NUMBER' AND c.DATA_PRECISION IS NOT NULL
                    THEN '(' || c.DATA_PRECISION ||
                         CASE WHEN c.DATA_SCALE > 0 THEN ',' || c.DATA_SCALE ELSE '' END || ')'
                    ELSE ''
                   END AS data_type_full,
            c.NULLABLE,
            c.DATA_DEFAULT,
            cs.NUM_DISTINCT,
            cs.NUM_NULLS,
            cs.LAST_ANALYZED,
            CASE WHEN ic.COLUMN_NAME IS NOT NULL THEN 'YES' ELSE 'NO' END AS indexed
        FROM ALL_TAB_COLUMNS c
        LEFT JOIN ALL_TAB_COL_STATISTICS cs
            ON cs.TABLE_NAME = c.TABLE_NAME AND cs.OWNER = c.OWNER AND cs.COLUMN_NAME = c.COLUMN_NAME
        LEFT JOIN (
            SELECT DISTINCT TABLE_OWNER, TABLE_NAME, COLUMN_NAME
            FROM ALL_IND_COLUMNS
        ) ic ON ic.TABLE_NAME = c.TABLE_NAME AND ic.TABLE_OWNER = c.OWNER
            AND ic.COLUMN_NAME = c.COLUMN_NAME
        WHERE {where_cols}
        ORDER BY c.COLUMN_ID
    """, params)

    # Index
    indexes = _query(conn, f"""
        SELECT i.INDEX_NAME, i.UNIQUENESS, i.STATUS, i.INDEX_TYPE,
               LISTAGG(ic.COLUMN_NAME || CASE ic.DESCEND WHEN 'DESC' THEN ' DESC' ELSE '' END, ', ')
                   WITHIN GROUP (ORDER BY ic.COLUMN_POSITION) AS columns
        FROM ALL_INDEXES i
        JOIN ALL_IND_COLUMNS ic ON ic.INDEX_NAME = i.INDEX_NAME AND ic.TABLE_OWNER = i.TABLE_OWNER
        WHERE {where_idx}
        GROUP BY i.INDEX_NAME, i.UNIQUENESS, i.STATUS, i.INDEX_TYPE
        ORDER BY i.UNIQUENESS DESC, i.INDEX_NAME
    """, params)

    # Contraintes
    constraints = _query(conn, f"""
        SELECT cn.CONSTRAINT_NAME,
               DECODE(cn.CONSTRAINT_TYPE,'P','PRIMARY KEY','U','UNIQUE','R','FOREIGN KEY','C','CHECK', cn.CONSTRAINT_TYPE) AS type,
               cn.STATUS, cn.VALIDATED,
               LISTAGG(cc.COLUMN_NAME, ', ') WITHIN GROUP (ORDER BY cc.POSITION) AS columns,
               cn.R_OWNER || '.' || cn.R_CONSTRAINT_NAME AS references
        FROM ALL_CONSTRAINTS cn
        JOIN ALL_CONS_COLUMNS cc ON cc.CONSTRAINT_NAME = cn.CONSTRAINT_NAME AND cc.OWNER = cn.OWNER
        WHERE {where_cst}
          AND cn.CONSTRAINT_TYPE IN ('P','U','R','C')
        GROUP BY cn.CONSTRAINT_NAME, cn.CONSTRAINT_TYPE, cn.STATUS, cn.VALIDATED, cn.R_OWNER, cn.R_CONSTRAINT_NAME
        ORDER BY cn.CONSTRAINT_TYPE
    """, params)

    # Stats globales
    stats = _query(conn, f"""
        SELECT NUM_ROWS, BLOCKS, ROUND(BLOCKS*8/1024,2) AS size_mb, LAST_ANALYZED
        FROM ALL_TAB_STATISTICS
        WHERE TABLE_NAME = :tbl {' AND OWNER = :sch' if s else ''}
        ORDER BY LAST_ANALYZED DESC NULLS LAST
        FETCH FIRST 1 ROW ONLY
    """, params)

    if not columns:
        return {"error": f"Table {t} introuvable ou droits insuffisants"}

    return {
        "table": f"{s + '.' if s else ''}{t}",
        "stats": stats[0] if stats else {},
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "note": f"{len(columns)} colonnes, {len(indexes)} index, {len(constraints)} contraintes"
    }


# Limite de sécurité pour les SELECT libres
_MAX_ROWS_FREE_SELECT = 50
_SELECT_ONLY = re.compile(r'^\s*SELECT\b', re.IGNORECASE)
_SUBQUERY_DANGEROUS = re.compile(
    r'\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|EXECUTE|CALL|COMMIT|ROLLBACK)\b',
    re.IGNORECASE
)

def run_select(conn, sql: str, limit: str = "20") -> dict:
    """Exécute un SELECT libre en lecture seule (usage exceptionnel par l'IA).
    Limité à 50 lignes maximum. Uniquement des SELECT purs."""
    from collector.connection import query_execution_enabled
    if not query_execution_enabled():
        return {"error": "Execution libre desactivee (ODIN_ALLOW_QUERY_EXECUTION)"}
    sql = sql.strip().rstrip(';')

    # Sécurité stricte
    if not _SELECT_ONLY.match(sql):
        return {"error": "Seuls les SELECT sont autorisés"}
    if _SUBQUERY_DANGEROUS.search(sql):
        return {"error": "Requête refusée : contient des mots-clés non autorisés"}

    try:
        lim = max(1, min(int(limit), _MAX_ROWS_FREE_SELECT))
    except (ValueError, TypeError):
        lim = 20

    wrapped = f"SELECT * FROM ({sql}) WHERE ROWNUM <= {lim}"

    try:
        rows = _query(conn, wrapped)
        return {
            "rows": rows,
            "count": len(rows),
            "limited_to": lim,
            "warning": "SELECT libre — usage exceptionnel uniquement. Requête exécutée en lecture seule."
        }
    except Exception as e:
        return {"error": str(e), "sql": sql[:200]}


# ─────────────────────────────────────────────
# Collecte de statistiques (opt-in, écriture)
# ─────────────────────────────────────────────
def gather_table_stats(conn, table_name: str, schema: str = "", estimate_percent: str = "AUTO_SAMPLE_SIZE") -> dict:
    """Lance DBMS_STATS.GATHER_TABLE_STATS sur une table. Opération d'écriture — uniquement si activé dans les settings."""
    from db.store import get_setting
    if get_setting("gather_stats_enabled", "false") != "true":
        return {"error": "Collecte de statistiques désactivée. Activer dans Administration > Outils IA."}
    tname = _safe_name(table_name)
    sch = _safe_name(schema) if schema else None
    owner = f"'{sch}'" if sch else "NULL"
    try:
        pct = "DBMS_STATS.AUTO_SAMPLE_SIZE" if estimate_percent.upper() in ("", "AUTO", "AUTO_SAMPLE_SIZE") else str(int(estimate_percent))
        cur = conn.cursor()
        cur.execute(f"""
            BEGIN
                DBMS_STATS.GATHER_TABLE_STATS(
                    ownname          => {owner},
                    tabname          => '{tname}',
                    estimate_percent => {pct},
                    method_opt       => 'FOR ALL COLUMNS SIZE AUTO',
                    cascade          => TRUE,
                    no_invalidate    => FALSE
                );
            END;
        """)
        conn.commit()
        return {"ok": True, "message": f"Statistiques collectées sur {sch + '.' if sch else ''}{tname} avec succès."}
    except Exception as e:
        return {"error": f"Erreur GATHER_TABLE_STATS : {e}"}


# ─────────────────────────────────────────────
def explain_plan(conn, sql_id: str) -> dict:
    """Génère un plan d'exécution via EXPLAIN PLAN FOR en cherchant le texte SQL dans V$SQL puis AWR.
    N'exécute pas la requête — 100% safe en production."""
    sid = sql_id.strip().strip("'\"")
    if not re.match(r'^[a-zA-Z0-9]+$', sid):
        return {"error": f"sql_id invalide : {sid!r}"}

    # 1. Récupérer le texte SQL complet
    sql_text = None
    try:
        cur = conn.cursor()
        cur.prefetchrows = 0
        cur.execute("SELECT sql_fulltext FROM v$sql WHERE sql_id=:sid AND ROWNUM=1", sid=sid)
        row = cur.fetchone()
        if row and row[0] is not None:
            val = row[0]
            sql_text = val.read() if hasattr(val, 'read') else str(val)
            if not sql_text or len(sql_text) < 5:
                sql_text = None
    except Exception:
        pass

    if not sql_text:
        try:
            cur = conn.cursor()
            cur.prefetchrows = 0
            cur.execute("SELECT sql_text FROM dba_hist_sqltext WHERE sql_id=:sid AND ROWNUM=1", sid=sid)
            row = cur.fetchone()
            if row and row[0] is not None:
                val = row[0]
                sql_text = val.read() if hasattr(val, 'read') else str(val)
                if not sql_text or len(sql_text) < 5:
                    sql_text = None
        except Exception:
            pass

    if not sql_text:
        return {"error": f"Texte SQL introuvable pour sql_id={sid} (ni V$SQL ni AWR)"}

    # 2. EXPLAIN PLAN FOR <sql>
    try:
        cur = conn.cursor()
        # Nettoyer le statement_id pour ce sql_id
        stmt_id = f"ODIN_{sid[:20]}"
        try:
            cur.execute("DELETE FROM plan_table WHERE statement_id=:sid", sid=stmt_id)
        except Exception:
            pass
        # Lancer EXPLAIN PLAN
        explain_sql = f"EXPLAIN PLAN SET STATEMENT_ID='{stmt_id}' FOR {sql_text.rstrip(';')}"
        cur.execute(explain_sql)
        # Lire le plan via DBMS_XPLAN.DISPLAY
        plan_lines = []
        cur2 = conn.cursor()
        cur2.execute(
            "SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', :stmt_id, 'ALL'))",
            stmt_id=stmt_id
        )
        for r in cur2.fetchall():
            plan_lines.append(str(r[0]))
        # Nettoyage plan_table
        try:
            cur.execute("DELETE FROM plan_table WHERE statement_id=:sid", sid=stmt_id)
            conn.commit()
        except Exception:
            pass
        if plan_lines:
            return {"source": "explain_plan", "plan": "\n".join(plan_lines)}
        return {"error": "EXPLAIN PLAN a été exécuté mais aucune ligne retournée"}
    except Exception as e:
        return {"error": f"Erreur EXPLAIN PLAN : {e}"}


# ─────────────────────────────────────────────
# Registre des outils
# ─────────────────────────────────────────────
def bind_captures(conn, sql_id: str) -> dict:
    """Récupère les dernières valeurs capturées des bind variables depuis V$SQL_BIND_CAPTURE."""
    try:
        rows = _query(conn, """
            SELECT
                b.name        AS bind_name,
                b.position    AS position,
                b.datatype_string AS datatype,
                b.value_string AS value,
                b.last_captured AS last_captured,
                b.max_length   AS max_length
            FROM v$sql_bind_capture b
            WHERE b.sql_id = :sql_id
              AND b.value_string IS NOT NULL
            ORDER BY b.child_number DESC, b.position ASC
        """, {"sql_id": sql_id})

        if not rows:
            # Peut-être pas encore capturé — essayer sans filtre value_string
            rows_all = _query(conn, """
                SELECT
                    b.name, b.position, b.datatype_string AS datatype,
                    b.value_string AS value, b.last_captured
                FROM v$sql_bind_capture b
                WHERE b.sql_id = :sql_id
                ORDER BY b.child_number DESC, b.position ASC
            """, {"sql_id": sql_id})
            return {
                "sql_id": sql_id,
                "captured": False,
                "message": "Aucune valeur capturée pour ce sql_id (Oracle capture périodiquement, pas à chaque exécution)",
                "binds_without_value": rows_all
            }

        # Déduplique par nom (garder la capture la plus récente)
        seen = {}
        for r in rows:
            name = r["bind_name"]
            if name not in seen:
                seen[name] = r

        return {
            "sql_id": sql_id,
            "captured": True,
            "count": len(seen),
            "binds": list(seen.values()),
            "note": "Valeurs issues de la dernière capture Oracle (V$SQL_BIND_CAPTURE). Oracle capture env. 1 fois toutes les 15 min par cursor."
        }
    except Exception as e:
        return {"error": str(e), "sql_id": sql_id}


# ─────────────────────────────────────────────
# Nouveaux outils
# ─────────────────────────────────────────────

def mview_definition(conn, mview_name: str, schema: str = "") -> dict:
    """Définition complète d'une vue matérialisée Oracle : requête source, options de refresh,
    tables sous-jacentes, colonnes et dernière date de refresh."""
    n = _safe_name(mview_name)
    s = _safe_name(schema) if schema else None
    p = {"obj": n}
    where_owner = " AND OWNER = :sch" if s else ""
    if s:
        p["sch"] = s

    # Info principale
    mview = _query(conn, f"""
        SELECT OWNER, MVIEW_NAME, CONTAINER_NAME, QUERY_LEN,
               REFRESH_MODE, REFRESH_METHOD, BUILD_MODE,
               FAST_REFRESHABLE, LAST_REFRESH_TYPE,
               TO_CHAR(LAST_REFRESH_DATE, 'YYYY-MM-DD HH24:MI:SS') AS LAST_REFRESH_DATE,
               COMPILE_STATE,
               STALENESS,
               TO_CHAR(STALE_SINCE, 'YYYY-MM-DD HH24:MI:SS') AS STALE_SINCE
        FROM ALL_MVIEWS
        WHERE MVIEW_NAME = :obj {where_owner}
    """, p)
    if not mview:
        return {"error": f"Vue matérialisée '{n}' introuvable. Vérifiez le nom et le schéma."}

    owner = mview[0].get("owner") or (s or "")
    p2 = {"obj": n, "sch": owner}

    # Texte de la requête via DBMS_METADATA
    query_text = ""
    try:
        cur = conn.cursor()
        cur.execute("SELECT DBMS_METADATA.GET_DDL('MATERIALIZED_VIEW', :obj, :sch) FROM DUAL", p2)
        row = cur.fetchone()
        if row and row[0]:
            query_text = str(row[0])[:4000]
    except Exception:
        query_text = "(DDL non disponible)"

    # Colonnes
    cols = _query(conn, """
        SELECT COLUMN_NAME, DATA_TYPE, COLUMN_ID, NULLABLE
        FROM ALL_TAB_COLUMNS
        WHERE TABLE_NAME = :obj AND OWNER = :sch
        ORDER BY COLUMN_ID
    """, p2)

    # Tables/vues sources (DETAIL)
    detail = _query(conn, """
        SELECT DETAILOBJ_OWNER, DETAILOBJ_NAME, DETAILOBJ_TYPE, DETAILOBJ_ALIAS
        FROM ALL_MVIEW_DETAIL_RELATIONS
        WHERE MVIEW_NAME = :obj AND OWNER = :sch
    """, p2)

    return {
        "mview": mview[0],
        "ddl": query_text,
        "columns": cols,
        "source_objects": detail,
    }


def mview_logs(conn, table_name: str, schema: str = "") -> dict:
    """Logs d'une vue matérialisée Oracle sur une table source :
    configuration du log (colonnes trackées, rowid, pk, sequence),
    volume de lignes en attente de refresh."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None
    p = {"tbl": t}
    where_owner = " AND LOG_OWNER = :sch" if s else ""
    if s:
        p["sch"] = s

    # Configuration du log
    log_info = _query(conn, f"""
        SELECT LOG_OWNER, LOG_TABLE, MASTER,
               ROWIDS, PRIMARY_KEY, OBJECT_ID, FILTER_COLUMNS, SEQUENCE,
               INCLUDE_NEW_VALUES, STAGING_LOG,
               TO_CHAR(LAST_PURGE_DATE, 'YYYY-MM-DD HH24:MI:SS') AS LAST_PURGE_DATE,
               NUM_ROWS_PURGED
        FROM ALL_MVIEW_LOGS
        WHERE MASTER = :tbl {where_owner}
    """, p)

    if not log_info:
        return {"error": f"Aucun log de vue matérialisée trouvé pour la table '{t}'.",
                "hint": "Le log n'existe que si la table est une source pour une MV en fast refresh."}

    result = {"logs": log_info, "log_volumes": []}

    # Volume de chaque log (lignes en attente)
    for lg in log_info:
        log_owner = lg.get("log_owner") or (s or "")
        log_table = lg.get("log_table") or ""
        if log_table:
            try:
                cnt = _query(conn, "SELECT COUNT(*) AS row_count FROM \""
                             + log_owner + "\".\"" + log_table + "\"")
                result["log_volumes"].append({
                    "log_table": log_table,
                    "log_owner": log_owner,
                    "pending_rows": cnt[0].get("row_count", 0) if cnt else 0,
                })
            except Exception as e:
                result["log_volumes"].append({"log_table": log_table, "error": str(e)})

    return result


def table_dml_since_stats(conn, table_name: str, schema: str = "") -> dict:
    """Nombre d'INSERTs, UPDATEs et DELETEs non encore pris en compte dans les stats Oracle
    sur une table depuis la dernière collecte des statistiques (via ALL_TAB_MODIFICATIONS)."""
    t = _safe_name(table_name)
    s = _safe_name(schema) if schema else None
    p = {"tbl": t}
    where_owner = " AND TABLE_OWNER = :sch" if s else ""
    if s:
        p["sch"] = s

    rows = _query(conn, f"""
        SELECT TABLE_OWNER, TABLE_NAME,
               INSERTS, UPDATES, DELETES, TRUNCATED,
               TO_CHAR(TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS') AS LAST_DML_TIME
        FROM ALL_TAB_MODIFICATIONS
        WHERE TABLE_NAME = :tbl {where_owner}
        ORDER BY INSERTS + UPDATES + DELETES DESC
    """, p)

    if not rows:
        return {
            "table": t,
            "schema": s or "",
            "inserts": 0, "updates": 0, "deletes": 0,
            "message": "Aucune modification non analysée détectée (ou stats à jour). "
                       "Essayez DBMS_STATS.FLUSH_DATABASE_MONITORING_INFO si les données semblent manquantes."
        }

    total_ins = sum(r.get("inserts", 0) or 0 for r in rows)
    total_upd = sum(r.get("updates", 0) or 0 for r in rows)
    total_del = sum(r.get("deletes", 0) or 0 for r in rows)
    return {
        "table": t,
        "schema": s or "",
        "total_inserts_since_stats": total_ins,
        "total_updates_since_stats": total_upd,
        "total_deletes_since_stats": total_del,
        "staleness_estimate": total_ins + total_upd + total_del,
        "partitions": rows,
        "hint": "Ces compteurs sont remis à zéro après DBMS_STATS.GATHER_TABLE_STATS."
    }


def scheduler_jobs(conn, schema: str = "", job_name: str = "") -> dict:
    """Configuration et historique récents des jobs Oracle Scheduler (DBMS_SCHEDULER).
    Retourne les jobs correspondants avec leur statut, planning, dernière / prochaine exécution
    et les 10 derniers runs."""
    s = _safe_name(schema) if schema else None
    j = _safe_name(job_name) if job_name else None

    where_parts = []
    p: dict = {}
    if s:
        where_parts.append("OWNER = :sch")
        p["sch"] = s
    if j:
        where_parts.append("JOB_NAME = :job")
        p["job"] = j
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    jobs = _query(conn, f"""
        SELECT OWNER, JOB_NAME, JOB_TYPE, JOB_ACTION,
               SCHEDULE_TYPE, REPEAT_INTERVAL,
               TO_CHAR(NEXT_RUN_DATE, 'YYYY-MM-DD HH24:MI:SS') AS NEXT_RUN_DATE,
               TO_CHAR(LAST_START_DATE, 'YYYY-MM-DD HH24:MI:SS') AS LAST_START_DATE,
               TO_CHAR(LAST_RUN_DURATION) AS LAST_RUN_DURATION,
               STATE, ENABLED, FAILURE_COUNT, RUN_COUNT
        FROM ALL_SCHEDULER_JOBS
        {where}
        ORDER BY OWNER, JOB_NAME
        FETCH FIRST 50 ROWS ONLY
    """, p)

    # Historique des derniers runs
    hist = _query(conn, f"""
        SELECT OWNER, JOB_NAME,
               TO_CHAR(LOG_DATE, 'YYYY-MM-DD HH24:MI:SS') AS LOG_DATE,
               STATUS, TO_CHAR(ACTUAL_START_DATE, 'YYYY-MM-DD HH24:MI:SS') AS ACTUAL_START_DATE,
               TO_CHAR(RUN_DURATION) AS RUN_DURATION,
               ERROR#, ADDITIONAL_INFO
        FROM ALL_SCHEDULER_JOB_RUN_DETAILS
        {where}
        ORDER BY LOG_DATE DESC
        FETCH FIRST 20 ROWS ONLY
    """, p)

    return {
        "jobs": jobs,
        "recent_runs": hist,
        "count": len(jobs),
    }


def active_locks(conn) -> dict:
    """Verrous actifs Oracle : sessions bloqueuses et bloquées,
    ressources verrouillées (table, ligne), type de verrou et durée d'attente."""

    # Verrous au niveau session (DML locks sur objets)
    locks = _query(conn, """
        SELECT
            l.SID,
            s.SERIAL#,
            s.USERNAME,
            s.STATUS      AS SESSION_STATUS,
            s.MACHINE,
            s.PROGRAM,
            l.TYPE,
            l.LMODE,
            l.REQUEST,
            l.BLOCK,
            TO_CHAR(TRUNC((SYSDATE - s.LOGON_TIME) * 24), 'FM990') || 'h' ||
                   TO_CHAR(MOD(TRUNC((SYSDATE - s.LOGON_TIME) * 1440), 60), 'FM00') || 'm' AS SESSION_AGE,
            o.OBJECT_NAME,
            o.OBJECT_TYPE,
            o.OWNER        AS OBJECT_OWNER
        FROM V$LOCK l
        JOIN V$SESSION s ON s.SID = l.SID
        LEFT JOIN ALL_OBJECTS o ON o.OBJECT_ID = l.ID1 AND l.TYPE = 'TM'
        WHERE l.LMODE > 0
          AND s.USERNAME IS NOT NULL
          AND s.TYPE != 'BACKGROUND'
        ORDER BY l.BLOCK DESC, l.SID
        FETCH FIRST 30 ROWS ONLY
    """)

    # Chaînes de blocage (qui bloque qui)
    blocking = _query(conn, """
        SELECT
            w.SID          AS WAITING_SID,
            w.SERIAL#      AS WAITING_SERIAL,
            w.USERNAME     AS WAITING_USER,
            w.EVENT        AS WAIT_EVENT,
            w.SECONDS_IN_WAIT,
            w.STATE,
            b.SID          AS BLOCKING_SID,
            b.USERNAME     AS BLOCKING_USER,
            b.STATUS       AS BLOCKING_STATUS,
            b.MACHINE      AS BLOCKING_MACHINE
        FROM V$SESSION w
        JOIN V$SESSION b ON b.SID = w.BLOCKING_SESSION
        WHERE w.BLOCKING_SESSION IS NOT NULL
        ORDER BY w.SECONDS_IN_WAIT DESC
        FETCH FIRST 20 ROWS ONLY
    """)

    if not locks and not blocking:
        return {"message": "Aucun verrou actif détecté.", "locks": [], "blocking_chains": []}

    return {
        "locks": locks,
        "blocking_chains": blocking,
        "summary": f"{len(locks)} verrou(s) actif(s), {len(blocking)} chaîne(s) de blocage."
    }

def describe_mview(conn, mview_name: str, schema: str = "") -> dict:
    """Description complète d'une vue matérialisée Oracle : DDL, options de refresh,
    tables sources, colonnes, date du dernier refresh ET log MV (config + lignes en attente).
    Remplace mview_definition + mview_logs en un seul appel."""
    # Définition
    defn = mview_definition(conn, mview_name, schema)
    if "error" in defn:
        return defn
    # Log MV sur chaque table source
    source_logs = {}
    for src in defn.get("source_objects", []):
        src_name  = src.get("detailobj_name") or ""
        src_owner = src.get("detailobj_owner") or schema
        if src_name:
            source_logs[src_name] = mview_logs(conn, src_name, src_owner)
    defn["source_logs"] = source_logs
    return defn


TOOLS = {
    # ─ Table
    "describe_table":        describe_table,    # colonnes + index + contraintes + stats
    "table_dml_since_stats": table_dml_since_stats,
    # ─ Objet générique (vue, proc, package, trigger, séquence...)
    "describe_object":       describe_object,
    # ─ Vue matérialisée
    "describe_mview":        describe_mview,    # définition + log MV en un seul appel
    # ─ SQL & performance
    "sql_plan_history":      sql_plan_history,
    "explain_plan":          explain_plan,      # génère un plan via EXPLAIN PLAN FOR (safe, sans exécution)
    "awr_sql_stats":         awr_sql_stats,
    "awr_top_sql":           awr_top_sql,
    "bind_captures":         bind_captures,
    # ─ Scheduler & locks
    "scheduler_jobs":        scheduler_jobs,
    "active_locks":          active_locks,
    # ─ SELECT libre (usage exceptionnel)
    "run_select":            run_select,
    # ─ Statistiques (opt-in, écriture)
    "gather_table_stats":    gather_table_stats,
    # ─ Legacy (mode classique uniquement)
    "table_stats":           table_stats,
    "index_list":            index_list,
    "column_stats":          column_stats,
    "table_constraints":     table_constraints,
    "related_views":         related_views,
    "mview_definition":      mview_definition,
    "mview_logs":            mview_logs,
}

TOOLS_DESCRIPTION = """
Tu as accès à des outils pour interroger la base Oracle en lecture seule.
Pour appeler un outil, écris une ligne exactement ainsi (sans autre texte autour) :

TOOL: <nom_outil>(<argument1>, <argument2>)

⚠️ CASSE DES NOMS D'OBJETS ORACLE :
- Les objets créés SANS guillemets sont stockés en MAJUSCULES dans Oracle
- Les objets créés AVEC guillemets préservent leur casse exacte
- Utilise toujours la casse EXACTE telle qu'elle apparaît dans le SQL analysé

Outils disponibles :
- `describe_table(table_name, schema?)` — description complète : colonnes, index, contraintes, taille (remplace table_stats/index_list/column_stats/table_constraints)
- `table_dml_since_stats(table_name, schema?)` — INSERTs/UPDATEs/DELETEs non analysés depuis la dernière collecte de stats
- `describe_object(object_name, schema?)` — N'IMPORTE QUEL objet : vue, procédure, package, trigger, séquence, synonyme...
- `describe_mview(mview_name, schema?)` — définition + log MV en un seul appel (remplace mview_definition + mview_logs)
- `sql_plan_history(sql_id)` — historique des plans d'exécution depuis V$SQL/AWR
- `explain_plan(sql_id)` — ⚠️ à utiliser SI le plan d'exécution est absent ou non disponible : génère un plan via EXPLAIN PLAN FOR (100% safe, n'exécute pas la requête)
- `awr_sql_stats(sql_id, days?)` — stats AWR par snapshot sur N jours (défaut 7)
- `awr_top_sql(days?, limit?)` — top N requêtes les plus coûteuses
- `bind_captures(sql_id)` — dernières valeurs des bind variables capturées
- `scheduler_jobs(schema?, job_name?)` — configuration et historique des jobs Oracle Scheduler
- `active_locks()` — verrous actifs : bloqueurs/bloqués, ressources, durée d'attente
- `run_select(sql, limit?)` — ⚠️ USAGE EXCEPTIONNEL — SELECT libre (max 50 lignes)

Exemples :
  TOOL: describe_table(COMMANDES, DEV5_01)
  TOOL: describe_object(GET_PRIX_TTC, DEV5_01)
  TOOL: describe_mview(MVw_PesRetour_CNEG, VB_02)
  TOOL: awr_sql_stats(abc123xyz, 7)
  TOOL: active_locks()

Tu peux appeler plusieurs outils, un par ligne.
Une fois que tu as les informations nécessaires, rédige l'analyse finale.
Si tu n'as pas besoin d'informations supplémentaires, rédige directement l'analyse.
"""


# Outils visibles dans l'UI (pas les legacy)
TOOLS_PUBLIC = [
    "describe_table", "table_dml_since_stats", "describe_object", "describe_mview",
    "sql_plan_history", "explain_plan", "awr_sql_stats", "awr_top_sql", "bind_captures",
    "scheduler_jobs", "active_locks", "run_select", "gather_table_stats",
]


def get_active_tools() -> set[str]:
    """Retourne l'ensemble des outils activés selon les settings."""
    from db.store import get_setting
    csv = get_setting("tools_enabled", "")
    gather = get_setting("gather_stats_enabled", "false") == "true"
    # Vide = tous les tools publics activés
    if not csv or csv.strip() == "":
        active = set(TOOLS_PUBLIC)
    elif csv.strip() == "none":
        active = set()
    else:
        active = {t.strip() for t in csv.split(",") if t.strip() in TOOLS}
    # gather_table_stats contrôlé séparément
    if gather:
        active.add("gather_table_stats")
    else:
        active.discard("gather_table_stats")
    return active


def parse_tool_calls(text: str) -> list[tuple[str, list[str]]]:
    """Extrait les appels d'outils d'une réponse IA (filtrés par tools activés)."""
    active = get_active_tools()
    calls = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'^TOOL:\s*(\w+)\(([^)]*)\)\s*$', line)
        if m:
            name = m.group(1)
            raw_args = m.group(2)
            args = [a.strip().strip('"\'') for a in raw_args.split(',') if a.strip()]
            if name in TOOLS and name in active:
                calls.append((name, args))
    return calls


def execute_tool(conn, name: str, args: list[str]) -> dict:
    """Exécute un outil et retourne le résultat."""
    active = get_active_tools()
    if name not in active:
        return {"error": f"Outil '{name}' désactivé dans les paramètres."}
    fn = TOOLS.get(name)
    if not fn:
        return {"error": f"Outil inconnu : {name}"}
    try:
        return fn(conn, *args)
    except Exception as e:
        return {"error": str(e)}


def execute_tool_native(conn, name: str, kwargs: dict) -> dict:
    """Exécute un outil depuis un appel natif (arguments nommés en dict)."""
    active = get_active_tools()
    if name not in active:
        return {"error": f"Outil '{name}' désactivé dans les paramètres."}
    fn = TOOLS.get(name)
    if not fn:
        return {"error": f"Outil inconnu : {name}"}
    try:
        return fn(conn, **kwargs)
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
# Schémas JSON pour le vrai function calling
# ─────────────────────────────────────────────
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": "Description complète d'une table Oracle : colonnes (types, nulls, défauts, stats), index (colonnes, unicité), contraintes (PK, FK, UK, CHECK) et taille. Remplace table_stats/index_list/column_stats/table_constraints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Nom de la table (respecter la casse Oracle exacte)."},
                    "schema":     {"type": "string", "description": "Schéma propriétaire (optionnel)."}
                },
                "required": ["table_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "table_dml_since_stats",
            "description": "INSERTs, UPDATEs et DELETEs non encore analysés sur une table depuis la dernière collecte des statistiques Oracle (ALL_TAB_MODIFICATIONS). Indique si les stats sont stales.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Nom de la table (respecter la casse Oracle exacte)."},
                    "schema":     {"type": "string", "description": "Schéma propriétaire (optionnel)."}
                },
                "required": ["table_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "describe_object",
            "description": "Description de N'IMPORTE QUEL objet Oracle : vue, procédure, fonction, package, trigger, séquence, synonyme, index, type. Pour les tables, préférer describe_table. Pour les MV, utiliser describe_mview.",
            "parameters": {
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "description": "Nom de l'objet Oracle (respecter la casse exacte)."},
                    "schema":      {"type": "string", "description": "Schéma propriétaire (optionnel)."}
                },
                "required": ["object_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "describe_mview",
            "description": "Description complète d'une vue matérialisée Oracle : DDL, options de refresh (mode, méthode, fréquence), tables sources, colonnes, date du dernier refresh ET logs MV (config + lignes en attente de refresh). Remplace mview_definition + mview_logs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mview_name": {"type": "string", "description": "Nom de la vue matérialisée (respecter la casse Oracle exacte)."},
                    "schema":     {"type": "string", "description": "Schéma propriétaire (optionnel)."}
                },
                "required": ["mview_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "explain_plan",
            "description": "Génère un plan d'exécution via EXPLAIN PLAN FOR à partir du SQL_ID. À appeler OBLIGATOIREMENT quand le plan d'exécution est absent ou marqué non disponible. N'exécute pas la requête — 100% safe en production.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_id": {"type": "string", "description": "SQL_ID Oracle (13 caractères alphanumériques)."}
                },
                "required": ["sql_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sql_plan_history",
            "description": "Historique des plans d'exécution Oracle pour un SQL_ID donné (AWR/V$SQL_PLAN_STATISTICS_ALL). Montre les différentes versions de plan et leurs stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_id": {"type": "string", "description": "SQL_ID Oracle (13 caractères alphanumériques)."}
                },
                "required": ["sql_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "awr_sql_stats",
            "description": "Statistiques AWR d'une requête SQL sur une période (temps d'exécution moyen/max, buffer gets, disk reads, executions). Données historiques.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_id": {"type": "string", "description": "SQL_ID Oracle."},
                    "days":   {"type": "string", "description": "Nombre de jours en arrière (défaut: 7)."}
                },
                "required": ["sql_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "awr_top_sql",
            "description": "Top SQL Oracle par temps elapsed depuis l'AWR. Identifie les requêtes les plus coûteuses sur la période.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days":  {"type": "string", "description": "Nombre de jours en arrière (défaut: 1)."},
                    "limit": {"type": "string", "description": "Nombre de requêtes à retourner (défaut: 10, max: 50)."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bind_captures",
            "description": "Dernières valeurs des bind variables capturées par Oracle pour un SQL_ID (V$SQL_BIND_CAPTURE). Utile pour comprendre les skewed plans.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_id": {"type": "string", "description": "SQL_ID Oracle."}
                },
                "required": ["sql_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scheduler_jobs",
            "description": "Configuration et historique récents des jobs Oracle Scheduler (DBMS_SCHEDULER) : statut, planning, dernière/prochaine exécution, taux d'échec et 20 derniers runs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "schema":   {"type": "string", "description": "Filtrer par schéma propriétaire (optionnel)."},
                    "job_name": {"type": "string", "description": "Filtrer par nom de job exact (optionnel, respecter la casse Oracle)."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "active_locks",
            "description": "Verrous actifs Oracle : sessions bloqueuses et bloquées, ressources verrouillées (table, ligne), type de verrou (TM/TX), mode et durée d'attente.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_select",
            "description": "⚠️ USAGE EXCEPTIONNEL — Exécute un SELECT SQL libre en lecture seule (max 50 lignes). Utiliser uniquement quand aucun outil spécialisé ne couvre le besoin.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql":   {"type": "string", "description": "Requête SELECT à exécuter (lecture seule stricte)."},
                    "limit": {"type": "string", "description": "Nombre max de lignes (défaut: 20, max: 50)."}
                },
                "required": ["sql"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "gather_table_stats",
            "description": "Lance DBMS_STATS.GATHER_TABLE_STATS sur une table pour mettre à jour les statistiques Oracle. Opération d'écriture — uniquement disponible si activé dans Administration > Outils IA. À utiliser quand les stats sont obsolètes (stale) et impactent le plan d'exécution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Nom de la table (casse Oracle exacte)."},
                    "schema":     {"type": "string", "description": "Schéma propriétaire (optionnel)."},
                    "estimate_percent": {"type": "string", "description": "Pourcentage d'échantillonnage (AUTO_SAMPLE_SIZE par défaut)."}
                },
                "required": ["table_name"]
            }
        }
    },
]


def get_tools_schema_filtered() -> list:
    """Retourne TOOLS_SCHEMA filtré selon les tools activés dans les settings."""
    active = get_active_tools()
    return [t for t in TOOLS_SCHEMA if t["function"]["name"] in active]


# ─────────────────────────────────────────────────────────────────────────────
# Prompt système centralisé — partagé par l'analyseur et le chat
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_NATIVE_BASE = """\
Tu es un expert Oracle Database 19c spécialisé en optimisation de performances SQL.

⚠️ CASSE DES NOMS D'OBJETS ORACLE :
- Objets créés SANS guillemets → stockés en MAJUSCULES (ex : COMMANDES, CLIENT_ID)
- Objets créés AVEC guillemets → casse préservée exacte (ex : MyTable, getPrixTTC)
- Utilise TOUJOURS la casse exacte telle qu'elle apparaît dans le SQL analysé

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WORKFLOW OBLIGATOIRE EN DEUX TEMPS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TEMPS 1 — Discovery (toujours en premier)
  Avant toute requête libre, utilise les outils de description pour connaître
  la structure exacte des objets impliqués :
  • describe_table(nom, schema)    → colonnes, types, index, contraintes, stats
  • describe_object(nom, schema)   → vues, procédures, packages, triggers...
  • describe_mview(nom, schema)    → vues matérialisées + logs MV
  Tu obtiens ainsi les noms de colonnes exacts, les types réels, les index disponibles.

TEMPS 2 — Requêtes ciblées (si besoin d'info supplémentaire)
  Une fois la structure connue, tu peux faire des requêtes précises avec :
  • run_select(sql, limit)  → SELECT libre, max 50 lignes, lecture seule stricte
  Le SQL que tu génères DOIT utiliser les colonnes/types découverts au Temps 1.
  Ne jamais inventer un nom de colonne — s'il n'est pas dans describe_*, il n'existe pas.

Outils de performance (pas besoin de discovery préalable) :
  • sql_plan_history(sql_id)
  • explain_plan(sql_id)  ← à appeler si le plan d'exécution est absent (EXPLAIN PLAN FOR, safe prod)
  • awr_sql_stats(sql_id, days?)
  • awr_top_sql(days?, limit?)
  • bind_captures(sql_id)
  • table_dml_since_stats(table_name, schema?)
  • scheduler_jobs(schema?, job_name?)
  • active_locks()
"""

SYSTEM_NATIVE_ANALYZE = SYSTEM_NATIVE_BASE + """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMAT DE RÉPONSE FINALE (OBLIGATOIRE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Quand tu as collecté toutes les informations, rédige l'analyse SANS appeler d'autres outils.
Les 3 premières lignes sont OBLIGATOIRES :

SCORE: <entier 0-100>
SEVERITY: <ok|warning|critical>
SUMMARY: <résumé en 1-2 phrases>

---

Puis une analyse complète en Markdown :
## ⚠️ Problèmes détectés
## ✅ Recommandations
## 🔍 SQL optimisé *(si applicable)*

Score : 100 = parfait, 0 = désastreux. Severity : ok(≥80), warning(50-79), critical(<50).
"""

SYSTEM_NATIVE_CHAT = SYSTEM_NATIVE_BASE + """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE CHAT — RÉPONSES INTERACTIVES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tu réponds aux questions de l'utilisateur sur la requête Oracle en contexte.
Utilise le workflow en deux temps si tu dois interroger Oracle pour répondre.
Réponds en français, de façon précise et actionnable. Utilise du Markdown pour le code SQL.
"""
