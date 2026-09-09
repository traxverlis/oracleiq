# ODIN — Observation · Détection · Intelligence · Notification

> Outil de surveillance et d'analyse de performances SQL boosté IA

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Oracle 19c](https://img.shields.io/badge/oracle-19c-red.svg)](https://www.oracle.com/database/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-green.svg)](https://fastapi.tiangolo.com/)

---

## Description

**Mise à jour sécurité et fiabilité :** configurer `ODIN_ADMIN_PASSWORD` avant
connexion. Voir [MAINTENANCE.md](MAINTENANCE.md) pour les rôles, la migration,
les limites Oracle, les sauvegardes et les tests isolés.

**ODIN** capture en continu les requêtes SQL depuis la vue dynamique `V$SQL` d'Oracle Database, les stocke localement, puis les soumet à un modèle d'IA (Claude, GPT-4, Ollama…) pour analyse approfondie.

L'IA peut interroger Oracle en lecture seule pendant l'analyse (statistiques de tables, index, historique AWR, bind variables) pour enrichir son diagnostic — c'est ce qu'on appelle le **mode agentique**.

### Ce que fait ODIN

- 🔍 **Capture** les requêtes lentes ou coûteuses depuis `V$SQL` (polling configurable)
- 📋 **Récupère** les plans d'exécution via `DBMS_XPLAN`
- 🧠 **Analyse** chaque requête avec l'IA : score 0-100, sévérité, recommandations SQL
- 🔄 **Détecte** les changements de plan d'exécution (variation `PLAN_HASH_VALUE`)
- 💬 **Chat IA contextuel** par requête : posez des questions sur n'importe quelle requête
- 📌 **Bind variables** : visualisation des valeurs capturées depuis `V$SQL_BIND_CAPTURE`
- 📊 **Dashboard web** avec thème dark ambre, navigation historique, export PDF
- ⚙️ **Settings web** : connexion Oracle, choix du provider IA, mode d'analyse

---

## Architecture

```
oracleiq/
├── oracleiq.py          ← Point d'entrée CLI (collect / analyze / web / all)
├── config.py            ← Lecture des variables d'environnement
├── requirements.txt     ← Dépendances Python
├── .env.example         ← Template de configuration
├── .env                 ← Votre configuration (ne jamais committer)
│
├── collector/
│   └── oracle_collector.py   ← Polling V$SQL, récupération plans, upsert SQLite
│
├── analyzer/
│   ├── ai_analyzer.py        ← Boucle d'analyse IA agentique (multi-providers)
│   ├── copilot_client.py     ← Client GitHub Copilot (Azure Inference)
│   └── oracle_tools.py       ← Outils Oracle read-only disponibles à l'IA
│
├── api/
│   └── app.py               ← Application FastAPI (REST + templates Jinja2)
│
├── db/
│   └── store.py             ← Accès SQLite (requêtes, analyses, chat, settings)
│
├── templates/
│   ├── queries.html          ← Liste paginée, recherche globale et regroupement
│   ├── query_detail.html     ← Détail d'une requête + analyse IA + chat
│   ├── settings.html         ← Configuration Oracle & IA via l'interface
│   └── export_pdf.html       ← Export PDF d'une analyse
│
└── static/
    └── css/
        └── main.css          ← Thème dark ambre
```

---

## Prérequis

### Système

| Composant | Version minimale | Notes |
|-----------|-----------------|-------|
| Python | 3.11+ | 3.12 recommandé |
| Oracle Client | 19c | Instant Client ou Full Client |
| Oracle DB | 19c | Support de versions antérieures non garanti |
| Système | Linux / macOS / Windows | Linux recommandé pour production |

### Python

- `oracledb >= 2.0.0` — driver Oracle pur Python (pas besoin d'Instant Client pour thin mode)
- `fastapi >= 0.111.0` + `uvicorn` — interface web
- `openai >= 1.30.0` — client OpenAI/GitHub Copilot
- `anthropic >= 0.28.0` — client Anthropic Claude
- `rich >= 13.7.0` — affichage console
- `jinja2` — templates HTML

### Accès Oracle

Un compte Oracle avec les privilèges listés dans la section **Permissions Oracle requises** ci-dessous.

### Clé API IA

Au moins l'un des providers suivants :

| Provider | Où obtenir la clé |
|----------|------------------|
| GitHub Copilot | [github.com/settings/tokens](https://github.com/settings/tokens) (token avec accès Copilot) |
| OpenAI | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| Anthropic | [console.anthropic.com](https://console.anthropic.com/) |
| Ollama | Pas de clé — installation locale : [ollama.ai](https://ollama.ai) |

---

## Installation

### 1. Cloner / copier le projet

```bash
git clone <url-du-repo> odin
cd odin
```

Ou décompresser l'archive :

```bash
tar xzf odin-v1.0.tar.gz
cd oracleiq
```

### 2. Créer et activer le venv

```bash
python3 -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Windows (cmd)
.venv\Scripts\activate.bat
```

### 3. Installer les dépendances

```bash
pip install -r requirements.lock
```

### 4. Configurer

```bash
cp .env.example .env
```

Éditez `.env` avec vos paramètres Oracle et IA :

```bash
# Éditeur de votre choix
nano .env
vim .env
```

Les paramètres essentiels à renseigner :

```ini
ORACLE_DSN=monserveur:1521/ORCL
ORACLE_USER=odin_user
ORACLE_PASSWORD=mon_mot_de_passe_secret
AI_PROVIDER=github-copilot
AI_API_KEY=ghp_xxxxxxxxxxxxxxxxxxxx
AI_MODEL=claude-sonnet-4.6
ODIN_ADMIN_PASSWORD=choisir_un_mot_de_passe_long_et_unique
ODIN_SESSION_SECRET=generer_un_secret_aleatoire_long
```

### 5. Lancer

```bash
python oracleiq.py all
```

Ouvrez votre navigateur sur **http://localhost:8080**

---

## Commandes disponibles

```
python oracleiq.py collect           Collecteur seul (poll V$SQL en continu)
python oracleiq.py analyze           Analyseur IA seul (boucle continue)
python oracleiq.py analyze --once    Une passe d'analyse puis quitte
python oracleiq.py web               Interface web seule (port 8080)
python oracleiq.py web 9090          Interface web sur un port personnalisé
python oracleiq.py all               Tout en parallèle (collect + analyze + web)
```

Le mode `all` lance trois processus parallèles via `multiprocessing`. Arrêt propre avec `Ctrl+C`.

---

## Permissions Oracle requises

### Compte minimum recommandé

Créez un compte dédié avec les privilèges nécessaires et **uniquement** ceux-là :

```sql
-- Créer le compte ODIN
CREATE USER odin_user IDENTIFIED BY "VotreMotDePasse123!";

-- Droits de connexion
GRANT CREATE SESSION TO odin_user;

-- Vues dynamiques de performance (V$)
GRANT SELECT ON V_$SQL              TO odin_user;
GRANT SELECT ON V_$SQLAREA          TO odin_user;
GRANT SELECT ON V_$SQL_PLAN         TO odin_user;
GRANT SELECT ON V_$SQL_PLAN_STATISTICS_ALL TO odin_user;
GRANT SELECT ON V_$SQL_BIND_CAPTURE TO odin_user;
GRANT SELECT ON V_$SESSION          TO odin_user;

-- Accès à DBMS_XPLAN pour les plans d'exécution
GRANT EXECUTE ON DBMS_XPLAN         TO odin_user;

-- Dictionnaire de données (statistiques tables, index, colonnes)
GRANT SELECT ON DBA_SEGMENTS        TO odin_user;
GRANT SELECT ON DBA_INDEXES         TO odin_user;
GRANT SELECT ON DBA_IND_COLUMNS     TO odin_user;
GRANT SELECT ON DBA_TAB_COLUMNS     TO odin_user;
GRANT SELECT ON DBA_TAB_STATISTICS  TO odin_user;
GRANT SELECT ON DBA_TAB_COL_STATISTICS TO odin_user;
GRANT SELECT ON DBA_CONSTRAINTS     TO odin_user;
GRANT SELECT ON DBA_CONS_COLUMNS    TO odin_user;
GRANT SELECT ON DBA_DEPENDENCIES    TO odin_user;
GRANT SELECT ON ALL_INDEXES         TO odin_user;
GRANT SELECT ON ALL_IND_COLUMNS     TO odin_user;
GRANT SELECT ON ALL_CONSTRAINTS     TO odin_user;
GRANT SELECT ON ALL_CONS_COLUMNS    TO odin_user;
GRANT SELECT ON ALL_TAB_STATISTICS  TO odin_user;
GRANT SELECT ON ALL_TAB_COL_STATISTICS TO odin_user;
GRANT SELECT ON ALL_DEPENDENCIES    TO odin_user;

-- AWR (si disponible — licence Diagnostics Pack requise)
-- Commenter ces lignes si vous n'avez pas la licence Diagnostics Pack
GRANT SELECT ON DBA_HIST_SQLSTAT    TO odin_user;
GRANT SELECT ON DBA_HIST_SNAPSHOT   TO odin_user;
GRANT SELECT ON DBA_HIST_SQLTEXT    TO odin_user;
GRANT SELECT ON DBA_HIST_SQL_PLAN   TO odin_user;
```

### Note sur les vues V$

Les vues publiques `V$SQL` etc. sont en réalité des synonymes publics vers `V_$SQL`. Le GRANT doit se faire sur l'objet réel (`V_$SQL`), pas sur le synonyme.

### Sans accès DBA (accès restreint)

Si vous ne pouvez pas obtenir les GRANTs DBA, ODIN fonctionne en mode dégradé :
- Collecte `V$SQL` : ✅ (si GRANT SELECT ON V_$SQL accordé)
- Plans d'exécution : ✅ (si EXECUTE ON DBMS_XPLAN accordé)
- Statistiques tables/index : ⚠️ partielles (selon vues ALL_* accessibles)
- AWR : ❌ non disponible sans Diagnostics Pack

---

## Providers IA supportés

| Provider | `AI_PROVIDER` | Modèles recommandés | `AI_BASE_URL` |
|----------|--------------|---------------------|--------------|
| GitHub Copilot | `github-copilot` | `claude-sonnet-4.6`, `gpt-4o` | `https://models.inference.ai.azure.com` |
| OpenAI | `openai` | `gpt-4o`, `gpt-4o-mini` | *(laisser vide)* |
| Anthropic | `anthropic` | `claude-3-5-sonnet-20241022`, `claude-opus-4-5` | *(non utilisé)* |
| Ollama (local) | `ollama` | `llama3`, `mistral`, `qwen2.5-coder` | `http://localhost:11434/v1` |

### GitHub Copilot (recommandé)

Le provider par défaut. Nécessite un token GitHub avec accès Copilot. Donne accès à Claude Sonnet et GPT-4o sans frais supplémentaires si vous avez un abonnement GitHub Copilot.

```ini
AI_PROVIDER=github-copilot
AI_API_KEY=ghp_votre_token_github
AI_MODEL=claude-sonnet-4.6
AI_BASE_URL=https://models.inference.ai.azure.com
```

### Ollama (sans abonnement, 100% local)

Pour une utilisation entièrement locale et gratuite. Performances moindres mais aucun coût et aucune donnée envoyée à l'extérieur.

```bash
# Installer Ollama
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3
```

```ini
AI_PROVIDER=ollama
AI_API_KEY=ollama
AI_MODEL=llama3
AI_BASE_URL=http://localhost:11434/v1
```

---

## Variables d'environnement

Toutes les variables peuvent être définies dans le fichier `.env` à la racine du projet.

### Connexion Oracle

| Variable | Défaut | Description |
|----------|--------|-------------|
| `ORACLE_DSN` | `localhost:1521/ORCL` | DSN Oracle au format `host:port/service_name` |
| `ORACLE_USER` | `system` | Nom d'utilisateur Oracle |
| `ORACLE_PASSWORD` | *(vide)* | Mot de passe Oracle |

### Collecteur

| Variable | Défaut | Description |
|----------|--------|-------------|
| `POLL_INTERVAL` | `5` | Secondes entre chaque poll de `V$SQL` |
| `MIN_ELAPSED_MS` | `0` | Ignorer les requêtes plus rapides que X ms (0 = tout capturer) |
| `IGNORE_SYS` | `true` | Ignorer les schémas système Oracle (SYS, SYSTEM, DBSNMP…) |

### Intelligence Artificielle

| Variable | Défaut | Description |
|----------|--------|-------------|
| `AI_PROVIDER` | `github-copilot` | Provider : `openai`, `anthropic`, `ollama`, `github-copilot` |
| `AI_API_KEY` | *(vide)* | Clé API du provider choisi |
| `AI_MODEL` | `claude-sonnet-4.6` | Modèle à utiliser (dépend du provider) |
| `AI_BASE_URL` | `https://models.inference.ai.azure.com` | URL de base de l'API (vide pour OpenAI direct) |
| `AI_MAX_TOKENS` | `8000` | Nombre maximum de tokens pour une analyse |
| `AI_THINKING_BUDGET` | `1024` | Budget de raisonnement (0=off, 1024=rapide, 5000=standard) |

### Interface Web

| Variable | Défaut | Description |
|----------|--------|-------------|
| `WEB_HOST` | `0.0.0.0` | Adresse d'écoute du serveur web |
| `WEB_PORT` | `8080` | Port du serveur web |

---

## Fonctionnalités principales

### Surveillance continue
- Polling configurable de `V$SQL` (toutes les 5 secondes par défaut)
- Capture des requêtes nouvelles ou dont les statistiques ont évolué
- Récupération du texte SQL complet (via `V$SQL.sql_fulltext`, pas limité à 1000 chars)
- Plans d'exécution via `DBMS_XPLAN.DISPLAY_CURSOR`
- Détection des requêtes avec valeurs littérales hardcodées (candidats aux bind variables)

### Analyse IA agentique
- Score de qualité 0-100 par requête
- Sévérité : `ok` (≥80) / `warning` (50-79) / `critical` (<50)
- Résumé + problèmes détectés + recommandations SQL concrètes
- Jusqu'à 3 tours d'outils Oracle pendant l'analyse (mode agentique)
- Types de problèmes identifiés : `FULL_TABLE_SCAN`, `MISSING_INDEX`, `BAD_JOIN_ORDER`, `CARTESIAN_PRODUCT`, `STALE_STATS`, `NON_SARGABLE`, `EXCESSIVE_BUFFER_GETS`, `HIGH_DISK_READS`, `MISSING_BIND_VARS`

### Outils Oracle disponibles à l'IA
L'IA peut, pendant son analyse, interroger Oracle pour obtenir :
- Statistiques de tables (`table_stats`)
- Liste des index et leurs colonnes (`index_list`)
- Statistiques des colonnes (`column_stats`)
- Contraintes PK/FK/UNIQUE (`table_constraints`)
- Historique temps réel depuis `V$SQL` (`sql_plan_history`)
- Données AWR par snapshot (`awr_sql_stats`)
- Top requêtes AWR (`awr_top_sql`)
- Vues référençant une table (`related_views`)
- Bind variables capturées (`bind_captures`)

### Interface web (FastAPI + Jinja2)
- **Dashboard** : liste des requêtes triées par score / sévérité / temps
- **Détail requête** : SQL coloré, plan d'exécution, analyse IA en Markdown, historique
- **Chat IA** : conversation contextuelle par requête (l'IA connaît le SQL et son analyse)
- **Settings** : configuration Oracle et IA sans redémarrer l'application
- **Export PDF** : rapport complet d'une analyse
- Thème dark avec accents ambre

### Gestion des plans d'exécution
- Détection automatique des changements de `PLAN_HASH_VALUE`
- Stockage de plusieurs snapshots de plans par requête
- Visualisation de l'historique des plans dans l'interface

### Bind variables
- Affichage des valeurs capturées depuis `V$SQL_BIND_CAPTURE`
- Alerte si des valeurs littérales sont détectées dans le SQL

---

## Mode d'analyse

Par défaut, l'analyseur IA est en mode **manuel** : il n'analyse pas automatiquement les nouvelles requêtes capturées. Vous déclenchez l'analyse depuis l'interface web ou en lançant `analyze --once`.

Pour passer en mode **automatique** (analyse continue de toutes les nouvelles requêtes) :
1. Ouvrez les **Settings** dans l'interface web
2. Changez le mode d'analyse sur "Automatique"

Ou via la commande :
```bash
python oracleiq.py analyze
```

---

## Structure de la base de données locale

ODIN stocke tout dans un fichier SQLite (`oracleiq.db`) :

| Table | Contenu |
|-------|---------|
| `queries` | Requêtes SQL capturées avec leurs statistiques d'exécution |
| `execution_plans` | Plans d'exécution (texte brut DBMS_XPLAN) |
| `analyses` | Résultats des analyses IA (JSON) |
| `chat_messages` | Historique des conversations IA par requête |
| `settings` | Paramètres modifiables via l'interface web |
| `analyzing_queue` | File d'attente des analyses en cours |

---

## Logs et débogage

```bash
# Logs d'erreur du collecteur
cat /tmp/oracleiq_err.log

# Tester la connexion Oracle manuellement
python3 -c "
import oracledb
conn = oracledb.connect(user='odin_user', password='xxx', dsn='host:1521/ORCL')
print('OK:', conn.version)
"
```

---

## Limitations connues

- **Oracle seulement** : support PostgreSQL prévu dans une version future
- **AWR** : les vues `DBA_HIST_*` nécessitent la licence Oracle Diagnostics Pack
- **Bind variables** : Oracle capture les bind variables périodiquement (env. toutes les 15 min), pas à chaque exécution
- **V$SQL** : les requêtes peuvent être évincées du shared pool — ODIN ne capture que ce qui est en cache au moment du poll
- **Parallélisme** : le mode `all` utilise `multiprocessing`, chaque processus a sa propre connexion Oracle

---

## Roadmap

- [ ] Support PostgreSQL (`pg_stat_statements`)
- [ ] Alertes email/webhook sur sévérité `critical`
- [ ] Export des recommandations en scripts SQL
- [ ] Multi-instance Oracle
- [ ] Authentification de l'interface web

---

## Licence

Usage interne. Contactez l'auteur pour toute redistribution.
