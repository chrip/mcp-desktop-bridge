# GUI desktop agent — how to use it

An Ubuntu XFCE desktop in a container, driven from opencode on the host, with the
local vLLM instance as the brain. Everything below was run and verified against a real setup,
not written from documentation.

```
opencode (host) --MCP/HTTP--> :8766 gui-bridge ---> XFCE desktop + Firefox
                                        |
                                        '--------> vLLM :8000  (the brain)
watch it: http://127.0.0.1:6901
```

## Why this exists

The target under test is **Euro-Office (EO)**, an office suite embedded in Nextcloud, and
it has a property that makes a naive CLI agent nearly useless against it:

- **Half the UI is a canvas.** The document itself is rendered, not marked up. All the
  meaning is in pixels — there is no DOM to query. Verified: `browser_read` on the editor
  frame returns an **empty string** while the document is plainly visible on screen. A
  DOM-only or HTTP-only agent is blind here, and worse, it is *confidently* blind: it sees
  a valid empty result rather than an error.
- **The other half is ordinary DOM** — ribbon, menus, dialogs, pickers — but spread across
  **nested iframes with inter-frame communication**. The editor lives two frames deep
  (`#euroofficeFrame >>> iframe`, cross-origin), while the dialogs it opens are rendered by
  the **parent** Nextcloud page and talk back to the editor by postMessage. So no single
  selector scope is ever the right one.

Testing EO therefore needs **both modes in the same session, against the same screen**:
pixels for the canvas, selectors for the controls, and an understanding of which frame each
element belongs to. That is the whole reason this bridge exists rather than a plain
Playwright script or a plain computer-use agent — either one alone can only reach half the
application.

It pays off: a Smart Picker bug that leaves the `/query` trigger text in the document was
found and root-caused this way, because the flow crosses both worlds — type into a canvas,
pick from a parent-rendered dialog, then check the canvas again.

### Design requirements

These are personal requirements, and they explain most of the choices in here:

| Requirement | Consequence in this repo |
|---|---|
| **Runs locally on a DGX Spark** | arm64 throughout: the base image is pinned to its arm64 digest. No cloud dependency of any kind. |
| **Open source** | Apache-2.0, and built on open components (trycua/cua, KasmVNC, Playwright, vLLM). |
| **Works with Qwen3.6 and Qwen3.8 27B dense** | The bridge is model-agnostic: it needs any OpenAI-compatible endpoint that accepts images. The *only* model-specific coupling is that cua selects its agent loop by regex on the model string — see the `qwen35` note below. When swapping models, check which loop registers for the new name and add a `--served-model-name` alias to match; nothing else changes. |
| **Reaches my other containers, where EO and Nextcloud run** | The desktop container joins those projects' docker networks, so it browses them by the same hostnames a developer uses — which also satisfies Nextcloud's trusted-domain check without editing its config. |
| **Full access to my host build chain** | So a rebuilt EO can be tested immediately, without shipping an artifact anywhere. |

That last point is worth being blunt about: **this is a development tool with broad access
by design, not a sandbox.** It drives a real browser holding real logins, reaches your
container networks, and every port it opens is bound to `127.0.0.1` for that reason. Do not
expose it to a network you do not control.

## Start / stop

```bash
docker compose up -d            # vllm + desktop
docker compose logs -f desktop  # startup progress
docker compose restart desktop  # desktop only; vLLM keeps running
```

Watch the desktop at <http://127.0.0.1:6901>:

- username: **`kasm_user`** — with an **underscore**. The Linux account inside the
  container is `kasm-user` with a hyphen, but the KasmVNC login is not; using the hyphen
  gets you a bare `401 Unauthorized`, which Firefox renders as a generic "problem with
  this site" page rather than a password prompt.
- password: the value of `VNC_PW` in `.env`.
- `kasm_viewer` also exists, for view-only access.

Both `http://` and `https://` work on 6901 (the HTTPS certificate is self-signed, so
expect a browser warning). All ports are bound to 127.0.0.1 only.

