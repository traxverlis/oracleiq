"""
api/app.py - FastAPI : API REST + Web UI
"""
import sys
import json
import asyncio
import hashlib
import hmac
import time
import threading
import queue as _queue_mod
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from fastapi import FastAPI, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.store import get_all_queries, get_query_detail, get_conn, get_setting, set_setting
from config import ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD
from collector.connection import connect_oracle, query_execution_enabled
import analyzer.copilot_client as _copilot_client  # import au niveau module pour éviter le cache stale dans les threads

# Suivi en mémoire des analyses en cours (survit aux refreshs, pas aux redémarrages serveur)
analyzing_ids: set[int] = set()
# Queues SSE par query_id — permet la reconnexion si refresh pendant analyse native
_stream_queues: dict[int, list[_queue_mod.Queue]] = {}
_analysis_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="odin-analysis")


def _safe_json(obj) -> str:
    """Sérialise en JSON en éliminant les surrogates Unicode (caractères Oracle invalides)."""
    import re as _re
    raw = json.dumps(obj, default=str, ensure_ascii=False)
    raw = _re.sub(r'[\ud800-\udfff]', '\ufffd', raw)
    return raw

app = FastAPI(title="OracleIQ", version="1.0.0")

from api.security import (
    COOKIE_NAME as _COOKIE_NAME, COOKIE_TTL as _COOKIE_TTL,
    is_admin as _is_admin, make_token as _make_token,
    password_role, install_security,
)

install_security(app)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analyzer_mode: Literal["auto", "manual"] | None = None
    analyzer_ai_mode: Literal["native"] | None = None
    collector_active: bool | None = None
    ai_model: str | None = Field(default=None, min_length=1, max_length=200)
    ai_max_tokens: int | None = Field(default=None, ge=256, le=64000)
    plan_truncate: int | None = Field(default=None, ge=100, le=100000)
    oracle_dsn: str | None = Field(default=None, max_length=1000)
    oracle_user: str | None = Field(default=None, max_length=128)
    oracle_password: str | None = Field(default=None, max_length=1024)
    tools_enabled: str | None = Field(default=None, max_length=2000)
    gather_stats_enabled: bool | None = None
    system_prompt: str | None = Field(default=None, max_length=50000)

# Migration DB au démarrage
from db.store import init_db as _init_db
_init_db()

BASE = Path(__file__).parent.parent
# Utiliser l'environnement Jinja2 du venv directement
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.auto_reload = True
templates.env.globals["round"] = round
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# ─────────────────────────────────────────────
# API REST
# ─────────────────────────────────────────────

@app.get("/api/query-page")
def query_page(search: str = Query("", max_length=500), schema: str = "", critical_only: bool = False,
               group: bool = False, sort: str = "elapsed", direction: Literal["asc", "desc"] = "desc",
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
               pattern: str = "", source: str = ""):
    from db.store import get_query_page
    return get_query_page(search, schema, critical_only, group, sort, direction, page, page_size, pattern, source)


@app.get("/api/queries")
def api_queries(limit: int = 100, order: str = "elapsed_ms_avg DESC"):
    allowed_orders = {
        "elapsed_ms_avg DESC", "executions DESC",
        "buffer_gets_avg DESC", "disk_reads_avg DESC",
        "perf_score ASC", "last_seen DESC"
    }
    if order not in allowed_orders:
        order = "elapsed_ms_avg DESC"
    return get_all_queries(limit=limit, order=order)


@app.get("/api/queries/{query_id}")
def api_query_detail(query_id: int):
    detail = get_query_detail(query_id)
    if not detail["query"]:
        raise HTTPException(404, "Requête introuvable")
    # Parse JSON fields
    if detail["analysis"]:
        for field in ("issues", "recommendations"):
            val = detail["analysis"].get(field)
            if isinstance(val, str):
                try:
                    detail["analysis"][field] = json.loads(val)
                except Exception:
                    detail["analysis"][field] = []
    return detail


