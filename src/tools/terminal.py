"""Terminal multiplexer tools — open, type, wait, close.

The agent interacts with terminals via these tools. Everything else uses
the SDK's built-in tools (Read, Grep, Glob) on the terminal log files.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from ..tty import MAX_TTYS, _is_tmux_key, get_tty_manager
from .awareness import is_logged_in


@tool(
    "open",
    """Open a new terminal. Use this to run things in parallel — long builds,
background servers, separate interactive sessions. Returns the terminal
number to use with type() and close().

Default command is bash. Pass a command to launch it directly
(e.g. open(command="python3")).""",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
        },
    },
)
async def open_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Open a new terminal."""
    if not is_logged_in():
        return _error("You must call login() first before using terminal tools.")

    command = args.get("command", "bash")
    manager = get_tty_manager()

    try:
        # Find next available ID
        tty_id = next(i for i in range(MAX_TTYS) if i not in manager.ttys)
    except StopIteration:
        return _error(f"Terminal limit reached ({MAX_TTYS}).")

    try:
        await manager.get_or_create_tty(tty_id, command=command)
        remaining = MAX_TTYS - len(manager.ttys)
        return _text(f"Opened terminal {tty_id} ({command}). {remaining} more available.")
    except RuntimeError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to open terminal: {e}")


@tool(
    "type",
    """Send keystrokes to a terminal.

For literal text, just pass the string — Enter is sent automatically after
literal text. For control characters, use tmux key name syntax: "Enter"
for return, "C-c" for Ctrl-C, "C-d" for EOF, "Tab" for tab, "Up"/"Down"
for arrow keys, etc.

IMPORTANT: You must specify `expect` — the command you believe is currently
running in this terminal (e.g. "bash", "chat", "bsky", "python3"). This is
a point-and-call safety check: the tool will fail if the actual running
command doesn't match, preventing you from accidentally sending keystrokes
to the wrong process.

IMPORTANT: This tool will fail if any terminal has unseen output. You must
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
    """Send keystrokes to a terminal."""
    if not is_logged_in():
        return _error("You must call login() first before using terminal tools.")

    tty_id = args.get("tty", 0)
    text = args.get("text", "")
    expect = args.get("expect", "")

    if not text:
        return _error("text is required")
    if not expect:
        return _error("expect is required — state what command you think is running in this terminal")

    manager = get_tty_manager()

    # Observe-before-act: fail if any terminal has unseen output
    if manager.has_unseen_changes():
        return _error("Terminals have unseen output. Call wait() first to observe output before sending more input.")

    # Terminal must exist (created by login() or open())
    if tty_id not in manager.ttys:
        return _error(f"Terminal {tty_id} does not exist. Use open() to create a new terminal.")

    # Point-and-call: verify the agent's expectation matches reality
    tty = manager.ttys[tty_id]
    actual = tty.current_command or tty.command
    if expect.lower() != actual.lower():
        return _error(
            f"Point-and-call mismatch: you expected '{expect}' but terminal {tty_id} is running '{actual}'. "
            f"Check which terminal you meant to use."
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
    """Wait for terminal output to settle, then return a summary of all terminals.

This is the only way to observe terminal output. After sending input with
type(), call wait() to see what happened. The tool blocks until output
settles (no new output for ~1.5s) or the timeout expires.

Returns a status summary for every open terminal showing new output as
diffs. Short output is shown inline; long output shows head/tail with
full content in the scrollback file.

To see current screen: Read("tmp/sessions/tty_N/screen")
To read full output: Read("tmp/sessions/tty_N/scrollback")
To search output: Grep("pattern", "tmp/sessions/")
To discover terminals: Glob("tmp/sessions/tty_*/status")

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
    """Force-close a terminal. Kills the running process (if any),
archives the scrollback, and removes the terminal.

Use this when a process is stuck and can't be exited normally (e.g.
after Ctrl-C fails), or to clean up terminals you're done with.

All terminals must be closed before the tick can end.""",
    {
        "type": "object",
        "properties": {
            "tty": {"type": "integer"},
        },
        "required": ["tty"],
    },
)
async def close_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Force-close a terminal."""
    if not is_logged_in():
        return _error("You must call login() first before using terminal tools.")

    tty_id = args.get("tty", 0)

    manager = get_tty_manager()
    closed = await manager.close_tty(tty_id)
    if closed:
        return _text(f"Terminal {tty_id} closed and archived.")
    else:
        return _error(f"Terminal {tty_id} not found.")


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
