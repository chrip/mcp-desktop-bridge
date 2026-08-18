# mcp-desktop-bridge

A real Ubuntu desktop in a container that a **local** LLM can see and operate, exposed to
your coding agent as MCP tools. Built to test an application that ordinary automation
cannot reach: half of its UI is a **canvas** where all the meaning is in pixels, and the
other half is **DOM spread across nested iframes**.

Nothing leaves the machine. The model is your own vLLM; the desktop is a container next to
it.

```
your agent (opencode, …)  ──MCP/HTTP──>  gui-bridge  ──>  Ubuntu XFCE + Firefox
                                              │                  ▲
                                              │                  │ you can watch and
                                              └──> vLLM ─────────┘ click along, live
                                                   (the brain)      at :6901
```

## Why not just Playwright, or just a computer-use agent?

Because the thing under test needs both, in the same session, on the same screen:

- **A canvas has no DOM.** Reading the document through selectors returns text that does
  not contain it — on one editor, the frame's `body` yields the *ribbon* ("File Home
  Insert…") and nothing of the document. So a DOM-only agent is not merely blind, it is
  confidently blind: it gets a plausible result instead of an error. `read_screen` answers
  those questions visually instead.
- **The controls are real DOM**, but nested two frames deep and cross-origin, while the
  dialogs they open are rendered by the *parent* page and talk back via postMessage. No
  single selector scope is ever the right one.

So this bridge carries three things at once: **pixel/vision control**, **selector control
with frame awareness**, and **runtime introspection** of the page. It found and root-caused
a real timing bug in an office suite's Smart Picker, which is documented in
[docs/](docs/) as a worked example.

## Requirements

- **arm64 Linux.** The desktop image is pinned to an arm64 digest — developed on a
  **DGX Spark (GB10)**. On x86 the build fails with a manifest error; you would need to
  repin the base image.
- **Docker** with Compose v2+ and the NVIDIA container runtime (for the vLLM service).
- **A vision-capable model** served over an OpenAI-compatible API. The bundled compose
  file runs `unsloth/Qwen3.8-27B-NVFP4` in vLLM; anything that accepts images works.
- **An MCP client.** Developed against [opencode](https://opencode.ai); any MCP client
  that speaks streamable HTTP will do.
- Roughly **30 GB** disk: ~22 GB model weights, ~5 GB desktop image.

## Quickstart

```bash
git clone <this repo> && cd mcp-desktop-bridge
cp .env-example .env      # add your HF_TOKEN, set a VNC_PW
docker compose up -d      # first run downloads ~22 GB of weights
```

Then point your MCP client at the bridge:

```json
"mcp": {
  "desktop": {
    "type": "remote",
    "url": "http://127.0.0.1:8766/mcp/",
    "timeout": 900000
  }
}
```

That `timeout` is not optional — opencode's default is **5000 ms**, while a single vision
turn takes seconds to tens of seconds (measured 7 s to 26 s depending on the model and
whether the model reasons first), so the vision tool can never finish inside it. See
[desktop/README.md](desktop/README.md#timeouts--read-this-before-using-gui_task).

### Turn down `reasoning_effort` before you time anything

Qwen3.8's chat template **defaults to `reasoning_effort: xhigh`**, so an MCP client that
never sets it — opencode included — runs every turn at maximum reasoning depth. Measured on
this stack, asking the model to name a single tool in one line:

| effort | wall | reasoning | answer |
|---|---|---|---|
| `xhigh` (default) | **232.5 s** | 21,184 chars | correct |
| `low` | **17.8 s** | 1,672 chars | correct |

Deciding which tool to call is most of what an agent loop does, so this is not a corner
case. Set a server-side default in `docker-compose.yml`:

```yaml
- --default-chat-template-kwargs
- '{"reasoning_effort": "low"}'
```

The bridge's own tools set their effort explicitly and are unaffected — the vision loop
runs at `none` (aim does not benefit from deliberation) and `read_screen` at `medium`
(transcription accuracy does). See
[desktop/README.md](desktop/README.md#reasoning_effort--and-the-xhigh-default-that-costs-you-everything)
for the full measurements, including why `none` is the wrong choice for the main loop.

Watch the desktop at <http://127.0.0.1:6901> — user **`kasm_user`** (underscore), password
`VNC_PW`. Then just ask your agent:

> open http://example.com, click "Learn more", scroll down and tell me what it says

## The tools

24 tools in four groups. Your MCP client prefixes them with the server name, so
`screenshot` appears as `desktop_screenshot`.

| Vision (X level — works on anything you can see) | |
|---|---|
| `gui_task` | delegated loop: the local model looks at screenshots and drives the mouse until the task is done, then answers a `report` question about the final screen. Hand it a whole block of clicking — driving the same block yourself costs two of your own turns per action |
| `read_screen` | **ask a question about the screen, get text back.** How you read a canvas: no DOM needed, no image to interpret, no action loop |
| `screenshot` | screenshot, optionally cropped, zoomed and with a labelled coordinate grid; also reports screen size and the focused window |
| `click` | click a pixel |
| `type_text` | type into whatever has focus |
| `press_key` | key or chord |
| `scroll_screen` | wheel scroll |
| `focus_browser` | put keyboard focus back on the browser |

| Selectors (precise — for DOM, across frames) | |
|---|---|
| `browser_open` / `browser_goto` | open the browser, navigate |
| `browser_frames` | list frames, including nested chains like `#outer >>> iframe` |
| `browser_find` | find elements by text, returning **verified-unique** selectors and the frame each lives in |
| `browser_click` / `browser_fill` / `browser_type` / `browser_press` | interact, with `frame` targeting |
| `browser_read` | read text instead of pixels |
| `browser_scroll` / `browser_close` | scroll, close |

| Introspection (why is it misbehaving?) | |
|---|---|
| `browser_eval` | run JS in any frame; read or set app internals. Test a fix at runtime without rebuilding |
| `browser_console` | rolling buffer of console output and uncaught page errors |
| `session_timeline` | recorded actions with the gap between each — timing as evidence |

| Replay | |
|---|---|
| `list_sessions` / `emit_replay_script` | turn a recorded session into a standalone Playwright script that runs with **no model in the loop** |

## Documentation

- **[desktop/README.md](desktop/README.md)** — the operator guide: prompting, office
  documents and canvases, keyboard focus, timeouts, debugging a web app, troubleshooting.
  Every gotcha in it was hit for real.
- **[docs/DESIGN.md](docs/DESIGN.md)** — design record: what was verified versus assumed,
  and where reality differed from the plan.

## Security

This is a development tool with broad access by design, **not a sandbox**. It drives a real
browser holding real logins and can reach the docker networks you attach it to. Every port
is bound to `127.0.0.1` for that reason. Do not expose it to a network you do not control.

Secrets live in `.env`, which is gitignored. Recorded sessions under `trajectories/` are
gitignored too — they contain screenshots of whatever was on screen.

## Built on

- [trycua/cua](https://github.com/trycua/cua) — computer-use agent loops and the desktop image
- [KasmVNC](https://github.com/kasmtech/KasmVNC) — the browser-accessible desktop
- [Playwright](https://playwright.dev) — selector-level browser control
- [vLLM](https://github.com/vllm-project/vllm) — local model serving

## License

Apache-2.0. See [LICENSE](LICENSE).
