#!/usr/bin/env python3
"""
ODIN — Observation · Détection · Intelligence · Notification

Outil de surveillance et d'analyse de performances SQL boosté IA.
Capture les requêtes lentes depuis Oracle (V$SQL), analyse avec l'IA,
détecte les changements de plan d'exécution, chat IA contextuel par requête.

Usage:
  python oracleiq.py collect        → Lance le collecteur (poll V$SQL)
  python oracleiq.py analyze        → Lance l'analyseur IA en continu
  python oracleiq.py analyze --once → Analyse une batch puis quitte
  python oracleiq.py web            → Lance l'interface web
  python oracleiq.py all            → Tout en parallèle (collect + analyze + web)
"""
import sys
import os
import argparse
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent


def load_env():
    """Charge le .env dans os.environ si présent."""
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env", override=False, encoding="utf-8-sig", interpolate=False)


def cmd_collect():
    import sys
    sys.path.insert(0, str(BASE))
    from collector.oracle_collector import run_collector
    run_collector()


def cmd_analyze(once=False):
    sys.path.insert(0, str(BASE))
    from analyzer.ai_analyzer import run_analyzer
    run_analyzer(once=once)


def cmd_web(host=None, port=None):
    import uvicorn
    sys.path.insert(0, str(BASE))
    uvicorn.run("api.app:app", host=host or os.getenv("WEB_HOST", "127.0.0.1"),
                port=port or int(os.getenv("WEB_PORT", "8080")), reload=False)


def cmd_all():
    import multiprocessing
    from multiprocessing.connection import wait

    print("🔮 ODIN — Démarrage complet")
    print(f"  🔌 Collector  → poll V$SQL toutes les N secondes")
    print(f"  🧠 Analyzer   → analyse IA en continu")
    print(f"  🌐 Web UI     → http://{os.getenv('WEB_HOST', '127.0.0.1')}:{os.getenv('WEB_PORT', '8080')}")

    processes = [
        multiprocessing.Process(target=cmd_collect, name="collector"),
        multiprocessing.Process(target=cmd_analyze, name="analyzer"),
        multiprocessing.Process(target=cmd_web, name="webui"),
    ]
    started = []
    try:
        for process in processes:
            process.start()
            started.append(process)
        finished = wait([process.sentinel for process in started])
        failed = next(process for process in started if process.sentinel in finished)
        failed.join()
        print(f"[Erreur] Service {failed.name} terminé (code {failed.exitcode}). Arrêt des autres services.")
        raise SystemExit(failed.exitcode if failed.exitcode and failed.exitcode > 0 else 1)
    except KeyboardInterrupt:
        print("\n[Arrêt]")
    finally:
        for process in started:
            if process.is_alive():
                process.terminate()
        for process in started:
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()


def _port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Le port doit etre compris entre 1 et 65535.")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description="ODIN - surveillance Oracle et analyse Copilot")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("collect", help="Collecteur Oracle")
    analyze = commands.add_parser("analyze", help="Analyseur Copilot")
    analyze.add_argument("--once", action="store_true")
    web = commands.add_parser("web", help="Interface web")
    web.add_argument("port", nargs="?", type=_port)
    commands.add_parser("all", help="Collecteur, analyseur et web supervises")
    backup = commands.add_parser("backup", help="Sauvegarde SQLite coherente, sans ecrasement")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="Restaurer dans un NOUVEAU fichier SQLite")
    restore.add_argument("source", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    load_env()
    if args.command == "collect":
        cmd_collect()
    elif args.command == "analyze":
        cmd_analyze(once=args.once)
    elif args.command == "web":
        cmd_web(port=args.port)
    elif args.command == "all":
        cmd_all()
    elif args.command in ("backup", "restore"):
        from scripts.sqlite_backup import copy_database
        source = args.source if args.command == "restore" else Path(
            os.getenv("ODIN_DB_PATH", str(BASE / "oracleiq.db"))
        )
        try:
            copy_database(source, args.destination)
        except (OSError, ValueError, sqlite3.Error) as error:
            parser.exit(1, f"Erreur : {error}\n")
        print(f"Copie SQLite verifiee : {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
