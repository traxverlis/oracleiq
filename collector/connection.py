import os
import re

import oracledb


def connect_oracle(*, user, password, dsn):
    connection = oracledb.connect(user=user, password=password, dsn=dsn, tcp_connect_timeout=10)
    try:
        connection.call_timeout = max(1000, min(int(os.getenv("ODIN_ORACLE_TIMEOUT_MS", "30000")), 300000))
        connection.module = "oracleiq"
    except BaseException:
        try:
            connection.close()
        except Exception:
            pass
        raise
    return connection


def get_oracle_settings() -> dict:
    """Read one atomic connection-settings snapshot, including environment defaults."""
    from config import ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD
    from db.store import get_settings

    return get_settings({
        "oracle_dsn": ORACLE_DSN,
        "oracle_user": ORACLE_USER,
        "oracle_password": ORACLE_PASSWORD,
    })


def oracle_source_id(connection) -> str:
    """Use the actual DSN; do not guess equivalence between Oracle aliases."""
    dsn = getattr(connection, "dsn", None)
    return dsn.strip() if isinstance(dsn, str) else ""


def assert_query_source(row, connection) -> None:
    """Reject missing or different sources before any query-specific Oracle call."""
    try:
        source = row["source_id"]
    except (KeyError, IndexError, TypeError):
        source = None
    source = source.strip() if isinstance(source, str) else ""
    actual = oracle_source_id(connection)
    if not source or not actual:
        raise ValueError("Identité source Oracle absente; opération refusée.")
    if source != actual:
        raise ValueError("Source de la requête différente de la connexion Oracle; opération refusée.")


def is_execution_plan_available(plan_text) -> bool:
    """A DBMS_XPLAN diagnostic is not an execution plan, even with a plan hash."""
    if not isinstance(plan_text, str) or not plan_text.strip():
        return False
    if re.search(
        r"^\s*(?:NOTE:\s*)?\[?(?:cannot fetch plan|could not fetch plan|"
        r"cannot find plan|no plan found|plan non disponible|plan not available|"
        r"ORA-\d+|ERROR:)",
        plan_text, re.IGNORECASE | re.MULTILINE,
    ):
        return False
    return bool(
        re.search(r"\|\s*Id\s*\|\s*Operation\s*\|", plan_text, re.IGNORECASE)
        and re.search(r"^\s*\|\s*\*?\s*\d+\s*\|\s*[A-Za-z][^|\n]*\|",
                      plan_text, re.MULTILINE)
    )


def query_execution_enabled():
    return os.getenv("ODIN_ALLOW_QUERY_EXECUTION", "false").lower() == "true"