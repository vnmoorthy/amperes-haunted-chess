#!/usr/bin/env bash
# Regenerate every reported result from scratch. Runs offline; no paid API; no robot commands.
# Usage: scripts/reproduce_all.sh            (uses ./amperes_data, wiping it first)
#        AMPERES_HOME=/tmp/x scripts/reproduce_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
export AMPERES_HOME=${AMPERES_HOME:-amperes_data}
case "$AMPERES_HOME" in amperes_data|*/amperes_data|/tmp/*|/private/tmp/*) ;; *) echo "refusing to wipe unexpected AMPERES_HOME=$AMPERES_HOME" >&2; exit 2;; esac
# Real robot captures cannot be regenerated: keep them across the wipe.
KEEP="$AMPERES_HOME/artifacts/viam_sessions"
if [ -d "$KEEP" ]; then mv "$KEEP" "$AMPERES_HOME.viam_sessions.keep"; fi
rm -rf "$AMPERES_HOME"
if [ -d "$AMPERES_HOME.viam_sessions.keep" ]; then
  mkdir -p "$AMPERES_HOME/artifacts" && mv "$AMPERES_HOME.viam_sessions.keep" "$KEEP"
  echo "kept real Viam captures in $KEEP (re-import one with: python -m amperes.cli import <dir> --adapter jsonl --rules-config scripts/viam_rules.json)"
fi

echo "== 1. tests"
$PY -m pytest -q tests/

echo "== 2. simulator experiment (baseline -> failures -> reproduction -> intervention -> held-out -> regression)"
$PY -m amperes.cli demo --n-dev 400 --n-heldout 1000

echo "== 3. detector evaluation on the labelled SYNTHETIC defect corpus"
$PY -m amperes.cli eval-detectors > /dev/null

echo "== 4. shadow-mode evaluation of the SYNTHETIC 3-workcell ops package"
$PY -m amperes.cli fixtures ops --out "$AMPERES_HOME/samples/ops_package" > /dev/null
$PY -m amperes.cli validate "$AMPERES_HOME/samples/ops_package" > /dev/null
$PY -m amperes.cli shadow "$AMPERES_HOME/samples/ops_package" --synthetic --name "SAMPLE (synthetic) 3-workcell program"

echo "== done. Reports under $AMPERES_HOME/artifacts/reports/"