@app.get("/api/health")
def api_health(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    from db.store import get_service_health, analyzing_queue_get
    connection = get_conn()
    try:
        last_analysis = connection.execute("SELECT MAX(analyzed_at) FROM ai_analyses").fetchone()[0]
        failed = connection.execute("SELECT COUNT(*) FROM queries WHERE analysis_error IS NOT NULL").fetchone()[0]
    finally:
        connection.close()
    return {"services": get_service_health(), "queue_size": len(analyzing_queue_get()),
            "failed_queries": failed, "last_analysis": last_analysis,
            "collector_enabled": get_setting("collector_active", "true") == "true",
            "automatic_analysis": get_setting("analyzer_mode", "manual") == "auto"}


@app.get("/api/stats")
def api_stats():
    from db.store import get_query_stats
    stats = get_query_stats()
    stats["analyzer_mode"] = get_setting("analyzer_mode", "manual")
    stats["collector_active"] = get_setting("collector_active", "true") == "true"
    # Merger les analyses en cours : bouton manuel (mémoire) + analyzer auto (DB)
    from db.store import analyzing_queue_get
    all_analyzing = set(analyzing_ids) | set(analyzing_queue_get())
    stats["analyzing_ids"] = list(all_analyzing)
    return stats


@app.delete("/api/queries/{query_id}")
def delete_query(query_id: int, request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Authentification requise")
    from db.store import analyzing_queue_get
    if query_id in analyzing_queue_get():
        raise HTTPException(409, "Une analyse est en cours pour cette requete")
    from db.store import delete_query_data
    delete_query_data(query_id)
    return {"ok": True}


@app.delete("/api/queries")
async def purge_queries(request: Request):
    """Purge les données enregistrées : tout, les requêtes analysées, ou les analyses seules."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Authentification requise")
    from db.store import analyzing_queue_get
    if analyzing_queue_get():
        raise HTTPException(409, "Attendez la fin des analyses avant de purger")
    try:
        body = await request.json()
    except Exception:
        body = {}
    scope = body.get("scope", "analyzed")  # 'all' | 'analyzed' | 'analyses'
    if scope not in ("all", "analyzed", "analyses"):
        raise HTTPException(status_code=400, detail="scope invalide")
    from db.store import purge_data
    counts = purge_data(scope)
    analyzing_ids.clear()
    return {"ok": True, **counts}


@app.get("/api/default_prompts")
def get_default_prompts():
    """Retourne le prompt système natif par défaut."""
    from analyzer.oracle_tools import SYSTEM_NATIVE_ANALYZE
    return {
        "native":  SYSTEM_NATIVE_ANALYZE,
    }


@app.get("/api/models")
def model_catalog(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    from config import AI_PROVIDER
    catalog = json.loads(get_setting("copilot_model_catalog", '{"models":[],"updated_at":null}'))
    return {**catalog, "provider": AI_PROVIDER}


@app.post("/api/models/refresh")
def refresh_model_catalog(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    from config import AI_PROVIDER
    if AI_PROVIDER != "github-copilot":
        raise HTTPException(409, "Actualisation disponible pour GitHub Copilot uniquement.")
    try:
        models = _copilot_client.list_account_models()
    except Exception:
        raise HTTPException(502, "Catalogue Copilot indisponible. Verifiez la connexion GitHub du serveur et reessayez.") from None
    catalog = {"models": models, "updated_at": time.time()}
    set_setting("copilot_model_catalog", json.dumps(catalog))
    return {**catalog, "provider": AI_PROVIDER}


@app.get("/api/settings/github_token")
def github_token_status(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    return {"configured": _copilot_client.has_github_token(), "source": _copilot_client.github_token_source()}


@app.post("/api/settings/github_token")
async def save_github_token_endpoint(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    if _copilot_client.github_token_source() == "env":
        raise HTTPException(409, "Jeton géré via la variable d'environnement GITHUB_TOKEN, non modifiable ici.")
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(422, "Jeton requis")
    await run_in_threadpool(_copilot_client.save_github_token, token)
    set_setting("copilot_model_catalog", '{"models":[],"updated_at":null}')
    return {"ok": True}


@app.delete("/api/settings/github_token")
def delete_github_token_endpoint(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    if _copilot_client.github_token_source() == "env":
        raise HTTPException(409, "Jeton géré via la variable d'environnement GITHUB_TOKEN, non modifiable ici.")
    _copilot_client.delete_github_token()
    set_setting("copilot_model_catalog", '{"models":[],"updated_at":null}')
    return {"ok": True}


@app.post("/api/settings/github_token/test")
async def test_github_token_endpoint(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    token = (body.get("token") or "").strip() or None
    try:
        await run_in_threadpool(_copilot_client.test_github_token, token)
    except _copilot_client.CopilotAuthenticationError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Connexion au service Copilot impossible ({type(e).__name__})."}
    return {"ok": True}


@app.get("/api/settings")
def get_all_settings(request: Request):
    """Retourne tous les paramètres configurables."""
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    return {
        "analyzer_mode":    get_setting("analyzer_mode", "manual"),
        "analyzer_ai_mode": "native",
        "collector_active": get_setting("collector_active", "true") == "true",
        "ai_model":         get_setting("ai_model", "claude-sonnet-4.6"),
        "ai_max_tokens":    int(get_setting("ai_max_tokens", "8000")),
        "plan_truncate":    int(get_setting("plan_truncate", "3000")),
        "oracle_dsn":       get_setting("oracle_dsn",  ORACLE_DSN),
        "oracle_user":      get_setting("oracle_user", ORACLE_USER),
        "oracle_has_pwd":   bool(get_setting("oracle_password", ORACLE_PASSWORD)),
        "tools_enabled":    get_setting("tools_enabled", ""),   # CSV de tools activés, vide = tous
        "gather_stats_enabled": get_setting("gather_stats_enabled", "false") == "true",
        "system_prompt":    get_setting("system_prompt", ""),  # vide = prompt par défaut
        # on ne retourne jamais le mot de passe
    }


@app.post("/api/settings")
async def save_all_settings(request: Request, settings: SettingsPatch):
    """Sauvegarde un ou plusieurs paramètres."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Authentification requise")
    body = settings.model_dump(exclude_unset=True, exclude_none=True)
    saved = {}
    for key, val in body.items():
        if isinstance(val, bool):
            val = "true" if val else "false"
        set_setting(key, str(val))
        if key != "oracle_password":
            saved[key] = val
    return {"ok": True, "saved": saved}


@app.post("/api/settings/analyzer_mode")
async def set_analyzer_mode(request: Request):
    body = await request.json()
    mode = body.get("mode", "manual")
    if mode not in ("auto", "manual"):
        raise HTTPException(400, "mode doit être 'auto' ou 'manual'")
    set_setting("analyzer_mode", mode)
    return {"ok": True, "mode": mode}


@app.post("/api/settings/collector_active")
async def set_collector_active(request: Request):
    body = await request.json()
    active = body.get("active", True)
    if not isinstance(active, bool):
        raise HTTPException(422, "active doit etre un booleen")
    set_setting("collector_active", "true" if active else "false")
    return {"ok": True, "active": active}


@app.post("/api/settings/test_oracle")
async def test_oracle_connection(request: Request):
    """Teste la connexion Oracle avec les paramètres fournis (ou ceux en DB)."""
    body = await request.json()
    return await run_in_threadpool(_test_oracle_connection, body)


def _test_oracle_connection(body):
    dsn  = body.get("oracle_dsn")  or get_setting("oracle_dsn",  ORACLE_DSN)
    user = body.get("oracle_user") or get_setting("oracle_user", ORACLE_USER)
    pwd  = body.get("oracle_password")
    if not pwd:
        pwd = get_setting("oracle_password", ORACLE_PASSWORD)
    try:
        import oracledb
        c = connect_oracle(user=user, password=pwd, dsn=dsn)
        v = c.version
        c.close()
        return {"ok": True, "version": v, "dsn": dsn, "user": user}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/repair_sql_texts")
def repair_sql_texts():
    """Récupère le texte SQL complet pour toutes les requêtes tronquées (length=1000)."""
    try:
        from config import ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD
        import oracledb
        from collector.oracle_collector import get_full_sql_text
        _dsn  = get_setting("oracle_dsn",      ORACLE_DSN)
        _user = get_setting("oracle_user",     ORACLE_USER)
        _pwd  = get_setting("oracle_password", ORACLE_PASSWORD)
        if not _dsn or not _user or not _pwd:
            raise ValueError("Connexion Oracle non configurée")
        conn_ora = connect_oracle(user=_user, password=_pwd, dsn=_dsn)

        conn_sqlite = get_conn()
        rows = conn_sqlite.execute(
            "SELECT id, sql_id, length(sql_text) as l FROM queries WHERE length(sql_text) >= 999"
        ).fetchall()
        conn_sqlite.close()

        fixed = 0
        failed = 0
        for row in rows:
            qid, sql_id, cur_len = row["id"], row["sql_id"], row["l"]
            try:
                full = get_full_sql_text(conn_ora, sql_id)
                if full and len(full) > cur_len:
                    conn_fix = get_conn()
                    conn_fix.execute("UPDATE queries SET sql_text=? WHERE id=?", (full, qid))
                    conn_fix.commit()
                    conn_fix.close()
                    fixed += 1
            except Exception:
                failed += 1

        conn_ora.close()
        return {"ok": True, "total": len(rows), "fixed": fixed, "failed": failed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/refresh_plan/{query_id}")
def refresh_plan_endpoint(query_id: int):
    """Rafraîchit le plan d’exécution Oracle pour une requête donnée."""
    conn = get_conn()
    q = conn.execute("SELECT id, sql_id, child_number FROM queries WHERE id=?", (query_id,)).fetchone()
    conn.close()
    if not q:
        raise HTTPException(404, "Requête introuvable")
    try:
        from config import ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD
        import oracledb
        from collector.oracle_collector import get_execution_plan
        _dsn  = get_setting("oracle_dsn",      ORACLE_DSN)
        _user = get_setting("oracle_user",     ORACLE_USER)
        _pwd  = get_setting("oracle_password", ORACLE_PASSWORD)
        if not _dsn or not _user or not _pwd:
            raise ValueError("Connexion Oracle non configurée")
        conn_ora = connect_oracle(user=_user, password=_pwd, dsn=_dsn)
        sql_id = dict(q)["sql_id"]
        plan = get_execution_plan(conn_ora, sql_id, dict(q).get("child_number", 0))
        conn_ora.close()
        if plan and 'non disponible' not in plan.lower():
            from db.store import save_plan
            save_plan(query_id, plan)
            return {"ok": True, "plan": plan}
        return {"ok": False, "error": "Plan non disponible depuis Oracle"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/analyze/{query_id:int}")
async def analyze_query_endpoint(query_id: int, request: Request):
    """Lance l'analyse IA. refresh_plan=true récupère un nouveau plan Oracle avant d'analyser."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    refresh_plan = True  # toujours rafraîchir le plan Oracle avant d'analyser

    # Garde anti-doublon : ne pas relancer si déjà en cours
    if query_id in analyzing_ids:
        return {"ok": True, "query_id": query_id, "status": "already_analyzing"}

    conn = get_conn()
    row = conn.execute("SELECT id FROM queries WHERE id=?", (query_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Requête introuvable")

    from db.store import analyzing_queue_add, analyzing_queue_remove, save_analysis_error
    try:
        if not analyzing_queue_add(query_id):
            return {"ok": True, "query_id": query_id, "status": "already_analyzing"}
    except ValueError as error:
        raise HTTPException(429, str(error)) from error
    analyzing_ids.add(query_id)

    def _run_sync():
        conn_ora = None
        try:
            from db.store import get_conn as gc, save_analysis, save_plan
            from analyzer.ai_analyzer import analyze_query as _analyze_query
            import logging
            log = logging.getLogger("oracleiq")

            conn2 = gc()
            q = conn2.execute("""
                SELECT q.*, ep.plan_text FROM queries q
                LEFT JOIN execution_plans ep ON ep.id=(
                    SELECT id FROM execution_plans WHERE query_id=q.id ORDER BY id DESC LIMIT 1
                )
                WHERE q.id=?
            """, (query_id,)).fetchone()
            conn2.close()
            if not q:
                log.error(f"Analyze {query_id}: query not found")
                return
            q = dict(q)

            # Connexion Oracle (pour refresh plan + outils agentiques)
            plan_text = q.get('plan_text') or 'N/A'
            try:
                from config import ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD
                import oracledb
                _dsn  = get_setting("oracle_dsn",      ORACLE_DSN)
                _user = get_setting("oracle_user",     ORACLE_USER)
                _pwd  = get_setting("oracle_password", ORACLE_PASSWORD)
                conn_ora = connect_oracle(user=_user, password=_pwd, dsn=_dsn)
                log.info(f"Analyze {query_id}: Oracle connected")

                # Rafraîchir le plan
                if refresh_plan:
                    from collector.oracle_collector import get_execution_plan
                    db2 = gc()
                    sql_id_row = db2.execute('SELECT sql_id FROM queries WHERE id=?', (query_id,)).fetchone()
                    db2.close()
                    sql_id = sql_id_row[0] if sql_id_row else ''
                    new_plan = get_execution_plan(conn_ora, sql_id, q.get("child_number", 0))
                    if new_plan and 'non disponible' not in new_plan.lower():
                        save_plan(query_id, new_plan)
                        plan_text = new_plan
                        log.info(f"Analyze {query_id}: plan refreshed ({len(new_plan)} chars)")
            except Exception as pe:
                log.warning(f"Analyze {query_id}: Oracle connect/plan failed: {pe}")
                conn_ora = None

            # Tronquer le plan
            plan_truncate  = int(get_setting("plan_truncate", "3000"))
            q['plan_text'] = plan_text[:plan_truncate] + ('\n[plan tronqué...]' if len(plan_text) > plan_truncate else '')

            # Analyse agentique (avec connexion Oracle si disponible)
            ai_model = get_setting("ai_model", "claude-sonnet-4.6")
            log.info(f"Analyze {query_id}: starting native analysis, oracle={'yes' if conn_ora else 'no'}")
            result = _analyze_query(q, oracle_conn=conn_ora)
            log.info(f"Analyze {query_id}: done score={result['score']} severity={result['severity']} usage={result['usage']}")
            save_analysis(query_id, {**result, "model": ai_model})
        except Exception as e:
            import logging, traceback
            save_analysis_error(query_id, "Analyse echouee. Consultez les journaux puis relancez.")
            tb = traceback.format_exc()
            logging.getLogger("oracleiq").error(f"Analyze {query_id} failed: {e}\n{tb}")
            with open("/tmp/oracleiq_analyze_err.log", "a") as _ef:
                _ef.write(f"=== Analyze {query_id} FAILED ===\n{tb}\n")
            print(f"[ANALYZE ERROR] {query_id}: {e}", flush=True)
        finally:
            if conn_ora:
                try: conn_ora.close()
                except Exception: pass
            analyzing_ids.discard(query_id)
            analyzing_queue_remove(query_id)

    _analysis_executor.submit(_run_sync)
    return {"ok": True, "query_id": query_id, "status": "analyzing", "refresh_plan": refresh_plan}


@app.get("/api/analyze/{query_id}/stream")
async def analyze_query_stream(query_id: int, request: Request):
    """
    SSE endpoint — analyse native en streaming.
    Émet des événements : tool_call, tool_result, text, done, error.
    Uniquement disponible en mode native.
    """
    conn = get_conn()
    row = conn.execute("SELECT id FROM queries WHERE id=?", (query_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Requête introuvable")

    if query_id in analyzing_ids:
        # Analyse déjà en cours — brancher sur la queue existante
        sub_queue: _queue_mod.Queue = _queue_mod.Queue()
        _stream_queues.setdefault(query_id, []).append(sub_queue)
        sub_queue.put({"type": "status", "message": "\ud83d\udd04 Reconnexion au stream en cours..."})

        async def _resume_generator():
            try:
                while True:
                    try:
                        evt = await asyncio.get_event_loop().run_in_executor(None, sub_queue.get, True, 1.0)
                    except _queue_mod.Empty:
                        yield ": keepalive\n\n"
                        continue
                    if evt is None:
                        break
                    yield "data: " + json.dumps(evt, default=str, ensure_ascii=True) + "\n\n"
            finally:
                # Nettoyer l'abonnement
                try:
                    _stream_queues.get(query_id, []).remove(sub_queue)
                except ValueError:
                    pass

        return StreamingResponse(
            _resume_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    from db.store import analyzing_queue_add, analyzing_queue_remove, save_analysis_error
    try:
        if not analyzing_queue_add(query_id):
            raise HTTPException(409, "Analyse deja reservee par un autre traitement")
    except ValueError as error:
        raise HTTPException(429, str(error)) from error
    analyzing_ids.add(query_id)
    evt_queue: _queue_mod.Queue = _queue_mod.Queue()
    _stream_queues[query_id] = []  # liste des abonnés secondaires

    def _broadcast(evt: dict):
        """Envoie l'événement à la queue principale + tous les abonnés (reconnexions)."""
        evt_queue.put(evt)
        for sub in list(_stream_queues.get(query_id, [])):
            sub.put(evt)

    def _run_stream():
        conn_ora = None
        try:
            from db.store import get_conn as gc, save_analysis, save_plan
            from analyzer.ai_analyzer import _build_initial_prompt, parse_ai_response
            from analyzer.copilot_client import chat_with_tools
            import logging
            log = logging.getLogger("oracleiq")

            conn2 = gc()
            q = conn2.execute("""
                SELECT q.*, ep.plan_text FROM queries q
                LEFT JOIN execution_plans ep ON ep.id=(
                    SELECT id FROM execution_plans WHERE query_id=q.id ORDER BY id DESC LIMIT 1
                )
                WHERE q.id=?
            """, (query_id,)).fetchone()
            conn2.close()
            if not q:
                _broadcast({"type": "error", "message": "Requête introuvable"})
                return
            q = dict(q)

            # Connexion Oracle
            try:
                from config import ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD
                import oracledb
                _dsn  = get_setting("oracle_dsn",      ORACLE_DSN)
                _user = get_setting("oracle_user",     ORACLE_USER)
                _pwd  = get_setting("oracle_password", ORACLE_PASSWORD)
                conn_ora = connect_oracle(user=_user, password=_pwd, dsn=_dsn)
                _broadcast({"type": "status", "message": "✅ Oracle connecté"})

                # Refresh plan
                from collector.oracle_collector import get_execution_plan
                db2 = gc()
                sql_id_row = db2.execute('SELECT sql_id FROM queries WHERE id=?', (query_id,)).fetchone()
                db2.close()
                sql_id = sql_id_row[0] if sql_id_row else ''
                new_plan = get_execution_plan(conn_ora, sql_id, q.get("child_number", 0))
                if new_plan and 'non disponible' not in new_plan.lower():
                    save_plan(query_id, new_plan)
                    q['plan_text'] = new_plan
                    _broadcast({"type": "status", "message": "📄 Plan Oracle rafraîci"})
            except Exception as pe:
                log.warning(f"Stream {query_id}: Oracle connect/plan: {pe}")
                conn_ora = None
                _broadcast({"type": "status", "message": "⚠️ Oracle non disponible"})

            # Tronquer le plan
            plan_truncate = int(get_setting("plan_truncate", "3000"))
            pt = q.get('plan_text') or 'N/A'
            q['plan_text'] = pt[:plan_truncate] + ('\n[plan tronqué...]' if len(pt) > plan_truncate else '')

            # Boucle native avec SSE
            model_used = get_setting("ai_model", "claude-sonnet-4.6")
            from analyzer.oracle_tools import get_tools_schema_filtered, execute_tool_native, SYSTEM_NATIVE_ANALYZE
            from analyzer.ai_analyzer import _get_max_tokens
            tools_schema = get_tools_schema_filtered()
            system_native = get_setting("system_prompt", "").strip() or SYSTEM_NATIVE_ANALYZE
            max_tokens = _get_max_tokens()

            messages = [{"role": "user", "content": _build_initial_prompt(q)}]
            total_usage: dict = {}
            all_parts: list[str] = []
            trace: list[dict] = []
            MAX_TOOL_CALLS = 30
            tool_calls_count = 0

            while True:
                _broadcast({"type": "status", "message": "🤔 IA réfléchit..."})
                text, tool_calls, usage = chat_with_tools(
                    messages=messages, tools=tools_schema,
                    model=model_used, max_tokens=max_tokens, system=system_native,
                )
                for k, v in (usage or {}).items():
                    if isinstance(v, (int, float)):
                        total_usage[k] = total_usage.get(k, 0) + v

                if text:
                    all_parts.append(text)

                if not tool_calls:
                    # Vérifier que le texte contient bien une analyse finale (SCORE: requis)
                    has_score = text and ("SCORE:" in text or "score:" in text.lower())
                    if not has_score and len(all_parts) <= 3:
                        # Le modèle a répondu sans tool_calls ni analyse finale (texte préliminaire)
                        # Forcer tool_choice=required pour qu'il appelle un outil
                        _broadcast({"type": "status", "message": "🔄 Réponse incomplète, forcçage des outils..."})
                        messages.append({"role": "assistant", "content": text or ""})
                        messages.append({"role": "user", "content": "Tu dois appeler au moins un outil Oracle pour collecter les informations nécessaires avant de rédiger l'analyse."})
                        _broadcast({"type": "status", "message": "🤔 IA réfléchit..."})
                        text, tool_calls, usage = chat_with_tools(
                            messages=messages, tools=tools_schema,
                            model=model_used, max_tokens=max_tokens, system=system_native,
                            tool_choice="required",
                        )
                        for k, v in (usage or {}).items():
                            if isinstance(v, (int, float)):
                                total_usage[k] = total_usage.get(k, 0) + v
                        if text:
                            all_parts.append(text)
                        if not tool_calls:
                            # Toujours rien — forcer conclusion directement
                            _broadcast({"type": "status", "message": "⚠️ Pas d'outils disponibles, conclusion forcée"})
                            messages.append({"role": "assistant", "content": text or ""})
                            messages.append({"role": "user", "content": "Rédige MAINTENANT l'analyse finale avec SCORE: / SEVERITY: / SUMMARY: basée sur le SQL et le plan d'exécution fournis."})
                            text3, _, usage3 = chat_with_tools(messages=messages, tools=[], model=model_used, max_tokens=max_tokens, system=system_native)
                            if text3:
                                all_parts.append(text3)
                                _broadcast({"type": "analysis", "content": text3})
                            for k, v in (usage3 or {}).items():
                                if isinstance(v, (int, float)):
                                    total_usage[k] = total_usage.get(k, 0) + v
                            break
                        # tool_calls disponibles — on continue la boucle normalement
                    else:
                        # Analyse finale réelle
                        _broadcast({"type": "analysis", "content": text or ""})
                        break

                # Émettre chaque tool call + son résultat
                if tool_calls_count + len(tool_calls) > MAX_TOOL_CALLS:
                    _broadcast({"type": "status", "message": "⚠️ Limite d'appels atteinte, conclusion forcée"})
                    if tool_calls:
                        messages.append({
                            "role": "assistant", "content": text,
                            "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}} for tc in tool_calls]
                        })
                        for tc in tool_calls:
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps({"error": "Limite atteinte."})})
                    messages.append({"role": "user", "content": "Rédige MAINTENANT l'analyse finale avec SCORE: / SEVERITY: / SUMMARY:"})
                    text2, _, usage2 = chat_with_tools(messages=messages, tools=[], model=model_used, max_tokens=max_tokens, system=system_native)
                    if text2:
                        all_parts.append(text2)
                        _broadcast({"type": "analysis", "content": text2})
                    for k, v in (usage2 or {}).items():
                        if isinstance(v, (int, float)):
                            total_usage[k] = total_usage.get(k, 0) + v
                    break

                assistant_msg = {
                    "role": "assistant", "content": text,
                    "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}} for tc in tool_calls]
                }
                messages.append(assistant_msg)

                for tc in tool_calls:
                    tool_calls_count += 1
                    tool_name = tc["name"]
                    tool_args = tc["arguments"]
                    _broadcast({"type": "tool_call", "name": tool_name, "args": tool_args})
                    import time as _time
                    t0 = _time.monotonic()
                    if conn_ora:
                        result = execute_tool_native(conn_ora, tool_name, tool_args)
                    else:
                        result = {"error": "Oracle non disponible"}
                    elapsed_ms = int((_time.monotonic() - t0) * 1000)
                    has_error = "error" in result
                    trace.append({
                        "t": _time.strftime("%H:%M:%S"),
                        "tool": tool_name,
                        "args": tool_args,
                        "ok": not has_error,
                        "error": result.get("error") if has_error else None,
                        "ms": elapsed_ms,
                    })
                    # N'envoyer que le résumé du résultat (pas tout le JSON brut)
                    result_preview = str(result)[:200] + ("..." if len(str(result)) > 200 else "")
                    _broadcast({"type": "tool_result", "name": tool_name, "preview": result_preview, "ok": not has_error, "ms": elapsed_ms})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": _safe_json(result)})

            # Sauvegarder l'analyse
            final_raw = all_parts[-1] if all_parts else ""
            full_raw = "\n\n---\n\n".join(all_parts)
            from analyzer.ai_analyzer import parse_ai_response
            parsed = parse_ai_response(final_raw)
            ai_model = get_setting("ai_model", "claude-sonnet-4.6")
            save_analysis(query_id, {
                "model": ai_model, "score": parsed.get("score", 50),
                "severity": parsed.get("severity", "warning"),
                "summary": parsed.get("summary", ""),
                "issues": parsed.get("issues", []),
                "recommendations": parsed.get("recommendations", []),
                "raw": full_raw, "usage": total_usage, "trace": trace,
            })
            _broadcast({"type": "done", "score": parsed.get("score", 50), "severity": parsed.get("severity", "warning")})

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log.error(f"Stream {query_id} failed: {e}\n{tb}")
            save_analysis_error(query_id, "Analyse echouee. Consultez les journaux puis relancez.")
            _broadcast({"type": "error", "message": "Analyse echouee. Consultez les journaux."})
        finally:
            if conn_ora:
                try: conn_ora.close()
                except: pass
            analyzing_ids.discard(query_id)
            analyzing_queue_remove(query_id)
            # Envoyer le sentinel à tous les abonnés secondaires
            for sub in list(_stream_queues.pop(query_id, [])):
                sub.put(None)
            evt_queue.put(None)  # sentinel principal

    _analysis_executor.submit(_run_stream)

    async def _event_generator():
        while True:
            # Polling non-bloquant sur la queue
            try:
                evt = await asyncio.get_event_loop().run_in_executor(None, evt_queue.get, True, 1.0)
            except _queue_mod.Empty:
                yield ": keepalive\n\n"
                continue
            if evt is None:
                break
            yield "data: " + _safe_json(evt) + "\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/queries/{query_id}/performance")
