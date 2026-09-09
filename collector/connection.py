import os

import oracledb


def connect_oracle(*, user, password, dsn):
    connection = oracledb.connect(user=user, password=password, dsn=dsn, tcp_connect_timeout=10)
    connection.call_timeout = max(1000, min(int(os.getenv("ODIN_ORACLE_TIMEOUT_MS", "30000")), 300000))
    connection.module = "oracleiq"
    return connection


def query_execution_enabled():
    return os.getenv("ODIN_ALLOW_QUERY_EXECUTION", "false").lower() == "true"