#!/usr/bin/env bash
#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/
#
# One-command Cloudera on AWS bootstrap script (Phase 2 of deployment plan):
#   1. Installs Python requirements (including impyla & thrift)
#   2. Initializes & seeds Apache Iceberg v2 tables in CDW Impala (or SQLite)
#
# Usage from Cloudera AI Workbench terminal:
#   bash scripts/bootstrap_cloudera_aws.sh [--strict] [--rows 10000]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

echo "=================================================================="
echo " Cloudera on AWS Bootstrap — Governed Banking NBA (Iceberg + CDW)"
echo " Project Root : ${PROJECT_ROOT}"
echo " Python       : $(command -v "${PYTHON_BIN}")"
echo " DB Backend   : ${NBA_DB_BACKEND:-impala}"
echo " Impala DB    : ${IMPALA_DATABASE:-banking_nba}"
echo "=================================================================="

"${PYTHON_BIN}" "${SCRIPT_DIR}/install_dependencies.py"
"${PYTHON_BIN}" "${SCRIPT_DIR}/seed_iceberg_tables.py" "$@"

echo "=================================================================="
echo " Bootstrap complete. Launch the Workbench in Cloudera AI with:"
echo "   Script: launch_app.py"
echo "=================================================================="
