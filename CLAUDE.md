# Agent Kernel

A portable runtime for persistent Claude agents. Each agent is an identity living in a data repository — the kernel provides the tick loop, terminal TTYs in a container, and a minimal tool surface.

## Architecture

### Core Design: Terminal TTYs + CLIs

The agent interacts with the world through **numbered terminal TTYs** in a container, using `type`/`wait` tools and CLI programs. The shift is from "agent that calls APIs" to "agent that inhabits a computer."

**Key components:**
- **5 custom tools** (`login`, `open`, `type`, `wait`, `close`) + SDK built-ins (Read, Write, Edit, Glob, Grep, TodoWrite, Skill)
- **TTY manager** (`src/tty.py`): tmux-backed TTYs in the container. Output captured via capture-pane, input via send-keys. Continuous capture loop writes per-TTY files. Diff tracking via high-water marks — observe-before-act pattern.
- **Container** (`agent-kernel-{name}`): Persistent podman container. Named after the registered instance. Managed by `src/container.py`.
- **Shared filesystem**: Data repo mounted into container

**How it works:**
1. Agent sends keystrokes via `type(tty=0, expect="bash", text="command")`, observes output via `wait()`
2. TTYs are tmux sessions; per-TTY files in `tmp/sessions/tty_N/`: `screen`, `screen.ansi`, `raw`, `scrollback`, `status`
3. Observe-before-act: `type()` fails if any TTY has unseen output — must call `wait()` first

### Agent Runner (`src/agent.py`)

- **Agent SDK**: Uses `claude-agent-sdk`, model configurable via `system/agent_config.json` (default: `claude-opus-4-6`)
- **Stateless ticks**: Each tick starts with a fresh SDK session — no persistent context between ticks
- **Context limit enforcement**: At ~70% (140K tokens), agent is told to wrap up. If compaction is about to fire, the PreCompact hook blocks it and ends the tick immediately.
- **Watch mode**: `agent-kernel watch` auto-ticks on new messages
- **Context tracking**: Parses SDK transcript (`~/.claude/projects/.../session.jsonl`) for real metrics
- **Mid-tick notifications**: TickWatcher (`src/tick_watcher.py`) watches notification files, delivers via `client.query()`
- **Per-tick transcript**: SDK session transcript copied to `system/logs/tick-NNN.jsonl` at tick end
- **Error handling**: `ErrorDetector` (`src/errors.py`) classifies API errors. Transient errors retry with exponential backoff. Fatal errors create `system/paused` to prevent crash loops.

**SDK mid-conversation injection:**
- `client.query()` injects messages anytime during a session
- Use `receive_messages()` (not `receive_response()`) to keep the loop open for injections

### Tick Lifecycle Hooks (`src/hooks.py`)

Executable scripts in `system/hooks/` run at tick boundaries. This lets post-tick logic (git commit, status updates) live in the data repo where the agent can modify it.

**Directories:**
- `system/hooks/pre-tick/` — run after state update, before agent starts
- `system/hooks/pre-stop/` — mid-tick validation when agent wants to stop. Stdout lines become blocking issues (fail-open: failures/timeouts produce no issues). 30s timeout.
- `system/hooks/post-tick/` — run after transcript copied, before function returns

**Execution model:**
- Executable scripts run in sorted order **inside the container** via `podman exec`
- The container is started before pre-tick hooks, so all hooks use the container's toolchain
- Each script gets: `DATA_DIR`, `{PREFIX}_TICK` (always), plus `{PREFIX}_TICK_DURATION`, `{PREFIX}_TICK_LOG`, `{PREFIX}_LAST_MESSAGE`, `{PREFIX}_SESSION_ID`, `{PREFIX}_TICK_STATUS` (post-tick only)
- `{PREFIX}_TICK_STATUS` is `"normal"` (agent ended cleanly) or `"abnormal"` (interrupted/compacted)
- `{PREFIX}` is `hook_env_prefix` from config (default: `"AGENT"`)
- 60s timeout (pre-tick/post-tick), 30s timeout (pre-stop). Failures logged, never fatal. Dotfiles and `~` backup files ignored.
- After post-tick hooks, the kernel runs `git push` on the **host** (best-effort, needs host SSH keys)

### Logging (`src/logging_config.py`)

- Rotating log files: `system/logs/{process}.log` (daily, 14 days) + `{process}-current.log` (5MB)
- Per-tick transcripts: `system/logs/tick-NNN.jsonl` (SDK transcript copy)
- Usage: `setup_process_logging("watcher")` at entry point, `logger = get_logger(__name__)` in modules
- Console output goes to stderr with immediate flush

## Directory Structure

