"""Persistent local alerts; acknowledgements never resolve an incident.

Regressions are suspicions, not proof: compare adjacent 15-minute windows of
counter deltas, with >=10 executions and >=50% coverage in *each* window,
and both >=50% and >=100 ms latency increases from a positive baseline.
Missing data (including an uncomparable zero baseline) is not recovery.
Regression scans run at most once per minute, shared across API processes.
Disabled settings resolve service incidents. An observed stopped service is an
incident when enabled; paused/manual heartbeats get their remaining TTL grace.
"""
import json
import time
from datetime import datetime, timezone
from itertools import groupby

from db import store
from db.performance import summarize_period


WINDOW_SECONDS = 15 * 60
EVALUATION_INTERVAL_SECONDS = 60
MIN_EXECUTIONS = 10
MIN_COVERAGE_PERCENT = 50
MIN_INCREASE_PERCENT = 50
MIN_INCREASE_MS = 100


def _timestamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def list_alerts(limit=50, offset=0, include_acknowledged=True) -> dict:
    """Return newest first; unacknowledged counts all unacknowledged history."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    predicate = "1=1" if include_acknowledged else "acknowledged_at IS NULL"
    connection = store.get_conn()
    try:
        connection.execute("BEGIN")
        total = connection.execute(f"SELECT COUNT(*) FROM alerts WHERE {predicate}").fetchone()[0]
        unacknowledged = connection.execute(
            "SELECT COUNT(*) FROM alerts WHERE acknowledged_at IS NULL"
        ).fetchone()[0]
        items = connection.execute(
            "SELECT id,kind,severity,title,message,query_id,created_at,acknowledged_at,resolved_at "
            f"FROM alerts WHERE {predicate} ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset),
        ).fetchall()
        return {"items": [dict(row) for row in items], "total": total, "unacknowledged": unacknowledged}
    finally:
        connection.close()


def acknowledge_alert(id) -> bool:
    """Idempotently acknowledge an existing incident, including resolved ones."""
    connection = store.get_conn()
    try:
        with connection:
            result = connection.execute(
                "UPDATE alerts SET acknowledged_at=COALESCE(acknowledged_at, ?) WHERE id=?",
                (_timestamp(time.time()), id),
            )
        return result.rowcount > 0
    finally:
        connection.close()


def _incident(connection, key, now, *, kind, severity, title, message, query_id=None):
    existing = connection.execute(
        "SELECT id FROM alerts WHERE dedup_key=? AND resolved_at IS NULL", (key,),
    ).fetchone()
    if existing:
        connection.execute(
            "UPDATE alerts SET severity=?,title=?,message=? WHERE id=?",
            (severity, title, message, existing["id"]),
        )
    else:
        connection.execute(
            "INSERT INTO alerts(dedup_key,kind,severity,title,message,query_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (key, kind, severity, title, message, query_id, _timestamp(now)),
        )


def _resolve(connection, key, now):
    connection.execute(
        "UPDATE alerts SET resolved_at=? WHERE dedup_key=? AND resolved_at IS NULL", (_timestamp(now), key),
    )


def _evaluate_services(connection, now):
    settings = dict(connection.execute(
        "SELECT key,value FROM settings WHERE key IN "
        "('collector_active','analyzer_mode','service_collector','service_analyzer')"
    ))
    for name, label in (("collector", "Collecteur"), ("analyzer", "Analyseur")):
        key = f"service:{name}"
        disabled = (name == "collector" and settings.get("collector_active", "true") != "true"
                    or name == "analyzer" and settings.get("analyzer_mode", "manual") != "auto")
        if disabled:
            _resolve(connection, key, now)
            continue
        try:
            status = json.loads(settings.get(f"service_{name}", "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(status, dict) or status.get("updated_at") is None:
            continue
        state = status.get("state")
        expires_at = status.get("expires_at")
        stale = isinstance(expires_at, (float, int)) and expires_at < now
        failure = state in ("error", "stopped")
        if failure or stale:
            reason = {"error": "erreur", "stopped": "arrêté"}.get(state, "signal expiré")
            if state == "error":
                message = f"Le service {label.lower()} a signalé une erreur."
            elif state == "stopped":
                message = f"Le service {label.lower()} est arrêté alors que sa configuration le demande actif."
            else:
                message = f"Le signal du service {label.lower()} a expiré ; vérifier son processus."
            _incident(
                connection, key, now, kind="service", severity="critical" if failure else "warning",
                title=f"{label} : {reason}", message=message,
            )
        elif state and state != "unknown":
            _resolve(connection, key, now)


def _evaluate_regressions(connection, now):
    key = "alerts_last_regression_evaluation"
    last = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if last:
        try:
            elapsed = now - float(last[0])
            if 0 <= elapsed < EVALUATION_INTERVAL_SECONDS:
                return
        except (TypeError, ValueError):
            pass
    rows = connection.execute(
        "SELECT * FROM performance_samples WHERE observed_at>=? AND observed_at<=? "
        "ORDER BY query_id,observed_at,id", (now - 2 * WINDOW_SECONDS, now),
    )
    for query_id, group in groupby(rows, key=lambda row: row["query_id"]):
        samples = list(group)
        previous = summarize_period(samples, now - 2 * WINDOW_SECONDS, now - WINDOW_SECONDS)
        current = summarize_period(samples, now - WINDOW_SECONDS, now)
        if not all(period["executions"] >= MIN_EXECUTIONS
                   and period["coverage_percent"] >= MIN_COVERAGE_PERCENT for period in (previous, current)):
            continue
        baseline, latency = previous["elapsed_ms_avg"], current["elapsed_ms_avg"]
        if baseline <= 0:
            continue
        increase = latency - baseline
        incident_key = f"regression:{query_id}"
        if increase >= MIN_INCREASE_MS and increase * 100 >= baseline * MIN_INCREASE_PERCENT:
            _incident(
                connection, incident_key, now, kind="regression", severity="warning", query_id=query_id,
                title="Suspicion de régression",
                message=(f"Latence mesurée : {baseline:.1f} ms → {latency:.1f} ms par exécution "
                         f"sur deux périodes de 15 min ({previous['executions']} / {current['executions']} exécutions). "
                         "Hausse d'au moins 50 % et 100 ms, couverture d'au moins 50 % par période. "
                         "Signal à investiguer, pas une preuve de régression."),
            )
        else:
            _resolve(connection, incident_key, now)
    connection.execute(
        "UPDATE alerts SET resolved_at=? WHERE kind='regression' AND query_id IS NULL AND resolved_at IS NULL",
        (_timestamp(now),),
    )
    connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(now)))


def evaluate_alerts(now=None) -> None:
    """Evaluate observed, enabled services and measured regressions atomically."""
    now = time.time() if now is None else now
    connection = store.get_conn()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _evaluate_services(connection, now)
            _evaluate_regressions(connection, now)
    finally:
        connection.close()