def api_query_performance(query_id: int, minutes: int = Query(15)):
    from db.performance import get_performance
    if minutes not in (15, 60, 1440):
        raise HTTPException(422, "Periode non prise en charge")
    result = get_performance(query_id, minutes)
    if result is None:
        raise HTTPException(404, "Requete introuvable")
    return result


@app.get("/api/queries/{query_id}/plan-comparison")
def api_plan_comparison(query_id: int, before_id: int | None = Query(None, ge=1),
                        after_id: int | None = Query(None, ge=1)):
    from db.performance import get_plan_comparison
    if (before_id is None) != (after_id is None):
        raise HTTPException(422, "Selectionner deux plans")
    result = get_plan_comparison(query_id, before_id, after_id)
    if result is None:
        raise HTTPException(404, "Requete ou plan introuvable")
    return result


@app.get("/api/queries/{query_id}/snapshots")
def api_query_snapshots(query_id: int):
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, captured_at, elapsed_ms_avg, executions, buffer_gets_avg, disk_reads_avg
        FROM query_snapshots WHERE query_id=?
        ORDER BY captured_at DESC LIMIT 50
    """, (query_id,)).fetchall()
    conn.close()
    result = [dict(r) for r in reversed(rows)]
    return result


@app.post("/api/queries/{query_id}/reset_plan_change")
def reset_plan_change(query_id: int):
    conn = get_conn()
    conn.execute("UPDATE queries SET plan_change_detected=0 WHERE id=?", (query_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/queries/{query_id}/binds")
def get_bind_captures(query_id: int):
    return _get_bind_captures(query_id)


@app.post("/api/queries/{query_id}/binds/refresh")
def refresh_bind_captures(query_id: int):
    return _get_bind_captures(query_id, refresh=True)


def _get_bind_captures(query_id: int, refresh: bool = False):
    from db.store import get_conn as sqlite_conn, get_bind_values
    sconn = sqlite_conn()
    row = sconn.execute("SELECT sql_id FROM queries WHERE id=?", (query_id,)).fetchone()
    sconn.close()
    if not row or not row["sql_id"]:
        raise HTTPException(404, "sql_id introuvable")
    sql_id = row["sql_id"]

    # 1. Lire depuis SQLite (collecte automatique)
    stored = get_bind_values(query_id)
    if stored and not refresh:
        # Normaliser le format pour le frontend
        binds = []
        for b in stored:
            binds.append({
                "bind_name":     b["bind_name"],
                "position":      b["position"],
                "datatype":      b["datatype"],
                "value":         b["value_string"],
                "last_captured": b["last_captured_oracle"] or b["captured_at"],
            })
        return {
            "sql_id":   sql_id,
            "captured": True,
            "count":    len(binds),
            "binds":    binds,
            "source":   "sqlite",
            "note":     "Valeurs issues de la collecte automatique ODIN (V$SQL_BIND_CAPTURE)."
        }

    if not refresh:
        return {"sql_id": sql_id, "captured": False, "source": "sqlite", "binds": [],
                "message": "Aucune valeur de bind capturee dans l'historique local."}

    oconn = None
    try:
        import oracledb
        _dsn  = get_setting("oracle_dsn",      ORACLE_DSN)
        _user = get_setting("oracle_user",     ORACLE_USER)
        _pwd  = get_setting("oracle_password", ORACLE_PASSWORD)
        oconn = connect_oracle(user=_user, password=_pwd, dsn=_dsn)
        from analyzer.oracle_tools import bind_captures
        from db.store import save_bind_values
        result = bind_captures(oconn, sql_id)
        # Stocker en SQLite pour les prochaines consultations
        if result.get("captured") and result.get("binds"):
            save_bind_values(query_id, [
                {"bind_name": b["bind_name"], "position": b.get("position"),
                 "datatype": b.get("datatype"), "value": b.get("value"),
                 "last_captured": b.get("last_captured")}
                for b in result["binds"]
            ])
            result["source"] = "oracle-live"
        return result
    except Exception:
        return {
            "sql_id":   sql_id,
            "captured": False,
            "source":   "none",
            "message":  "Rafraichissement Oracle indisponible. Les captures locales sont conservees."
        }
    finally:
        if oconn is not None:
            oconn.close()


# ─────────────────────────────────────────────
# Chat par requête
# ─────────────────────────────────────────────

from analyzer.oracle_tools import SYSTEM_NATIVE_CHAT
CHAT_SYSTEM = SYSTEM_NATIVE_CHAT


# ─────────────────────────────────────────────
# Replay SQL
# ─────────────────────────────────────────────

@app.post("/api/queries/{query_id}/replay")
def replay_query(query_id: int):
    """
    Rejoue le SELECT directement sur Oracle :
    1. Vérifie que c'est bien un SELECT
    2. Exécute EXPLAIN PLAN FOR <sql> pour obtenir un nouveau plan
    3. Exécute le SELECT (ROWNUM <= 5) pour mesurer le temps réel
    4. Retourne : plan_text, elapsed_ms, rows_returned, ok
    """
    import re, time as _time
    import oracledb
    if not query_execution_enabled():
        raise HTTPException(403, "Rejeu desactive : ODIN_ALLOW_QUERY_EXECUTION requis")

    sconn = get_conn()
    row = sconn.execute(
        "SELECT id, sql_text, sql_id FROM queries WHERE id=?", (query_id,)
    ).fetchone()
    sconn.close()
    if not row:
        raise HTTPException(404, "Requête introuvable")

    sql_text = (row["sql_text"] or "").strip().rstrip(";")

    # Sécurité : SELECT uniquement
    _SELECT_RE = re.compile(
        r'^\s*(SELECT|WITH)\b', re.IGNORECASE | re.DOTALL
    )
    _FORBIDDEN = re.compile(
        r'\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|MERGE|EXECUTE|CALL|BEGIN|COMMIT|ROLLBACK|GRANT|REVOKE)\b',
        re.IGNORECASE
    )
    if not _SELECT_RE.match(sql_text):
        return {"ok": False, "error": "Seules les requêtes SELECT peuvent être rejouées."}
    if _FORBIDDEN.search(sql_text):
        return {"ok": False, "error": "Requête refusée : contient des mots-clés non autorisés."}

    try:
        _dsn  = get_setting("oracle_dsn",      ORACLE_DSN)
        _user = get_setting("oracle_user",     ORACLE_USER)
        _pwd  = get_setting("oracle_password", ORACLE_PASSWORD)
        oconn = connect_oracle(user=_user, password=_pwd, dsn=_dsn)
    except Exception as e:
        return {"ok": False, "error": f"Connexion Oracle impossible : {e}"}

    result = {"ok": True, "query_id": query_id}

    # 1. EXPLAIN PLAN pour obtenir le plan sans les stats ALLSTATS (SQL non exécuté)
    try:
        cur = oconn.cursor()
        cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID='ODIN_REPLAY_{query_id}' FOR {sql_text}")
        cur2 = oconn.cursor()
        cur2.execute("""
            SELECT * FROM TABLE(
                DBMS_XPLAN.DISPLAY('PLAN_TABLE', 'ODIN_REPLAY_{qid}', 'ALL')
            )
        """.replace("{qid}", str(query_id)))
        plan_lines = []
        for r in cur2.fetchall():
            v = r[0]
            if hasattr(v, 'read'): v = v.read()
            if v: plan_lines.append(str(v))
        result["plan_text"] = "\n".join(plan_lines) if plan_lines else "[Plan non disponible]"
        # Nettoyage de la plan table
        try:
            oconn.cursor().execute(
                f"DELETE FROM plan_table WHERE statement_id='ODIN_REPLAY_{query_id}'"
            )
            oconn.commit()
        except Exception:
            pass
    except Exception as e:
        result["plan_text"] = f"[EXPLAIN PLAN non disponible : {e}]"

    # 2. Exécution réelle avec ROWNUM <= 5 pour mesurer le temps
    try:
        wrapped = f"SELECT * FROM ({sql_text}) WHERE ROWNUM <= 5"
        cur3 = oconn.cursor()
        t0 = _time.perf_counter()
        cur3.execute(wrapped)
        rows = cur3.fetchall()
        elapsed_ms = round((_time.perf_counter() - t0) * 1000, 1)
        cols = [d[0] for d in cur3.description] if cur3.description else []
        result["elapsed_ms"]     = elapsed_ms
        result["rows_returned"]  = len(rows)
        result["columns"]        = cols
        result["sample_rows"]    = [
            {cols[i]: (str(r[i]) if r[i] is not None else None) for i in range(len(cols))}
            for r in rows
        ]
        result["note"] = "Exécution réelle (max 5 lignes) — temps mesuré côté serveur."
    except Exception as e:
        result["elapsed_ms"]   = None
        result["rows_returned"] = None
        result["exec_error"]   = str(e)

    oconn.close()
    return result


@app.get("/api/queries/{query_id}/chat")
def get_chat(query_id: int):
    from db.store import chat_get_messages
    return {"messages": chat_get_messages(query_id)}


@app.delete("/api/queries/{query_id}/chat")
def clear_chat(query_id: int, request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Authentification requise")
    from db.store import chat_clear
    chat_clear(query_id)
    return {"ok": True}


@app.post("/api/queries/{query_id}/chat")
async def post_chat(query_id: int, request: Request):
    body = await request.json()
    return await run_in_threadpool(_post_chat, query_id, body)


def _post_chat(query_id: int, body: dict):
    user_msg = body.get("message")
    if not isinstance(user_msg, str) or len(user_msg) > 10000:
        raise HTTPException(422, "message doit contenir au maximum 10000 caracteres")
    user_msg = user_msg.strip()
    if not user_msg:
        raise HTTPException(400, "message requis")

    analysis_id = body.get("analysis_id")
    from db.store import chat_get_messages, chat_add_message, get_query_detail, get_setting, get_conn

    # Charger le contexte de la requête
    detail = get_query_detail(query_id)
    if not detail["query"]:
        raise HTTPException(404, "Requête introuvable")
    q = detail["query"]
    plan = detail["plan"]

    # Analyse : celle affichée à l'écran (analysis_id) ou la dernière
    if analysis_id:
        conn = get_conn()
        arow = conn.execute("SELECT * FROM ai_analyses WHERE id=? AND query_id=?", (analysis_id, query_id)).fetchone()
        conn.close()
        analysis = dict(arow) if arow else detail["analysis"]
    else:
        analysis = detail["analysis"]

    context_block = f"""=== REQUÊTE SQL ===
{q['sql_text']}

