# Mise à jour sécurité

Avant le lancement, définir `ODIN_ADMIN_PASSWORD` et un `ODIN_SESSION_SECRET`
aléatoire dans `.env`. Aucun ancien mot de passe par défaut n'est accepté.
Installer les dépendances avec `pip install -r requirements.lock`.
Consulter [MAINTENANCE.md](MAINTENANCE.md) pour la migration, les accès lecture seule
et les commandes de tests non destructives.

# ODIN — Guide d'installation express

**5 étapes, 5 minutes.**

---

## Étape 1 — Créer le compte Oracle

Connectez-vous à Oracle en tant que DBA et exécutez :

```sql
-- Créer l'utilisateur
CREATE USER odin_user IDENTIFIED BY "VotreMotDePasse123!";
GRANT CREATE SESSION TO odin_user;

-- Vues V$ essentielles
GRANT SELECT ON V_$SQL              TO odin_user;
GRANT SELECT ON V_$SQLAREA          TO odin_user;
GRANT SELECT ON V_$SQL_PLAN         TO odin_user;
GRANT SELECT ON V_$SQL_PLAN_STATISTICS_ALL TO odin_user;
GRANT SELECT ON V_$SQL_BIND_CAPTURE TO odin_user;
GRANT SELECT ON V_$SESSION          TO odin_user;
GRANT EXECUTE ON DBMS_XPLAN         TO odin_user;

-- Dictionnaire (stats tables/index)
GRANT SELECT ON DBA_SEGMENTS           TO odin_user;
GRANT SELECT ON DBA_INDEXES            TO odin_user;
GRANT SELECT ON DBA_IND_COLUMNS        TO odin_user;
GRANT SELECT ON DBA_TAB_COLUMNS        TO odin_user;
GRANT SELECT ON DBA_TAB_STATISTICS     TO odin_user;
GRANT SELECT ON DBA_TAB_COL_STATISTICS TO odin_user;
GRANT SELECT ON DBA_CONSTRAINTS        TO odin_user;
GRANT SELECT ON DBA_CONS_COLUMNS       TO odin_user;
GRANT SELECT ON DBA_DEPENDENCIES       TO odin_user;
GRANT SELECT ON ALL_INDEXES            TO odin_user;
GRANT SELECT ON ALL_IND_COLUMNS        TO odin_user;
GRANT SELECT ON ALL_CONSTRAINTS        TO odin_user;
GRANT SELECT ON ALL_CONS_COLUMNS       TO odin_user;
GRANT SELECT ON ALL_TAB_STATISTICS     TO odin_user;
GRANT SELECT ON ALL_TAB_COL_STATISTICS TO odin_user;
GRANT SELECT ON ALL_DEPENDENCIES       TO odin_user;

-- AWR (optionnel — licence Diagnostics Pack requise)
GRANT SELECT ON DBA_HIST_SQLSTAT  TO odin_user;
GRANT SELECT ON DBA_HIST_SNAPSHOT TO odin_user;
GRANT SELECT ON DBA_HIST_SQLTEXT  TO odin_user;
GRANT SELECT ON DBA_HIST_SQL_PLAN TO odin_user;
```

---

## Étape 2 — Installer Python et le projet

```bash
# Python 3.11+ requis
python3 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows

pip install -r requirements.lock
python -m copilot download-runtime
```

---

## Étape 3 — Configurer

```bash
cp .env.example .env
nano .env   # ou vim, gedit, notepad…
```

Renseigner au minimum :

```ini
ORACLE_DSN=monserveur:1521/MONSERVICE
ORACLE_USER=odin_user
ORACLE_PASSWORD=VotreMotDePasse123!

AI_PROVIDER=github-copilot
GITHUB_TOKEN=github_pat_REMPLACER_PAR_VOTRE_JETON
AI_MODEL=claude-sonnet-4.6
```

Copilot utilise le SDK officiel et son runtime épinglé. Le jeton fine-grained doit
appartenir au compte personnel et posséder la permission **Copilot Requests**.
Les PAT classic (`ghp_`) ne sont pas supportés. `GITHUB_TOKEN` est prioritaire sur
le jeton saisi dans Administration > Modèles ; après modification de `.env`,
redémarrer ODIN. Le bouton Tester vérifie le catalogue sans enregistrer le jeton.
Le SDK gère les limites du modèle : la longueur de réponse configurée dans ODIN
est une consigne, et non un plafond strict. Aucun compte connecté au CLI n'est
utilisé implicitement.

Copilot est le seul fournisseur pris en charge. Les anciennes configurations
OpenAI, Anthropic et Ollama sont refusees avant transmission : aucun basculement
silencieux vers Copilot. Les litteraux SQL et valeurs de binds sont masques par
defaut ; l'envoi brut exige une activation explicite dans l'administration.

---

## Étape 4 — Tester la connexion Oracle

```bash
python3 -c "
import oracledb, os
from dotenv import load_dotenv
load_dotenv(encoding="utf-8-sig", interpolate=False)
conn = oracledb.connect(
    user=os.getenv('ORACLE_USER'),
    password=os.getenv('ORACLE_PASSWORD'),
    dsn=os.getenv('ORACLE_DSN')
)
print('✓ Oracle', conn.version)
conn.close()
"
```

Si vous obtenez `✓ Oracle 19.x.x.x`, passez à l'étape suivante.

---

## Étape 5 — Lancer

```bash
python oracleiq.py all
```

Ouvrir : **http://localhost:8080**

Pour analyser les requêtes capturées :
```bash
python oracleiq.py analyze --once   # analyse une fois
python oracleiq.py analyze          # boucle continue
```

---

## Résolution de problèmes courants

| Erreur | Cause probable | Solution |
|--------|---------------|----------|
| `ORA-01017: invalid username/password` | Mauvais identifiants | Vérifier `ORACLE_USER` / `ORACLE_PASSWORD` |
| `ORA-12541: no listener` | DSN incorrect ou Oracle arrêté | Vérifier `ORACLE_DSN` |
| `ORA-00942: table or view does not exist` | GRANTs manquants | Relancer le script SQL de l'étape 1 |
| `ORA-01031: insufficient privileges` | GRANT manquant sur une V$ | Vérifier les GRANTs sur les vues V_ |
| `ModuleNotFoundError: oracledb` | venv non activé | `source .venv/bin/activate` |
| `Address already in use` | Port 8080 occupé | `python oracleiq.py web 9090` |

---

*Pour la documentation complète, voir `README.md`.*
