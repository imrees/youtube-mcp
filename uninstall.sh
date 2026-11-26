#!/bin/sh
# Remove the installed YouTubeMCP app. Your credentials, settings and upload history are KEPT
# unless you add --delete-data (you will be asked to type DELETE).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
for c in "${YOUTUBE_MCP_PYTHON:-}" python3.12 python3.13 python3.11 python3.10 /opt/homebrew/bin/python3.12 python3; do
  p=$(command -v "$c" 2>/dev/null || true)
  if [ -n "$p" ] && "$p" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
    exec "$p" "$here/install.py" --uninstall "$@"
  fi
done
echo "No Python 3.10+ found. You can also run the uninstaller copied into the app folder:" >&2
echo '  "$HOME/Library/Application Support/youtube-mcp-app/bin/youtube-mcp-uninstall"' >&2
exit 1
