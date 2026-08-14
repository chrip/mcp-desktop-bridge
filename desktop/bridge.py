#!/usr/bin/env python3
"""
gui-bridge: the MCP surface that opencode on the host talks to.

Runs INSIDE the desktop container. Three groups of tools:

  gui_task(...)       delegated vision loop. The local model on the host decides, cua's
                      qwen35 loop drives the mouse/keyboard. One call per instruction;
                      the screenshot traffic never crosses opencode's context.
  browser_*(...)      Playwright with real CSS selectors against the visible Firefox.
                      This is the precise path -- no pixel guessing for logins/links.
  emit_replay_script  turn a recorded session into a runnable script with NO model
                      in the loop, so a GUI test can be re-run cheaply.

Why the bridge owns the browser instead of using computer-server's /playwright_exec:
that endpoint supports only 9 fixed verbs (visit_url/click/type/scroll/back/forward/
screenshot/get_current_url/web_search) with no selector support, and a browser it
launched cannot be attached to from here (Firefox has no CDP, and a persistent profile
directory cannot be opened twice). Owning one headed Firefox gives selector precision,
a profile we control for pre-seeded logins, and honest action recording -- and the
vision loop still sees that same window on screen.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("gui-bridge")

# --- configuration ----------------------------------------------------------------
# "qwen35-27b" is not a typo and is load-bearing: cua selects its agent loop by
# regex on the model string, and loops/qwen35.py registers as r"(?i).*qwen35.*".
# A name like "qwen3.8-27b" misses that regex silently and falls back to the
# generic_vlm catch-all. vLLM serves both names for the same weights.
MODEL = os.environ.get("CUA_MODEL_NAME", "openai/qwen35-27b")
API_BASE = os.environ.get("OPENAI_BASE_URL", "http://host.docker.internal:8000/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")  # vLLM ignores it, litellm wants one
MAX_IMAGES = int(os.environ.get("CUA_MAX_IMAGES", "3"))
TRAJECTORY_DIR = Path(os.environ.get("TRAJECTORY_DIR", "/trajectories"))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
# Separator for nested-iframe chains, e.g. "#euroofficeFrame >>> iframe".
FRAME_SEP = ">>>"
# How many console lines to keep from the page. The Nextcloud/EuroOffice code is full of
# "[EO picker] ..." diagnostics that were invisible to us during a whole debugging
# session -- worth keeping a rolling buffer of.
CONSOLE_LIMIT = int(os.environ.get("CONSOLE_LIMIT", "400"))

# X (xdotool) and Playwright disagree on key names, and picking the wrong spelling fails
# SILENTLY -- the call reports ok, nothing happens. Cost real time: "BackSpace" is right
# for X, "Backspace" for Playwright. Normalise both ways so either spelling works on
# either path.
_X_KEYS = {"backspace": "BackSpace", "enter": "Return", "return": "Return",
           "esc": "Escape", "escape": "Escape", "del": "Delete", "delete": "Delete",
           "tab": "Tab", "space": "space", "up": "Up", "down": "Down",
           "left": "Left", "right": "Right", "pageup": "Prior", "pagedown": "Next"}
_PW_KEYS = {"backspace": "Backspace", "return": "Enter", "enter": "Enter",
            "esc": "Escape", "escape": "Escape", "del": "Delete", "delete": "Delete",
            "prior": "PageUp", "next": "PageDown", "ctrl": "Control"}
# Deletion keys the editor canvas ignores when they arrive at the X level.
_DELETE_KEYS = {"backspace", "delete", "del"}


def _norm_key(key: str, table: Dict[str, str]) -> str:
    """Translate one key or chord into the spelling the target expects."""
    parts = [p.strip() for p in key.split("+") if p.strip()]
    out = []
    for part in parts:
        lowered = part.lower()
        if lowered in ("ctrl", "control"):
            out.append("Control" if table is _PW_KEYS else "ctrl")
        elif lowered in ("shift", "alt", "meta", "super"):
            out.append(part.capitalize() if table is _PW_KEYS else lowered)
        else:
            out.append(table.get(lowered, part))
    return "+".join(out)
# Kept out of $HOME deliberately: $HOME is the persistent volume holding the profile,
# while browsers are baked into the image.
BROWSER_PROFILE = Path(os.environ.get("BROWSER_PROFILE", "/home/kasm-user/.gui-bridge-profile"))

INSTRUCTIONS = """You are controlling a real Ubuntu XFCE desktop. You click actual pixels
on the actual screen, so iframes, canvas rendering and which document a menu belongs to
are all irrelevant to you -- if you can see it, you can click it.

The screen size is NOT fixed: the viewer resizes the display, so read the size from each
screenshot rather than assuming one. Coordinates from an older screenshot may be stale.

KEYBOARD FOCUS IS THE MAIN HAZARD. Keys go to whichever window X has focused, which is
not always the browser. If a native window appears over the page -- the XFCE Application
Finder, a file dialog, a panel menu -- then everything you type goes THERE and the web
page never sees it. Before typing, confirm from the screenshot that the browser is the
frontmost window; if it is not, close the intruding window (Escape) and click once on the
browser window to focus it. Never keep typing into an unexpected window.

Work in small, verifiable steps. Take a screenshot after anything that changes the
screen, and CHECK it before continuing rather than assuming the last action worked.

If a click did not do what you expected, do NOT click the same place again. Take a
screenshot, find the target's new position, and aim at its centre.

Dropdown and autocomplete lists (a text field that filters a list of entries below it):
  1. Click the text field, then select-all and overwrite rather than appending, so a
     leftover prefix cannot keep filtering the list.
  2. Type the search text, then take a screenshot and WAIT for the list to appear --
     these lists populate asynchronously and are often empty for a moment.
  3. Click the list ENTRY itself, in the middle of its row, not the field and not the
     row's edge. Alternatively press Down to highlight it and then Return.
  4. Screenshot again to confirm the entry was accepted before moving on to buttons
     like "Insert" or "OK".

Dialog buttons ("Insert", "OK", "Save") are usually at the bottom right of the dialog.
Confirm the dialog closed afterwards; if it is still open, the click missed.

