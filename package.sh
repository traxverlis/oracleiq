#!/usr/bin/env bash
# package.sh — Crée un tarball propre du projet ODIN
# Usage : bash package.sh
# Sortie : /tmp/odin-v1.0.tar.gz

set -euo pipefail

VERSION="1.0"
PROJECT_NAME="odin-v${VERSION}"
ARCHIVE="/tmp/${PROJECT_NAME}.tar.gz"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🔮 ODIN — Packaging v${VERSION}"
echo "   Source  : ${SOURCE_DIR}"
echo "   Archive : ${ARCHIVE}"
echo ""

# Supprimer l'archive précédente si elle existe
if [[ -f "${ARCHIVE}" ]]; then
    rm -f "${ARCHIVE}"
    echo "   (archive précédente supprimée)"
fi

# Créer le tarball depuis le répertoire parent pour avoir un dossier racine propre
cd "${SOURCE_DIR}/.."
SOURCE_BASENAME="$(basename "${SOURCE_DIR}")"

tar czf "${ARCHIVE}" \
    --exclude="${SOURCE_BASENAME}/.venv" \
    --exclude="${SOURCE_BASENAME}/.venv/*" \
    --exclude="${SOURCE_BASENAME}/*.db" \
    --exclude="${SOURCE_BASENAME}/*.db-shm" \
    --exclude="${SOURCE_BASENAME}/*.db-wal" \
    --exclude="${SOURCE_BASENAME}/*.db-journal" \
    --exclude="${SOURCE_BASENAME}/node_modules" \
    --exclude="${SOURCE_BASENAME}/test-results" \
    --exclude="${SOURCE_BASENAME}/playwright-report" \
    --exclude="${SOURCE_BASENAME}/.vscode" \
    --exclude="${SOURCE_BASENAME}/__pycache__" \
    --exclude="${SOURCE_BASENAME}/**/__pycache__" \
    --exclude="${SOURCE_BASENAME}/**/*.pyc" \
    --exclude="${SOURCE_BASENAME}/**/*.pyo" \
    --exclude="${SOURCE_BASENAME}/.git" \
    --exclude="${SOURCE_BASENAME}/.git/*" \
    --exclude="${SOURCE_BASENAME}/.env" \
    --exclude="/tmp/oracleiq.log" \
    --exclude="${SOURCE_BASENAME}/oracleiq_err.log" \
    "${SOURCE_BASENAME}/"

echo "✅ Archive créée : ${ARCHIVE}"
echo ""

# Afficher la liste des fichiers inclus
echo "📦 Fichiers inclus :"
tar tzf "${ARCHIVE}" | grep -v '/$' | sort | sed 's|^|   |'

echo ""

# Taille finale
SIZE=$(du -sh "${ARCHIVE}" | cut -f1)
FILE_COUNT=$(tar tzf "${ARCHIVE}" | grep -v '/$' | wc -l | tr -d ' ')
echo "📊 Résumé : ${FILE_COUNT} fichiers — taille archive : ${SIZE}"
echo ""
echo "Pour déployer :"
echo "  tar xzf ${ARCHIVE}"
echo "  cd ${SOURCE_BASENAME}"
echo "  python3 -m venv .venv && source .venv/bin/activate"
echo "  pip install -r requirements.lock"
echo "  python -m copilot download-runtime"
echo "  cp .env.example .env && nano .env"
echo "  python oracleiq.py all"
