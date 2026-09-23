# Mise a jour et exploitation

## Acces web

Definir `ODIN_ADMIN_PASSWORD` dans `.env` avant de se connecter. Aucun mot de passe
administrateur par defaut n'est accepte. `ODIN_VIEWER_PASSWORD` permet un acces
distinct en lecture seule. Ne pas reutiliser le meme mot de passe pour les deux roles.

Definir `ODIN_SESSION_SECRET` avec une valeur aleatoire d'au moins 32 octets pour
conserver les sessions au redemarrage. Sinon un secret ephemere est genere.
Les sessions durent huit heures. Pour invalider toutes les sessions, changer le secret.

`WEB_HOST` vaut `127.0.0.1` par defaut. Pour un acces reseau, configurer HTTPS via
un reverse proxy, `ODIN_COOKIE_SECURE=true` et les restrictions reseau appropriees.
`ODIN_PUBLIC_READ=true` rend les SQL, analyses et donnees capturees lisibles sans
connexion : ne l'activer que deliberement sur un reseau de confiance.
La limitation des appels est locale au processus ; garder un seul worker web.

## Oracle et donnees sensibles

Utiliser un compte Oracle dedie a privileges minimaux, jamais SYSTEM/SYS.
Le texte SQL, les valeurs de bind et les resultats des outils peuvent contenir des
donnees sensibles et etre transmis a GitHub Copilot. Restreindre les outils
et les privileges Oracle selon les regles de votre organisation.

Le rejeu et l'outil `run_select` exigent `ODIN_ALLOW_QUERY_EXECUTION=true`.
Une limite de lignes ne garantit pas une requete peu couteuse, ni l'absence
d'effets de bord de fonctions appelees depuis un SELECT. Les privileges Oracle
restent la barriere de securite principale. Le delai par appel Oracle est de 30 s,
configurable via `ODIN_ORACLE_TIMEOUT_MS` ; la connexion TCP est limitee a 10 s.
`gather_table_stats` reste une operation d'ecriture explicitement activable dans
les parametres. EXPLAIN PLAN ecrit temporairement dans PLAN_TABLE.
Les vues AWR exigent les droits et licences Oracle appropries.

Le rejeu refuse explicitement les requetes avec binds, faute de valeurs de rejeu
validees. Il refuse aussi les fiches dont le schema de parsing est inconnu ou
different du schema courant de la connexion : il ne rejoue jamais la requete
sur des objets homonymes par defaut. Ces refus ne sont pas des executions reussies.
L'outil IA EXPLAIN cible l'enfant et le schema de parsing exacts, restaure le
contexte de session et exige une `PLAN_TABLE` provisionnee dans le schema du
compte de connexion. Il produit un plan estime, pas une mesure d'execution.
La preparation Oracle de cette table doit etre effectuee par le DBA en recette,
avec les privileges minimaux ; ODIN ne la cree pas automatiquement.

Proteger `.env`, SQLite et les sauvegardes par les permissions du systeme et,
si necessaire, le chiffrement du disque. Les secrets Oracle en SQLite ne sont pas
chiffres par l'application. Aucun secret ne doit entrer dans Git ou les archives.

## Confidentialite et limites IA

Le reglage `ai_send_raw_values` vaut `false` par defaut. Avant chaque transmission
a Copilot, les litteraux SQL, commentaires SQL, valeurs de binds et valeurs des
lignes de donnees renvoyees par les outils sont masques. Les statistiques utiles
au diagnostic sont conservees. L'administrateur peut activer explicitement les
valeurs brutes dans les parametres ; ce choix s'applique aux prochaines
transmissions, sans effacer les donnees deja envoyees.

Ce masquage n'est pas une DLP exhaustive : les noms d'objets, metadonnees et textes
libres peuvent rester sensibles. Il reduit aussi la precision du diagnostic de
selectivite. Les captures SQLite locales et leurs sauvegardes conservent leurs
valeurs originales. Garder des droits Oracle minimaux et verifier la politique
de confidentialite de l'organisation avant d'activer l'analyse.