Say explicitly when the task is done, or explain what is blocking you."""

TRAJECTORY_DIR.mkdir(parents=True, exist_ok=True)

try:
    from fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"fastmcp is required inside the container: {exc}")

mcp = FastMCP("desktop-gui-bridge")


# --- session recording ------------------------------------------------------------
class Recorder:
    """Appends every action to a JSONL trajectory so a session can be replayed.

    cua's ComputerAgent writes its own richer trajectory (screenshots + model output)
    into trajectory_dir; this records the *replayable* subset -- the browser calls we
    made deliberately, which is what a deterministic script can be built from.
    """

    def __init__(self) -> None:
        self.session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.path = TRAJECTORY_DIR / f"{self.session_id}.jsonl"

    def record(self, kind: str, **fields: Any) -> None:
        entry = {"ts": time.time(), "kind": kind, **fields}
        try:
            with self.path.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            log.warning("could not write trajectory: %s", exc)


recorder = Recorder()

# The qwen35 loop hands back the model's raw text, which still contains the
# <tool_call>{...}</tool_call> markup it parsed the actions out of, plus the loop's own
# "Taking a screenshot" filler. Those are already represented in `actions`, so strip
# them from the summary rather than forwarding the duplication into opencode's context.
_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.S)
_FILLER = "Taking a screenshot to see the current computer screen."


def clean_text(text: str) -> str:
    text = _TOOL_CALL_RE.sub("", text or "")
    text = text.replace(_FILLER, "")
    # collapse the blank lines the removals leave behind
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- the computer handle (shared by the vision loop) -------------------------------
_computer: Any = None
_computer_lock = asyncio.Lock()


async def get_computer() -> Any:
    """Connect to the computer-server running in this same container.

    cua's Computer() is constructed differently across versions, so the supported
    kwarg is discovered rather than assumed -- this is exactly the kind of thing that
    breaks silently when the base image upgrades packages underneath us.
    """
    global _computer
    async with _computer_lock:
        if _computer is not None:
            return _computer
        from computer import Computer  # imported late: needs a consistent cua install

        params = inspect.signature(Computer.__init__).parameters
        kwargs: Dict[str, Any] = {}
        if "use_host_computer_server" in params:
            kwargs["use_host_computer_server"] = True
        elif "host" in params:  # older/newer shape: point at localhost explicitly
            kwargs["host"] = "127.0.0.1"
            if "port" in params:
                kwargs["port"] = 8000
        else:
            raise RuntimeError(
                f"cannot tell Computer() to use the local computer-server; "
                f"available params: {sorted(params)}"
            )
        if "verbosity" in params:
            kwargs["verbosity"] = logging.WARNING

        log.info("Computer(%s)", ", ".join(f"{k}={v!r}" for k, v in kwargs.items()))
        computer = Computer(**kwargs)
        await computer.run()
        _computer = computer
        return computer


# --- 1. delegated vision loop -----------------------------------------------------
@mcp.tool
async def gui_task(
    task: str, max_turns: int = 12, budget_s: int = 420, thinking: bool = False
) -> Dict[str, Any]:
    """Do something on the Ubuntu desktop using vision, in one delegated call.

    The local model looks at screenshots and drives the mouse and keyboard
    until the task is done. Use this for anything that is not a web page: launching
    apps, window management, file manager, dialogs -- and for web pages when you do
    not know the selectors.

    For web work where reliability matters (logins, forms, precise clicks), prefer the
    browser_* tools instead: they use real selectors and cannot misclick.

    Args:
        task: what to do, in plain language, e.g. "open the Files app and create a
            folder called demo on the Desktop".
        max_turns: safety cap on model turns before returning.
        thinking: let the model emit a reasoning block before acting. Off by default,
            and the difference is not marginal -- measured on one screenshot-and-click
            turn: 700 output tokens / 29.6 s with thinking on (which hit the token cap
            while still reasoning, so it never answered at all) versus 35 tokens / 2.7 s
            with it off, for the same correct coordinates. A vision turn is ~94% decode,
            so reasoning tokens dominate wall-clock. Turn it on for a step that needs
            real judgement rather than "find this and click it".
        budget_s: wall-clock budget (a turn is ~2-4 s with thinking off, ~20-30 s with
            it on). When it runs out the tool RETURNS what it achieved
            so far with truncated=true, instead of running until the MCP client gives
            up. Keep this below your client's MCP timeout -- opencode's `timeout` for a
            remote MCP server defaults to only 5000 ms, so it must be raised in
            opencode.json for this tool to be usable at all.

    Returns:
        summary of what the model said, the actions it took, and the trajectory id.
        A truncated result is still useful: the actions already happened on screen, so
        call again to continue rather than starting over.
    """
    from cua_agent import ComputerAgent

    computer = await get_computer()
    traj = TRAJECTORY_DIR / recorder.session_id
    traj.mkdir(parents=True, exist_ok=True)

    agent = ComputerAgent(
        model=MODEL,
        tools=[computer],
        api_base=API_BASE,
        api_key=API_KEY,
        # Bounds context growth: each 1280x800 screenshot costs roughly 1.2-1.6k
        # image tokens, so an unbounded history would balloon prefill on every turn.
        only_n_most_recent_images=MAX_IMAGES,
        trajectory_dir=str(traj),
        instructions=INSTRUCTIONS,
        telemetry_enabled=False,
        max_retries=3,
        # Reaches vLLM as a body field and is applied by the chat template. Verified in
        # place: the loop forwards every extra kwarg into litellm.acompletion, and
        # litellm passes extra_body through to the OpenAI-compatible endpoint.
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
    )

    recorder.record("gui_task", task=task, model=MODEL, thinking=thinking)
    messages: List[Dict[str, Any]] = [{"role": "user", "content": task}]
    texts: List[str] = []
    actions: List[str] = []
    turns = 0
    truncated = False
    started = time.monotonic()

    try:
        async for result in agent.run(messages):
            turns += 1
            for item in result.get("output", []) or []:
                itype = item.get("type")
                if itype == "message":
                    content = item.get("content")
                    if isinstance(content, str):
                        texts.append(clean_text(content))
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("text"):
                                texts.append(clean_text(part["text"]))
                elif itype == "computer_call":
                    action = item.get("action") or {}
                    name = action.get("type") or action.get("action") or "?"
                    detail = {
                        k: v for k, v in action.items() if k not in ("type", "action")
                    }
                    actions.append(f"{name}({json.dumps(detail, default=str)})")
                    recorder.record("agent_action", action=name, params=detail)
            if turns >= max_turns:
                truncated = True
                log.warning("gui_task hit max_turns=%s", max_turns)
                break
            # A vision turn costs a full multimodal round-trip to vLLM, so a handful of
            # turns can outlast an MCP client's patience. Stop on our own terms and
            # hand back partial progress: a timed-out MCP call discards everything the
            # model learned, while a truncated result can simply be continued.
            if time.monotonic() - started > budget_s:
                truncated = True
                log.warning("gui_task hit budget_s=%s after %s turns", budget_s, turns)
                break
    except Exception as exc:  # surfaced to opencode rather than hidden
        log.exception("gui_task failed")
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "turns": turns,
            "actions": actions,
            "summary": "\n".join(t for t in texts[-3:] if t),
            "trajectory": str(traj),
        }

    return {
        "ok": True,
        "turns": turns,
        "truncated": truncated,
        "elapsed_s": round(time.monotonic() - started, 1),
        "thinking": thinking,
        "actions": actions,
        "summary": "\n".join(t for t in texts if t) or "(model returned no text)",
        "trajectory": str(traj),
        "session_id": recorder.session_id,
    }


# --- 2. precise browser control ---------------------------------------------------
from collections import deque

_console: Any = deque(maxlen=CONSOLE_LIMIT)
_pw: Any = None
_ctx: Any = None
_page: Any = None
_browser_lock = asyncio.Lock()


async def get_page() -> Any:
    """Launch (once) a visible Firefox with a persistent profile, and return the page.

    headless=False is required: the whole point is that you can watch it at :6901 and
    that the vision loop can see the same window. The persistent profile is what makes
    pre-seeded logins work -- log in by hand once and the cookies stay.
    """
    global _pw, _ctx, _page
    async with _browser_lock:
        if _page is not None and not _page.is_closed():
            return _page
        from playwright.async_api import async_playwright

        if _pw is None:
            _pw = await async_playwright().start()
        BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
        log.info("launching Firefox, profile=%s", BROWSER_PROFILE)
        _ctx = await _pw.firefox.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE),
            headless=False,
            viewport={"width": 1280, "height": 720},
        )
        _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()

        # Listen once, for the life of the page. Without this the app's own
        # diagnostics and every uncaught page error are simply lost.
        _page.on("console", lambda msg: _console.append({
            "ts": time.time(), "type": msg.type, "text": msg.text[:600],
        }))
        _page.on("pageerror", lambda err: _console.append({
            "ts": time.time(), "type": "pageerror", "text": str(err)[:600],
        }))
        return _page


@mcp.tool
async def browser_open(
    url: Optional[str] = None, urls: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Open the browser on the desktop, optionally at a URL. Safe to call repeatedly.

    Args:
        url: optional page to open, e.g. "nextcloud.local". If given, this behaves
            exactly like browser_goto.
        urls: tolerated alias for `url` -- if a list is passed, the first entry is used.

    Returns:
        the current URL and page title.
    """
    # `url` and the `urls` alias both exist because models reliably try to hand this
    # tool a page to open -- "open" invites an argument. It previously took none, so
    # the call failed schema validation and the model concluded it had no browser tool
    # at all and gave up. Accepting the obvious argument is far better than being right
    # about the original signature.
    target = url
    if target is None and urls:
        target = urls[0]

    if target:
        return await _goto(target)

    page = await get_page()
    recorder.record("browser_open")
    return {"ok": True, "url": page.url, "title": await page.title()}