```
src/
├── agent.py           # Tick loop, SDK client, error handling, transcript copy
├── cli.py             # Host CLI (agent-kernel <cmd> <name>)
├── config.py          # init()/data_dir()/instance_name() accessor, state helpers, agent_config
├── container.py       # Container management (build, start, exec)
├── errors.py          # Error detection and classification
├── hooks.py           # Hook runner (pre-tick, pre-stop, post-tick)
├── logging_config.py  # Rotating log files, journalctl output
├── registry.py        # Instance registry (name → path mapping)
├── tick_watcher.py    # Mid-tick notification delivery (inotify + polling)
├── transcript.py      # SDK transcript parser (context metrics)
├── tty.py             # TTY manager (tmux sessions, capture loop, diff tracking)
├── types.py           # Data models (State)
├── watcher.py         # Poll-and-tick loop, crash notifications
└── tools/             # MCP tools (minimal surface)
    ├── __init__.py    # Server assembly — 5 tools
    ├── awareness.py   # login + tick-end conditions
    ├── terminal.py    # open, type, wait, close
    └── schedule.py    # Wake helpers (used by watcher)

tests/
├── Containerfile.test      # Minimal test container (ubuntu + tmux + procps)
├── conftest.py             # Fixtures (ephemeral test containers, data_dir)
├── test_config.py          # Config module tests
├── test_errors.py          # Error detection tests
├── test_hooks.py           # Hook runner tests
├── test_registry.py        # Instance registry tests
├── test_schedule.py        # Wake scheduling tests
├── test_terminal_tools.py  # type/wait tool tests
├── test_tick_watcher.py    # Tick watcher tests
├── test_tty.py             # TTY manager tests
└── test_types.py           # State dataclass tests
```

## Tools

### Custom MCP Tools (5)

**`login()`** — Call first every tick. Opens terminals per `startup.json`, reports any terminals lost to container restart.

**`open(command?)`** — Open a new terminal. Returns terminal number + capacity. Default command is bash.

**`type(tty, expect, text, enter?)`** — Send keystrokes to a terminal. `expect` is a point-and-call safety check — confirms what you think is running before sending keystrokes. Fails if any terminal has unseen output (observe-before-act). Auto-sends Enter for literal text; suppress with `enter=false`. For control characters: `"C-c"`, `"Tab"`, `"Enter"` (no auto-Enter).

**`wait(timeout?)`** — Block until output settles (~1.5s of silence). Returns diff summary for all open terminals. Max 60s. **Tip:** Call `type()` + `wait()` as parallel tool calls.

**`close(tty)`** — Kill and archive a terminal. Dead terminals (process exited) are auto-closed after `wait()` reports their exit. Live terminals must be closed before tick ends.

### SDK Built-in Tools

Read, Write, Edit, Glob, Grep, TodoWrite, Skill. Path-restricted to data repo. Bash is disabled — terminal TTYs replace it. Web search and fetching are done via CLI tools in the container (e.g. `ddgr`, `trafilatura`, `w3m`) — the agent installs these in its Containerfile.

### TTY Details

**Lifecycle:**
- Numbered (0-19) tmux sessions inside the container — survive process restarts
- Auto-created on first use. TTY 0 created by `login()` with startup command
- Per-TTY files in `tmp/sessions/tty_N/`: `screen`, `screen.ansi`, `raw`, `scrollback`, `status`
- `tmp/sessions/registry.json` — metadata flushed on every lifecycle event
- Archives saved to `system/logs/sessions/` as `tty_N-tick-NNN`
- Dead TTYs (process exited) auto-close after `wait()` reports them

**Diff tracking:**
- Capture loop runs every 0.5s. High-water marks track agent's last observation.
- Short output (≤20 lines) inline; long output head/tail with elision
- `wait()` is the only way to observe output. `type()` enforces observe-before-act.

**Recovery:**
- Container restart: stale TTYs reported via `login`, scrollback archived to `scrollback.prev`

**Completion detection:**
- Settle-based: polls every 0.5s, returns after ~1.5s of no new output
- Works with any program — no special markers needed

## Development

```bash
uv sync && uv run pre-commit install    # setup
uv run pre-commit run --all-files        # lint + type check
uv run pytest tests/                     # run tests
```

- **Adding a CLI**: Create executable in data repo — add to container PATH via Containerfile
- **Adding an MCP tool**: `@tool` decorator, async handler, register in `src/tools/__init__.py`. Prefer CLIs.
- **Service logs**: `journalctl --user -u agent-kernel-{name}`
- **Services**: `agent-kernel install <name>` creates the watcher service.
- **Container**: Containerfile in data repo at `system/container/Containerfile`. Auto-rebuild on content change. Stale containers and unused images pruned automatically.
