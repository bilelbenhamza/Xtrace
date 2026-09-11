#!/bin/bash
# ------------------------------------------------------------------
# Lance le test reseau complet (network_probe / main.py) et logge
# tout ce qui se passe, pour pouvoir debugger si launchd ne se
# comporte pas comme prevu (plist mal formé, chemin faux, etc.).
# ------------------------------------------------------------------

# A ADAPTER : chemin absolu vers le dossier du projet (celui qui
# contient main.py). Trouve-le avec `pwd` depuis ce dossier.
PROJECT_DIR="/Users/bilelbenhamza/xtrace"

# A ADAPTER si besoin : chemin absolu vers python3.
# Trouve-le avec `which python3` dans ton terminal.
PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR" || exit 1

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
echo "=== Run demarre le $TIMESTAMP ===" >> "$LOG_DIR/run_probe.log"

"$PYTHON_BIN" main.py --mode full >> "$LOG_DIR/run_probe.log" 2>&1

echo "=== Run termine ===" >> "$LOG_DIR/run_probe.log"
echo "" >> "$LOG_DIR/run_probe.log"
