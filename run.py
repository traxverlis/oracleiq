#!/usr/bin/env python3
"""
run.py — Lance collecteur + analyseur + web UI en parallèle
"""
import os
import sys
import time
import multiprocessing
from pathlib import Path

BASE = Path(__file__).parent

# Charger le .env
env_path = BASE / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

sys.path.insert(0, str(BASE))


def run_collector():
    from collector.oracle_collector import run_collector
    run_collector()


def run_analyzer():
    from analyzer.ai_analyzer import run_analyzer
    run_analyzer(once=False, batch_size=5)


def run_web():
    from oracleiq import cmd_web
    cmd_web()


if __name__ == "__main__":
    from rich.console import Console
    console = Console()

    console.rule("[bold blue]⚡ OracleIQ — Démarrage[/bold blue]")
    console.print(f"[dim]Oracle  : {os.environ.get('ORACLE_DSN','?')}[/dim]")
    console.print(f"[dim]IA      : {os.environ.get('AI_PROVIDER','?')} / {os.environ.get('AI_MODEL','?')}[/dim]")
    console.print(f"[dim]Web UI  : http://0.0.0.0:8080[/dim]\n")

    from db.store import init_db
    init_db()

    procs = [
        multiprocessing.Process(target=run_collector, name="collector", daemon=True),
        multiprocessing.Process(target=run_analyzer,  name="analyzer",  daemon=True),
        multiprocessing.Process(target=run_web,       name="webui",     daemon=True),
    ]

    for p in procs:
        p.start()
        console.print(f"[green]✓[/green] {p.name} démarré (pid {p.pid})")
        time.sleep(1)

    console.print("\n[bold green]✅ OracleIQ opérationnel[/bold green]")
    console.print("[dim]Ctrl+C pour arrêter[/dim]\n")

    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        console.print("\n[yellow]Arrêt...[/yellow]")
        for p in procs:
            p.terminate()
