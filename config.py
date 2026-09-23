# OracleIQ — Configuration
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Oracle connexion
ORACLE_DSN      = os.getenv("ORACLE_DSN", "localhost:1521/ORCL")
ORACLE_USER     = os.getenv("ORACLE_USER", "")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "")

# Polling
POLL_INTERVAL_SEC   = int(os.getenv("POLL_INTERVAL", "5"))   # secondes entre chaque poll V$SQL
MIN_ELAPSED_MS      = int(os.getenv("MIN_ELAPSED_MS", "0"))  # ignorer requêtes < X ms
IGNORE_SYS_QUERIES  = os.getenv("IGNORE_SYS", "true").lower() == "true"

# IA
# Seul fournisseur pris en charge ; validation avant tout appel IA.
AI_PROVIDER  = os.getenv("AI_PROVIDER", "github-copilot")
AI_API_KEY   = os.getenv("AI_API_KEY", "")          # compatibilite des anciennes configurations
# Jeton GitHub (device flow) : si défini ici, prioritaire sur celui enregistré via l'administration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
AI_MODEL     = os.getenv("AI_MODEL", "claude-opus-5")
# Ancienne option, ignoree par le transport du SDK Copilot.
AI_BASE_URL  = os.getenv("AI_BASE_URL", "https://models.inference.ai.azure.com")

# Tokens max pour les analyses IA (markdown complet)
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "8000"))

# SQLite local
DB_PATH = Path(os.getenv("ODIN_DB_PATH", str(BASE_DIR / "oracleiq.db")))

# Filtres : schémas à ignorer (système Oracle)
IGNORED_SCHEMAS = {
    "SYS", "SYSTEM", "DBSNMP", "OUTLN", "MDSYS", "ORDSYS",
    "EXFSYS", "DMSYS", "WMSYS", "CTXSYS", "ANONYMOUS",
    "XDB", "ORDPLUGINS", "OLAPSYS", "LBACSYS", "XS$NULL",
    "APEX_PUBLIC_USER", "FLOWS_FILES", "APEX_040000",
}
