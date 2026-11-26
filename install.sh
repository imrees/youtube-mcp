#!/bin/sh
# YouTubeMCP installer bootstrap: finds a supported Python (3.10-3.13) and runs install.py.
# Usage: ./install.sh [options]      (./install.sh --help for all options)
# Nothing here needs sudo. Nothing is downloaded by this script itself; install.py installs
# hash-locked packages from PyPI into a private environment.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

supported() {  # prints the version and succeeds if $1 is Python 3.10-3.13
  "$1" -c 'import sys; v=sys.version_info[:2]; print("%d.%d" % v); sys.exit(0 if (3,10) <= v <= (3,13) else 1)' 2>/dev/null
}

candidates="${YOUTUBE_MCP_PYTHON:-}"
for v in 3.12 3.13 3.11 3.10; do
  candidates="$candidates python$v /opt/homebrew/bin/python$v /usr/local/bin/python$v /Library/Frameworks/Python.framework/Versions/$v/bin/python$v"
done
candidates="$candidates python3"

for c in $candidates; do
  p=$(command -v "$c" 2>/dev/null || true)
  [ -n "$p" ] || continue
  if ver=$(supported "$p"); then
    echo "Using Python $ver: $p"
    exec "$p" "$here/install.py" "$@"
  fi
done

cat >&2 <<'MSG'
No supported Python was found (YouTubeMCP needs Python 3.10 to 3.13; 3.12 is recommended).
macOS's built-in /usr/bin/python3 is 3.9 and too old.

Install Python 3.12 from https://www.python.org/downloads/ (the macOS installer), or with
Homebrew: brew install python@3.12 . Then run ./install.sh again.
To use a specific Python: YOUTUBE_MCP_PYTHON=/path/to/python3.12 ./install.sh
MSG
exit 1
