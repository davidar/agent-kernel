# Creating an Agent

This guide walks through building a data repo from scratch. The kernel is the runtime; the data repo is the agent. Everything that makes your agent *yours* lives here.

## Minimal Data Repo

Create a directory with this structure:

```
my-agent/
  system/
    prompt.md              # System prompt — who the agent is
    agent_config.json      # Model, tick behavior
    startup.json           # Which terminals to open on login
    container/
      Containerfile        # Container image definition
    hooks/
      pre-tick/            # Scripts run before each tick
      pre-stop/            # Scripts that gate tick-end
      post-tick/           # Scripts run after each tick
```

Initialize it as a git repo (`git init`), then register it with the kernel:

```bash
agent-kernel init --path /path/to/my-agent --name my-agent
```

## System Prompt (`system/prompt.md`)

This is the agent's identity — loaded as the Claude system prompt every tick. The kernel caches it and reloads when the file changes.

Write it as direct instructions. The agent starts each tick with no memory of previous ticks, so the prompt should orient it: who it is, what it has access to, how to check its own state.

```markdown
You are an assistant that lives in a persistent environment.

Your files are in this directory. Between ticks, you don't retain
conversation history — check your notes/ directory for context.

Call login() first every tick to set up your terminals.
```

The prompt can reference files the agent should read at startup (a notebook, a task list, etc.). The agent has Read/Write/Edit tools and can manage its own files.

## Agent Config (`system/agent_config.json`)

```json
{
  "model": "claude-sonnet-4-20250514",
  "max_thinking_tokens": 10000,
  "initial_query": "Tick {tick} starting. Call login() to begin.",
  "hook_env_prefix": "MYAGENT"
}
```

| Field | Default | Purpose |
|-------|---------|---------|
| `model` | `claude-opus-4-6` | Claude model to use |
| `max_thinking_tokens` | _unset → adaptive_ | Optional explicit thinking budget for models that support manual budgets (e.g. Sonnet 4, Opus 4.6). Leave unset to use adaptive thinking — that's what Opus 4.7 needs (manual budgets are gone there). If set to a positive integer, the kernel uses an explicit-budget `ThinkingConfig`; otherwise adaptive. Either way, thinking text is requested as **summarized** so Opus 4.7+ surfaces something. |
| `initial_query` | `"Tick {tick} starting..."` | First message each tick. `{tick}` and `{data_dir}` are interpolated. |
| `hook_env_prefix` | `AGENT` | Prefix for hook environment variables |

The initial query can also come from a file: if `system/initial_query.md` exists, its contents are used as the first message (then the file is deleted). This lets post-tick hooks or external tools direct the next tick.

## Startup Config (`system/startup.json`)

Defines what terminals to open when `login()` is called:

```json
{
  "terminals": [
    {"command": "bash"},
    {"command": "my-chat-client"}
  ],
  "motd": "motd"
}
```

- `terminals`: List of terminals to create. Each gets a sequential ID (0, 1, 2...). The `command` field is what runs in that terminal.
- `motd`: Command to run in terminal 0 after creation. Its output becomes part of the login response the agent sees. Use this for orientation — show tick number, pending notifications, quick-reference commands.

If the file doesn't exist, a single bash terminal is created.

## Containerfile (`system/container/Containerfile`)

The agent runs inside a podman container. The Containerfile defines the environment.

Minimal example:

```dockerfile
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    tmux procps git curl python3 \
    && rm -rf /var/lib/apt/lists/*

# Keep PID 1 alive — the kernel uses `podman exec` for everything,
# so PID 1 just needs to not exit. Skip this only if your image already
# has systemd installed as the entrypoint.
CMD ["sleep", "infinity"]
```

The kernel mounts the data repo into the container at the same absolute path as on the host. This means paths are the same inside and outside — the SDK's file tools (Read, Write, Edit) and the terminal see the same filesystem.

The kernel runs containers with `--systemd=always` but does **not** install systemd for you — that's the image's job if you want it. If your image's default `CMD` exits (as `ubuntu:24.04`'s `/bin/bash` does when there's no TTY), the container will report "created but not responding" and the tick will fail. Either install systemd in the image, or end the Containerfile with `CMD ["sleep", "infinity"]` as shown above.

### Adding CLIs

The agent interacts with the world through CLI programs. Install them in the container and put them on PATH:

```dockerfile
# Copy CLI scripts from the data repo's build context
COPY cli/ /usr/local/bin/
RUN chmod +x /usr/local/bin/*
```

Or install CLIs from the data repo at runtime by adding the CLI directory to PATH in the container's bashrc. The data repo is mounted, so the agent can modify its own CLIs.

### What the container needs