| Port | What |
|---|---|
| 6901 | KasmVNC web viewer (this is where you watch and click yourself) |
| 8001 | computer-server: `/status`, `/cmd`, and its own MCP at `/mcp/` (44 primitives) |
| 8766 | gui-bridge MCP — this is what opencode talks to. 8766 rather than 8765 only because 8765 was occupied on the author's machine; change it in the compose file if you prefer |

### Logging out

There is no logout, and this is not a missing feature: KasmVNC standalone authenticates
with HTTP Basic auth through websockify, which has no server-side session. Verified on
this image — `/logout`, `/api/logout` and `/disconnect` all return 404, and the web app
contains no logout control. The browser resends your credentials until it forgets them.

To be asked again:

- **Private window** — open 6901 there; closing the window drops the credentials.
- **Quit the browser entirely** (all windows, not just the tab).
- **`Ctrl+Shift+Del`** → clear "Active Logins" (older Firefox) or "Cookies and site
  data" (newer Firefox, where that dialog was reorganised).
- **Rotate the password**, the only option that invalidates it server-side:

  ```bash
  sed -i 's/^VNC_PW=.*/VNC_PW=new-password/' .env
  docker compose up -d desktop   # NOT `restart`: that reuses the old environment
  ```

  `vnc_startup.sh` purges and rewrites `~/.kasmpasswd` from `VNC_PW` on every boot, so
  the persistent home volume does not pin an old password. This also re-keys the audio,
  upload and gamepad helper services, which share the same credential.

## Prompting it from opencode

The `desktop` MCP server is enabled in `~/.config/opencode/opencode.json`, so just ask:

> open the browser at example.com, click the "Learn more" link, then scroll down

There are two paths, and it matters which one gets used:

- **`browser_*` — precise.** Real CSS selectors: `browser_goto`, `browser_find`,
  `browser_click`, `browser_fill`, `browser_read`, `browser_scroll`. No pixel guessing,
  so logins and forms are reliable. Prefer these for web work.
- **`gui_task(task)` — vision.** One delegated call; the model looks at screenshots and
  drives mouse/keyboard until done. Use for anything that is not a web page (launching
  apps, dialogs, file manager). Verified: it clicked a dock icon at (566,770) and
  launched a terminal in 3 turns, and clicked a link on a page correctly.

Nudge opencode toward the precise path for web work, e.g. "use the browser tools, not
vision". Vision costs a screenshot round-trip (~1.2-1.6k image tokens) per step.

## Surfing a website with the AI

Just ask opencode. Two worked examples, both verified:

> go to example.com, click "Learn more", scroll down and tell me what the page says

> open http://nextcloud.local, log in, and tell me if anything looks broken on the dashboard

You can also drive it interactively: watch at :6901, and when the agent gets stuck, click
something yourself — it is the same X display and the same browser window, so the agent
picks up from wherever you left it. `desktop_screenshot` lets opencode look at the screen
whenever it wants.

Rule of thumb for good results: let it use `browser_goto` / `browser_find` /
`browser_click` for anything with a URL or a clickable element, and fall back to
`gui_task` for judgement calls ("does this page look broken?", "what is in the menu?").
Vision is the expensive path — roughly 1.2-1.6k image tokens per look.

## Testing the Nextcloud instances on this host

The desktop container is attached to both Nextcloud dev networks, so it browses them by
**the same hostnames you use**. Verified working:

| Instance | Use this URL | Why |
|---|---|---|
| `master-nextcloud-1` (nextcloud-docker-dev) | `http://nextcloud.local` | in its `trusted_domains`, and its `overwrite.cli.url` |
| `nextcloud` (host port 8081) | `http://nextcloud` | in its `trusted_domains` |
| EuroOffice documentserver | `http://eurooffice.local` | proxy alias on the dev network |

Do **not** use `http://host.docker.internal:8081` — it answers **HTTP 400**, because
Nextcloud rejects any request whose `Host` header is not a trusted domain. And
`master-proxy-1` publishes only to `127.0.0.1` on the host, which a container cannot
reach at all. Network hostnames avoid both problems without editing any Nextcloud config.