async def _goto(url: str, wait_for: str = "load") -> Dict[str, Any]:
    """Shared navigation used by browser_goto and browser_open.

    A plain function rather than one tool calling the other, so it does not depend on
    how a given FastMCP version wraps decorated tools.
    """
    page = await get_page()
    if not url.startswith(("http://", "https://", "file://", "about:")):
        # Bare hosts: prefer https, but fall back to http for local dev hostnames that
        # have no certificate (nextcloud.local, *.local, localhost and friends).
        candidate = "https://" + url
        host = url.split("/")[0]
        if host.endswith(".local") or host.split(":")[0] in ("localhost", "127.0.0.1"):
            candidate = "http://" + url
        url = candidate
    await page.goto(url, wait_until=wait_for)
    recorder.record("browser_goto", url=url, wait_for=wait_for)
    return {"ok": True, "url": page.url, "title": await page.title()}


@mcp.tool
async def browser_goto(url: str, wait_for: str = "load") -> Dict[str, Any]:
    """Navigate the visible browser to a URL and wait for it to settle.

    Reliable by construction -- no clicking the address bar and no typos, which is the
    single biggest source of failure when driving a browser by pixels.

    Args:
        url: destination. A bare host like "example.com" gets https:// prepended,
            except .local/localhost hosts which get http://.
        wait_for: playwright load state: "load", "domcontentloaded" or "networkidle".
    """
    return await _goto(url, wait_for)


def _scope(page: Any, frame: Optional[str]) -> Any:
    """Return the thing to locate against: the page, or a frame inside it.

    Content in Nextcloud office apps (EuroOffice, Collabora, OnlyOffice) lives inside an
    <iframe>, and selectors are resolved against the top document by default -- so a
    selector for something in the editor silently matches the iframe ELEMENT instead of
    the element inside it. Passing frame="#euroofficeViewerFrame" crosses that boundary.
    """
    if not frame:
        return page
    # A frame reference may be a CHAIN, because frames nest: the EuroOffice editor is
    # an unnamed <iframe> inside #euroofficeFrame, so "#euroofficeFrame" alone cannot
    # reach its toolbar and a bare "iframe" does not exist in the top document. Each
    # hop is separated by ">>>" and becomes another frame_locator.
    scope = page
    for hop in [h.strip() for h in frame.split(FRAME_SEP) if h.strip()]:
        scope = scope.frame_locator(hop)
    return scope


def _inner(selector: str, frame: Optional[str]) -> str:
    """Absorb the "selector IS the frame" mistake.

    Passing frame="#euroofficeFrame" together with selector="#euroofficeFrame" asks
    Playwright to find the iframe INSIDE itself, which cannot match and burns the full
    locator timeout before failing. It is the obvious way to express "click into that
    frame", so treat it as such and target the frame's body instead.
    """
    if frame:
        hops = [h.strip() for h in frame.split(FRAME_SEP) if h.strip()]
        if hops and selector.strip() in (frame.strip(), hops[-1]):
            return "body"
    return selector


async def _chain_for_frame(page: Any, target: Any) -> Optional[str]:
    """Top-down chain of iframe selectors identifying `target`, e.g. "#a >>> iframe".

    One implementation for all callers. browser_frames and browser_find each grew their
    own copy of this, which is how they ended up disagreeing: one reported a bare
    "iframe" that was meaningless from the top document.
    """
    hops: List[str] = []
    cur = target
    while cur is not None and cur != page.main_frame:
        sel = None
        try:
            handle = await cur.frame_element()
            sel = await handle.evaluate(
                """(el) => {
                     if (el.id) return '#' + CSS.escape(el.id);
                     const cls = (el.className || '').trim();
                     if (cls) return 'iframe.' + cls.split(/\\s+/).map(c => CSS.escape(c)).join('.');
                     const sibs = [...(el.parentElement ? el.parentElement.children : [])]
                       .filter(c => c.tagName === 'IFRAME');
                     return sibs.length > 1
                       ? `iframe:nth-of-type(${sibs.indexOf(el) + 1})` : 'iframe';
                   }"""
            )
        except Exception:
            pass
        hops.insert(0, sel or "iframe")
        cur = cur.parent_frame
    return (" " + FRAME_SEP + " ").join(hops) if hops else None


async def _resolve_frame(page: Any, chain: Optional[str]) -> Any:
    """Return the Playwright Frame for a chain string, or the main frame for None.

    frame_locator() is for locating elements; evaluating JS needs the Frame itself, so
    the chain has to be resolved back to a real frame object.
    """
    if not chain:
        return page.main_frame
    wanted = FRAME_SEP.join(h.strip() for h in chain.split(FRAME_SEP) if h.strip())
    for f in page.frames:
        if f == page.main_frame:
            continue
        got = await _chain_for_frame(page, f)
        if got and FRAME_SEP.join(h.strip() for h in got.split(FRAME_SEP)) == wanted:
            return f
    raise ValueError(f"no frame matches {chain!r}; call browser_frames for the list")