=== MÉTRIQUES ===
- Exécutions : {q.get('executions', '?')}
- Temps moyen : {q.get('elapsed_ms_avg', '?')} ms
- Temps max : {q.get('elapsed_ms_max', '?')} ms
- Buffer gets moy : {q.get('buffer_gets_avg', '?')}
- Disk reads moy : {q.get('disk_reads_avg', '?')}
- Schéma : {q.get('schema_name', '?')}
"""
    if plan:
        context_block += f"\n=== PLAN D'EXÉCUTION ===\n{(plan.get('plan_text') or '')}\n"
    if analysis:
        context_block += f"\n=== ANALYSE IA PRÉCÉDENTE (score {analysis.get('perf_score')}/100) ===\n{(analysis.get('raw_response') or '')}\n"

    # Historique du chat
    history = chat_get_messages(query_id)[-20:]

    # Construire les messages pour l'IA
    messages = [{"role": "user", "content": f"Voici le contexte de la requête Oracle à analyser :\n\n{context_block}\n\nTu es prêt à répondre à mes questions sur cette requête."}]
    messages.append({"role": "assistant", "content": "Parfait, j'ai bien le contexte complet de cette requête. Que voulez-vous savoir ?"})

    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_msg})

    # Sauvegarder la question
    chat_add_message(query_id, "user", user_msg)

    # Appel IA
    ai_model = get_setting("ai_model", "claude-sonnet-4.6")
    conn_ora = None
    try:
        from analyzer.oracle_tools import get_tools_schema_filtered, execute_tool_native
        from analyzer.copilot_client import chat_with_tools
        try:
            conn_ora = connect_oracle(
                user=get_setting("oracle_user", ORACLE_USER),
                password=get_setting("oracle_password", ORACLE_PASSWORD),
                dsn=get_setting("oracle_dsn", ORACLE_DSN),
            )
        except Exception:
            pass
        tools = get_tools_schema_filtered()
        raw = ""
        usage = {}
        for round_index in range(11):
            text, tool_calls, round_usage = chat_with_tools(
                messages=messages, tools=tools if round_index < 10 else [],
                model=ai_model, max_tokens=3000, system=CHAT_SYSTEM,
            )
            for key, value in (round_usage or {}).items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
            if text:
                raw = text
            if not tool_calls or round_index == 10:
                break
            messages.append({
                "role": "assistant", "content": text,
                "tool_calls": [{"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"])
                }} for call in tool_calls],
            })
            for call in tool_calls:
                result = execute_tool_native(conn_ora, call["name"], call["arguments"]) if conn_ora else {"error": "Oracle non disponible"}
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, default=str, ensure_ascii=True)})
    except Exception as e:
        import logging
        logging.getLogger("oracleiq").exception("Chat IA echoue pour %s", query_id)
        raise HTTPException(502, "Erreur IA. Consultez les journaux puis reessayez.") from e
    finally:
        if conn_ora:
            try:
                conn_ora.close()
            except Exception:
                pass

    # Sauvegarder la réponse
    chat_add_message(query_id, "assistant", raw)

    return {"reply": raw, "usage": usage}


@app.post("/api/analyze/all")
async def analyze_all_endpoint(request: Request):
    """Lance l'analyse IA pour toutes les requêtes non analysées (ou toutes si force=true)."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    force = body.get("force", False)

    conn = get_conn()
    if force:
        rows = conn.execute("SELECT id FROM queries").fetchall()
    else:
        rows = conn.execute("SELECT id FROM queries WHERE analyzed=0").fetchall()
    conn.close()
    ids = [r[0] for r in rows]

    queued = 0
    for qid in ids:
        try:
            result = await analyze_query_endpoint(qid, request)
            if result["status"] == "analyzing":
                queued += 1
        except HTTPException as error:
            if error.status_code == 429:
                break
            if error.status_code != 404:
                raise
    return {"ok": True, "queued": queued, "remaining": max(0, len(ids) - queued), "force": force}