A verified login flow, and the shape to ask opencode for:

```
browser_goto  http://nextcloud.local
browser_fill  #user      admin
browser_fill  #password  <password>   secret=true
browser_click button[type=submit]     wait_for_nav=true
  -> lands on http://nextcloud.local/index.php/apps/dashboard/
```

**The session persists.** After `browser_close`, going back to `http://nextcloud.local`
lands straight on the dashboard — the cookie lives in the profile on the `desktop-home`
volume. So log in once (by hand at :6901, or once via `browser_fill`) and subsequent test
runs start authenticated.

If a stack is down, `docker compose up -d desktop` fails with a missing-network error,
because those networks are declared `external`. Start the stack, or comment the network
out of `docker-compose.yml`.

## Debugging a web app through the bridge

Three tools exist because a real investigation (a Smart Picker bug in EuroOffice) needed
them and they were missing. Each replaces something that otherwise costs an hour.

**`browser_eval(expression, frame)`** — run JS in the page or inside any iframe, and read
or set app internals. Cross-origin frames included.

```
browser_frames                              -> "#euroofficeFrame >>> iframe"  (depth 2)
browser_eval "Common.Utils.SmartPicker.PENDING_TIMEOUT"  frame="#euroofficeFrame >>> iframe"
   -> 60000
```

That single call found the constant behind the bug. It also lets you **test a fix without
building anything**: set the value at runtime, re-run the failing flow, and see whether the
symptom disappears. Verified doing exactly that — far better than patching a served bundle
and having to restore it. A reload discards the override, so there is nothing to undo.

Accepts an expression (`document.title`) or a body (`return x`).

**`browser_console(limit, level)`** — rolling buffer of console output and uncaught page
errors, collected from browser start, so messages logged before you asked are still there.
Nextcloud apps say a lot here that never reaches the screen (`[EO picker] providers for the
editor: …`). Filter with `level="error"`.

**`session_timeline(session_id)`** — recorded actions with the gap between each one, plus
the slowest gap. Timing is evidence: a bug that only appears when a step takes too long is
invisible in a list of actions and obvious in the gaps. This is what identified a 60 s
timeout as the cause of a wrong document, by showing `+70.0s` before the confirming click.

## Key names: either spelling works, but the path matters

`press_key` (X level) and `browser_press` (Playwright) expect **different spellings** —
`BackSpace` vs `Backspace`, `Return` vs `Enter` — and the wrong one fails **silently**:
the call returns ok and nothing happens. Both tools now normalise either spelling and
report `normalised_from` when they translate.

The paths are not interchangeable, though:

| Target | Use |
|---|---|
| document editor canvas, in-page elements | **`browser_press`** |
| native windows, XFCE, anything outside the page | **`press_key`** |

Measured: the office editor canvas **ignores `Backspace`/`Delete` delivered at the X
level** while accepting `ctrl+a` — so a selection appears and then nothing is deleted, with
no error. `press_key` now returns a `note` pointing at `browser_press` whenever you send a
deletion key, because that combination is silent and cost real debugging time.

## `thinking` — the speed/reliability trade-off in gui_task

`gui_task(task, thinking=False)` controls whether the model emits a reasoning block
before acting. It reaches vLLM as `chat_template_kwargs: {enable_thinking: …}`.

The per-call speedup is dramatic. On one screenshot-and-click completion:

| | output tokens | wall | result |
|---|---|---|---|
| thinking on | 700 | 29.6 s | hit the token cap while still reasoning — never answered |
| thinking off | 35 | 2.7 s | correct coordinates |

A vision turn is **~94% decode** (prefill of a full 1280x800 screenshot is only ~1.6 s of
it), so reasoning tokens dominate wall-clock. That is why the difference is so large.

**But faster per call is not faster per task.** The same multi-step action, one trial each:

| | turns | wall | outcome |
|---|---|---|---|
| `thinking=False` | 6 (hit the cap) | 29.9 s | failed — three stray clicks, no terminal |
| `thinking=True` | 3 | 28.1 s | succeeded — one precise click |