@mcp.tool
async def browser_frames() -> Dict[str, Any]:
    """List the iframes on the page, so you know what to pass as `frame`.

    Call this first when working with a Nextcloud office document, a viewer, or any
    embedded app -- its content is not reachable with plain page selectors.
    """
    page = await get_page()

    # Walk Playwright's real frame tree, so NESTED frames appear. Scanning only the top
    # document's <iframe> tags misses them: the EuroOffice editor UI lives in an
    # unnamed iframe inside #euroofficeFrame, and its toolbar is unreachable unless the
    # caller is given the whole chain "#euroofficeFrame >>> iframe".
    chains: List[Dict[str, Any]] = []
    for f in page.frames:
        if f == page.main_frame:
            continue
        chain = await _chain_for_frame(page, f)
        chains.append({
            "frame": chain,
            "depth": len((chain or "").split(FRAME_SEP)),
            "url": (f.url or "")[:160],
        })

    # Sizes come from the top document's <iframe> tags, matched back onto the frame tree
    # by src. Only for orientation ("which one is the big editor?") -- the `frame` chain
    # is the part callers pass to the other tools.
    sizes = await page.evaluate(
        """() => [...document.querySelectorAll('iframe')].map(f => {
             const r = f.getBoundingClientRect();
             return {src: f.src || '', width: Math.round(r.width), height: Math.round(r.height)};
           })"""
    )
    by_src = {row["src"]: row for row in sizes if row.get("src")}
    for entry in chains:
        hit = by_src.get(entry["url"]) or next(
            (v for k, v in by_src.items() if entry["url"] and k.startswith(entry["url"][:80])), None)
        if hit:
            entry["width"], entry["height"] = hit["width"], hit["height"]

    return {
        "ok": True,
        "count": len(chains),
        # Deepest first: the innermost frame is usually the one holding the app you are
        # actually after, and it is the one a top-document-only scan cannot see.
        "frames": sorted(chains, key=lambda e: -e["depth"]),
        "hint": f'pass a "frame" value verbatim to browser_click/fill/read/eval; '
                f'nested chains are joined with "{FRAME_SEP}"',
    }


@mcp.tool
async def browser_fill(
    selector: str, text: str, secret: bool = False, frame: Optional[str] = None
) -> Dict[str, Any]:
    """Type text into a form field matching a CSS selector.

    Only works on real form fields (<input>, <textarea>, <select>, [contenteditable]).
    A document editor canvas is NOT one of those: for EuroOffice / Collabora /
    OnlyOffice documents, click into the document and use browser_type instead.

    Args:
        selector: CSS selector, e.g. "input[name=email]".
        text: the value to enter.
        secret: if true the value is written as "***" in the trajectory and the
            replay script reads it from an env var instead of hardcoding it. Use this
            for anything you would not want to appear in a log or a checked-in script.
        frame: CSS selector of an iframe to look inside, from browser_frames.
    """
    page = await get_page()
    sel = _inner(selector, frame)
    await _scope(page, frame).locator(sel).fill(text, timeout=12000)
    recorder.record("browser_fill", selector=sel, text="***" if secret else text,
                    secret=secret, frame=frame)
    return {"ok": True, "selector": sel, "frame": frame,
            "typed": "***" if secret else text}


@mcp.tool
async def browser_type(
    text: str, delay: int = 25, secret: bool = False
) -> Dict[str, Any]:
    """Type text as real key presses, wherever the focus currently is.

    This is the way to enter text into a document editor (EuroOffice, Collabora,
    OnlyOffice) or any canvas-based app, where there is no input element to fill.
    Click into the document first so it has focus, then call this.

    Args:
        text: the characters to type.
        delay: milliseconds between keystrokes. Editors that sync to a server can drop
            input that arrives too fast; raise this if characters go missing.
        secret: redact the value in the trajectory and replay script.
    """
    page = await get_page()
    await page.keyboard.type(text, delay=delay)
    recorder.record("browser_type", text="***" if secret else text, delay=delay,
                    secret=secret)
    return {"ok": True, "typed": "***" if secret else text}


@mcp.tool
async def browser_press(key: str, times: int = 1) -> Dict[str, Any]:
    """Press a key or a chord, e.g. "Enter", "Tab", "Control+s", "ArrowDown".

    Use for saving, navigating menus and keyboard shortcuts inside editors.

    Args:
        key: playwright key name or chord.
        times: how many times to press it.
    """
    page = await get_page()
    sent = _norm_key(key, _PW_KEYS)
    for _ in range(max(1, times)):
        await page.keyboard.press(sent)
    recorder.record("browser_press", key=sent, times=times)
    out = {"ok": True, "key": sent, "times": times}
    if sent != key:
        out["normalised_from"] = key
    return out


@mcp.tool
async def browser_click(
    selector: str,
    wait_for_nav: bool = False,
    frame: Optional[str] = None,
    timeout_ms: int = 12000,
) -> Dict[str, Any]:
    """Click the element matching a CSS selector.

    Args:
        selector: CSS selector to click. Supports Playwright text matching, e.g.
            'button:has-text("Save")'. To click into an iframe's document, pass the
            iframe as `frame` and use "body" here (passing the iframe selector as both
            is accepted and treated the same way).
        wait_for_nav: set true when the click triggers a page load (e.g. submitting
            a login form), so the call returns only once the new page has loaded.
        frame: CSS selector of an iframe to look inside, from browser_frames.
        timeout_ms: how long to wait for the element before giving up.
    """
    page = await get_page()
    sel = _inner(selector, frame)
    target = _scope(page, frame).locator(sel)
    try:
        if wait_for_nav:
            async with page.expect_navigation():
                await target.click(timeout=timeout_ms)
        else:
            await target.click(timeout=timeout_ms)
    except Exception as exc:
        # Return a usable failure instead of a raw stack: a timed-out click usually
        # means the selector is wrong, so say what to try next.
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}",
            "selector": sel,
            "frame": frame,
            "hint": "run browser_find to get a verified-unique selector, or "
                    "browser_frames if the element is inside an iframe",
        }
    recorder.record("browser_click", selector=sel, wait_for_nav=wait_for_nav,
                    frame=frame)
    return {"ok": True, "selector": sel, "frame": frame, "url": page.url}


@mcp.tool
async def browser_scroll(dy: int = 600, dx: int = 0) -> Dict[str, Any]:
    """Scroll the page by a pixel delta. Positive dy scrolls down."""
    page = await get_page()
    await page.mouse.wheel(dx, dy)
    recorder.record("browser_scroll", dx=dx, dy=dy)
    return {"ok": True, "dx": dx, "dy": dy}


