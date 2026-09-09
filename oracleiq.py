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
import subprocess
import os
from pathlib import Path

BASE = Path(__file__).parent


def load_env():
    """Charge le .env dans os.environ si présent."""
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


load_env()


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


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd == "collect":
        cmd_collect()
    elif cmd == "analyze":
        cmd_analyze(once="--once" in args)
    elif cmd == "web":
        port = int(args[1]) if len(args) > 1 else int(os.getenv("WEB_PORT", "8080"))
        cmd_web(port=port)
    elif cmd == "all":
        cmd_all()
    else:
        print(f"Commande inconnue : {cmd}")
        print(__doc__)
        sys.exit(1)
