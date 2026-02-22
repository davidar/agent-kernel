"""Main agent runner."""

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
    HookMatcher,
)
from claude_agent_sdk.types import HookJSONOutput

from .config import data_dir, ensure_dirs, get_state, save_state, get_agent_config
from .tools import (
    agent_server,
    AGENT_TOOLS,
    reset_tick_state,
    check_tick_end_conditions,
    run_tick_end_script,
    is_logged_in,
)
from .logging_config import setup_process_logging, get_logger
from .hooks import run_hooks
from .tick_watcher import TickWatcher
from .tty import init_tty_manager, shutdown_tty_manager
from .errors import ErrorDetector
from .transcript import parse_transcript_metrics, get_transcript_path
from collections.abc import Callable

# Monkey-patch SDK message parser to handle unknown message types gracefully.
# claude-agent-sdk 0.1.39 raises MessageParseError on unknown types (e.g. "rate_limit_event")
# inside an async generator, which kills the session permanently.
# See: anthropics/claude-agent-sdk-python#583
import claude_agent_sdk._internal.message_parser as _mp  # noqa: E402

_original_parse = _mp.parse_message


def _patched_parse(data):
    try:
        return _original_parse(data)
    except Exception as e:
        if "Unknown message type" in str(e):
            from claude_agent_sdk.types import SystemMessage

            logging.getLogger(__name__).debug(f"Ignoring unknown message type: {data.get('type', '?')}")
            return SystemMessage(subtype="unknown", data=data)
        raise


_mp.parse_message = _patched_parse

# Logger is configured lazily - either by main() or by watcher
logger = get_logger(__name__)


# Workaround for Claude Code bug: WebFetch (and potentially other built-in tools)
# can hang indefinitely with no timeout and no error.
# See: anthropics/claude-code#8980, #11650, #12113
TOOL_CALL_TIMEOUT = 300  # 5 minutes — generous; normal tool calls complete in seconds

# Retry configuration for transient API errors (500, rate limit, overloaded)
# Exponential backoff: 10s, 20s, 40s, 80s, 160s, 320s, 600s, 600s, ...
MAX_API_RETRIES = 10
API_BACKOFF_BASE = 10  # seconds
API_BACKOFF_MAX = 600  # cap at 10 minutes


def _write_live_status(status: str, tick: int | None = None, tool: str | None = None):
    """Write live status for external consumers."""
    data: dict[str, str | int] = {
        "status": status,
        "updated": datetime.now().isoformat(),
    }
    if tick is not None:
        data["tick"] = tick
    if tool is not None:
        data["tool"] = tool
    (data_dir() / "tmp" / "live_status.json").write_text(json.dumps(data))


def _make_precompact_hook(set_context_limit_hit: Callable[[], None]):
    """Create a PreCompact hook that blocks compaction and sets a flag.

    Instead of allowing SDK auto-compaction (which would lose mid-tick context),
    we block it and signal the message loop to end the tick immediately.
    """

    async def _precompact_hook(input, _tool_use_id, _context) -> HookJSONOutput:
        trigger = input.get("trigger", "auto")
        logger.warning(f"Context limit hit ({trigger}) — blocking compaction, ending tick")
        set_context_limit_hit()
        return {"continue_": False}

    return _precompact_hook


# Cached system prompt — only rebuilt when prompt.md changes on disk
_cached_prompt: str | None = None
_cached_prompt_mtime: float = 0.0


def _get_system_prompt() -> str:
    """Return system prompt, rebuilding only if prompt.md changes."""
    global _cached_prompt, _cached_prompt_mtime

    prompt_file = data_dir() / "system" / "prompt.md"
    try:
        prompt_mtime = prompt_file.stat().st_mtime
    except OSError:
        prompt_mtime = 0.0

    if _cached_prompt is None or prompt_mtime != _cached_prompt_mtime:
        _cached_prompt = prompt_file.read_text().strip() if prompt_file.exists() else ""
        _cached_prompt_mtime = prompt_mtime
    return _cached_prompt