@mcp.tool
async def browser_read(
    selector: str = "body", max_chars: int = 4000, frame: Optional[str] = None
) -> Dict[str, Any]:
    """Read visible text from the page as text, not pixels.

    Much cheaper and more accurate than asking the model to read a screenshot, so use
    this to check whether a login worked or to extract content.

    Args:
        selector: CSS selector to read; defaults to the whole body.
        max_chars: truncate after this many characters.
        frame: CSS selector of an iframe to read inside, from browser_frames. Needed for
            Nextcloud office documents and other embedded apps. Note a canvas-rendered
            document body may still yield little or no text -- use desktop_screenshot or
            gui_task to judge those visually.
    """
    page = await get_page()
    text = await _scope(page, frame).locator(_inner(selector, frame)).inner_text(
        timeout=12000
    )
    truncated = len(text) > max_chars
    return {
        "ok": True,
        "url": page.url,
        "title": await page.title(),
        "text": text[:max_chars],
        "truncated": truncated,
    }


FINDER_JS = """
        ([needle, limit]) => {
          const cssPath = (el) => {
            if (el.id) return '#' + CSS.escape(el.id);
            const parts = [];
            for (let n = el; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
              if (n.id) { parts.unshift('#' + CSS.escape(n.id)); break; }
              const tag = n.tagName.toLowerCase();
              const sibs = n.parentElement
                ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [];
              parts.unshift(sibs.length > 1
                ? `${tag}:nth-of-type(${sibs.indexOf(n) + 1})` : tag);
            }
            return parts.join(' > ');
          };

          const out = [];
          const sel = 'a,button,input,select,textarea,[role=button],[role=link],'
                    + '[role=menuitem],[role=tab],[role=checkbox],[onclick]';
          const want = needle.toLowerCase();
          for (const el of document.querySelectorAll(sel)) {
            const label = (el.innerText || el.value || el.getAttribute('aria-label')
                        || el.getAttribute('title') || el.getAttribute('placeholder')
                        || '').trim();
            if (!label || !label.toLowerCase().includes(want)) continue;
            const path = cssPath(el);
            let unique = false;
            try { unique = document.querySelectorAll(path).length === 1; } catch (e) {}
            const r = el.getBoundingClientRect();
            out.push({
              label: label.replace(/\\s+/g, ' ').slice(0, 80),
              selector: path,
              unique: unique,
              tag: el.tagName.toLowerCase(),
              type: el.getAttribute('type') || null,
              visible: r.width > 0 && r.height > 0,
            });
            if (out.length >= limit) break;
          }
          return out;
        }
        """


@mcp.tool
async def browser_find(
    text: str, limit: int = 10, frame: Optional[str] = None, search_frames: bool = True
) -> Dict[str, Any]:
    """Find clickable elements containing some text, and report usable selectors.

    Use this to discover what to pass to browser_click when you do not know the page
    structure, instead of guessing coordinates from a screenshot.

    Searches inside iframes too, and each match reports the `frame` it lives in -- pass
    that straight back to browser_click/browser_fill. This matters for Nextcloud office
    apps: their menus, dialogs and dropdowns are real DOM, but they live inside the
    editor's iframe, so a top-document-only search finds nothing and makes the element
    look unreachable.

    Args:
        text: substring to match against label, value, aria-label, title, placeholder.
        limit: maximum matches per frame.
        frame: restrict the search to this iframe (selector from browser_frames).
        search_frames: also search every iframe, not just the top document.

    Returns:
        matches, each with `selector`, `frame` (null for the top document), `unique`
        and `visible`.
    """
    page = await get_page()
    # NOTE: Playwright Python's evaluate() takes exactly ONE arg, unlike the JS API
    # where you can pass several. Both values go in as a single array and are
    # destructured on the JS side.
    #
    # The returned selector is a full nth-child CSS path, not just the tag name. An
    # earlier version returned bare "a"/"button", which matched dozens of elements and
    # made browser_click time out on the wrong node -- worse than returning nothing,
    # because it looks like a usable answer. Every selector below is verified unique
    # against the live document before being handed back.
    # The finder runs in the top document AND in each iframe. Every match carries the
    # frame it came from, so the caller can hand `frame` straight back to click/fill.
    results: List[Dict[str, Any]] = []

    async def run_in(target: Any, frame_sel: Optional[str]) -> None:
        try:
            found = await target.evaluate(FINDER_JS, [text, limit])
        except Exception as exc:  # a cross-origin or detached frame must not kill the call
            log.debug("finder failed in frame %s: %s", frame_sel, exc)
            return
        for item in found:
            item["frame"] = frame_sel
            results.append(item)

    if frame:
        await run_in(_scope(page, frame), frame)
    else:
        await run_in(page, None)
        if search_frames:
            for f in page.frames:
                if f == page.main_frame:
                    continue
                await run_in(f, await _chain_for_frame(page, f))

    return {
        "ok": True,
        "matches": results[: limit * 4],
        "count": len(results),
        "hint": 'pass `frame` back to browser_click for matches inside an iframe; '
                'you can also click by text, e.g. \'button:has-text("Save")\'',
    }



@mcp.tool
async def browser_close() -> Dict[str, Any]:
    """Close the browser, flushing the profile so logins persist."""
    global _ctx, _page
    async with _browser_lock:
        if _ctx is not None:
            await _ctx.close()
        _ctx = None
        _page = None
    recorder.record("browser_close")
    return {"ok": True}


# --- 3. eyes for opencode ---------------------------------------------------------
@mcp.tool
async def screenshot(grid: bool = False) -> Any:
    """Take a screenshot of the whole desktop, to check the state yourself.

    The reply also carries a text line with the screen size and the currently FOCUSED
    window. Read both before acting:

    - Coordinates are only valid for the size reported here. KasmVNC resizes the X
      display to fit whoever is watching at :6901, so the screen is NOT a fixed
      1280x800 -- it was measured at 1500x803 while a browser was connected. A
      coordinate remembered from an earlier screenshot may now point somewhere else.
    - If the focused window is not Firefox, typing will go somewhere unintended. Call
      focus_browser first.

    Args:
        grid: overlay a labelled 100 px coordinate grid. Turn this on when you intend to
            click by pixel -- reading a coordinate off a ruled, numbered grid is far
            more reliable than estimating it from a bare image.
    """
    # Via the Computer SDK rather than computer-server's /cmd: that endpoint returns a
    # STREAMING response ("Backup endpoint for when WebSocket connections fail"), not a
    # single JSON object, so json-decoding it fails outright.
    computer = await get_computer()
    shot = await computer.interface.screenshot()

    if isinstance(shot, str):  # some versions hand back base64 already
        raw_bytes = base64.b64decode(shot)
    else:
        raw_bytes = shot

    if grid:
        raw_bytes = _draw_grid(raw_bytes)
    raw_b64 = base64.b64encode(raw_bytes).decode()

    meta = {"active_window": await _active_title()}
    try:
        computer = await get_computer()
        size = await computer.interface.get_screen_size()
        meta["screen"] = f"{size.get('width')}x{size.get('height')}"
    except Exception:
        meta["screen"] = "unknown"

    try:
        from fastmcp.utilities.types import Image

        # Both parts come back: the picture, plus the coordinate space and focus state
        # it belongs to. Returning the image alone invites clicking with a stale
        # coordinate system or typing into the wrong window.
        return [
            f"screen={meta['screen']} focused_window={meta['active_window']!r} "
            f"(coordinates are valid for this size only)",
            Image(data=raw_bytes, format="png"),
        ]
    except Exception:  # fall back to base64 text if this fastmcp lacks Image
        return {"ok": True, "format": "base64/png", "image_base64": raw_b64, **meta}