L'apercu IA, reserve aux administrateurs, est construit depuis les donnees locales,
sans connexion Oracle ni appel IA. Il montre le prompt utilisateur, le systeme
effectif et les schemas d'outils proposes, pas les resultats
futurs des outils. Un rafraichissement de plan au demarrage de l'analyse peut
modifier ce contexte ; l'apercu n'est pas une approbation figee d'une transmission.

Les budgets sont en caracteres, pas en tokens : 120 000 pour l'entree globale,
40 000 pour le SQL, 16 000 pour le plan fourni et chaque resultat d'outil,
20 000 pour le prompt systeme, 80 messages d'historique. Une reponse depassant
48 000 caracteres est refusee apres reception. Une analyse est limitee a trente
appels d'outils et trente-deux tours, avec une echeance globale de 180 secondes.
Les depassements sont des erreurs explicites, pas des scores de remplacement.
Ces limites ne constituent pas un plafond de facturation du fournisseur :
`AI_MAX_TOKENS` reste une longueur souhaitee et la limite de reponse est verifiee
apres reception.

## Migration et sauvegarde

La migration SQLite est additive : colonnes d'identite Oracle, hash de plan,
erreur d'analyse et index. WAL et les cles etrangeres sont actives.
Sauvegarder SQLite avec l'API `sqlite3.Connection.backup` ou la commande SQLite
`.backup`, pas en copiant seulement le fichier principal pendant une ecriture WAL.
Arreter les anciens processus avant de lancer la nouvelle version.

Les commandes suivantes utilisent l'API de sauvegarde SQLite, verifient
`integrity_check` et refusent d'ecraser un fichier existant :

```sh
python oracleiq.py backup sauvegarde-2026-09-23.db
python oracleiq.py restore sauvegarde-2026-09-23.db restauration-2026-09-23.db
```

La sauvegarde peut lire une base active en WAL. La restauration cree une copie
independante : elle ne remplace jamais la base en service et ne change pas
`ODIN_DB_PATH`. Avant de basculer, arreter tous les processus ODIN, conserver
l'ancienne base pour retour arriere, definir le nouveau `ODIN_DB_PATH`, puis
redemarrer et controler les donnees. Une verification d'integrite SQLite n'est
pas une validation metier. Les copies contiennent les memes donnees sensibles
que l'original ; utiliser un repertoire a acces restreint, en particulier des
ACL appropriees sous Windows. La planification et la conservation hors machine
restent a configurer dans l'environnement de deploiement.

L'identite est desormais `(source DSN, schema, SQL_ID, child_number)` ; le pattern
normalise sert uniquement au regroupement. Les anciennes fiches sont rattachees
lors de leur prochaine capture compatible. Les metriques deja fusionnees par
l'ancienne version ne peuvent pas etre reconstituees : conserver cet historique
avec prudence et attendre les nouvelles captures. Les orphelins preexistants ne
sont pas effaces automatiquement. Les nouvelles suppressions nettoient toutes
les tables liees dans une transaction.

Toute operation Oracle sur une fiche verifie le DSN d'origine contre celui de
la connexion reelle. Une source absente ou differente est refusee, y compris
apres un changement de cible dans l'administration. Deux alias Oracle ne sont
pas supposes equivalents. La consultation de l'historique local reste possible.
Les parametres Oracle sont enregistres et lus en un seul instantane transactionnel.

Le « pic de moyenne » correspond au maximum des moyennes observees, pas au temps
maximum d'une execution. Les moyennes viennent des compteurs cumulatifs V$SQL.
Un groupe de patterns utilise des moyennes ponderees par les executions et le
plus mauvais score de ses variantes ; ouvrir les variantes pour chaque diagnostic.

## Performances recentes et comparaison des plans

