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
donnees sensibles et etre transmis au fournisseur IA choisi. Restreindre les outils
et les privileges Oracle selon les regles de votre organisation.

Le rejeu et l'outil `run_select` exigent `ODIN_ALLOW_QUERY_EXECUTION=true`.
Une limite de lignes ne garantit pas une requete peu couteuse, ni l'absence
d'effets de bord de fonctions appelees depuis un SELECT. Les privileges Oracle
restent la barriere de securite principale. Le delai par appel Oracle est de 30 s,
configurable via `ODIN_ORACLE_TIMEOUT_MS` ; la connexion TCP est limitee a 10 s.
`gather_table_stats` reste une operation d'ecriture explicitement activable dans
les parametres. EXPLAIN PLAN ecrit temporairement dans PLAN_TABLE.
Les vues AWR exigent les droits et licences Oracle appropries.

Proteger `.env`, SQLite et les sauvegardes par les permissions du systeme et,
si necessaire, le chiffrement du disque. Les secrets Oracle en SQLite ne sont pas
chiffres par l'application. Aucun secret ne doit entrer dans Git ou les archives.

## Migration et sauvegarde

La migration SQLite est additive : colonnes d'identite Oracle, hash de plan,
erreur d'analyse et index. WAL et les cles etrangeres sont actives.
Sauvegarder SQLite avec l'API `sqlite3.Connection.backup` ou la commande SQLite
`.backup`, pas en copiant seulement le fichier principal pendant une ecriture WAL.
Arreter les anciens processus avant de lancer la nouvelle version.

L'identite est desormais `(source DSN, schema, SQL_ID, child_number)` ; le pattern
normalise sert uniquement au regroupement. Les anciennes fiches sont rattachees
lors de leur prochaine capture compatible. Les metriques deja fusionnees par
l'ancienne version ne peuvent pas etre reconstituees : conserver cet historique
avec prudence et attendre les nouvelles captures. Les orphelins preexistants ne
sont pas effaces automatiquement. Les nouvelles suppressions nettoient toutes
les tables liees dans une transaction.

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

## Analyses

Deux analyses web simultanees au maximum, 32 reservations partagees en SQLite.
« Analyser tout » remplit ce lot et indique le nombre restant ; relancer ensuite.
Les reservations expirent apres deux heures pour permettre la reprise apres crash.
Une tache interrompue par un redemarrage n'est pas automatiquement rejouee.
Le service doit rester mono-worker web pour la reconnexion SSE en memoire.

Une reponse IA vide ou invalide n'est jamais transformee en score. L'erreur est
enregistree separement et une relance manuelle est possible. Un nouveau hash de
plan rend la requete eligible a une nouvelle analyse automatique.

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
(appel Copilot explicite) sont reservees aux administrateurs. Les autres
fournisseurs IA conservent la selection locale, sans ce rafraichissement Copilot.

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
Le pipeline GitHub Actions execute les deux suites sur Python 3.12 et Node 22.

Pour regenerer le verrouillage Python apres une mise a jour volontaire :

```sh
pip install pip-tools
pip-compile --no-emit-index-url --no-emit-trusted-host -o requirements.lock requirements.txt
```

Les tests Oracle reels (permissions, timeout du driver, plans multi-curseurs) et les
appels aux fournisseurs IA doivent etre effectues dans un environnement de recette.