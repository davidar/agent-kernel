"""Main agent runner."""

import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
    HookMatcher,
)
from claude_agent_sdk.types import ContextUsageResponse, HookJSONOutput, ThinkingConfig

from .config import data_dir, ensure_dirs, get_container_name, get_state, save_state, get_agent_config
from .container import ensure_ready
from .tools import (
    agent_server,
    AGENT_TOOLS,
    reset_tick_state,
    check_tick_end_conditions,
    is_logged_in,
)
from .logging_config import setup_process_logging, get_logger
from .hooks import run_hooks, run_hooks_collect
from .tick_watcher import TickWatcher
from .tty import init_tty_manager, shutdown_tty_manager
from .errors import ErrorDetector
from .session_store import TickJsonlStore
from collections.abc import Callable

# Logger is configured lazily - either by main() or by watcher
logger = get_logger(__name__)


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

    # Per-tick transcript file. The TickJsonlStore writes here as frames stream
    # in, so we never have to reach into ~/.claude/projects/ to read the
    # transcript (the SDK still writes its own copy there).
    tick_log_path = logs_dir / f"tick-{tick_number:03d}.jsonl"

    # Start container before hooks so they run inside it
    container_name = get_container_name()
    build_error = await ensure_ready()

    await run_hooks(
        "pre-tick",
        {f"{hook_env_prefix}_TICK": str(tick_number)},
        container=container_name,
    )

    # Context limit enforcement: PreCompact hook blocks compaction and sets flag
    # to end the tick immediately (instead of losing mid-tick context).
    context_limit_hit = False

    def _set_context_limit():
        nonlocal context_limit_hit
        context_limit_hit = True

    # Thinking config: default to adaptive + summarized display. If the
    # data repo sets an explicit `max_thinking_tokens`, honor it via the
    # "enabled" mode (works on models that still support manual budgets;
    # Opus 4.7 ignores the budget but the request itself is fine).
    # `display: "summarized"` is what Opus 4.7+ needs to return any
    # thinking text at all and is harmless on older models.
    thinking_cfg: ThinkingConfig
    budget = agent_config.get("max_thinking_tokens")
    if isinstance(budget, int) and budget > 0:
        thinking_cfg = {
            "type": "enabled",
            "budget_tokens": budget,
            "display": "summarized",
        }
    else:
        thinking_cfg = {"type": "adaptive", "display": "summarized"}

    options = ClaudeAgentOptions(
        model=agent_config["model"],
        system_prompt=prompt,
        mcp_servers={server_name: agent_server},
        thinking=thinking_cfg,
        session_store=TickJsonlStore(tick_log_path),
        allowed_tools=[f"{mcp_prefix}{t.name}" for t in AGENT_TOOLS]
        + [
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "TodoWrite",
            "Skill",
        ],
        disallowed_tools=[
            "Bash",
            "BashOutput",
            "KillBash",
            "WebSearch",
            "WebFetch",
            "NotebookEdit",
            "Task",
            "AskUserQuestion",
        ],
        permission_mode="acceptEdits",
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
    last_context_usage: ContextUsageResponse | None = None
    tick_active = True

    # Context warning threshold — agent is told to wrap up beyond this.
    CONTEXT_WARN_PERCENT = 70.0

    watcher: TickWatcher | None = None
    post_tick_hooks_done = False

    try:
        async with ClaudeSDKClient(options=options) as client:
            tty_mgr = await init_tty_manager(tick_number=tick_number, build_error=build_error)

            async def _notify_and_interrupt(msg: str) -> object:
                tty_mgr.interrupt()
                return await client.query(msg)

            watcher = TickWatcher(notify_callback=_notify_and_interrupt)
            await watcher.start()

            # Check for a file-based initial query (e.g. written by a post-tick hook)
            initial_query_file = data_dir() / "system" / "initial_query.md"
            if initial_query_file.exists():
                initial_query = initial_query_file.read_text().strip()
                initial_query_file.unlink()
                logger.info("Using initial query from %s", initial_query_file.name)
            else:
                initial_query = agent_config.get("initial_query", "Tick {tick} starting. Call login() to begin.")
                initial_query = initial_query.format(tick=tick_number, data_dir=data_dir())
            await client.query(initial_query)

            message_iter = client.receive_messages().__aiter__()
            while True:
                try:
                    message = await message_iter.__anext__()
                except StopAsyncIteration:
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

                    # Context approaching limit — tell agent to wrap up.
                    # Use the SDK's get_context_usage() instead of parsing the
                    # transcript file ourselves; it's the same data the CLI's
                    # /context command shows.
                    try:
                        last_context_usage = await client.get_context_usage()
                    except Exception as e:
                        logger.debug("get_context_usage() failed: %s", e)
                        last_context_usage = None

                    if (
                        not context_warning_sent
                        and last_context_usage is not None
                        and last_context_usage["percentage"] >= CONTEXT_WARN_PERCENT
                    ):
                        context_warning_sent = True
                        pct = last_context_usage["percentage"]
                        used = last_context_usage["totalTokens"]
                        cap = last_context_usage["maxTokens"]
                        logger.warning("Context at %.0f%% — telling agent to wrap up", pct)
                        await client.query(
                            f"Context at {pct:.0f}% ({used:,}/{cap:,} tokens). "
                            "Wrap up now — save your work, close TTYs, and end the tick. "
                            "The tick will be forcibly terminated if context fills up."
                        )

                elif isinstance(message, ResultMessage):
                    # Error detection: ResultMessage.is_error
                    err = error_detector.check_result_error(message.is_error, message.result or "")
                    if err:
                        logger.warning("Result error: %s (%s)", err.category, err.detection_method)

                    # Fatal error — end tick (watcher will continue)
                    if error_detector.is_fatal:
                        logger.error("Fatal error detected — ending tick")
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
                        script_issues = await run_hooks_collect(
                            "pre-stop", script_env, container=container_name, timeout=30
                        )
                        issues.extend(script_issues)

                    if not last_assistant_text.strip():
                        issues.append("Send a final message before the tick can end.")

                    if issues:
                        nag = "Tick can't end yet:\n" + "\n".join(f"- {i}" for i in issues)
                        await client.query(nag)
                        continue

                    tick_active = False
                    break

            if watcher:
                await watcher.stop()

        # --- Post-session (client closed, transcript complete) ---
        tick_end = datetime.now()
        duration = (tick_end - tick_start).total_seconds()

        state = get_state()
        state.last_tick_end = tick_end.isoformat()
        save_state(state)

        # The TickJsonlStore has been streaming entries to tick_log_path during
        # the tick; if no frames arrived (e.g. very early failure) the file
        # will not exist.
        transcript_path = tick_log_path if tick_log_path.exists() else None

        # Log usage summary from the last get_context_usage() snapshot we took
        # inside the loop (the client is closed by now, so we can't ask again).
        usage_info = ""
        if last_context_usage is not None:
            ctx = last_context_usage["totalTokens"]
            cap = last_context_usage["maxTokens"]
            pct = last_context_usage["percentage"]
            usage_info = f" | Context: {pct:.0f}% ({ctx:,}/{cap:,})"

        logger.info("=" * 60)
        logger.info("TICK %d COMPLETE (%.1fs)%s", tick_number, duration, usage_info)
        logger.info("=" * 60)

        tick_status = "abnormal" if tick_active else "normal"

        await run_hooks(
            "post-tick",
            {
                f"{hook_env_prefix}_TICK": str(tick_number),
                f"{hook_env_prefix}_TICK_DURATION": f"{duration:.1f}",
                f"{hook_env_prefix}_TICK_LOG": str(transcript_path or ""),
                f"{hook_env_prefix}_LAST_MESSAGE": (last_assistant_text or "")[:2000],
                f"{hook_env_prefix}_SESSION_ID": tick_session_id,
                f"{hook_env_prefix}_TICK_STATUS": tick_status,
            },
            container=container_name,
        )
        post_tick_hooks_done = True

        # Push data repo (best-effort, runs on host for SSH key access)
        if (data_dir() / ".git").is_dir():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    str(data_dir()),
                    "push",
                    "--quiet",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                if proc.returncode != 0:
                    logger.warning("git push failed: %s", stderr.decode().strip())
            except Exception:
                logger.warning("git push failed", exc_info=True)

    except KeyboardInterrupt:
        duration = (datetime.now() - tick_start).total_seconds()
        logger.info("TICK %d INTERRUPTED (%.1fs)", tick_number, duration)

    except Exception as e:
        duration = (datetime.now() - tick_start).total_seconds()
        err = ErrorDetector.classify_exception(e)
        logger.error("TICK %d FAILED (%.1fs): %s: %s", tick_number, duration, err.category, err.text)

        if err.fatal:
            logger.error("Fatal exception — tick will not retry")
            # fall through to finally (which runs post-tick hooks)
        else:
            raise

    finally:
        if watcher and watcher.running:
            await watcher.stop()
        await shutdown_tty_manager()

        # Run post-tick hooks if they didn't run in the happy path
        if not post_tick_hooks_done:
            duration = (datetime.now() - tick_start).total_seconds()
            logger.warning("Running post-tick hooks after abnormal exit")
            try:
                await run_hooks(
                    "post-tick",
                    {
                        f"{hook_env_prefix}_TICK": str(tick_number),
                        f"{hook_env_prefix}_TICK_DURATION": f"{duration:.1f}",
                        f"{hook_env_prefix}_TICK_LOG": "",
                        f"{hook_env_prefix}_LAST_MESSAGE": (last_assistant_text or "")[:2000],
                        f"{hook_env_prefix}_SESSION_ID": tick_session_id,
                        f"{hook_env_prefix}_TICK_STATUS": "abnormal",
                    },
                    container=container_name,
                )
            except Exception:
                logger.warning("Post-tick hooks failed during cleanup", exc_info=True)

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