La fiche requete compare deux periodes consecutives de 15 minutes, une heure ou
24 heures. Les nouvelles captures conservent les compteurs bruts V$SQL (temps
ecoule, CPU, executions, lectures et lignes) au maximum une fois par minute dans
`performance_samples`. Les moyennes de periode sont calculees a partir des
differences de compteurs, ponderees par les nouvelles executions, jamais par une
moyenne de moyennes. Les anciens snapshots cumulatifs restent separes et ne sont
pas convertis en mesures recentes.

Redemarrer le collecteur et le serveur web pour activer cette fonction. Il faut
au moins deux nouvelles captures valides dans une periode pour la mesurer, et
deux periodes mesurees pour obtenir une variation. Les intervalles qui traversent
une borne de periode ne sont pas repartis artificiellement entre les periodes.
La couverture affichee correspond aux intervalles effectivement retenus.

Un changement de chargement/adresse du curseur, un changement de hash de plan,
une baisse de compteur, un trou de plus de trois minutes ou des compteurs qui
avancent sans nouvelle execution rendent l'intervalle inutilisable. Ces exclusions
sont comptees a l'ecran. Une periode sans nouvelles executions n'a pas de temps
moyen ; une reference nulle ou absente n'a pas de variation en pourcentage.

Le collecteur reste limite aux 200 requetes correspondant a ses filtres V$SQL.
Une requete devenue rapide peut sortir de cette selection : l'absence de mesures
ne signifie donc ni inactivite ni amelioration. Les compteurs Oracle evoluent
pendant les executions et ne constituent pas un chronometrage individuel des
executions terminees. Comparer des charges et valeurs de bind equivalentes avant
de conclure. L'interface ne declare pas automatiquement une correction validee.

Les echantillons de plus de sept jours d'une requete sont supprimes lors de sa
prochaine capture. Les requetes qui ne sont plus capturees conservent leurs
echantillons jusqu'a suppression de la fiche. Les suppressions de requetes
suppriment ces echantillons en cascade ; les plans et anciens snapshots suivent
les regles existantes. Cette retention ne constitue pas une sauvegarde.

La comparaison des plans est en lecture seule, sans acces Oracle ni appel IA.
Elle propose les 100 captures les plus recentes de la fiche. Le diff compare le
texte complet capture, y compris statistiques d'execution et binds eventuels ;
une difference textuelle n'implique pas necessairement un changement structurel.
Chaque texte est borne a 100 000 caracteres et 1 000 lignes, avec un avertissement
si tronque. Ces donnees restent sensibles et suivent les droits de lecture de la
fiche, y compris le mode lecture publique.

L'ouverture d'une fiche lit les binds deja stockes, sans connexion Oracle implicite.
Le rafraichissement explicite utilise `POST /api/queries/{id}/binds/refresh`, reserve
aux administrateurs. Une panne de rafraichissement ne supprime pas les captures
locales. Les lecteurs ne disposent pas de ce bouton.

Le collecteur rafraichit les plans/statistiques au plus toutes les cinq minutes
et les binds toutes les minutes, avec capture anticipee lors d'un changement de
plan ou de generation de curseur. Un diagnostic DBMS_XPLAN n'est pas un plan :
il est rejete et une nouvelle tentative est possible apres trente secondes.
Les captures de binds ciblent l'enfant Oracle exact. Ces intervalles ne rendent
pas disponibles les binds qu'Oracle n'a pas lui-meme captures.

## Supervision des services

L'administration affiche les derniers signaux du collecteur et de l'analyseur
automatique, le dernier cycle de collecte reussi, la derniere analyse enregistree,
les reservations d'analyse et les requetes en echec. Le rafraichissement toutes
les 15 secondes ne recharge pas les formulaires. L'API `/api/health` est reservee
aux administrateurs, y compris en lecture publique, et ne contacte ni Oracle ni l'IA.

