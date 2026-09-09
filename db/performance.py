import difflib
import time

from db import store


COUNTERS = ("executions", "elapsed_us", "cpu_us", "buffer_gets", "disk_reads", "rows_processed")


def summarize_period(samples, start, end):
    totals = dict.fromkeys(COUNTERS, 0)
    excluded = {"gap": 0, "cursor_change": 0, "plan_change": 0, "reset": 0, "in_flight": 0}
    coverage = 0
    intervals = 0
    for previous, current in zip(samples, samples[1:]):
        if previous["observed_at"] < start or current["observed_at"] > end:
            continue
        duration = current["observed_at"] - previous["observed_at"]
        if duration <= 0:
            continue
        if duration > 180:
            excluded["gap"] += 1
            continue
        if previous["cursor_generation"] != current["cursor_generation"]:
            excluded["cursor_change"] += 1
            continue
        if previous["plan_hash_value"] != current["plan_hash_value"]:
            excluded["plan_change"] += 1
            continue
        delta = {field: current[field] - previous[field] for field in COUNTERS}
        if any(value < 0 for value in delta.values()):
            excluded["reset"] += 1
            continue
        if delta["executions"] == 0 and any(delta.values()):
            excluded["in_flight"] += 1
            continue
        coverage += duration
        intervals += 1
        for field, value in delta.items():
            totals[field] += value
    executions = totals["executions"]
    return {
        "start": start, "end": end, "coverage_seconds": coverage,
        "coverage_percent": 100 * coverage / (end - start), "intervals": intervals,
        "excluded": excluded, "executions": executions,
        "state": "insufficient" if not intervals else "measured" if executions else "idle",
        "elapsed_ms_avg": totals["elapsed_us"] / executions / 1000 if executions else None,
        "cpu_ms_avg": totals["cpu_us"] / executions / 1000 if executions else None,
        "buffer_gets_avg": totals["buffer_gets"] / executions if executions else None,
        "disk_reads_avg": totals["disk_reads"] / executions if executions else None,
        "rows_avg": totals["rows_processed"] / executions if executions else None,
        "elapsed_ms_total": totals["elapsed_us"] / 1000,
    }


def get_performance(query_id, minutes, now=None):
    if minutes not in (15, 60, 1440):
        raise ValueError("Unsupported period")
    now = time.time() if now is None else now
    duration = minutes * 60
    connection = store.get_conn()
    try:
        if connection.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone() is None:
            return None
        samples = connection.execute(
            "SELECT * FROM performance_samples WHERE query_id=? AND observed_at>=? AND observed_at<=? "
            "ORDER BY observed_at, id", (query_id, now - 2 * duration, now)
        ).fetchall()
    finally:
        connection.close()
    previous = summarize_period(samples, now - 2 * duration, now - duration)
    current = summarize_period(samples, now - duration, now)
    variations = {}
    for metric in ("elapsed_ms_avg", "cpu_ms_avg", "buffer_gets_avg", "disk_reads_avg", "rows_avg"):
        baseline = previous[metric]
        value = current[metric]
        variations[metric] = 100 * (value - baseline) / baseline if baseline and value is not None else None
    return {"minutes": minutes, "current": current, "previous": previous, "variations": variations}


def get_plan_comparison(query_id, before_id=None, after_id=None):
    connection = store.get_conn()
    try:
        if connection.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone() is None:
            return None
        plans = [dict(row) for row in connection.execute(
            "SELECT id, captured_at, plan_hash_value FROM execution_plans WHERE query_id=? ORDER BY id DESC LIMIT 100",
            (query_id,),
        )]
        if len(plans) < 2 and before_id is None and after_id is None:
            return {"plans": plans, "before": None, "after": None, "diff": [], "truncated": False}
        before_id = before_id if before_id is not None else plans[1]["id"] if len(plans) > 1 else None
        after_id = after_id if after_id is not None else plans[0]["id"] if plans else None
        selected = []
        for plan_id in (before_id, after_id):
            row = connection.execute(
                "SELECT id, captured_at, plan_hash_value, SUBSTR(plan_text,1,100001) AS text "
                "FROM execution_plans WHERE query_id=? AND id=?", (query_id, plan_id)
            ).fetchone()
            if row is None:
                return None
            selected.append(dict(row))
    finally:
        connection.close()
    truncated = False
    for plan in selected:
        text = plan.pop("text")
        lines = text[:100000].splitlines()
        truncated = truncated or len(text) > 100000 or len(lines) > 1000
        plan["lines"] = lines[:1000]
    before, after = selected
    diff = list(difflib.unified_diff(before["lines"], after["lines"], fromfile=f"Plan {before['id']}",
                                     tofile=f"Plan {after['id']}", lineterm=""))
    return {"plans": plans, "before": before, "after": after, "diff": diff, "truncated": truncated}