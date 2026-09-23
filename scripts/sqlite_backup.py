"""Coherent SQLite backup and restore to a new, nonexisting destination."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def copy_database(source: Path, destination: Path) -> None:
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if source == destination.resolve():
        raise ValueError("La source et la destination doivent etre distinctes.")
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    completed = False
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin:
            with closing(sqlite3.connect(destination)) as target:
                origin.backup(target, pages=256, sleep=0.05)
                integrity = target.execute("PRAGMA integrity_check").fetchall()
                if integrity != [("ok",)]:
                    raise ValueError("La copie SQLite a echoue au controle d'integrite.")
                target.execute("PRAGMA journal_mode=DELETE")
        completed = True
    finally:
        if not completed:
            destination.unlink(missing_ok=True)