Identical wall time, opposite outcome: without a reasoning block the model needed more
turns and still missed. With thinking off it also reasons in the *content* channel
instead, which is the same tokens without the benefit of being separated out.

Rule of thumb from these measurements:

- **`thinking=False`** for cheap single-shot reads — "what is on screen?", "which window
  is focused?", one-step grounding. Measured 4.4 s versus 10.0 s, both correct.
- **`thinking=True`** for anything multi-step or requiring judgement — locating a control,
  recovering from a missed click, deciding whether a dialog is the right one.

## Timeouts — read this before using `gui_task`

**opencode's remote-MCP `timeout` defaults to 5000 ms.** A single vision turn was
measured at **~20 s** on a DGX Spark (screenshot -> vLLM multimodal round-trip -> action),
so `gui_task` can never complete inside the default and fails with
`MCP error -32001: Request timed out`. The fast `browser_*` calls fit in 5 s, which is
why they appear to work while vision does not — a confusing failure signature.

Already set in `~/.config/opencode/opencode.json`:

```json
"desktop": { "type": "remote", "url": "http://127.0.0.1:8766/mcp/", "timeout": 900000 }
```

`gui_task` also limits itself, so it never just hangs on the client:

- `max_turns` (default 12) — roughly 12 x 20 s
- `budget_s` (default 420) — wall-clock backstop

When either runs out it **returns** `truncated: true` with `elapsed_s`, the actions taken
and the summary so far. That is deliberate: a timed-out MCP call throws away everything
the model did, whereas a truncated result is continuable — the actions already happened
on screen, so call `gui_task` again to carry on instead of starting over.

Rough budgeting: keep `budget_s` comfortably below the client `timeout`. Prefer several
small `gui_task` calls over one big one — each returns something you can check.

## Keyboard focus is the main hazard

The single most common way a vision session fails silently: **keys go to whichever window
X has focused, which is not always the browser.** Seen live — a stray keystroke opened the
XFCE **Application Finder** over the page, and from then on every character and every
Return went into that finder. The agent kept narrating progress while the web dialog never
received a thing, and nothing was inserted.

Defences now built in:

- `screenshot` returns a text line alongside the image:
  `screen=1280x800 focused_window='slashtest.docx - Nextcloud — Nightly'`
  so the focused window is visible before you type.
- `focus_browser` puts focus back on the browser. Verified against the real failure: with
  the Application Finder focused, it restores the browser and reports `ok: true`.
- The vision loop's instructions name this hazard explicitly: if a native window is
  frontmost, Escape and re-focus rather than continuing to type.

Habit worth keeping: **`focus_browser` before any `type_text` / `press_key`** if the
previous step could have moved focus.

Two implementation notes, in case they resurface:

- Playwright bundles Firefox **Nightly**, so looking a window up by the name "firefox"
  finds nothing — only "Nightly" matches, because the lookup matches window titles. The
  fallback matches `WM_CLASS` (`Navigator.firefox-default`) via `wmctrl`, which is stable
  across browser channels.
- Reclaiming focus from a native dialog makes `activate_window` raise
  `"Failed to activate window"` **even though focus does move**. `focus_browser` therefore
  verifies by reading the active window title afterwards instead of trusting the call, and
  reports success with a note. A false failure is worse than a real one — it sends the
  caller off recovering from a problem that no longer exists.

## The screen is not a fixed size

KasmVNC resizes the X display to fit whoever is watching at :6901. It is 1280x800 with
nobody connected, and was measured at **1500x803** with a browser attached. So:

- Read the size from each `screenshot`; do not assume.
- A coordinate from an earlier screenshot can be stale if you resized your viewer window
  in between.
- This does not affect `gui_task`, whose loop works in normalised 0-1000 coordinates
  scaled to the current screenshot, nor the `browser_*` selector tools.

## Driving it by vision (the right tool for office documents)

Selectors lose to the Nextcloud office case, and it is worth being explicit about why:
the document is a **canvas**, inside a **nested iframe** (`#euroofficeFrame` contains a
second unnamed iframe holding the editor UI), while menus and dropdowns are rendered by
the **parent** Nextcloud page. So a single selector scope is never the right one.