def _load_agents(agent_config: dict) -> dict[str, AgentDefinition]:
    """Load subagent definitions from data repo JSON, with empty fallback."""
    agents_file = data_dir() / "system" / "agents.json"
    if not agents_file.exists():
        return {}
    try:
        raw = json.loads(agents_file.read_text())
        result = {}
        for name, defn in raw.items():
            result[name] = AgentDefinition(
                description=defn.get("description", ""),
                prompt=defn.get("prompt", ""),
                tools=defn.get("tools", []),
                model=defn.get("model"),
            )
        return result
    except (OSError, json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to load agents from %s: %s", agents_file, e)
        return {}


def _write_pause_file(tick_number: int, reason: str) -> Path:
    """Create a pause file to prevent crash loops after fatal errors."""
    pause_file = data_dir() / "system" / "paused"
    pause_file.write_text(
        f"Paused at {datetime.now().isoformat()} due to {reason}.\n"
        f"Tick: {tick_number}\n"
        f"\n"
        f"Options:\n"
        f"1. Delete this file to retry (may fail again)\n"
        f"2. Investigate the tick log for root cause\n"
    )
    return pause_file


def _copy_tick_transcript(session_id: str, tick_number: int) -> Path | None:
    """Copy the SDK session transcript to per-tick log directory."""
    if not session_id:
        return None
    src = get_transcript_path(session_id)
    if not src:
        return None
    dst = data_dir() / "system" / "logs" / f"tick-{tick_number:03d}.jsonl"
    try:
        shutil.copy2(src, dst)
        return dst
    except OSError as e:
        logger.warning("Failed to copy transcript: %s", e)
        return None


async def run_tick():
    """Run a single agent tick."""
    ensure_dirs()
    logs_dir = data_dir() / "system" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Update state
    state = get_state()
    state.tick_count += 1
    tick_number = state.tick_count
    tick_start = datetime.now()
    state.last_tick = tick_start.isoformat()

    if tick_number == 1 or not state.first_tick_date:
        state.first_tick_date = tick_start.strftime("%Y-%m-%d")

    save_state(state)

    # Load agent configuration
    agent_config = get_agent_config()
    server_name = "agent"
    mcp_prefix = f"mcp__{server_name}__"
    hook_env_prefix = agent_config.get("hook_env_prefix", "AGENT")

    prompt = _get_system_prompt()

    logger.info("=" * 60)
    logger.info("TICK %d — Started: %s", tick_number, tick_start.isoformat())
    logger.info("=" * 60)

    reset_tick_state()

    await run_hooks(
        "pre-tick",
        {f"{hook_env_prefix}_TICK": str(tick_number)},
    )

    agents = _load_agents(agent_config)

    # Context limit enforcement: PreCompact hook blocks compaction and sets flag
    # to end the tick immediately (instead of losing mid-tick context).
    context_limit_hit = False

    def _set_context_limit():
        nonlocal context_limit_hit
        context_limit_hit = True

    options = ClaudeAgentOptions(
        model=agent_config["model"],
        system_prompt=prompt,
        mcp_servers={server_name: agent_server},
        max_thinking_tokens=agent_config["max_thinking_tokens"],
        allowed_tools=[f"{mcp_prefix}{t.name}" for t in AGENT_TOOLS]
        + [
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "NotebookEdit",
            "WebSearch",
            "WebFetch",
            "TodoWrite",
            "Task",
            "Skill",
        ],
        agents=agents,
        permission_mode="acceptEdits",
        disallowed_tools=["Bash", "BashOutput", "KillBash"],
        cwd=str(data_dir()),
        add_dirs=[
            str(Path(__file__).parent),  # src/ (kernel source)
            str(Path(__file__).parent.parent / "docs"),  # docs/
            str(data_dir() / "tmp" / "sessions"),
        ],
        resume=None,
        hooks={  # type: ignore[arg-type]
            "PreCompact": [HookMatcher(hooks=[_make_precompact_hook(_set_context_limit)])],
        },
        setting_sources=["project"],
        extra_args={"strict-mcp-config": None},
    )

    api_retries = 0
    error_detector = ErrorDetector()
    context_warning_sent = False
    last_assistant_text = ""
    tick_session_id = ""  # captured from init message
    tick_active = True

    watcher: TickWatcher | None = None

    try:
        async with ClaudeSDKClient(options=options) as client:
            tty_mgr = await init_tty_manager(tick_number=tick_number)

            async def _notify_and_interrupt(msg: str) -> object:
                tty_mgr.interrupt()
                return await client.query(msg)

            watcher = TickWatcher(notify_callback=_notify_and_interrupt)
            await watcher.start()

            initial_query = agent_config.get("initial_query", "Tick {tick} starting. Call login() to begin.")
            await client.query(initial_query.format(tick=tick_number, data_dir=data_dir()))

            message_iter = client.receive_messages().__aiter__()
            while True:
                try:
                    message = await asyncio.wait_for(message_iter.__anext__(), timeout=TOOL_CALL_TIMEOUT)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning("Tool call hung — no message for %ds. Terminating tick.", TOOL_CALL_TIMEOUT)
                    break
                except Exception as e:
                    logger.warning("SDK message stream error: %s. Terminating tick.", e)
                    break

                if context_limit_hit:
                    logger.warning("Context limit hit — ending tick to avoid compaction")
                    break

                # Capture session ID from init message
                if hasattr(message, "subtype") and message.subtype == "init":  # type: ignore[union-attr]
                    data = getattr(message, "data", {}) or {}
                    sid = data.get("session_id") if isinstance(data, dict) else None
                    if not sid:
                        sid = getattr(message, "session_id", None)
                    if sid:
                        tick_session_id = sid
                        logger.info("Session: %s", tick_session_id)

                if isinstance(message, AssistantMessage):
                    # Error detection: message.error field
                    err = error_detector.check_message_error(message.error)
                    if err:
                        logger.warning("API error: %s (%s)", err.category, err.detection_method)

                    for block in message.content:
                        if isinstance(block, TextBlock):
                            last_assistant_text = block.text
                            # Error detection: string matching fallback
                            err = error_detector.check_text_content(block.text)
                            if err:
                                logger.warning("Error in text: %s (%s)", err.category, err.detection_method)

                        elif isinstance(block, ToolUseBlock):
                            short_tool = block.name.replace(mcp_prefix, "")
                            _write_live_status(f"Tick {tick_number}: {short_tool}", tick=tick_number, tool=short_tool)

                    # Context approaching limit — tell agent to wrap up
                    if not context_warning_sent and tick_session_id:
                        metrics = parse_transcript_metrics(tick_session_id)
                        context_tokens = metrics.get("context_tokens", 0)
                        if context_tokens >= 140000:
                            context_warning_sent = True
                            usage_pct = (context_tokens / 200000) * 100
                            logger.warning("Context at %.0f%% — telling agent to wrap up", usage_pct)
                            await client.query(
                                f"Context at {usage_pct:.0f}% ({context_tokens:,} tokens). "
                                "Wrap up now — save your work, close TTYs, and end the tick. "
                                "The tick will be forcibly terminated if context fills up."
                            )

                elif isinstance(message, ResultMessage):
                    # Error detection: ResultMessage.is_error
                    err = error_detector.check_result_error(message.is_error, message.result or "")
                    if err:
                        logger.warning("Result error: %s (%s)", err.category, err.detection_method)

                    # Fatal error → pause
                    if error_detector.is_fatal:
                        _write_pause_file(tick_number, "fatal error")
                        logger.error("FATAL error — pausing to prevent crash loop")
                        tick_active = False
                        break

                    # Non-fatal API error → retry with backoff (preserves context)
                    if error_detector.error:
                        if api_retries < MAX_API_RETRIES:
                            api_retries += 1
                            delay = min(API_BACKOFF_BASE * (2 ** (api_retries - 1)), API_BACKOFF_MAX)
                            logger.warning(
                                "Transient error (%s): retry %d/%d in %ds",
                                error_detector.error.category,
                                api_retries,
                                MAX_API_RETRIES,
                                delay,
                            )
                            error_detector.reset()
                            await asyncio.sleep(delay)
                            await client.query(
                                "The previous API call hit a transient error. Continue where you left off."
                            )
                            continue
                        else:
                            logger.error("Retries exhausted (%d). Ending tick.", MAX_API_RETRIES)
                            break

                    # Tick-end conditions: kernel checks + data repo script
                    issues = check_tick_end_conditions()

                    if is_logged_in():
                        script_env = {
                            f"{hook_env_prefix}_TICK": str(tick_number),
                            f"{hook_env_prefix}_LAST_MESSAGE": (last_assistant_text or "")[:2000],
                            f"{hook_env_prefix}_SESSION_ID": tick_session_id,
                        }
                        script_issues = await run_tick_end_script(script_env)
                        issues.extend(script_issues)

                    if not last_assistant_text.strip():
                        issues.append("Send a final message before the tick can end.")

                    if issues:
                        nag = "Tick can't end yet:\n" + "\n".join(f"- {i}" for i in issues)
                        await client.query(nag)
                        continue

                    tick_active = False
                    break

            # Abnormal termination: run data repo's abnormal-exit script
            if tick_active:
                script_path = data_dir() / "system" / "abnormal-exit"
                if script_path.exists() and os.access(script_path, os.X_OK):
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            str(script_path),
                            env={
                                **os.environ,
                                "DATA_DIR": str(data_dir()),
                                f"{hook_env_prefix}_TICK": str(tick_number),
                            },
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                        if stdout.strip():
                            logger.info(stdout.decode().strip())
                    except Exception:
                        logger.exception("abnormal-exit script failed")

            if watcher:
                await watcher.stop()

        # --- Post-session (client closed, transcript complete) ---
        tick_end = datetime.now()
        duration = (tick_end - tick_start).total_seconds()

        state = get_state()
        state.last_tick_end = tick_end.isoformat()
        save_state(state)

        # Copy SDK transcript to per-tick log
        transcript_path = _copy_tick_transcript(tick_session_id, tick_number)

        # Log usage summary
        usage_info = ""
        if tick_session_id:
            metrics = parse_transcript_metrics(tick_session_id)
            if metrics:
                ctx = metrics.get("context_tokens", 0)
                pct = (ctx / 200000) * 100 if ctx else 0
                usage_info = f" | Context: {pct:.0f}% ({ctx:,}/200,000)"

        logger.info("=" * 60)
        logger.info("TICK %d COMPLETE (%.1fs)%s", tick_number, duration, usage_info)
        logger.info("=" * 60)

        await run_hooks(
            "post-tick",
            {
                f"{hook_env_prefix}_TICK": str(tick_number),
                f"{hook_env_prefix}_TICK_DURATION": f"{duration:.1f}",
                f"{hook_env_prefix}_TICK_LOG": str(transcript_path or ""),
                f"{hook_env_prefix}_LAST_MESSAGE": (last_assistant_text or "")[:2000],
                f"{hook_env_prefix}_SESSION_ID": tick_session_id,
            },
        )

    except KeyboardInterrupt:
        duration = (datetime.now() - tick_start).total_seconds()
        logger.info("TICK %d INTERRUPTED (%.1fs)", tick_number, duration)

    except Exception as e:
        duration = (datetime.now() - tick_start).total_seconds()
        err = ErrorDetector.classify_exception(e)
        logger.error("TICK %d FAILED (%.1fs): %s: %s", tick_number, duration, err.category, err.text)

        if err.fatal:
            _write_pause_file(tick_number, f"exception: {type(e).__name__}")
            return

        raise

    finally:
        if watcher and watcher.running:
            await watcher.stop()
        await shutdown_tty_manager()
        # Wipe tmp/ so nothing lingers between ticks
        tmp_dir = data_dir() / "tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    """Entry point for running a tick directly (not via watcher)."""
    if not logging.getLogger().handlers:
        setup_process_logging("agent")
    asyncio.run(run_tick())


if __name__ == "__main__":
    main()
