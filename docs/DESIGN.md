# GUI agent on DGX Spark: opencode (host) -> local Qwen3.6 -> Ubuntu desktop container

> **STATUS: built and verified end-to-end on 2026-08-14.** See `desktop/README.md` for
> how to use it. This document is the design record and the list of things that were
> tested rather than assumed. Where reality differed from the plan, the "as built"
> notes at the bottom say so.

Goal: from opencode on the host, prompt in natural language ("open the browser at
example.com, log in, scroll down"). The brain is the local vLLM Qwen3.6-27B already
running on port 8000. The hands are a visible Ubuntu XFCE desktop in a container,
driven by trycua/cua. Sessions leave behind a replayable script.

Decisions taken in the interview: delegated control loop, hybrid vision+browser-API,
record replayable scripts, verify-before-build, persistent home volume, 1280x800,
pre-seeded logins (model never sees a password).

---

## 1. What was actually verified on this box (not assumed)

All of the following was tested against the live system, not read off a doc page.

| Check | Result |
|---|---|
| `trycua/cua-ubuntu:latest` on arm64 | **Works.** arm64/linux manifest exists, 4.9 GB, pulled and booted. GitHub issue #353 (no arm64) is resolved. |
| Desktop actually renders | **Yes.** Booted, screenshotted, real XFCE desktop with taskbar and icons. |
| Qwen3.6-27B is multimodal | **Yes.** `Qwen3_5ForConditionalGeneration`, `language_model_only = False`, vision tower live. |
| vLLM accepts images today | **Yes.** Sent a synthetic 200x100 button image to the running server; it returned `{"x":100,"y":50}`. y exact, x within 10 px. |
| cua loop for this exact architecture | **Exists** as `loops/qwen35.py`, using normalised 0-1000 coordinates (so pixel-perfect grounding is not required). |
| computer-server in the image | **Yes**, `cua-computer-server 0.3.17`, starts and binds :8000. |
| MCP over HTTP from the container | **Yes**, at `/mcp/`, **44 tools** incl. `computer_get_accessibility_tree` and `computer_find_element`. |
| Playwright present | **Yes**, 1.58.0 with a pre-downloaded **arm64 Firefox**; `/playwright_exec` endpoint live. |
| `qwen-vl-utils` (hard dep of qwen35 loop) | **Yes**, 0.0.14 pre-installed. |

### Gotchas found — each one would have broken the naive build

1. **The qwen35 loop would not have been selected.** It registers as
   `@register_agent(models=r"(?i).*qwen35.*")`. Our served name is `qwen3.6-27b`,
   which does not match, so cua would have silently fallen back to `generic_vlm`
   (catch-all, priority -100) and performed worse for no visible reason.
   *Fix:* add a second served-model alias `qwen35-27b` in vLLM. The existing name is
   kept, so opencode needs no change.
2. **The image's cua-agent is too old.** Shipped 0.7.25 has **no** `qwen35.py`
   (module is named `agent`). PyPI 0.8.4 has it (module renamed `cua_agent`).
   *Fix:* thin image layer that upgrades cua-agent to 0.8.4.
3. **Port 8000 collides** — computer-server wants 8000, which vLLM already owns on
   the host. *Fix:* publish as `127.0.0.1:8001->8000`.
4. **`VNC_RESOLUTION=1280x800` is silently ignored.** The env var *is* set and *is*
   passed as `-geometry`, but KasmVNC still came up 1024x768. `xrandr` sets 1280x800
   fine after start. *Fix:* post-start `xrandr` nudge in the entrypoint.
5. **The image ships Firefox 147 only**, and Ubuntu's `chromium` apt candidate is a snap
   stub that cannot work in a container. *Consequence:* "open chrome" means Firefox here.
   That is a convenience choice, not a limitation of the platform — `playwright install
   chromium` adds an arm64 Chromium (~170 MB) if a Chromium-family browser is wanted.
6. **cua telemetry writes to a root-owned dir** and logs a permission error as
   user 1000. *Fix:* `CUA_TELEMETRY_ENABLED=false`.
7. **Built-in browser tool is coarse.** `/playwright_exec` only supports 9 verbs
   (`visit_url`, `click`, `type`, `scroll`, `go_back`, `go_forward`, `screenshot`,
   `get_current_url`, `web_search`) — no CSS selectors. Its profile is a persistent
   context at `~/.playwright-firefox` with `headless=False` and a hardcoded
   1024x768 viewport.
   *Consequence:* selector-precise login filling needs our own thin Playwright layer
   (see 3.3). Note two processes cannot share one Firefox profile dir concurrently.

---

## 2. Architecture

```
HOST                                    CONTAINER  "desktop"  (arm64, XFCE)
                                        .-------------------------------------.
opencode --remote MCP--> :8765/mcp/ --> | gui-bridge (FastMCP)                |
         \                              |   run_gui_task(...)  <- delegated   |
          \                             |   browser_*          <- selectors   |
           \                            |   emit_replay_script                |
            \                           |        |                            |
             '--remote MCP--> :8001/mcp/|-> computer-server (44 tools)        |
                                        |        |                            |
vLLM :8000  <---- OpenAI HTTP ----------|--- ComputerAgent (qwen35 loop)      |
(the brain)      host.docker.internal   |        |                            |
                                        |     Xorg :1 + XFCE + Firefox        |
                                        '-------------------------------------'
you watch: http://127.0.0.1:6901  (KasmVNC in a browser)
```

Why the brain stays on the host: the model is already loaded and warm in vLLM. The
container only needs pixels and hands. No GPU is requested by the desktop service.

Why delegated: the screenshot->decide->click loop runs *inside* the container against
vLLM directly. opencode sends one instruction and gets a text summary back, so the
image traffic never crosses the opencode context and we do not depend on opencode
forwarding MCP image results back into the model.

---

## 3. Work plan

### Phase 2 — vLLM served-name alias  (requires your go-ahead: brief restart)
One line changes in `docker-compose.yml`:
```yaml
- --served-model-name=qwen3.6-27b qwen35-27b
```
Both names then resolve to the same weights. `qwen3.6-27b` keeps working for opencode;
`qwen35-27b` is what makes cua pick the correct qwen35 loop.
Cost: one vLLM restart. Weights and the torch.compile cache are already on disk, so
this is a short restart rather than a cold 15-minute boot — but opencode is
unavailable while it happens, which is why this needs your explicit OK.

### Phase 3 — desktop service
Thin `Dockerfile.desktop` on top of the pinned upstream image:
- `pip install cua-agent==0.8.4` (brings `cua_agent` with the qwen35 loop)
- startup wrapper: `xrandr --output VNC-0 --mode 1280x800`, launch computer-server on
  :8000, launch gui-bridge on :8765
- `CUA_TELEMETRY_ENABLED=false`

Compose service, pinned by digest like the vllm service:
- ports `127.0.0.1:6901` (view), `127.0.0.1:8001->8000`, `127.0.0.1:8765` — all
  loopback-bound, nothing on the LAN
- `extra_hosts: host.docker.internal:host-gateway` so the container reaches vLLM
- `shm_size: 1g` (Firefox), `VNC_PW` set from `.env`, not the shipped default
- volumes: `desktop-home:/home/kasm-user` (persistent profile + logins),
  `./trajectories:/trajectories`
- healthcheck on `/status`

### Phase 4 — gui-bridge + first real task
FastMCP service (fastmcp 2.14.5 is already in the image), exposing:
- `run_gui_task(task, max_steps)` — the delegated tool. Runs `ComputerAgent(
  model="openai/qwen35-27b", api_base=host vLLM)`. Returns a step summary.
  *First try the built-in `/responses` endpoint* (`features: ["agent","mcp"]`), which
  may already run this loop server-side and let us delete most of this code.
- `browser_goto/fill/click/read` — our own headed Playwright on a **separate** profile
  dir (selector-precise; needed because the built-in verbs have no selectors)
- `screenshot()` — for eyeballing from opencode
- `only_n_most_recent_images=3` to bound context growth

Then the acceptance test: one real end-to-end task through the whole chain.

### Phase 5 — replay recorder
Every action appends to `/trajectories/<id>.jsonl` (action + screenshot ref).
`emit_replay_script(id)` renders a runnable script — Playwright for web steps,
computer-server calls for desktop steps — that replays with **no model in the loop**.
That is what turns this from one-shot GUI driving into GUI scripting you can re-run
cheaply.

### opencode wiring (host, `~/.config/opencode/opencode.json`)
```json
"desktop": { "type": "remote", "url": "http://127.0.0.1:8765/mcp/", "enabled": true },
"desktop-raw": { "type": "remote", "url": "http://127.0.0.1:8001/mcp/", "enabled": true }
```
`desktop-raw` is optional and free — it is computer-server's own 44 primitives, no
code required. Useful for debugging and manual nudges. Note it adds 44 tool
definitions to opencode's context, so it may be worth leaving disabled by default.

---

## 4. Honest risks

- **Grounding accuracy.** A 27B general VLM is not a GUI-specialised model. Measured
  error was ~10 px on a trivial synthetic target; dense real UI will be harder. Three
  mitigations are already in the design: normalised coords (the loop's own scheme),
  `find_element` / accessibility tree instead of pixels where available, and
  selector-based Playwright for web. If it is still weak, the fallback is a composed
  loop with a small dedicated grounder (cua supports `planner+grounder`), which costs
  extra unified memory.
- **Firefox sandbox** logged `CanCreateUserNamespace() EPERM` in the container. It ran
  anyway, but if pages crash we may need `security_opt: seccomp=unconfined`.
- **Unified memory is shared.** 121 GiB total, ~83 GiB in use with vLLM at
  `--gpu-memory-utilization=0.5`. The desktop adds roughly 1-2 GiB. Comfortable, but
  it is the same pool the GPU budget comes from.
- **Image token cost per step.** 1280x800 is ~1.2-1.6k tokens per screenshot; with 3
  retained images a long task stays modest against 262k context, but every step pays
  a prefill.
- **cua 0.8.4 renames the module** `agent` -> `cua_agent`. The image's own tooling
  targets 0.7.25, so the upgrade could disturb something else in the image. This is
  why it goes in a thin layer we control, on a digest-pinned base, and is easy to drop.

---

## 5. As built — where reality differed from this plan

**Grounding was better than feared, and my first risk assessment was wrong.**
The ~10 px error above came from asking for raw pixels. In the normalised 0-1000
coordinate scheme the qwen35 loop actually uses, the model returned `[447, 500]` against
a true `[450, 500]` — 0.7% error. Live, it clicked a dock icon at (566,770) and a link
in a page correctly on the first attempt. Grounding is no longer the top risk.

**The base image self-upgrades cua on every boot** — `custom_startup.sh` runs
`uv pip install --upgrade cua-computer-server "cua-agent[all]"` before starting anything.
Three consequences: the digest pin does not pin the software, computer-server took ~7
minutes to answer, and the upgrade covered cua-agent/cua-core but not cua-computer,
leaving `import computer` broken (`No module named 'core'`). Replaced with a pinned
build-time install. This also means the qwen35 loop would have arrived on its own — but
in an inconsistent install.

**`/responses` cannot be the delegated loop.** It is documented in the source as a
"Minimal proxy to run ComputerAgent for up to 2 turns", so the bridge runs its own
ComputerAgent instead. Discovered by reading the endpoint, not the docs.

**computer-server's own MCP endpoint is a free win.** It serves 44 `computer_*` tools
over HTTP at `/mcp/` (including `get_accessibility_tree` and `find_element`), wired into
opencode as `desktop-raw` but disabled by default — 44 extra tool definitions in context
is a real cost, and the bridge covers normal use.

**Bugs found and fixed during bring-up** (each one caught by testing, not review):
1. `page.evaluate()` in Playwright *Python* takes exactly one argument, unlike the JS
   API. `browser_find` raised until both values were passed as a single array.
2. `python3 -m computer_server` binds 127.0.0.1 by default, and docker publishes to the
   container's bridge IP — so the host-side 8001 mapping was dead until `--host 0.0.0.0`.
3. `/cmd` returns a *streaming* response, so json-decoding it fails; `desktop_screenshot`
   now goes through the Computer SDK.
4. The qwen35 loop passes the model's raw text through, so `<tool_call>` markup leaked
   into summaries and would have polluted opencode's context. Now stripped.
5. **Root-owned `$HOME` broke XFCE entirely.** The base image sets
   `HOME=/home/kasm-user` for root build steps, so pip/playwright created a root-owned
   `~/.config`; uid 1000 could not write it, and the desktop came up as "Unable to load
   a failsafe session". Worse, `/home/kasm-user` is a persistent volume seeded from the
   image, so the bad ownership was baked in. Fixed by chowning after the root steps,
   plus a startup repair for volumes created by an older build — note that repair lands
   one boot late, because `vnc_startup.sh` starts XFCE before `custom_startup.sh` runs.

**Host port 8765 was already occupied** by a pre-existing `python3 -m http.server 8765`
that had been running for over a day. The bridge is published on **8766** instead; the
container side is unchanged.

**Not done:** Chromium was not installed — Firefox only, because it was already in the
image with a matching Playwright build, which made it the shortest path. Vision steps
remain non-replayable by design; only `browser_*` steps become deterministic scripts.
