#!/usr/bin/env bash
# aitrack paigaldus macOS / Linux jaoks.
# Loob lühikese 'aitrack' käsu ja käivitab seadistusnõustaja.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# leia python (3.9+)
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
if [[ -z "$PY" ]]; then
  echo "VIGA: Pythonit (3.9+) ei leitud. Paigalda Python ja proovi uuesti."
  echo "  macOS:  brew install python   |  Linux: kasuta paketihaldurit"
  exit 1
fi
echo "==> Python: $PY"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)' || {
  echo "VIGA: vajalik on Python 3.9 või uuem."; exit 1; }

# lühike 'aitrack' käsk PATH-i (~/.local/bin)
chmod +x "$DIR/aitrack.py" || true
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$DIR/aitrack.py" "$BIN/aitrack"
echo "==> Loodud käsk: $BIN/aitrack  →  $DIR/aitrack.py"
echo "    Abi: aitrack help"
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "    NB! $BIN ei ole PATH-is. Lisa see oma ~/.bashrc / ~/.config/fish'i:"
     echo "        export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# käivita seadistusnõustaja (küsib sink'i, projektid, taimeri, teeb testi)
echo
"$PY" "$DIR/aitrack.py" setup
