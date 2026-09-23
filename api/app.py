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
import logging
from contextlib import asynccontextmanager
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
from db.store import get_all_queries, get_query_detail, get_conn, get_setting, set_setting, get_settings, set_settings
from config import ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD, AI_MODEL
from collector.connection import (
    connect_oracle, query_execution_enabled, get_oracle_settings, assert_query_source,
    is_execution_plan_available,
)
import analyzer.copilot_client as _copilot_client  # import au niveau module pour éviter le cache stale dans les threads

# Suivi en mémoire des analyses en cours (survit aux refreshs, pas aux redémarrages serveur)
analyzing_ids: set[int] = set()
# Queues SSE par query_id — permet la reconnexion si refresh pendant analyse native
_stream_queues: dict[int, list[_queue_mod.Queue]] = {}
_analysis_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="odin-analysis")
_analysis_lock = threading.RLock()
_analysis_results: dict[int, tuple[float, dict]] = {}
_chat_ids: set[int] = set()
_log = logging.getLogger("oracleiq.api")
_STREAM_TIMEOUT = 600


def _query_row(query_id: int) -> dict:
    connection = get_conn()
    try:
        row = connection.execute("""
            SELECT q.*, ep.plan_text, ep.id AS plan_id FROM queries q
            LEFT JOIN execution_plans ep ON ep.id=(
                SELECT id FROM execution_plans WHERE query_id=q.id ORDER BY id DESC LIMIT 1
            ) WHERE q.id=?
        """, (query_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(404, "Requête introuvable")
    return dict(row)


def _oracle_connection(row=None):
    settings = get_oracle_settings()
    connection = connect_oracle(user=settings["oracle_user"], password=settings["oracle_password"],
                                dsn=settings["oracle_dsn"])
    try:
        if row is not None:
            assert_query_source(row, connection)
        return connection
    except Exception:
        try:
            connection.close()
        except Exception:
            _log.exception("Fermeture Oracle apres refus de source echouee")
        raise


async def _alert_loop(stop):
    from db.alerts import evaluate_alerts
    while not stop.is_set():
        try:
            await run_in_threadpool(evaluate_alerts)
        except Exception:
            _log.exception("Evaluation periodique des alertes echouee")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def _lifespan(application):
    stop = asyncio.Event()
    task = asyncio.create_task(_alert_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await task


def _safe_json(obj) -> str:
    """Sérialise en JSON en éliminant les surrogates Unicode (caractères Oracle invalides)."""
    import re as _re
    raw = json.dumps(obj, default=str, ensure_ascii=False)
    raw = _re.sub(r'[\ud800-\udfff]', '\ufffd', raw)
    return raw

app = FastAPI(title="OracleIQ", version="1.0.0", lifespan=_lifespan)

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
    ai_max_tokens: int | None = Field(default=None, ge=256, le=32000)
    plan_truncate: int | None = Field(default=None, ge=100, le=40000)
    oracle_dsn: str | None = Field(default=None, max_length=1000)
    oracle_user: str | None = Field(default=None, max_length=128)
    oracle_password: str | None = Field(default=None, max_length=1024)
    tools_enabled: str | None = Field(default=None, max_length=2000)
    gather_stats_enabled: bool | None = None
    ai_send_raw_values: bool | None = None
    system_prompt: str | None = Field(default=None, max_length=20000)

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


@app.get("/api/alerts")
def api_alerts(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
               include_acknowledged: bool = True):
    from db.alerts import list_alerts
    return list_alerts(limit=limit, offset=offset, include_acknowledged=include_acknowledged)


@app.post("/api/alerts/{alert_id}/acknowledge")
def api_acknowledge_alert(alert_id: int, request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    from db.alerts import acknowledge_alert
    if not acknowledge_alert(alert_id):
        raise HTTPException(404, "Alerte introuvable")
    return {"ok": True}


@app.get("/api/queries/{query_id}/ai-preview")
def api_ai_preview(query_id: int, request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "Droits administrateur requis")
    from analyzer.ai_analyzer import build_ai_preview
    row = _query_row(query_id)
    try:
        preview = build_ai_preview(row)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    context = {key: preview[key] for key in ("prompt", "system", "tools") if key in preview}
    return {**preview, "ai_send_raw_values": bool(preview.get("send_raw_values", False)),
            "context": context, "scope": "initial_context", "plan_id": row.get("plan_id"),
            "warnings": [*preview.get("warnings", []),
                         "Apercu du contexte initial actuellement enregistre ; un rafraichissement du plan peut le modifier.",
                         "Les futurs appels et resultats d'outils, ainsi que les reponses IA, ne sont pas inclus."]}


@app.get("/api/stats")
def api_stats():
    from db.store import get_query_stats
    stats = get_query_stats()
    stats["analyzer_mode"] = get_setting("analyzer_mode", "manual")
    stats["collector_active"] = get_setting("collector_active", "true") == "true"
    # Merger les analyses en cours : bouton manuel (mémoire) + analyzer auto (DB)
    from db.store import analyzing_queue_get
    with _analysis_lock:
        all_analyzing = set(analyzing_ids) | set(analyzing_queue_get())
    stats["analyzing_ids"] = list(all_analyzing)
    return stats


@app.delete("/api/queries/{query_id}")
def delete_query(query_id: int, request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Authentification requise")
    from db.store import analyzing_queue_get
    from db.store import delete_query_data
    with _analysis_lock:
        if query_id in analyzing_queue_get() or query_id in _chat_ids:
            raise HTTPException(409, "Une analyse ou un chat est en cours pour cette requete")
        try:
            delete_query_data(query_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        _analysis_results.pop(query_id, None)
    return {"ok": True}


@app.delete("/api/queries")
async def purge_queries(request: Request):
    """Purge les données enregistrées : tout, les requêtes analysées, ou les analyses seules."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Authentification requise")
    from db.store import analyzing_queue_get
    try:
        body = await request.json()
    except Exception:
        body = {}
    scope = body.get("scope", "analyzed")  # 'all' | 'analyzed' | 'analyses'
    if scope not in ("all", "analyzed", "analyses"):
        raise HTTPException(status_code=400, detail="scope invalide")
    from db.store import purge_data
    with _analysis_lock:
        if analyzing_queue_get() or _chat_ids:
            raise HTTPException(409, "Attendez la fin des analyses et chats avant de purger")
        try:
            counts = purge_data(scope)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        analyzing_ids.clear()
        _analysis_results.clear()
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
    except _copilot_client.CopilotAuthenticationError as error:
        raise HTTPException(502, str(error)) from None
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
    values = get_settings({
        "analyzer_mode": "manual", "collector_active": "true",
        "ai_model": AI_MODEL, "ai_max_tokens": "8000", "plan_truncate": "40000",
        "oracle_dsn": ORACLE_DSN, "oracle_user": ORACLE_USER, "oracle_password": ORACLE_PASSWORD,
        "tools_enabled": "", "gather_stats_enabled": "false", "system_prompt": "",
        "ai_send_raw_values": "false",
    })
    return {
        "analyzer_mode":    values["analyzer_mode"],
        "analyzer_ai_mode": "native",
        "collector_active": values["collector_active"] == "true",
        "ai_model":         values["ai_model"],
        "ai_max_tokens":    int(values["ai_max_tokens"]),
        "plan_truncate":    int(values["plan_truncate"]),
        "oracle_dsn":       values["oracle_dsn"],
        "oracle_user":      values["oracle_user"],
        "oracle_has_pwd":   bool(values["oracle_password"]),
        "tools_enabled":    values["tools_enabled"],
        "gather_stats_enabled": values["gather_stats_enabled"] == "true",
        "ai_send_raw_values": values["ai_send_raw_values"] == "true",
        "system_prompt":    values["system_prompt"],
        # on ne retourne jamais le mot de passe
    }


@app.post("/api/settings")
async def save_all_settings(request: Request, settings: SettingsPatch):
    """Sauvegarde un ou plusieurs paramètres."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Authentification requise")
    body = settings.model_dump(exclude_unset=True, exclude_none=True)
    values = {}
    for key, val in body.items():
        if isinstance(val, bool):
            val = "true" if val else "false"
        values[key] = str(val)
    set_settings(values)
    saved = {key: val for key, val in values.items() if key != "oracle_password"}
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
    settings = get_oracle_settings()
    dsn  = body.get("oracle_dsn")  or settings["oracle_dsn"]
    user = body.get("oracle_user") or settings["oracle_user"]
    pwd  = body.get("oracle_password")
    if not pwd:
        pwd = settings["oracle_password"]
    try:
        import oracledb
        c = connect_oracle(user=user, password=pwd, dsn=dsn)
        try:
            v = c.version
        finally:
            c.close()
        return {"ok": True, "version": v, "dsn": dsn, "user": user}
    except Exception:
        _log.exception("Test de connexion Oracle echoue")
        return {"ok": False, "error": "Connexion Oracle impossible. Consultez les journaux."}


@app.post("/api/repair_sql_texts")
def repair_sql_texts():
    """Récupère le texte SQL complet pour toutes les requêtes tronquées (length=1000)."""
    conn_ora = None
    try:
        from collector.oracle_collector import get_full_sql_text
        conn_sqlite = get_conn()
        try:
            rows = conn_sqlite.execute(
                "SELECT *, length(sql_text) as l FROM queries WHERE length(sql_text) >= 999"
            ).fetchall()
        finally:
            conn_sqlite.close()
        if not rows:
            return {"ok": True, "total": 0, "fixed": 0, "failed": 0}
        conn_ora = _oracle_connection()
        # Validate the entire batch before enriching even one historical row.
        for row in rows:
            assert_query_source(dict(row), conn_ora)

        fixed = 0
        failed = 0
        for row in rows:
            qid, sql_id, cur_len = row["id"], row["sql_id"], row["l"]
            try:
                full = get_full_sql_text(conn_ora, sql_id)
                if full and len(full) > cur_len:
                    conn_fix = get_conn()
                    try:
                        with conn_fix:
                            conn_fix.execute("UPDATE queries SET sql_text=? WHERE id=?", (full, qid))
                    finally:
                        conn_fix.close()
                    fixed += 1
            except Exception:
                _log.exception("Reparation SQL echouee pour %s", qid)
                failed += 1
        return {"ok": failed == 0, "total": len(rows), "fixed": fixed, "failed": failed}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except Exception:
        _log.exception("Reparation des textes SQL echouee")
        return {"ok": False, "error": "Reparation Oracle indisponible."}
    finally:
        if conn_ora is not None:
            conn_ora.close()


@app.post("/api/refresh_plan/{query_id}")
def refresh_plan_endpoint(query_id: int):
    """Rafraîchit le plan d’exécution Oracle pour une requête donnée."""
    q = _query_row(query_id)
    conn_ora = None
    try:
        from collector.oracle_collector import get_execution_plan
        conn_ora = _oracle_connection(q)
        plan = get_execution_plan(conn_ora, q["sql_id"], q.get("child_number", 0))
        if is_execution_plan_available(plan):
            from db.store import save_plan
            save_plan(query_id, plan)
            return {"ok": True, "plan": plan}
        return {"ok": False, "error": "Plan non disponible depuis Oracle"}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except Exception:
        _log.exception("Rafraichissement du plan echoue pour %s", query_id)
        return {"ok": False, "error": "Plan Oracle indisponible."}
    finally:
        if conn_ora is not None:
            conn_ora.close()


@app.post("/api/analyze/{query_id:int}")
async def analyze_query_endpoint(query_id: int, request: Request):
    _query_row(query_id)
    started = _start_analysis(query_id)
    return {"ok": True, "query_id": query_id,
            "status": "analyzing" if started else "already_analyzing", "refresh_plan": True}


def _publish(query_id: int, event: dict):
    with _analysis_lock:
        for subscriber in _stream_queues.get(query_id, []):
            if subscriber.full():
                try:
                    subscriber.get_nowait()
                except _queue_mod.Empty:
                    pass
            subscriber.put_nowait(event)


def _finish_analysis(query_id: int, event: dict):
    from db.store import analyzing_queue_remove
    with _analysis_lock:
        try:
            analyzing_queue_remove(query_id)
        except Exception:
            _log.exception("Liberation de reservation echouee pour %s", query_id)
        analyzing_ids.discard(query_id)
        _analysis_results[query_id] = (time.monotonic(), event)
        for old_id, (finished, _) in list(_analysis_results.items()):
            if time.monotonic() - finished > _STREAM_TIMEOUT:
                _analysis_results.pop(old_id, None)
        _publish(query_id, event)
        _stream_queues.pop(query_id, None)


def _analysis_event(query_id: int, event: dict):
    kind = event.get("type")
    if kind == "thinking":
        event = {"type": "status", "message": "IA en cours d'analyse"}
    elif kind == "tool_start":
        event = {"type": "tool_call", "name": event["tool"], "args": event.get("args", {})}
    elif kind == "tool_result":
        event = {"type": "tool_result", "name": event["tool"],
                 "preview": _safe_json(event.get("result", {}))[:200],
                 "ok": event.get("ok", False), "ms": event.get("ms", 0)}
    elif kind == "complete":
        event = {"type": "analysis", "content": event["result"].get("raw", "")}
    _publish(query_id, event)


def _run_analysis(query_id: int):
    connection = None
    row = None
    stale_result = False
    terminal = {"type": "error", "message": "Analyse echouee. Consultez les journaux."}
    try:
        from db.store import save_analysis, save_plan
        from analyzer.ai_analyzer import analyze_query
        row = _query_row(query_id)
        terminal["plan_id"] = row.get("plan_id")
        try:
            connection = _oracle_connection(row)
        except ValueError:
            # Identity failures must never silently become offline analyses.
            raise
        except Exception:
            _log.warning("Oracle indisponible pour l'analyse %s", query_id, exc_info=True)
            _publish(query_id, {"type": "status", "message": "Oracle indisponible : contexte local uniquement."})
        if connection is not None:
            from collector.oracle_collector import get_execution_plan
            new_plan = get_execution_plan(connection, row["sql_id"], row.get("child_number", 0))
            if is_execution_plan_available(new_plan):
                save_plan(query_id, new_plan)
                row = _query_row(query_id)
                terminal["plan_id"] = row.get("plan_id")
                _publish(query_id, {"type": "status", "message": "Plan Oracle rafraichi"})
        result = analyze_query(row, oracle_conn=connection,
                               on_event=lambda event: _analysis_event(query_id, event))
        saved = save_analysis(query_id, result, expected_plan_id=row.get("plan_id"))
        current = _query_row(query_id)
        if saved is False or current.get("plan_id") != row.get("plan_id") or not current.get("analyzed"):
            stale_result = True
            raise ValueError("Le plan a change pendant l'analyse. Relancez sur la version courante.")
        terminal = {"type": "done", "score": result["score"], "severity": result["severity"],
                    "plan_id": row.get("plan_id")}
    except Exception as error:
        _log.exception("Analyse echouee pour %s", query_id)
        if isinstance(error, ValueError):
            terminal["message"] = str(error)
        try:
            from db.store import save_analysis_error
            if row is not None and not stale_result:
                save_analysis_error(query_id, terminal["message"], expected_plan_id=row.get("plan_id"))
        except Exception:
            _log.exception("Enregistrement d'erreur d'analyse echoue pour %s", query_id)
    finally:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            _log.exception("Fermeture Oracle echouee pour %s", query_id)
        finally:
            _finish_analysis(query_id, terminal)


def _start_analysis(query_id: int) -> bool:
    from db.store import analyzing_queue_add
    with _analysis_lock:
        if query_id in analyzing_ids:
            return False
        try:
            if not analyzing_queue_add(query_id):
                return False
        except ValueError as error:
            _query_row(query_id)
            raise HTTPException(429, str(error)) from error
        analyzing_ids.add(query_id)
        _analysis_results.pop(query_id, None)
        try:
            _analysis_executor.submit(_run_analysis, query_id)
        except Exception as error:
            _finish_analysis(query_id, {"type": "error", "message": "Service d'analyse indisponible."})
            raise HTTPException(503, "Service d'analyse indisponible.") from error
        return True


@app.get("/api/analyze/{query_id}/stream")
async def analyze_query_stream(query_id: int, request: Request):
    """Start or subscribe to the same job used by POST and bulk analyses."""
    from db.store import analyzing_queue_get
    row = _query_row(query_id)
    initial_analysis = get_query_detail(query_id).get("analysis")
    initial_analysis_id = initial_analysis["id"] if initial_analysis else None
    subscriber = _queue_mod.Queue(maxsize=128)
    external = False
    with _analysis_lock:
        terminal = _analysis_results.get(query_id)
        if (terminal and time.monotonic() - terminal[0] < 30
                and terminal[1].get("plan_id") == row.get("plan_id")):
            subscriber.put(terminal[1])
        else:
            _stream_queues.setdefault(query_id, []).append(subscriber)
            try:
                if query_id not in analyzing_ids:
                    external = not _start_analysis(query_id)
            except Exception:
                subscribers = _stream_queues.get(query_id, [])
                if subscriber in subscribers:
                    subscribers.remove(subscriber)
                if not subscribers:
                    _stream_queues.pop(query_id, None)
                raise

    async def events():
        deadline = time.monotonic() + _STREAM_TIMEOUT
        try:
            while time.monotonic() < deadline:
                if await request.is_disconnected():
                    return
                try:
                    event = subscriber.get_nowait()
                except _queue_mod.Empty:
                    if external and query_id not in await run_in_threadpool(analyzing_queue_get):
                        detail = await run_in_threadpool(get_query_detail, query_id)
                        analysis = detail.get("analysis")
                        current = detail.get("query") or {}
                        if current.get("analysis_error"):
                            event = {"type": "error", "message": "Analyse automatique echouee. Consultez les journaux."}
                        elif (analysis and analysis["id"] != initial_analysis_id
                              and current.get("analyzed")
                              and analysis.get("plan_id") == (detail.get("plan") or {}).get("id")):
                            event = {"type": "done", "score": analysis["perf_score"],
                                     "severity": analysis["severity"]}
                        else:
                            event = {"type": "error", "message": "Analyse terminee sans nouveau resultat valide."}
                    else:
                        yield ": keepalive\n\n"
                        await asyncio.sleep(0.2)
                        continue
                yield "data: " + _safe_json(event) + "\n\n"
                if event["type"] in {"done", "error"}:
                    return
            yield "data: " + _safe_json({"type": "error", "message": "Delai d'attente depasse. Reconnectez-vous pour suivre l'analyse."}) + "\n\n"
        except Exception:
            _log.exception("Suivi d'analyse echoue pour %s", query_id)
            yield "data: " + _safe_json({"type": "error", "message": "Suivi d'analyse indisponible. Reconnectez-vous."}) + "\n\n"
        finally:
            with _analysis_lock:
                subscribers = _stream_queues.get(query_id, [])
                if subscriber in subscribers:
                    subscribers.remove(subscriber)
                if not subscribers:
                    _stream_queues.pop(query_id, None)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    row = sconn.execute("SELECT * FROM queries WHERE id=?", (query_id,)).fetchone()
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

    child_number = row["child_number"]
    if not isinstance(child_number, int) or child_number < 0:
        raise HTTPException(409, "Identite du curseur enfant inconnue ; rafraichissement des binds refuse.")
    oconn = None
    try:
        oconn = _oracle_connection(dict(row))
        from analyzer.oracle_tools import bind_captures
        from db.store import save_bind_values
        result = bind_captures(oconn, sql_id, child_number=child_number)
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
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except Exception:
        _log.exception("Rafraichissement des binds echoue pour %s", query_id)
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

def _has_replay_binds(sql_text: str) -> bool:
    import re
    from analyzer.data_policy import mask_sql
    masked = mask_sql(sql_text, mask_numbers=False)
    # The shared lexer removes literals/comments; skip quoted identifiers as units.
    tokens = re.finditer(r'"(?:[^"]|"")*(?:"|$)|(:[A-Za-z_0-9"])', masked)
    return any(token.group(1) is not None for token in tokens)


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
        "SELECT * FROM queries WHERE id=?", (query_id,)
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
    if _has_replay_binds(sql_text):
        return {"ok": False, "error": "Rejeu des requetes bindees non pris en charge : valeurs de binds explicites requises."}
    parsing_schema = row["schema_name"]
    if not isinstance(parsing_schema, str) or not parsing_schema.strip() or parsing_schema == "UNKNOWN":
        return {"ok": False, "error": "Rejeu refuse : schema de parsing historique inconnu."}

    try:
        oconn = _oracle_connection(dict(row))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except Exception:
        _log.exception("Connexion de rejeu echouee pour %s", query_id)
        return {"ok": False, "error": "Connexion Oracle impossible."}

    schema_cursor = None
    schema_verified = False
    try:
        schema_cursor = oconn.cursor()
        schema_cursor.execute("SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual")
        schema_row = schema_cursor.fetchone()
        schema_verified = bool(schema_row and schema_row[0] == parsing_schema)
        if not schema_verified:
            return {"ok": False, "error": "Rejeu refuse : le schema Oracle courant differe du schema de parsing collecte. Aucun SQL rejoue."}
    except Exception:
        _log.exception("Verification du schema de rejeu echouee pour %s", query_id)
        return {"ok": False, "error": "Rejeu refuse : schema Oracle courant impossible a verifier."}
    finally:
        if schema_cursor is not None:
            try:
                schema_cursor.close()
            except Exception:
                _log.exception("Fermeture du curseur de schema echouee pour %s", query_id)
        if not schema_verified:
            try:
                oconn.close()
            except Exception:
                _log.exception("Fermeture Oracle apres refus du rejeu echouee pour %s", query_id)

    result = {"ok": True, "query_id": query_id}
    cursors = []
    statement_id = "ODIN_" + __import__("uuid").uuid4().hex[:24]

    # 1. EXPLAIN PLAN pour obtenir le plan sans les stats ALLSTATS (SQL non exécuté)
    try:
        cur = oconn.cursor()
        cursors.append(cur)
        cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID='{statement_id}' FOR {sql_text}")
        cur2 = oconn.cursor()
        cursors.append(cur2)
        cur2.execute("""
            SELECT * FROM TABLE(
                DBMS_XPLAN.DISPLAY('PLAN_TABLE', :statement_id, 'ALL')
            )
        """, statement_id=statement_id)
        plan_lines = []
        for r in cur2.fetchall():
            v = r[0]
            if hasattr(v, 'read'): v = v.read()
            if v: plan_lines.append(str(v))
        result["plan_text"] = "\n".join(plan_lines) if plan_lines else "[Plan non disponible]"
        # Nettoyage de la plan table
        try:
            cur.execute("DELETE FROM plan_table WHERE statement_id=:statement_id",
                        statement_id=statement_id)
            oconn.commit()
        except Exception:
            _log.exception("Nettoyage PLAN_TABLE echoue pour %s", query_id)
            result["ok"] = False
            result["error"] = "Nettoyage du plan de rejeu echoue."
    except Exception:
        _log.exception("EXPLAIN PLAN de rejeu echoue pour %s", query_id)
        result["ok"] = False
        result["plan_text"] = "[EXPLAIN PLAN non disponible]"
        result["error"] = "Generation du plan de rejeu echouee."

    # 2. Exécution réelle avec ROWNUM <= 5 pour mesurer le temps
    try:
        wrapped = f"SELECT * FROM ({sql_text}) WHERE ROWNUM <= 5"
        cur3 = oconn.cursor()
        cursors.append(cur3)
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
    except Exception:
        _log.exception("Execution de rejeu echouee pour %s", query_id)
        result["ok"] = False
        result["elapsed_ms"]   = None
        result["rows_returned"] = None
        result["exec_error"] = "Execution Oracle echouee."
    finally:
        for cursor in cursors:
            try:
                cursor.close()
            except Exception:
                _log.exception("Fermeture du curseur de rejeu echouee")
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
    with _analysis_lock:
        if query_id in _chat_ids:
            raise HTTPException(409, "Une reponse de chat est en cours.")
        chat_clear(query_id)
    return {"ok": True}


@app.post("/api/queries/{query_id}/chat")
async def post_chat(query_id: int, request: Request):
    body = await request.json()
    return await run_in_threadpool(_post_chat, query_id, body)


def _post_chat(query_id: int, body: dict):
    with _analysis_lock:
        if query_id in _chat_ids:
            raise HTTPException(409, "Une reponse de chat est deja en cours.")
        _chat_ids.add(query_id)
    try:
        return _post_chat_turn(query_id, body)
    finally:
        with _analysis_lock:
            _chat_ids.discard(query_id)


def _post_chat_turn(query_id: int, body: dict):
    if not isinstance(body, dict):
        raise HTTPException(422, "Objet JSON requis")
    user_msg = body.get("message")
    if not isinstance(user_msg, str) or len(user_msg) > 10000:
        raise HTTPException(422, "message doit contenir au maximum 10000 caracteres")
    user_msg = user_msg.strip()
    if not user_msg:
        raise HTTPException(400, "message requis")

    analysis_id = body.get("analysis_id")
    from db.store import chat_get_messages, chat_add_turn, get_query_detail, get_setting, get_conn

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

    from analyzer.ai_analyzer import build_ai_preview
    try:
        context_block = build_ai_preview({
            **q, "plan_text": (plan or {}).get("plan_text", ""),
        })["prompt"]
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
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

    # Appel IA
    ai_model = get_setting("ai_model", AI_MODEL)
    conn_ora = None
    try:
        from analyzer.oracle_tools import get_tools_schema_filtered, execute_tool_native
        from analyzer.copilot_client import chat_with_tools
        from analyzer.data_policy import (
            prepare_messages, sanitize_data, raw_values_enabled, check_budget, tool_payload,
            ANALYSIS_TIMEOUT_SECONDS, MAX_TOOL_CALLS,
        )
        deadline = time.monotonic() + ANALYSIS_TIMEOUT_SECONDS
        send_raw = raw_values_enabled()
        try:
            conn_ora = _oracle_connection(q)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Exception:
            _log.warning("Oracle indisponible pour le chat %s", query_id, exc_info=True)
        tools = get_tools_schema_filtered() if conn_ora is not None else []
        raw = ""
        usage = {}
        tool_count = 0
        for round_index in range(11):
            check_budget(deadline)
            messages, safe_system = prepare_messages(messages, CHAT_SYSTEM, raw=send_raw)
            text, tool_calls, round_usage = chat_with_tools(
                messages=messages, tools=tools if round_index < 10 else [],
                model=ai_model, max_tokens=3000, system=safe_system, deadline=deadline,
            )
            check_budget(deadline)
            for key, value in (round_usage or {}).items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
            if text:
                raw = text
            if round_index == 10 and tool_calls:
                raise ValueError("Limite des tours de chat depassee.")
            if not tool_calls:
                raw = text or ""
                break
            tool_count += len(tool_calls)
            if tool_count > MAX_TOOL_CALLS:
                raise ValueError("Limite des outils de chat depassee.")
            messages.append({
                "role": "assistant", "content": text,
                "tool_calls": [{"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"])
                }} for call in tool_calls],
            })
            for call in tool_calls:
                check_budget(deadline)
                arguments = dict(call["arguments"])
                if call["name"] == "explain_plan":
                    arguments.setdefault("child_number", q.get("child_number", 0))
                elif call["name"] == "bind_captures" and arguments.get("sql_id") == q.get("sql_id"):
                    arguments["child_number"] = q.get("child_number", 0)
                result = execute_tool_native(conn_ora, call["name"], arguments,
                                             deadline=deadline) if conn_ora else {"error": "Oracle non disponible"}
                result = sanitize_data(result, raw=send_raw)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": tool_payload(result)})
        if not raw.strip():
            raise ValueError("Reponse IA vide")
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger("oracleiq").exception("Chat IA echoue pour %s", query_id)
        raise HTTPException(502, "Erreur IA. Consultez les journaux puis reessayez.") from e
    finally:
        if conn_ora:
            try:
                conn_ora.close()
            except Exception:
                _log.exception("Fermeture Oracle du chat echouee pour %s", query_id)

    # Persist only complete turns while the per-query reservation is held.
    chat_add_turn(query_id, user_msg, raw)

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
            "SELECT perf_score, severity, plan_id FROM ai_analyses WHERE query_id=? ORDER BY id DESC LIMIT 1",
            (query_id,)
        ).fetchone()
        if latest:
            plan = conn.execute("SELECT id FROM execution_plans WHERE query_id=? ORDER BY id DESC LIMIT 1",
                                (query_id,)).fetchone()
            current = latest["plan_id"] == (plan["id"] if plan else None)
            conn.execute(
                "UPDATE queries SET analyzed=?, perf_score=?, severity=? WHERE id=?",
                (int(current), latest["perf_score"] if current else None,
                 latest["severity"] if current else None, query_id)
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
    settings = get_all_settings(request)
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