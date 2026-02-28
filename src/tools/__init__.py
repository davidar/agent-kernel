"""Agent MCP tools package.

The agent's tool surface is deliberately minimal. Terminal interaction happens
through the terminal multiplexer (open/type/wait/close) and the SDK's built-in
file tools (Read/Grep/Glob) on terminal log files.

Custom tools: login, open, type, wait, close
SDK built-ins: Read, Write, Edit, Glob, Grep, TodoWrite, Skill
"""

from claude_agent_sdk import create_sdk_mcp_server

# === Lifecycle tools ===
from .awareness import (
    check_tick_end_conditions,
    is_logged_in,
    login,
    reset_tick_state,
)

# === Terminal multiplexer tools ===
from .terminal import close_tool, open_tool, type_tool, wait_tool

# ============================================================================
# AGENT_TOOLS: The MCP tools exposed to the agent.
#
# This is the entire custom tool surface. The agent interacts with terminals
# via open/type/wait/close, and reads output files with SDK file tools.
# ============================================================================

AGENT_TOOLS = [
    # Lifecycle (agent harness — login starts the tick)
    login,
    # Terminal multiplexer
    open_tool,
    type_tool,
    wait_tool,
    close_tool,
]

agent_server = create_sdk_mcp_server(
    name="agent",
    version="0.1.0",
    tools=AGENT_TOOLS,
)

__all__ = [
    "agent_server",
    "AGENT_TOOLS",
    "login",
    "is_logged_in",
    "reset_tick_state",
    "check_tick_end_conditions",
    "open_tool",
    "type_tool",
    "wait_tool",
    "close_tool",
]
