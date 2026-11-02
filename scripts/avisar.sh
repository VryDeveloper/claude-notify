#!/bin/sh
# Acha um Python e roda o avisar.py. Se não houver Python, sai calado:
# o Claude segue com o fluxo normal do terminal.
DIR=$(dirname "$0")
if [ -n "$CLAUDE_AVISOS_PYTHON" ]; then
  exec "$CLAUDE_AVISOS_PYTHON" "$DIR/avisar.py" "$@"
fi
for p in python3 python py; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c "import tkinter" >/dev/null 2>&1; then
    exec "$p" "$DIR/avisar.py" "$@"
  fi
done
cat >/dev/null
exit 0
