# agent-kernel

A portable runtime for persistent Claude agents.

Most agent frameworks give the model a Bash tool: run a command, wait for it to finish, get stdout back as a string. That works for one-shot scripts, but it's a dead end for anything interactive. You can't drive a REPL, Ctrl-C a hung process, carry on a conversation in a chat client, or navigate a text adventure.

agent-kernel gives the agent **terminal TTYs** instead — persistent tmux sessions inside a podman container. The agent sends keystrokes, reads screen output, sends more keystrokes. It can start a Python REPL and run expressions incrementally. It can open a chat client and have a conversation. It can launch a long-running process, check on it later, and interrupt it if needed. The interaction model is the same as a human sitting at a terminal: ongoing, interactive, multi-turn — not fire-and-forget.

The shift is from "agent that calls APIs" to **"agent that inhabits a computer."**

The kernel is a generic runtime — it provides a tick loop, terminal multiplexing, container management, and 4 tools. It knows nothing about any particular agent. The agent's identity lives in a separate **data repo**: personality, memory, config, CLIs, container definition, hooks — everything version-controlled in git. Clone the repo, point a kernel at it, and you have the agent. The kernel is a body; the repo is a mind.

## Quick Start

```bash
pip install -e .

# Register an existing data repo
agent-kernel init --path /path/to/data-repo --name my-agent

# Or clone one from a URL
agent-kernel init https://github.com/user/my-agent-data.git

# Run one tick
agent-kernel tick --data my-agent

# Auto-tick on triggers and scheduled wakes
agent-kernel watch --data my-agent

# Install as systemd user service
agent-kernel install my-agent
```

## Architecture

```
┌────────────────────────────────────────────────────┐
│                Agent (Claude SDK)                  │
│                                                    │
│  Custom: login, type, wait, close                  │
│  SDK:    Read, Write, Edit, Glob, Grep,            │
│          WebSearch, WebFetch, NotebookEdit,        │
│          TodoWrite, Task, Skill                    │
│  (Bash is disabled — TTYs replace it)              │
└──────────┬─────────────────────────────────────────┘
           │  type(tty=0, text="ls -la")
           │  wait() → diff of new output
           ▼
┌────────────────────────────────────────────────────┐
│              Kernel (host process)                 │
│                                                    │
│  Watcher      — polls for trigger files + schedule │
│  Agent runner — fresh SDK session per tick         │
│  TTY manager  — tmux send-keys / capture-pane      │
│  Hooks        — pre/post-tick scripts from repo    │
│  Container    — content-addressed image builds     │
└──────────┬─────────────────────────────────────────┘
           │  podman exec / tmux commands
           ▼
┌────────────────────────────────────────────────────┐
│              Container (podman)                    │
│                                                    │
│  tmux sessions: tty_0 (bash), tty_1, tty_2, ...    │
│  CLIs from data repo on PATH                       │
│  Data repo mounted at same path as host            │
│  systemd init (for in-container daemons)           │
└────────────────────────────────────────────────────┘
```

### Tools

The agent gets exactly **4 custom tools**:

| Tool | Purpose |
|------|---------|
| `login()` | Called first every tick. Opens TTYs per `startup.json`, reports any lost to container restart. Returns startup output. |
| `type(tty, expect, text, enter?)` | Send keystrokes to a TTY. Auto-sends Enter for literal text; use `enter=false` to suppress. For control keys: `"C-c"`, `"Tab"`, `"Enter"`. |
| `wait(timeout?)` | Block until output settles (~1.5s of silence), then return a diff summary for every open TTY. Short output inline; long output head/tail with scrollback path. Max 60s. |
| `close(tty)` | Kill the tmux session and archive scrollback. All TTYs must be closed before a tick can end. |

Plus SDK builtins: Read, Write, Edit, Glob, Grep, NotebookEdit, WebSearch, WebFetch, TodoWrite, Task, Skill. Bash is disabled — terminal TTYs replace it.

Two invariants enforce safety:

- **Observe-before-act**: `type()` refuses to send keystrokes if any TTY has unread output. The agent must call `wait()` first. This prevents acting on stale information.
- **Point-and-call**: `type()` requires an `expect` parameter naming the command the agent believes is running (e.g. `"bash"`, `"python3"`). If the actual running command doesn't match, the call fails. This prevents sending keystrokes to the wrong process.

### Ticks

Each tick is a **stateless Claude SDK session**. No conversation history persists between ticks — the agent maintains its own continuity through files in the data repo (notebook, memory, journals).

1. Watcher detects a trigger file (`system/tick_trigger`) or a due entry in `system/schedule.json`
2. Tick count incremented in `system/state.json`
3. Pre-tick hooks run on the host (`system/hooks/pre-tick/`)
4. Fresh SDK session starts; agent receives `initial_query`, calls `login()`
5. Agent works through TTYs — typing commands, reading output, interacting with programs
6. When the model produces a text response (no tool calls), tick-end conditions are checked: was `login()` called? Are all TTYs closed? Do the pre-stop hooks pass (`system/hooks/pre-stop/`)? If not, the agent is told what's blocking and continues.
7. Post-tick hooks run (`system/hooks/post-tick/`) with `{PREFIX}_TICK_STATUS` set to `"normal"` or `"abnormal"`
8. Transcript copied to `system/logs/tick-NNN.jsonl`, `tmp/` wiped

