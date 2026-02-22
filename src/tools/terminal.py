"""Terminal multiplexer tools — type and wait.

Only two custom tools. Everything else uses the SDK's built-in tools
(Read, Grep, Glob) on the TTY log files.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from ..tty import _is_tmux_key, get_tty_manager
from .awareness import is_logged_in


@tool(
    "type",
    """Send keystrokes to a terminal TTY.

For literal text, just pass the string — Enter is sent automatically after
literal text. For control characters, use tmux key name syntax: "Enter"
for return, "C-c" for Ctrl-C, "C-d" for EOF, "Tab" for tab, "Up"/"Down"
for arrow keys, etc.

TTYs are auto-created on first use (default: bash shell).

IMPORTANT: You must specify `expect` — the command you believe is currently
running in this TTY (e.g. "bash", "chat", "bsky", "python3"). This is a
point-and-call safety check: the tool will fail if the actual running
command doesn't match, preventing you from accidentally sending keystrokes
to the wrong process.

IMPORTANT: This tool will fail if any TTY has unseen output. You must
call wait() first to observe terminal output before sending more input.

Examples:
  type(tty=0, expect="bash", text="echo hello")    # types text + Enter
  type(tty=0, expect="bash", text="C-c")           # sends Ctrl-C
  type(tty=0, expect="bash", text="ls", enter=false)  # types without Enter
  type(tty=1, expect="chat", text="hello there")   # sends to chat""",
    {
        "type": "object",
        "properties": {
            "tty": {"type": "integer"},
            "expect": {"type": "string"},
            "text": {"type": "string"},
            "enter": {"type": "boolean"},
        },
        "required": ["tty", "expect", "text"],
    },
)
async def type_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Send keystrokes to a terminal TTY."""
    if not is_logged_in():
        return _error("You must call login() first before using terminal tools.")

    tty_id = args.get("tty", 0)
    text = args.get("text", "")
    expect = args.get("expect", "")

    if not text:
        return _error("text is required")
    if not expect:
        return _error("expect is required — state what command you think is running in this TTY")

    manager = get_tty_manager()

    # Observe-before-act: fail if any TTY has unseen output
    if manager.has_unseen_changes():
        return _error(
            "Terminal TTYs have unseen output. Call wait() first to observe output before sending more input."
        )

    # Point-and-call: verify the agent's expectation matches reality
    if tty_id in manager.ttys:
        tty = manager.ttys[tty_id]
        actual = tty.current_command or tty.command
        if expect.lower() != actual.lower():
            return _error(
                f"Point-and-call mismatch: you expected '{expect}' but tty {tty_id} is running '{actual}'. "
                f"Check which TTY you meant to use."
            )

    try:
        await manager.send_keys(tty_id, text)

        # Auto-send Enter after literal text (not tmux key names)
        should_enter = not _is_tmux_key(text) and args.get("enter", True)
        if should_enter:
            await manager.send_keys(tty_id, "Enter")

        return _text("Keystrokes sent.")
    except RuntimeError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to send keystrokes: {e}")


@tool(
    "wait",
    """Wait for terminal output to settle, then return a summary of all TTYs.

This is the only way to observe terminal output. After sending input with
type(), call wait() to see what happened. The tool blocks until output
settles (no new output for ~1.5s) or the timeout expires.

Returns a status summary for every open TTY showing new output as diffs.
Short output is shown inline; long output shows head/tail with full
content available in the scrollback file.

The timeout has a maximum of 60 seconds regardless of the value passed.

Tip: Call type() and wait() as parallel tool calls to achieve send-and-wait
with no extra round-trip latency.""",
    {
        "timeout": int,
    },
)
async def wait_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Wait for terminal activity or timeout."""
    if not is_logged_in():
        return _error("You must call login() first before using terminal tools.")

    timeout = args.get("timeout", 30)

    manager = get_tty_manager()

    try:
        summary = await manager.wait_for_activity(timeout)
        return _text(summary)
    except Exception as e:
        return _error(f"Wait failed: {e}")


@tool(
    "close",
    """Force-close a terminal TTY. Kills the running process (if any),
archives the TTY's scrollback, and removes the TTY.

Use this when a process is stuck and can't be exited normally (e.g.
after Ctrl-C fails), or to clean up TTYs you're done with.

All TTYs must be closed before the tick can end.""",
    {
        "type": "object",
        "properties": {
            "tty": {"type": "integer"},
        },
        "required": ["tty"],
    },
)
async def close_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Force-close a terminal TTY."""
    if not is_logged_in():
        return _error("You must call login() first before using terminal tools.")

    tty_id = args.get("tty", 0)

    manager = get_tty_manager()
    closed = await manager.close_tty(tty_id)
    if closed:
        return _text(f"TTY {tty_id} closed and archived.")
    else:
        return _error(f"TTY {tty_id} not found.")


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
