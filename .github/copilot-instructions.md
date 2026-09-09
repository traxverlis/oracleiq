# Instructions du depot

## Workflow Git obligatoire

- Pour chaque nouvelle modification ou tache coherente, creer une branche dediee depuis `main` avant de modifier les fichiers. Ne jamais travailler directement sur `main`.
- Utiliser un nom descriptif, par exemple `fix/description`, `feat/description` ou `docs/description`. Continuer sur la meme branche pour les corrections et tests de la meme tache.
- Verifier l'etat Git avant de changer de branche. Preserver les modifications existantes et ne pas melanger des changements sans rapport dans une meme branche.
- Toute integration vers `main` doit passer par une Pull Request GitHub depuis la branche dediee, y compris pour la documentation et les petites corrections. Ne pas fusionner localement une branche dans `main` et ne pas pousser directement sur `main`.
- Lorsqu'une publication est demandee, pousser la branche dediee et ouvrir une Pull Request ciblant `main`. Decrire les changements, les tests executes, leurs resultats et les limites eventuelles.
- Avant toute fusion vers `main`, executer les tests pertinents, les controles de compilation ou de lint disponibles et verifier le diff. Pour une modification d'interface, verifier aussi le comportement dans le navigateur sur ordinateur et mobile.
- Fusionner la Pull Request uniquement lorsque le comportement demande est valide, que les tests locaux et les controles CI requis sur la derniere revision reussissent, et que le diff a ete relu. Respecter les approbations requises et resoudre les retours bloquants. Ne pas fusionner si un test echoue, si une erreur reste non resolue ou si une verification necessaire est impossible ; signaler le blocage.
- Ne jamais contourner les protections de `main`, les controles requis ou les regles de revue. Si GitHub ou la CI est indisponible, garder la branche en attente plutot que fusionner directement.
- Apres fusion de la Pull Request, synchroniser le `main` local par avance rapide. Ne supprimer la branche de travail que si sa fusion est confirmee et sa suppression autorisee.
- Cette regle definit le workflow, sans autoriser a elle seule un commit, un push, l'ouverture d'une Pull Request ou une fusion automatique. Effectuer ces operations lorsque l'utilisateur les demande ; une demande de fusion signifie fusionner via la Pull Request, pas directement dans `main`.