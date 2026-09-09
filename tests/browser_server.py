import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
temporary = tempfile.TemporaryDirectory()
os.environ["ODIN_DB_PATH"] = str(Path(temporary.name) / "browser.db")
os.environ["ODIN_ADMIN_PASSWORD"] = "test-admin-only"
os.environ["ODIN_VIEWER_PASSWORD"] = "test-viewer-only"
os.environ["ODIN_PUBLIC_READ"] = "true"
os.environ["GITHUB_TOKEN"] = ""

from analyzer import copilot_client
copilot_client.TOKEN_CACHE_PATH = Path(temporary.name) / "copilot_token.json"

from api.app import app
from db.store import upsert_query, save_analysis, save_plan, chat_add_message, set_setting, get_conn

set_setting("collector_active", "false")
for index in range(65):
    query_id = upsert_query({
        "sql_id": f"sql{index:04}", "sql_text": "select " + "column_name, " * 20 + f"needle{index:04} from orders",
        "sql_hash": f"pattern{index // 2}", "schema_name": "APP" if index < 50 else "REPORTING",
        "source_id": "fixture", "elapsed_ms_avg": 1200 - index * 10, "executions": 100 + index,
        "buffer_gets_avg": 50000 + index, "module": "Facturation" if index % 2 else "Reporting",
    })
    if index == 0:
        payload = '<img src="/missing-test-image" onerror="window.injected=true">'
        save_plan(query_id, "Plan hash value: 100\nTABLE ACCESS FULL ORDERS")
        save_plan(query_id, "Plan hash value: 123\nINDEX RANGE SCAN ORDERS_ID\n" + payload)
        save_analysis(query_id, {"score": 0, "severity": "critical", "summary": payload,
                                "raw": "SCORE: 0\nSEVERITY: critical\nSUMMARY: Diagnostic\n\n## Diagnostic\n" + payload,
                                "trace": [{"tool": payload, "args": {"table": payload}, "ok": False, "error": payload}]})
        chat_add_message(query_id, "assistant", payload + "\n**Diagnostic**")
        connection = get_conn()
        try:
            with connection:
                started_at = int(time.time()) - 7200
                elapsed = 0
                for sample_index in range(121):
                    elapsed += 500000 if sample_index <= 105 else 1000000
                    connection.execute(
                        "INSERT INTO performance_samples (query_id, observed_at, cursor_generation, plan_hash_value, "
                        "executions, elapsed_us, cpu_us, buffer_gets, disk_reads, rows_processed) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (query_id, started_at + sample_index * 60, "fixture", 123, sample_index * 10,
                         elapsed, elapsed // 2, sample_index * 1000, sample_index * 10, sample_index * 20),
                    )
        finally:
            connection.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ODIN_TEST_PORT", "8099")), log_level="warning")