@mcp.tool
async def focus_browser() -> Dict[str, Any]:
    """Give keyboard focus back to the Firefox window before typing.

    THE most common cause of a vision session going wrong: keystrokes land in whatever
    window X considers focused, which is not necessarily the browser. Observed live --
    a stray keystroke reached the XFCE desktop and opened the Application Finder over
    the page, after which every subsequent key went into that finder instead of the web
    dialog, and the run silently accomplished nothing.

    Call this before type_text / press_key whenever the last action might have moved
    focus, and after closing any native dialog.
    """
    computer = await get_computer()
    iface = computer.interface
    win_id, how = await _find_browser_window(iface)
    if win_id is None:
        return {"ok": False, "error": how,
                "hint": "call browser_open first, or check the desktop at :6901"}
    activate_error: Optional[str] = None
    try:
        await iface.activate_window(win_id)
    except Exception as exc:
        activate_error = str(exc)

    # VERIFY BY OBSERVATION, do not trust the return value. Measured live: reclaiming
    # focus from the XFCE Application Finder raises "Failed to activate window" and yet
    # focus lands on the browser correctly. Reporting that as a failure is worse than
    # useless -- the caller then "recovers" from a problem that does not exist.
    await asyncio.sleep(0.3)  # the WM needs a moment before the new focus is readable
    title = await _active_title() or ""
    focused = any(mark in title for mark in ("Nightly", "Firefox", "Mozilla"))

    recorder.record("focus_browser", window=str(win_id), focused=focused)
    if focused:
        return {
            "ok": True, "activated": str(win_id), "matched_by": how,
            "active_window": title,
            **({"note": f"activate_window reported {activate_error!r} but the browser "
                        "is focused, which is what matters"} if activate_error else {}),
        }
    return {
        "ok": False,
        "error": activate_error or f"window did not take focus; still on {title!r}",
        "matched_by": how,
        "active_window": title,
        "hint": "a native window may be modal; press Escape (press_key) to dismiss it, "
                "then call focus_browser again",
    }


async def _find_browser_window(iface: Any) -> tuple[Optional[Any], str]:
    """Locate the browser window, without depending on its product name.

    get_application_windows() matches window TITLES, and Playwright ships Firefox
    *Nightly* -- so "firefox" finds nothing while "Nightly" works. Relying on that would
    break the moment the bundled browser changes channel. The authoritative signal is the
    WM_CLASS, which is "Navigator.firefox-default" regardless of channel, so try the
    names first for speed and then fall back to matching the class via wmctrl.
    """
    for name in ("Nightly", "Firefox", "firefox", "Mozilla"):
        try:
            ids = await iface.get_application_windows(name)
        except Exception:
            continue
        if ids:
            return ids[0], f"title match {name!r}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "wmctrl", "-lx",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except FileNotFoundError:
        return None, "no browser window found and wmctrl is not installed"

    for line in out.decode(errors="replace").splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 3 and "firefox" in parts[2].lower():
            try:
                return int(parts[0], 16), f"WM_CLASS match {parts[2]}"
            except ValueError:
                continue
    return None, "no browser window found; is the browser open?"


async def _active_title() -> Optional[str]:
    """Title of the window that currently has focus, or None if it cannot be read."""
    try:
        computer = await get_computer()
        iface = computer.interface
        return await iface.get_window_title(await iface.get_current_window_id())
    except Exception:
        return None


def _xy(x: Any, y: Any = None) -> tuple[int, int]:
    """Accept the several shapes a model actually sends for a coordinate pair.

    Observed in practice: x="(400, 650)" as a STRING with y omitted entirely, which a
    strict int schema rejects outright -- two wasted turns and no clue what to fix. Also
    accepts [400, 650], "400,650" and plain ints. Being liberal here costs nothing and
    saves the caller from guessing the one blessed shape.
    """
    if y is None and isinstance(x, str):
        nums = re.findall(r"-?\d+", x)
        if len(nums) >= 2:
            return int(nums[0]), int(nums[1])
        raise ValueError(f"need two coordinates, got {x!r}")
    if y is None and isinstance(x, (list, tuple)) and len(x) >= 2:
        return int(x[0]), int(x[1])
    if y is None:
        raise ValueError(f"missing y coordinate (x={x!r})")
    if isinstance(x, str):
        x = int(re.findall(r"-?\d+", x)[0])
    if isinstance(y, str):
        y = int(re.findall(r"-?\d+", y)[0])
    return int(x), int(y)