Un reglage "Collecte autorisee" n'est pas une preuve de connexion. "Non observe"
signifie qu'aucun signal n'a ete enregistre ; "Sans signal recent" signifie que le
signal a expire, sans prouver a lui seul que le processus est mort. Le signal
expire normalement apres deux minutes, davantage pendant l'attente configuree
du collecteur ou une analyse IA (30 minutes). Un cycle reussi peut capturer zero
requete. L'etat de l'analyseur automatique ne mesure pas la disponibilite du
fournisseur IA ; les analyses manuelles sont representees par les reservations
et la derniere analyse enregistree.

Redemarrer les processus existants pour activer ce suivi. Un serveur web seul ne
lance pas le collecteur ni l'analyseur. Un arret force peut laisser un ancien
signal jusqu'a expiration. Garder une seule instance de chaque service par base.

`python oracleiq.py all` surveille les trois processus : si l'un se termine
inopinement, les autres sont arretes et la commande renvoie un code non nul.
Cela inclut les identifiants Oracle refuses et un port web deja occupe. Aucun
redemarrage automatique n'est effectue par ce superviseur. Pour modifier les
identifiants apres cet echec, lancer `python oracleiq.py web`, enregistrer et tester
la configuration, arreter ce serveur web, puis relancer `python oracleiq.py all`.
Les commandes `collect`, `analyze` et `web` restent utilisables separement.
Les anciens points d'entree `run.py` et `run_web_only.py` deleguent au meme
lanceur. Le chargement `.env` accepte UTF-8 avec BOM, guillemets et commentaires,
sans expansion `${...}` ni remplacement des variables d'environnement existantes.
Importer le module de lancement ne charge plus `.env`.

Les journaux du collecteur sont emis via le logger du processus, sans dependance
a un fichier `/tmp`. Configurer rotation et conservation dans le superviseur.

## Analyses

Le prompt natif suit une methode adaptative : examiner d'abord le contexte, puis
appeler uniquement les outils necessaires a une verification determinante.
Aucun appel ni inventaire general n'est impose lorsque les donnees suffisent.
Les reponses distinguent faits, hypotheses, limites et recommandations a valider ;
le score reste indicatif. Le chat applique les memes principes sans imposer
le format d'une analyse complete a chaque question.

Un prompt personnalise enregistre dans l'administration reste prioritaire et
n'est jamais remplace automatiquement. Pour revenir au nouveau prompt natif,
ouvrir Administration > Prompt et choisir "Reinitialiser (vide)".

Deux analyses web simultanees au maximum, 32 reservations partagees en SQLite.
« Analyser tout » remplit ce lot et indique le nombre restant ; relancer ensuite.
Les reservations expirent apres deux heures pour permettre la reprise apres crash.
Une tache interrompue par un redemarrage n'est pas automatiquement rejouee.
Le service doit rester mono-worker web pour la reconnexion SSE en memoire.

Une reponse IA vide ou invalide n'est jamais transformee en score. L'erreur est
enregistree separement et une relance manuelle est possible. Un nouveau hash de
plan rend la requete eligible a une nouvelle analyse automatique.

Chaque resultat est rattache a l'identifiant de capture du plan utilise.
Si une capture arrive pendant l'analyse, le resultat reste dans l'historique
mais ne valide pas l'etat courant. Cette regle est conservatrice, meme si le hash
structurel n'a pas change. Une purge des analyses efface aussi les erreurs
precedentes pour permettre une reprise.
Les erreurs sont egalement rattachees a la capture utilisee : l'echec tardif
d'une ancienne analyse ne bloque pas l'analyse automatique de la nouvelle version.

## Alertes locales

Le dashboard affiche les alertes et leur historique. Seul un administrateur
peut les acquitter. L'acquittement signifie "pris en compte", pas "corrige" :
la resolution suit les mesures, et une rechute ouvre un nouvel incident.
L'historique est conserve dans SQLite ; aucune notification externe n'est envoyee.