X-level input has none of those problems. It clicks real pixels on the real display, so
iframes, canvas and frame ownership are all irrelevant — if you can see it, you can click
it. Two ways to use it:

**A. Delegated — one call, the local model does the loop**

```
gui_task "In the Profile picker, type bob, click the bob entry in the list, then click Insert"
```

Its system prompt now includes explicit handling for this pattern: overwrite the filter
field rather than appending, wait for the list to populate, click the middle of the list
row (or Down then Return), and screenshot to confirm the entry was accepted before going
for the Insert button. Budget ~20 s per turn.

**B. Manual — you (or opencode) drive it screenshot by screenshot**

```
desktop_screenshot   grid=true      # labelled 100px grid: red = x, blue = y
desktop_click        x=470 y=520    # click a pixel, any pixel
desktop_press_key    "Down"         # or step the list and confirm
desktop_press_key    "Return"
desktop_type_text    "bob"          # types into whatever has focus
desktop_scroll_screen clicks=3
```

`grid=true` is the important part: reading a coordinate off a ruled, numbered overlay is
much more reliable than estimating "about 40% across" from a bare screenshot.

Use B when the delegated loop keeps missing — you see exactly one screenshot, choose one
coordinate, and check the result, instead of hoping a multi-step loop converges.

`desktop_press_key` takes single keys ("Return", "Escape", "Down") or chords ("ctrl+a").
To replace the contents of a filter field: `desktop_press_key "ctrl+a"` then
`desktop_type_text "bob"`.

**A note on these names.** opencode prefixes every MCP tool with its server name, so the
tool this bridge calls `screenshot` appears to the model as `desktop_screenshot`, and
`press_key` as `desktop_press_key`. This document uses the prefixed form, because that is
what you type and what you see in a transcript. If you talk to the bridge directly over
HTTP, drop the `desktop_` prefix.

## Office documents (EuroOffice / Collabora / OnlyOffice)

Two things make documents different from ordinary pages, and both have a dedicated tool:

**1. The editor lives in an iframe.** Plain selectors resolve against the top document,
so a selector aimed at the editor matches the `<iframe>` element itself — which produces
`Element is not an <input>, <textarea>, <select> or [contenteditable]`. Call
`browser_frames` to discover it, then pass `frame=`:

```
browser_frames            -> [{selector: "#euroofficeFrame", src: ".../apps/eurooffice/540?..."}]
browser_click  selector=body  frame=#euroofficeFrame     # focus the document
```

**2. The document is a canvas, not text.** Verified on `slashtest.docx`:
`browser_read` with `frame=#euroofficeFrame` returns an **empty string** even though the
document is plainly visible on screen. There is no DOM text to read.

So for document *content*:

| Goal | Use |
|---|---|
| read / judge what the document shows | `desktop_screenshot` or `gui_task` (vision) |
| type into the document | `browser_click` into the frame, then `browser_type` |
| shortcuts, save, menus | `browser_press` (e.g. `Control+s`, `Enter`, `ArrowDown`) |
| the surrounding Nextcloud UI (Files, dialogs, sharing) | `browser_*` selectors as usual |

`browser_fill` is only for real form fields. `browser_type` sends genuine key events to
whatever has focus, with a `delay` between keystrokes — raise it if a server-syncing
editor drops characters.

## Finding things to click

`browser_find` returns **verified-unique** CSS paths, e.g.
`#app-content-vue > div:nth-of-type(3) > table > tbody > tr:nth-of-type(14) > td:nth-of-type(2) > button`,
each flagged `unique` and `visible`. Prefer a match with `unique: true`.

You can also click by text, which survives DOM reshuffling better:
`browser_click 'button:has-text("slashtest")'`.

## Logins: pre-seed them, do not hand over passwords

The browser profile is persistent (`~/.gui-bridge-profile` inside the container, on the
`desktop-home` volume), so:

1. Open <http://127.0.0.1:6901> and log into the site **yourself**, by hand.
2. Later tasks start already authenticated.

