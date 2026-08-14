# Ubuntu XFCE desktop + trycua computer-use, driven by the local Qwen3.6 vLLM.
#
# Base pinned by digest to the arm64 (GB10 / DGX Spark) manifest verified on this box:
# real XFCE desktop, KasmVNC, cua-computer-server, litellm, qwen-vl-utils, Playwright.
FROM trycua/cua-ubuntu@sha256:e2a800152d7d0a2a43b1f7f715f964ecb4a7501b262f3ca626f15a9e6b083e32

USER root

# ---------------------------------------------------------------------------------
# WHY THIS LAYER EXISTS AT ALL
#
# The base image ships /dockerstartup/custom_startup.sh containing:
#     sudo uv pip install --upgrade --system cua-computer-server "cua-agent[all]"
#     /usr/bin/python3 -m computer_server
#
# That runs on EVERY container start, which has three consequences we do not want:
#   1. Not reproducible. The digest pin above pins the filesystem but NOT the cua
#      software, which is refetched from PyPI at each boot.
#   2. It needs working internet at boot, and computer-server does not start until
#      the upgrade finishes -- measured ~7 minutes of "port 8000 is dead" on this box.
#   3. It leaves an INCONSISTENT install. The upgrade covers cua-computer-server and
#      cua-agent but not their dependency cua-computer. Observed live: cua-agent 0.8.4
#      + cua-core 0.3.1 alongside cua-computer 0.5.13, which still does
#      `from core.telemetry import ...` -- a module cua-core 0.3.1 renamed to cua_core.
#      Result: `import computer` raises ModuleNotFoundError and the Computer SDK is
#      unusable. cua-computer 0.5.19 is the version that matches cua-core 0.3.x.
#
# So: install one consistent set at BUILD time, and replace custom_startup.sh with a
# script that only starts things. Boots become fast, offline-safe and identical.
# ---------------------------------------------------------------------------------
RUN uv pip install --system --no-cache \
        "cua-core==0.3.1" \
        "cua-computer==0.5.19" \
        "cua-computer-server==0.3.42" \
        "cua-agent==0.8.4" \
        "playwright==1.62.0" \
 && python3 -c "\
import cua_agent, os, computer;\
p=os.path.join(os.path.dirname(cua_agent.__file__),'loops','qwen35.py');\
assert os.path.exists(p), 'qwen35 loop missing';\
print('OK: qwen35 loop present, Computer SDK imports cleanly')"

# Playwright browsers live OUTSIDE $HOME on purpose. /home/kasm-user is a persistent
# named volume at runtime; keeping browsers in the image instead of the volume means
# an image rebuild can update them, and a wiped volume does not cost a re-download.
# The base image's pre-downloaded Firefox belongs to Playwright 1.58 and does not
# match the 1.62 pinned above, so fetch the matching build explicitly.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install firefox \
 && chmod -R a+rX /opt/ms-playwright

COPY bridge.py /opt/gui-bridge/bridge.py
COPY custom_startup.sh /dockerstartup/custom_startup.sh
RUN chmod +x /dockerstartup/custom_startup.sh \
 && mkdir -p /trajectories && chown 1000:1000 /trajectories \
 # cua telemetry writes into a root-owned dir and logs a permission error on every
 # call as uid 1000. The env vars below disable it; this makes the path writable in
 # case anything still reaches for it.
 && mkdir -p /usr/local/lib/python3.12/dist-packages/cua_core/.storage \
 && chown -R 1000:1000 /usr/local/lib/python3.12/dist-packages/cua_core/.storage

ENV CUA_TELEMETRY_ENABLED=false \
    CUA_TELEMETRY=off \
    PYTHONUNBUFFERED=1 \
    TRAJECTORY_DIR=/trajectories \
    BRIDGE_PORT=8765

# LOAD-BEARING, and the cause of a real failure during bring-up.
# The base image sets HOME=/home/kasm-user globally, so every RUN step above executes
# as root WITH THAT HOME -- and pip/playwright/cua happily create ~/.config and
# ~/.cache owned by root:root. uid 1000 then cannot write its own config, xfconfd
# fails, and the desktop comes up as "Unable to load a failsafe session" instead of
# XFCE. It is doubly nasty because /home/kasm-user is a persistent named volume:
# docker seeds the volume from the image, so the broken ownership is baked in and
# survives container recreation.
# Fix the ownership after all root-run steps, before dropping privileges.
RUN chown -R 1000:1000 /home/kasm-user

USER 1000