Le serveur web evalue les signaux de service toutes les trente secondes.
Un service jamais observe n'est pas declare en panne. Desactiver volontairement
la collecte ou le mode automatique de l'analyseur n'est pas une panne. Le serveur
web doit rester actif pour produire ces alertes : ce dispositif ne remplace pas
une supervision externe de la machine ou de l'ensemble ODIN.

Les regressions sont evaluees au plus une fois par minute, sur deux periodes
consecutives de quinze minutes. Chaque periode doit avoir au moins dix executions
et 50 % de couverture. Une alerte exige une hausse d'au moins 50 % ET 100 ms,
avec une reference strictement positive. Ce sont des suspicions a investiguer,
pas des preuves de regression. Une mesure absente ou insuffisante ne resout pas
un incident existant. Les limites de selection V$SQL restent applicables.

## Catalogue des modeles Copilot

Les boutons d'actualisation dans "Analyse IA" et "Modeles" recuperent le catalogue
du compte GitHub Copilot utilise par le serveur. Le jeton existant est reutilise
ou renouvele sans lancer de device flow interactif dans une requete web.
La connexion GitHub doit donc deja etre etablie sur ce serveur.

Seuls les modeles de chat visibles, non desactives par la politique du compte et
compatibles avec l'endpoint de chat utilise par ODIN sont proposes. Le catalogue
ne garantit pas la disponibilite instantanee ni un quota restant au moment d'une
analyse. La liste et la date du dernier succes sont conservees dans SQLite.
Un echec de rafraichissement ne les efface pas et ne modifie jamais `ai_model`.
Un modele selectionne mais absent du catalogue reste selectionne et signale.

Les routes `GET /api/models` (cache local uniquement) et `POST /api/models/refresh`
(appel Copilot explicite) sont reservees aux administrateurs. Copilot est le
seul fournisseur : les anciennes valeurs `openai`, `anthropic` et `ollama` sont
refusees avant transmission, sans basculement silencieux.

## Archive de distribution

```sh
python scripts/package_release.py odin-release.tar.gz
```

Le script utilise une liste de fichiers et de repertoires de code autorises,
pas une copie globale du repertoire de travail. Les variantes `.env.*` (sauf
`.env.example`), bases SQLite, sauvegardes, logs et liens symboliques sont exclus.
Une archive existante n'est jamais ecrasee. `package.sh` appelle le meme script.
Cela ne remplace pas la revue du code distribue : un secret insere dans un fichier
source autorise reste un secret. Ne jamais renseigner `.env.example` avec des
valeurs reelles.

## Installation et tests

```sh
pip install -r requirements.lock
python test_odin.py
npm ci
npm run vendor
npx playwright install chromium
npm test
python oracleiq.py web
```

Node est necessaire pour regenerer les ressources frontend et les tests navigateur,
pas pour servir les fichiers deja presents dans `static/vendor`.
Les tests Python utilisent SQLite temporaire ; les tests navigateur demarrent un
serveur isole avec 65 requetes fictives. Aucun test ne contacte Oracle ou l'IA.
Le pipeline GitHub Actions execute les deux suites sur Linux et Windows avec
Python 3.12 et Node 22. Playwright choisit le Python du venv selon la plateforme ;
`ODIN_TEST_PYTHON` permet de le remplacer et `ODIN_TEST_PORT` de choisir un autre
port que 8099. Le serveur navigateur isole active UTF-8.

Pour regenerer le verrouillage Python apres une mise a jour volontaire :

```sh
pip install pip-tools
pip-compile --no-emit-index-url --no-emit-trusted-host -o requirements.lock requirements.txt
```

Verifier les marqueurs de plateforme apres regeneration : `uvloop` ne doit pas
etre installe sur Windows, Cygwin ou PyPy. Le verrouillage partage conserve cette
condition explicitement.

Les tests Oracle reels (permissions, timeout du driver, plans multi-curseurs) et les
appels aux fournisseurs IA doivent etre effectues dans un environnement de recette.