The model never sees the password, and nothing sensitive lands in a prompt, trajectory
or replay script. If you must automate a login, use
`browser_fill(selector, text, secret=true)` — the value is redacted in the trajectory
and the generated script reads it from an env var instead of hardcoding it.

## Replayable GUI scripts

Every browser action is recorded to `./trajectories/<session>.jsonl` (visible on the
host). Ask opencode to `emit_replay_script`, and you get a standalone Playwright script:

```bash
ls trajectories/                       # sessions + generated replay_*.py
docker exec qwen-gui-desktop python3 /trajectories/replay_<session>.py
```

Verified: a generated script replayed navigate → click → scroll with **no model
involved** and ended on the right URL.

Two real limits:
- **Close the agent's browser first** (`browser_close`), or the replay cannot open the
  same Firefox profile — a profile directory cannot be opened twice.
- **Vision steps are not replayable** and are emitted as comments. A pixel click is not
  reproducible; if you want a durable test, do that step with `browser_*`.

## Gotchas worth knowing

- **The model name `qwen35-27b` is load-bearing.** cua picks its agent loop by regex on
  the model string (`r"(?i).*qwen35.*"`). vLLM serves both `qwen3.6-27b` (for opencode)
  and `qwen35-27b` (for cua) from the same weights. Renaming it silently degrades GUI
  performance to a generic fallback loop with nothing in the logs.
- **Firefox is the browser because it was the easiest path, not on principle.** It ships in
  the base image, cua's own browser tooling targets it, and Playwright's arm64 Firefox
  build is pre-downloaded — so nothing extra had to be fetched. `playwright install
  chromium` in the Dockerfile adds an arm64 Chromium if you would rather use that.
- **The base image self-upgrades cua packages on every boot.** `Dockerfile` pins a
  consistent set at build time and replaces `custom_startup.sh` to stop that. If you
  ever revert to the stock script, expect ~7 minutes before port 8000 answers, plus a
  broken `import computer` from a half-upgraded dependency set.
- **Do not run root-owned writes into `/home/kasm-user` in the Dockerfile.** The base
  image sets `HOME=/home/kasm-user` for root build steps, and a root-owned `~/.config`
  makes XFCE come up as "Unable to load a failsafe session". The Dockerfile chowns after
  its root steps and `custom_startup.sh` repairs a stale volume — but that repair lands
  one boot late, so restart the container once after such a fix.
- **1280x800 is set by xrandr, not `VNC_RESOLUTION`.** KasmVNC ignores the geometry arg
  and starts 1024x768. Both live in the compose file; keep them in agreement.

## Restarting the desktop kills the session you are in

`docker compose up -d desktop` after any change to `bridge.py`, the `Dockerfile` or the
compose entry **recreates the container**. That destroys:

- the running Firefox and every open tab (including an open document),
- **unsaved editor changes**,
- the in-flight `gui_task` loop, whose next tool call then fails against a dead bridge.

What survives is anything on the `desktop-home` volume: the browser profile, so logins
stay, and `/trajectories`.

If an agent session stops mid-action for no visible reason, check whether the container
was recreated underneath it:

```bash
docker inspect qwen-gui-desktop --format '{{.State.StartedAt}} restarts={{.RestartCount}}'
```

A `StartedAt` matching when the session died is the answer — nothing crashed, the ground
moved. So: finish or pause a session before rebuilding, and save documents first.

## Troubleshooting

```bash
docker exec qwen-gui-desktop tail -30 /tmp/gui-bridge.log        # bridge
docker exec qwen-gui-desktop tail -30 /tmp/computer-server.log   # hands
docker logs qwen-gui-desktop | grep gui-bridge\]                 # startup sequence
curl -sS http://127.0.0.1:8001/status                            # {"status":"ok",...}
curl -sS http://127.0.0.1:8000/v1/models | jq -r '.data[].id'     # both names present?
```

If the desktop is up but tasks do nothing, check that vLLM answers on **both** names —
`qwen35-27b` missing means the alias was lost and cua fell back to the generic loop.
