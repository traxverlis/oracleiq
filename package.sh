#!/usr/bin/env bash
# Usage: bash package.sh [destination.tar.gz]
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${PYTHON:-python3}" "${SOURCE_DIR}/scripts/package_release.py" "${1:-${TMPDIR:-/tmp}/odin-v1.0.tar.gz}"
