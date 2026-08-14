#!/bin/bash
# Replaces the base image's custom_startup.sh. The original did an unpinned
# `uv pip install --upgrade` on every boot before starting anything (see Dockerfile
# for why that is a problem). Everything is pinned at build time now, so this script
# only starts services -- no network needed, and :8000 is live in seconds.
#
# vnc_startup.sh runs this in the background and tracks the PID, so it must not exit.

set -u

log() { echo "[gui-bridge] $*"; }

# --- 0. Repair home ownership ---------------------------------------------------
# Defensive counterpart to the `chown -R 1000:1000 /home/kasm-user` in the Dockerfile.
# That fixes freshly-seeded volumes, but a volume created by an EARLIER build still
# carries root-owned ~/.config, and the symptom is brutal to diagnose: XFCE refuses to
# start with "Unable to load a failsafe session" while everything else looks healthy.
# Cheap to check on every boot, so check.
if [ ! -w "$HOME/.config" ] || [ ! -O "$HOME/.config" ]; then
    log "repairing ownership of $HOME (was not owned/writable by uid $(id -u))"
    sudo chown -R "$(id -u):$(id -g)" "$HOME" 2>/dev/null \
        || log "WARNING: chown failed; XFCE may fail to start"
fi

# --- 1. Screen geometry ---------------------------------------------------------
# VNC_RESOLUTION is honoured by vnc_startup.sh as `vncserver -geometry`, but KasmVNC
# was measured coming up 1024x768 regardless. The mode does exist in the RANDR list
# and xrandr sets it correctly after startup, so nudge it here.
if [ -n "${VNC_RESOLUTION:-}" ]; then
    for _ in $(seq 1 30); do
        if xrandr --display "$DISPLAY" >/dev/null 2>&1; then
            output=$(xrandr --display "$DISPLAY" | awk '/ connected/{print $1; exit}')
            if [ -n "$output" ] && xrandr --display "$DISPLAY" --output "$output" \
                    --mode "$VNC_RESOLUTION" 2>/dev/null; then
                log "set $output to $VNC_RESOLUTION"
            else
                log "WARNING: could not set $VNC_RESOLUTION on '${output:-?}', staying at default"
            fi
            break
        fi
        sleep 1
    done
fi

# --- 2. computer-server: the 44 computer_* MCP tools + /cmd + /responses --------
# --host 0.0.0.0 matters: the default bind is 127.0.0.1, and docker publishes ports to
# the container's bridge IP, not its loopback. Without this the host-side 8001 mapping
# accepts connections and then dies, which looks like a broken service rather than a
# bind problem. Still only reachable from this machine -- compose binds 8001 to
# 127.0.0.1 on the host.
log "starting computer-server on :8000"
/usr/bin/python3 -m computer_server --host 0.0.0.0 --port 8000 >/tmp/computer-server.log 2>&1 &
CS_PID=$!

# --- 3. gui-bridge: the delegated agent loop + Playwright browser + replay ------
# Wait for computer-server before starting the bridge, so the bridge's first call
# cannot race the server's startup.
for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:8000/status" >/dev/null 2>&1 && break
    sleep 1
done
log "computer-server status: $(curl -sf http://127.0.0.1:8000/status 2>/dev/null || echo UNREACHABLE)"

log "starting gui-bridge on :${BRIDGE_PORT:-8765}"
/usr/bin/python3 /opt/gui-bridge/bridge.py >/tmp/gui-bridge.log 2>&1 &
BRIDGE_PID=$!

# --- 4. Keep the process alive; if either child dies, surface it in the logs ----
while true; do
    if ! kill -0 "$CS_PID" 2>/dev/null; then
        log "computer-server exited; last log lines:"; tail -20 /tmp/computer-server.log
        /usr/bin/python3 -m computer_server --host 0.0.0.0 --port 8000 >/tmp/computer-server.log 2>&1 &
        CS_PID=$!
        log "computer-server restarted as $CS_PID"
    fi
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        log "gui-bridge exited; last log lines:"; tail -20 /tmp/gui-bridge.log
        /usr/bin/python3 /opt/gui-bridge/bridge.py >/tmp/gui-bridge.log 2>&1 &
        BRIDGE_PID=$!
        log "gui-bridge restarted as $BRIDGE_PID"
    fi
    sleep 10
done