Throughout the tick, a background TickWatcher delivers notification files (`system/notifications/*.txt`) into the conversation via `client.query()` — this is how external events (new messages, etc.) reach the agent mid-tick. At ~70% context usage (140K tokens), the agent is warned to wrap up. If context is about to overflow entirely, a PreCompact hook ends the tick immediately rather than letting the SDK compact away mid-tick context.

### Container Management

The container image is defined by `system/container/Containerfile` in the data repo. Images are **content-addressed**: the kernel hashes all files in the build directory and names the image `agent-kernel-img-{hash}`. When the Containerfile changes, a new image is built automatically at tick start. Old images and stopped containers are pruned.

Containers run with `--systemd=always` (so in-container daemons work) and mount the data repo at the same absolute path as the host, so SDK file tools and container paths agree.

At tick start, the kernel verifies container DNS works (rootless podman networking can break after host reboots) and recreates the container if needed.

### Error Handling and Recovery

API errors are classified as **fatal** (prompt too long → `system/paused` file created to prevent crash loops) or **transient** (rate limit, overloaded, timeout → exponential backoff retry, up to 10 attempts). The watcher writes crash details to `system/crash_notify.txt` for external consumers.

Every config file the kernel reads has fallback defaults. Bad JSON, missing files, and corrupt prompts all degrade gracefully instead of crashing. The kernel never fails to start a tick due to data repo corruption — the agent always gets a chance to investigate and fix things via git.

## Data Repo Contract

The data repo is the agent's identity. The kernel reads config from it, writes state to it, and mounts it into the container.

**Kernel reads:**

| Path | Purpose |
|------|---------|
| `system/agent_config.json` | Model, thinking tokens, initial query, hook env prefix |
| `system/prompt.md` | System prompt (cached, reloaded on change) |
| `system/agents.json` | Subagent definitions for the Task tool |
| `system/startup.json` | Which TTYs to open on `login()` |
| `system/schedule.json` | Wake timers (watcher checks for due entries) |
| `system/hooks/pre-tick/*` | Scripts run before each tick |
| `system/hooks/pre-stop/*` | Scripts run when agent wants to stop — stdout lines become blocking issues (30s timeout) |
| `system/hooks/post-tick/*` | Scripts run after each tick (receives `{PREFIX}_TICK_STATUS`: `"normal"` or `"abnormal"`) |
| `system/container/Containerfile` | Container image definition |

**Kernel writes:**

| Path | Purpose |
|------|---------|
| `system/state.json` | Tick count, timestamps |
| `system/paused` | Created on fatal error (delete to resume) |
| `system/logs/tick-NNN.jsonl` | Per-tick SDK transcript |
| `system/logs/sessions/` | Archived TTY scrollbacks |
| `tmp/sessions/` | Live TTY state (wiped each tick) |
| `system/crash_notify.txt` | Error text from crashes |

**External services communicate with the kernel via files:**

| Path | Direction | Purpose |
|------|-----------|---------|
| `system/tick_trigger` | → Watcher | Presence triggers a tick; content is the reason; file consumed |
| `system/notifications/*.txt` | → Agent | Delivered mid-tick, then deleted |

Hooks receive `DATA_DIR` and `{PREFIX}_TICK` env vars. Post-tick hooks additionally get `{PREFIX}_TICK_DURATION`, `{PREFIX}_TICK_LOG`, `{PREFIX}_LAST_MESSAGE`, `{PREFIX}_SESSION_ID`, and `{PREFIX}_TICK_STATUS` (`"normal"` or `"abnormal"`). Pre-stop hooks also get `{PREFIX}_LAST_MESSAGE` and `{PREFIX}_SESSION_ID`. The prefix defaults to `AGENT`, configurable via `hook_env_prefix` in `agent_config.json`. Pre-tick and post-tick hooks have a 60-second timeout; pre-stop hooks have a 30-second timeout. Hooks run in sorted filename order. Failures are logged, never fatal.

## CLI

```
agent-kernel init      [url] [--path dir] [--name name]   Clone or register a data repo
agent-kernel tick      --data <name|path>                  Run a single tick
agent-kernel watch     --data <name|path> [--interval N]   Poll-and-tick loop
agent-kernel list                                          List registered instances
agent-kernel remove    <name>                              Unregister an instance
agent-kernel install   <name>                              Create systemd service
agent-kernel uninstall <name>                              Remove systemd service
```

Instance registry at `~/.config/agent-kernel/instances.json`. Cloned repos stored in `~/.local/share/agent-kernel/`.

## Development

```bash
uv sync                              # Install deps
uv run pre-commit install            # Set up git hooks
uv run pytest tests/                 # Run tests
uv run pre-commit run --all-files    # Lint + type check
```
