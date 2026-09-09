# Instructions du depot

## Workflow Git obligatoire

- Pour chaque nouvelle modification ou tache coherente, creer une branche dediee depuis `main` avant de modifier les fichiers. Ne jamais travailler directement sur `main`.
- Utiliser un nom descriptif, par exemple `fix/description`, `feat/description` ou `docs/description`. Continuer sur la meme branche pour les corrections et tests de la meme tache.
- Verifier l'etat Git avant de changer de branche. Preserver les modifications existantes et ne pas melanger des changements sans rapport dans une meme branche.
- Avant toute fusion vers `main`, executer les tests pertinents, les controles de compilation ou de lint disponibles et verifier le diff. Pour une modification d'interface, verifier aussi le comportement dans le navigateur sur ordinateur et mobile.
- Fusionner vers `main` uniquement lorsque tous les controles requis reussissent et que le comportement demande est valide. Ne pas fusionner si un test echoue, si une erreur reste non resolue ou si une verification necessaire est impossible ; signaler le blocage.
- Cette regle definit le workflow, sans autoriser a elle seule un commit, un push ou une fusion automatique. Effectuer ces operations lorsque l'utilisateur les demande.