def _draw_grid(png_bytes: bytes, step: int = 100) -> bytes:
    """Overlay a labelled coordinate grid, to make pixel-accurate clicking practical.

    Estimating "roughly 40% across" from a bare screenshot is where vision clicking goes
    wrong; reading a number off a ruled line does not have that failure mode.
    """
    try:
        import io
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("Pillow unavailable, returning screenshot without grid")
        return png_bytes

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for x in range(step, w, step):
        draw.line([(x, 0), (x, h)], fill=(255, 0, 0), width=1)
        # label twice so a number is near wherever you happen to be looking
        for ly in (2, h - 12):
            draw.text((x + 2, ly), str(x), fill=(255, 0, 0))
    for y in range(step, h, step):
        draw.line([(0, y), (w, y)], fill=(0, 90, 255), width=1)
        for lx in (2, w - 34):
            draw.text((lx, y + 2), str(y), fill=(0, 90, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# --- 3b. X-level vision primitives ------------------------------------------------
# These click actual pixels on the actual X display, so they do not care about iframes,
# canvas rendering, shadow DOM, or which document a menu was rendered by. That is the
# whole point: some UI cannot be addressed with selectors at all. The Nextcloud office
# case is the worst of it -- a canvas document inside a nested iframe, with dropdowns
# and menus rendered by the PARENT Nextcloud page. Pixels cut through all of it.
#
# Intended loop: desktop_screenshot -> decide coordinates -> desktop_click -> repeat.
# Coordinates are pixels in the screenshot you just took, origin top-left, 1280x800.


@mcp.tool
async def click(
    x: Union[int, str, List[int]],
    y: Optional[Union[int, str]] = None,
    button: str = "left",
    double: bool = False,
) -> Dict[str, Any]:
    """Click at absolute screen pixel coordinates, bypassing the DOM entirely.

    Use this after desktop_screenshot when the target cannot be reached with a CSS
    selector: a canvas document, a dropdown rendered by another frame, a native menu.

    Args:
        x: pixel x in the screenshot, 0-1279, origin top-left.
        y: pixel y in the screenshot, 0-799.
        button: "left" or "right".
        double: double-click instead of single.
    """
    try:
        x, y = _xy(x, y)
    except ValueError as exc:
        return {"ok": False, "error": str(exc),
                "hint": "pass x and y as separate integers, e.g. x=400, y=650"}

    computer = await get_computer()
    iface = computer.interface

    # Bounds-check against the LIVE screen size. Seen in a real run: clicks at y=850 and
    # y=870 on an 803px-tall display, which fail with no useful explanation and cost two
    # turns. The display is resized by the viewer, so a coordinate that was valid earlier
    # can fall off the screen later -- say so explicitly instead of failing obscurely.
    try:
        size = await iface.get_screen_size()
        sw, sh = int(size.get("width", 0)), int(size.get("height", 0))
    except Exception:
        sw = sh = 0
    if sw and sh and not (0 <= x < sw and 0 <= y < sh):
        return {
            "ok": False,
            "error": f"({x}, {y}) is outside the {sw}x{sh} screen",
            "screen": f"{sw}x{sh}",
            "hint": "take a fresh screenshot and read the coordinate off it; the display "
                    "is resized by whoever is watching, so older coordinates go stale",
        }

    if double:
        await iface.double_click(x, y)
    elif button == "right":
        await iface.right_click(x, y)
    else:
        await iface.left_click(x, y)
    recorder.record("desktop_click", x=x, y=y, button=button, double=double)
    return {"ok": True, "x": x, "y": y, "button": button, "double": double}


@mcp.tool
async def type_text(text: str, delay: int = 30, secret: bool = False) -> Dict[str, Any]:
    """Type text at the X level, into whatever currently has keyboard focus.

    Works in any application, including canvas editors. Click the target first.

    Args:
        text: characters to type.
        delay: ms between keystrokes; raise it if a syncing editor drops characters.
        secret: redact the value in the trajectory.
    """
    computer = await get_computer()
    await computer.interface.type_text(text, delay=delay / 1000.0)
    recorder.record("desktop_type", text="***" if secret else text, secret=secret)
    return {"ok": True, "typed": "***" if secret else text}


@mcp.tool
async def press_key(key: str, times: int = 1) -> Dict[str, Any]:
    """Press a key or chord at the X level, e.g. "Return", "Escape", "ctrl+s", "Down".

    This is how you confirm a highlighted dropdown entry ("Return") or dismiss a menu
    ("Escape") when there is no clickable element to target.

    Args:
        key: single key, or a chord joined with "+" such as "ctrl+a".
        times: repeat count -- handy for stepping down a list with "Down".
    """
    computer = await get_computer()
    iface = computer.interface
    sent = _norm_key(key, _X_KEYS)
    for _ in range(max(1, times)):
        if "+" in sent:
            await iface.hotkey(*[k.strip() for k in sent.split("+") if k.strip()])
        else:
            await iface.press_key(sent)
    recorder.record("desktop_key", key=sent, times=times)
    result = {"ok": True, "key": sent, "times": times}
    if sent != key:
        result["normalised_from"] = key
    # Measured: the office editor canvas ignores Backspace/Delete delivered at the X
    # level while accepting ctrl+a, so a selection appears and then nothing is deleted.
    # Silent, and it looks like the tool worked. Point at the path that does work.
    if key.split("+")[-1].strip().lower() in _DELETE_KEYS:
        result["note"] = ("editor canvases often ignore this key at the X level; if "
                          "nothing was deleted use browser_press instead")
    return result


@mcp.tool
async def scroll_screen(clicks: int = 3, down: bool = True) -> Dict[str, Any]:
    """Scroll the X display with the mouse wheel, wherever the pointer is.

    Move the pointer first with desktop_click if the wrong pane scrolls.
    """
    computer = await get_computer()
    iface = computer.interface
    if down:
        await iface.scroll_down(clicks=clicks)
    else:
        await iface.scroll_up(clicks=clicks)
    recorder.record("desktop_scroll", clicks=clicks, down=down)
    return {"ok": True, "clicks": clicks, "down": down}


@mcp.tool
async def browser_eval(expression: str, frame: Optional[str] = None) -> Dict[str, Any]:
    """Evaluate JavaScript in the page (or inside an iframe) and return the result.

    The tool that turns "the GUI misbehaves" into "here is the state that explains it".
    Reads app internals a screenshot cannot show -- a config constant, a component's
    state, whether a handler is registered -- and can set values to test a hypothesis
    without rebuilding anything.

    Args:
        expression: a JS expression ("document.title", "window.Common?.Utils") or a
            statement body ending in `return`. Both forms are accepted.
        frame: iframe chain from browser_frames, e.g. "#euroofficeFrame >>> iframe".
            Needed to reach code inside an embedded editor.

    Returns:
        the value, JSON-serialised. Values JS cannot serialise come back as a string.
    """
    page = await get_page()
    try:
        target = await _resolve_frame(page, frame)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    expr = expression.strip()
    # Accept both an expression and a body: wrapping an expression in a function is what
    # Playwright wants, but a caller who writes "return x" should not get a syntax error.
    candidates = ([f"() => {{ {expr} }}"] if "return" in expr
                  else [f"() => ({expr})", f"() => {{ {expr} }}"])
    last: Optional[str] = None
    for candidate in candidates:
        try:
            value = await target.evaluate(candidate)
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            return {"ok": True, "frame": frame, "value": value}
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    return {"ok": False, "error": last, "frame": frame}


@mcp.tool
async def browser_console(limit: int = 50, level: Optional[str] = None) -> Dict[str, Any]:
    """Recent browser console output and uncaught page errors.

    Collected continuously from the moment the browser starts, so a message logged before
    you asked is still here. Apps say a great deal here that never reaches the screen --
    the EuroOffice integration logs "[EO picker] ..." diagnostics, for instance -- and
    without this the whole channel was invisible.

    Args:
        limit: how many of the most recent lines to return.
        level: optional filter, e.g. "error", "warning", "log", "pageerror".
    """
    rows = list(_console)
    if level:
        rows = [r for r in rows if r.get("type") == level]
    rows = rows[-max(1, limit):]
    return {
        "ok": True,
        "count": len(rows),
        "buffered": len(_console),
        "capacity": CONSOLE_LIMIT,
        "messages": [
            {"at": time.strftime("%H:%M:%S", time.localtime(r["ts"])),
             "type": r["type"], "text": r["text"]}
            for r in rows
        ],
    }


@mcp.tool
async def session_timeline(session_id: Optional[str] = None) -> Dict[str, Any]:
    """Recorded actions with the elapsed time between them.

    Timing is evidence. A GUI bug that only appears when a step takes too long is
    invisible in a list of actions but obvious in the gaps between them -- this is what
    identified a 60s timeout as the cause of a wrong document, by showing 79.7s between
    picking a provider and confirming it.

    Args:
        session_id: which session; defaults to the current one.
    """
    sid = session_id or recorder.session_id
    path = TRAJECTORY_DIR / f"{sid}.jsonl"
    if not path.exists():
        return {"ok": False, "error": f"no such session: {sid}"}

    rows = [json.loads(line) for line in path.open() if line.strip()]
    if not rows:
        return {"ok": True, "session_id": sid, "steps": [], "total_s": 0}

    t0 = rows[0]["ts"]
    steps, prev = [], t0
    for r in rows:
        detail = {k: v for k, v in r.items() if k not in ("ts", "kind")}
        steps.append({
            "at": time.strftime("%H:%M:%S", time.localtime(r["ts"])),
            "since_prev_s": round(r["ts"] - prev, 1),
            "since_start_s": round(r["ts"] - t0, 1),
            "kind": r["kind"],
            "detail": json.dumps(detail, default=str)[:160],
        })
        prev = r["ts"]
    slowest = max(steps, key=lambda s: s["since_prev_s"])
    return {
        "ok": True,
        "session_id": sid,
        "total_s": round(rows[-1]["ts"] - t0, 1),
        "slowest_gap": {"kind": slowest["kind"], "since_prev_s": slowest["since_prev_s"]},
        "steps": steps,
    }


# --- 4. replay -------------------------------------------------------------------
def _target_expr(entry: Dict[str, Any]) -> str:
    """Render a recorded action's target as Playwright source, crossing iframes."""
    selector = entry.get("selector", "")
    frame = entry.get("frame")
    if frame:
        return f"page.frame_locator({frame!r}).locator({selector!r})"
    return f"page.locator({selector!r})"



@mcp.tool
async def list_sessions(limit: int = 20) -> Dict[str, Any]:
    """List recorded sessions that can be turned into replay scripts."""
    files = sorted(TRAJECTORY_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:limit]
    return {
        "ok": True,
        "current": recorder.session_id,
        "sessions": [
            {"session_id": f.stem, "actions": sum(1 for _ in f.open()),
             "modified": f.stat().st_mtime}
            for f in files
        ],
    }


@mcp.tool
async def emit_replay_script(session_id: Optional[str] = None) -> Dict[str, Any]:
    """Turn a recorded session into a standalone Playwright script.

    The result runs with NO model in the loop: deterministic, fast and cheap enough
    for CI. Only the browser_* steps are replayable this way -- vision steps from
    gui_task are emitted as comments, because a pixel click is not reproducible.

    Args:
        session_id: which session; defaults to the current one.
    """
    sid = session_id or recorder.session_id
    path = TRAJECTORY_DIR / f"{sid}.jsonl"
    if not path.exists():
        return {"ok": False, "error": f"no such session: {sid}"}

    entries = [json.loads(line) for line in path.open() if line.strip()]
    lines = [
        '"""Replay of GUI session %s -- generated by gui-bridge.' % sid,
        "",
        "Deterministic: no model is involved. Run inside the desktop container with",
        "    python3 %s" % f"/trajectories/replay_{sid}.py",
        '"""',
        "import os",
        "from playwright.sync_api import sync_playwright",
        "",
        "PROFILE = %r" % str(BROWSER_PROFILE),
        "",
        "with sync_playwright() as p:",
        "    ctx = p.firefox.launch_persistent_context(",
        "        PROFILE, headless=False, viewport={'width': 1280, 'height': 720})",
        "    page = ctx.pages[0] if ctx.pages else ctx.new_page()",
    ]

    replayable = 0
    for e in entries:
        kind = e.get("kind")
        if kind == "browser_goto":
            lines.append(f"    page.goto({e['url']!r}, wait_until={e.get('wait_for','load')!r})")
            replayable += 1
        elif kind == "browser_fill":
            if e.get("secret"):
                env = "GUI_SECRET_" + str(replayable)
                lines.append(
                    f"    {_target_expr(e)}.fill(os.environ[{env!r}])  # secret, not stored"
                )
            else:
                lines.append(f"    {_target_expr(e)}.fill({e['text']!r})")
            replayable += 1
        elif kind == "browser_click":
            if e.get("wait_for_nav"):
                lines += [
                    "    with page.expect_navigation():",
                    f"        {_target_expr(e)}.click()",
                ]
            else:
                lines.append(f"    {_target_expr(e)}.click()")
            replayable += 1
        elif kind == "browser_scroll":
            lines.append(f"    page.mouse.wheel({e.get('dx',0)}, {e.get('dy',0)})")
            replayable += 1
        elif kind == "browser_type":
            if e.get("secret"):
                env = "GUI_SECRET_" + str(replayable)
                lines.append(
                    f"    page.keyboard.type(os.environ[{env!r}], delay={e.get('delay',25)})"
                )
            else:
                lines.append(
                    f"    page.keyboard.type({e.get('text','')!r}, delay={e.get('delay',25)})"
                )
            replayable += 1
        elif kind == "browser_press":
            for _ in range(int(e.get("times", 1) or 1)):
                lines.append(f"    page.keyboard.press({e.get('key','')!r})")
            replayable += 1
        elif kind == "gui_task":
            lines.append(f"    # vision step, NOT replayable: {e.get('task','')!r}")
        elif kind == "agent_action":
            lines.append(f"    # vision action: {e.get('action')} {e.get('params')}")

    lines += [
        "    print('replay finished:', page.url)",
        "    ctx.close()",
        "",
    ]
    script = "\n".join(lines)
    out = TRAJECTORY_DIR / f"replay_{sid}.py"
    out.write_text(script)

    return {
        "ok": True,
        "session_id": sid,
        "script_path": str(out),
        "replayable_steps": replayable,
        "total_entries": len(entries),
        "script": script,
    }


# --- entrypoint -------------------------------------------------------------------
def main() -> None:
    log.info("gui-bridge on :%s | model=%s via %s", BRIDGE_PORT, MODEL, API_BASE)
    # fastmcp renamed this transport across 2.x; "http" is the current alias for
    # streamable-http. Try both so a base-image package bump cannot strand us.
    for transport in ("http", "streamable-http"):
        try:
            mcp.run(transport=transport, host="0.0.0.0", port=BRIDGE_PORT, path="/mcp/")
            return
        except (TypeError, ValueError) as exc:
            log.warning("transport %r rejected (%s), trying next", transport, exc)
    raise SystemExit("no usable MCP HTTP transport in this fastmcp build")


if __name__ == "__main__":
    main()