@app.get("/api/analyses/{analysis_id}")
def get_analysis_detail(analysis_id: int):
    """Détail complet d'une analyse historique."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM ai_analyses WHERE id=?", (analysis_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Analyse introuvable")
    a = dict(row)
    for field in ("issues", "recommendations", "trace"):
        val = a.get(field)
        if isinstance(val, str):
            try:
                a[field] = json.loads(val)
            except Exception:
                a[field] = [] if field != "trace" else []
    return a


@app.delete("/api/analyses/{analysis_id}")
async def delete_analysis(analysis_id: int, request: Request):
    """Supprime une analyse spécifique (admin requis)."""
    if not _is_admin(request):
        raise HTTPException(403, "Non autorisé")
    conn = get_conn()
    row = conn.execute("SELECT id, query_id FROM ai_analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Analyse introuvable")
    query_id = row["query_id"]
    conn.execute("DELETE FROM ai_analyses WHERE id=?", (analysis_id,))
    # Si plus aucune analyse, marquer la requête comme non analysée
    remaining = conn.execute("SELECT COUNT(*) FROM ai_analyses WHERE query_id=?", (query_id,)).fetchone()[0]
    if remaining == 0:
        conn.execute("UPDATE queries SET analyzed=0, perf_score=NULL, severity=NULL WHERE id=?", (query_id,))
    else:
        # Mettre à jour le score de la requête avec l'analyse la plus récente restante
        latest = conn.execute(
            "SELECT perf_score, severity FROM ai_analyses WHERE query_id=? ORDER BY id DESC LIMIT 1",
            (query_id,)
        ).fetchone()
        if latest:
            conn.execute(
                "UPDATE queries SET perf_score=?, severity=? WHERE id=?",
                (latest["perf_score"], latest["severity"], query_id)
            )
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": analysis_id, "remaining": remaining}


# ─────────────────────────────────────────────
# Web UI
# ─────────────────────────────────────────────

@app.get("/export/pdf", response_class=HTMLResponse)
async def export_pdf(request: Request, ids: str = ""):
    """Page HTML optimisée impression/PDF pour les analyses sélectionnées."""
    if not ids:
        raise HTTPException(400, "ids requis")
    id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    if not id_list:
        raise HTTPException(400, "ids invalides")

    conn = get_conn()
    items = []
    for qid in id_list:
        row = conn.execute("""
            SELECT q.id, q.sql_text, q.schema_name, q.executions,
                   q.elapsed_ms_avg, q.elapsed_ms_max, q.buffer_gets_avg, q.disk_reads_avg,
                   a.analyzed_at, a.model_used, a.perf_score, a.severity,
                   a.summary, a.issues, a.recommendations, a.raw_response,
                   a.tokens_in, a.tokens_out
            FROM queries q
            LEFT JOIN ai_analyses a ON a.query_id = q.id
            WHERE q.id = ?
            ORDER BY a.analyzed_at DESC LIMIT 1
        """, (qid,)).fetchone()
        if row:
            d = dict(row)
            for field in ('issues', 'recommendations'):
                if isinstance(d.get(field), str):
                    try:
                        d[field] = json.loads(d[field])
                    except Exception:
                        d[field] = []
            items.append(d)
    conn.close()
    from datetime import datetime as _dt
    return templates.TemplateResponse(request=request, name="export_pdf.html", context={
        "request": request,
        "items": items,
        "generated_at": _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "count": len(items),
    })


@app.get("/settings/login", response_class=HTMLResponse)
async def settings_login_page(request: Request):
    return templates.TemplateResponse(request=request, name="settings_login.html", context={"request": request, "error": ""})

@app.post("/settings/login")
async def settings_login(request: Request, password: str = Form(...)):
    role = password_role(password)
    if role:
        token = _make_token(role)
        resp = RedirectResponse("/settings" if role == "admin" else "/", status_code=303)
        resp.set_cookie(_COOKIE_NAME, token, max_age=_COOKIE_TTL, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https" or os.getenv("ODIN_COOKIE_SECURE") == "true")
        return resp
    return templates.TemplateResponse(request=request, name="settings_login.html", context={"request": request, "error": "Mot de passe incorrect"}, status_code=401)

@app.get("/settings/logout")
async def settings_logout():
    resp = RedirectResponse("/settings/login", status_code=303)
    resp.delete_cookie(_COOKIE_NAME)
    return resp

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/settings/login", status_code=303)
    settings = {
        "analyzer_mode":    get_setting("analyzer_mode", "manual"),
        "analyzer_ai_mode": "native",
        "collector_active": get_setting("collector_active", "true") == "true",
        "ai_model":         get_setting("ai_model", "claude-sonnet-4.6"),
        "ai_max_tokens":    int(get_setting("ai_max_tokens", "8000")),
        "plan_truncate":    int(get_setting("plan_truncate", "3000")),
        "oracle_dsn":       get_setting("oracle_dsn",  ORACLE_DSN),
        "oracle_user":      get_setting("oracle_user", ORACLE_USER),
        "oracle_has_pwd":   bool(get_setting("oracle_password", ORACLE_PASSWORD)),
        "tools_enabled":    get_setting("tools_enabled", ""),
        "gather_stats_enabled": get_setting("gather_stats_enabled", "false") == "true",
        "system_prompt":    get_setting("system_prompt", ""),
    }
    return templates.TemplateResponse(request=request, name="settings.html", context={
        "request": request,
        "settings": settings,
    })


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from db.store import get_query_stats
    stats = get_query_stats()
    return templates.TemplateResponse(request=request, name="queries.html", context={
        "request": request,
        "stats": stats,
        "is_admin": _is_admin(request),
        "analyzer_ai_mode": "native",
    })


@app.get("/query/{query_id}", response_class=HTMLResponse)
async def query_detail_page(request: Request, query_id: int):
    detail = get_query_detail(query_id)
    if not detail["query"]:
        raise HTTPException(404)
    if detail["analysis"]:
        for field in ("issues", "recommendations"):
            val = detail["analysis"].get(field)
            if isinstance(val, str):
                try:
                    detail["analysis"][field] = json.loads(val)
                except Exception:
                    detail["analysis"][field] = []
    return templates.TemplateResponse(request=request, name="query_detail.html", context={
        "request": request,
        "is_admin": _is_admin(request),
        "analyzer_ai_mode": "native",
        **detail,
    })