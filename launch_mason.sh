#!/usr/bin/env bash
# ============================================================================
# launch_mason.sh — WSL/bash-native launcher for Mason WITH Headroom compression.
#
# WHY THIS EXISTS:
#   launch_mason.bat only works when Mason is started from Windows (it shells
#   out to `wsl.exe bash -lc "headroom proxy ..."` and sets a WINDOWS env var).
#   If you start Mason from inside a WSL/Ubuntu terminal instead — e.g. just
#   running `python3 mason_app.py` directly, which is also what mason_app.py's
#   own module docstring tells you to do — launch_mason.bat is never invoked,
#   nothing ever starts Headroom, and ANTHROPIC_BASE_URL is never set. Every
#   Anthropic API call then goes straight to api.anthropic.com, uncompressed,
#   full price. This script is the WSL-native equivalent of launch_mason.bat's
#   fail-safe logic, so launching Mason from WSL also gets compression.
#
# FLOW:
#   Mason (this process, in WSL) -> localhost:$HEADROOM_PORT -> Headroom -> Anthropic.
#
# FAILS SAFE:
#   If `headroom` isn't installed, or the proxy doesn't come up within the wait
#   window, this script does NOT abort — it unsets ANTHROPIC_BASE_URL, prints a
#   clear warning, and still launches Mason talking to Anthropic directly. Mason
#   never fails to start because of Headroom.
#
# ONE-TIME WSL SETUP (inside Ubuntu) — same as launch_mason.bat's header comment:
#   sudo apt update && sudo apt install -y pipx
#   pipx ensurepath
#   pipx install "headroom-ai[proxy]"
#   (restart the Ubuntu shell, then confirm:  headroom --version)
#
# USAGE:
#   chmod +x launch_mason.sh      # one-time, makes it directly executable
#   ./launch_mason.sh             # every time you want compression in WSL
#
#   (If you don't chmod it, `bash launch_mason.sh` works identically.)
# ============================================================================

set -u

# Always run from this script's own directory, regardless of caller's cwd
# (mirrors launch_mason.bat's `cd /d %~dp0`).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "${SCRIPT_DIR}" || { echo "ERROR: could not cd to ${SCRIPT_DIR}"; exit 1; }

# ── Headroom DISABLED 2026-07-28 — it breaks Anthropic prompt caching (the cache
#    matches on an exact token prefix; Headroom rewrites text in flight, so the two
#    fight and cost goes UP, ~$0.60 vs ~$0.19/query). Mirrors launch_mason.bat's
#    USE_HEADROOM=0. Set USE_HEADROOM=1 to re-enable. Default OFF. ────────────────
USE_HEADROOM="${USE_HEADROOM:-0}"
if [ "${USE_HEADROOM}" != "1" ]; then
    echo
    echo "  Headroom DISABLED — talking to Anthropic directly (prompt caching ON)."
    unset ANTHROPIC_BASE_URL
    echo
    echo "  Starting MASON..."
    echo "  mason_app.py opens http://127.0.0.1:5050 automatically."
    echo "  Press Ctrl+C to stop."
    echo
    exec python3 mason_app.py
fi

HEADROOM_PORT="${HEADROOM_PORT:-8787}"
HEADROOM_LOG="${SCRIPT_DIR}/headroom_proxy.log"
WAIT_ITERATIONS=12   # 12 * 2s = ~24s — matches launch_mason.bat's wait window
WAIT_INTERVAL=2

# Quick, dependency-free "is something listening on this port" check.
# Uses python3 (already a hard requirement to run mason_app.py at all), and
# mirrors the exact check launch_mason.bat uses so behavior stays consistent
# between the Windows and WSL launch paths.
port_open() {
    python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex(('127.0.0.1', ${HEADROOM_PORT})) == 0 else 1)
" 2>/dev/null
}

echo
echo "  Checking Headroom proxy on port ${HEADROOM_PORT}..."

if port_open; then
    echo "  Something is already listening on ${HEADROOM_PORT} — assuming it's Headroom, reusing it."
else
    if command -v headroom >/dev/null 2>&1; then
        echo "  Starting Headroom proxy in the background (log: ${HEADROOM_LOG})..."
        nohup headroom proxy --port "${HEADROOM_PORT}" > "${HEADROOM_LOG}" 2>&1 &
        disown 2>/dev/null || true
    else
        echo "  WARNING: 'headroom' command not found on PATH in this shell."
        echo "  Install it (one-time):"
        echo "    sudo apt update && sudo apt install -y pipx"
        echo "    pipx ensurepath   # then restart the shell"
        echo "    pipx install \"headroom-ai[proxy]\""
        echo "    headroom --version   # confirm it resolves before re-running this script"
    fi
fi

# Wait up to ~24s for the proxy to accept connections.
HEADROOM_UP=0
for _ in $(seq 1 "${WAIT_ITERATIONS}"); do
    if port_open; then
        HEADROOM_UP=1
        break
    fi
    sleep "${WAIT_INTERVAL}"
done

if [ "${HEADROOM_UP}" -eq 1 ]; then
    export ANTHROPIC_BASE_URL="http://localhost:${HEADROOM_PORT}"
    echo "  Headroom ready — compression ON. ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}"
else
    unset ANTHROPIC_BASE_URL
    echo "  WARNING: compression OFF — running direct. Headroom never came up on port ${HEADROOM_PORT}."
    echo "  Mason will still work fine, but every Anthropic call is billed at full (uncompressed) price."
fi

echo
echo "  Starting MASON..."
echo "  mason_app.py opens http://127.0.0.1:5050 automatically."
echo "  Press Ctrl+C to stop."
echo

# exec replaces this shell with the python process, so ANTHROPIC_BASE_URL
# (exported above, or intentionally left unset) is inherited correctly and
# Ctrl+C / signals go straight to Mason instead of a wrapper shell.
exec python3 mason_app.py