At minimum: `tmux` (for terminal multiplexing) and `procps` (for process detection — the `ps` command). Everything else depends on what your agent does.

Common additions:
- `python3` + `pip` for scripting
- `gcc` for compiled programs
- `git` for version control
- `curl` / `w3m` / `ddgr` for web access
- Chat clients, API tools, whatever the agent needs to interact with

The image is **content-addressed**: the kernel hashes all files in `system/container/` and names the image accordingly. Change the Containerfile, and the kernel automatically rebuilds at the next tick start.

## Hooks

Hooks are executable scripts in `system/hooks/{pre-tick,pre-stop,post-tick}/`. They run **inside the container** in sorted filename order.

### Pre-tick hooks

Run after state is updated but before the agent starts. Use for:
- Pulling latest code
- Checking for new messages / notifications
- Writing trigger files the agent should see

```bash
#!/bin/bash
# system/hooks/pre-tick/01-check-messages
# timeout: 30
cd "$DATA_DIR" && python3 cli/check-messages
```

### Pre-stop hooks

Run when the agent tries to end a tick. Any line written to stdout becomes a **blocking issue** — the agent is told what's blocking and must address it before the tick can end. Empty stdout = no issues = tick can end.

This is how you enforce invariants: "commit your changes", "update your notebook", "close all terminals".

```bash
#!/bin/bash
# system/hooks/pre-stop/01-check-notebook
if ! git -C "$DATA_DIR" diff --quiet notebook.md 2>/dev/null; then
    echo "Uncommitted changes to notebook.md — commit before ending the tick."
fi
```

Pre-stop hooks have a 30-second timeout and fail-open (timeout/error = no blocking issues).

### Post-tick hooks

Run after the agent session ends. Receive extra environment variables:

- `{PREFIX}_TICK_DURATION` — tick duration in seconds
- `{PREFIX}_TICK_LOG` — path to the tick's transcript JSONL
- `{PREFIX}_LAST_MESSAGE` — the agent's final message (truncated to 2000 chars)
- `{PREFIX}_SESSION_ID` — Claude SDK session ID
- `{PREFIX}_TICK_STATUS` — `"normal"` (agent ended cleanly) or `"abnormal"` (interrupted/compacted)

Common uses: git commit + push, activity logging, triggering the next tick.

```bash
#!/bin/bash
# system/hooks/post-tick/01-git-commit
cd "$DATA_DIR"
git add -A
git commit -m "tick ${MYAGENT_TICK}: ${MYAGENT_TICK_STATUS}" --allow-empty
```

After post-tick hooks, the kernel runs `git push` on the host (best-effort).

### Hook timeout

Default: 60 seconds (30 for pre-stop). Override per-script with a comment:

```bash
#!/bin/bash
# timeout: 120
```

## Notifications

External services can inject messages into a running tick by writing files to `system/notifications/`. The TickWatcher polls this directory and delivers each `.txt` file's contents into the conversation via `client.query()`, then deletes the file.

This is how you build integrations: a Discord listener writes incoming messages to notification files, and the agent sees them mid-tick without polling.

## Running

```bash
# Single tick (foreground)
agent-kernel tick my-agent

# Watch mode (polls for trigger files)
agent-kernel watch my-agent

# As a systemd service
agent-kernel install my-agent
systemctl --user start agent-kernel-my-agent
```

To trigger a tick in watch mode, create the trigger file:

```bash
echo "wake timer" > /path/to/my-agent/system/tick_trigger
```

## Practical Notes

Things that matter in practice, from experience:

**The agent starts fresh every tick.** No conversation history carries over. Everything the agent needs to remember must be in files. A notebook file that the agent reads at tick start is the most common pattern. The system prompt should tell the agent to read it.

**Terminals persist across container restarts but not across ticks.** The kernel archives terminal scrollbacks at tick end and wipes `tmp/`. If the container restarts mid-tick, `login()` reports which terminals were lost.

**The observe-before-act pattern is strict.** The agent must call `wait()` before every `type()`. This feels cumbersome at first but prevents a class of bugs where the agent acts on stale terminal output.

**Context has a hard limit.** At ~70% usage (140K tokens), the agent is warned to wrap up. If it doesn't, the kernel ends the tick rather than letting the SDK compact away context. This means long ticks aren't possible — the agent should work in focused increments and save state frequently.

**Hooks are your control surface.** Pre-stop hooks enforce invariants the agent must satisfy before ending a tick. Post-tick hooks handle housekeeping. The agent can modify its own hooks — it has write access to the data repo.

**The agent can modify everything.** System prompt, config, hooks, Containerfile, CLIs — it's all in the data repo, which is mounted read-write. The agent can change its own identity, add tools, modify its container environment. Git history provides a safety net.
