"""CLI interface for the agent kernel.

Entry point: agent-kernel <subcommand> <name> [args...]
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .agent import main as run_agent
from .registry import DATA_BASE_DIR, get_instance_info, list_instances, register, unregister
from .watcher import run_watcher


# --- Subcommands ---


def cmd_tick(args):
    """Run a single agent tick."""
    config.init(args.name)
    run_agent()


def cmd_watch(args):
    """Watch for triggers and auto-tick."""
    config.init(args.name)
    run_watcher(poll_interval=args.interval)


def cmd_init(args):
    """Initialize a new agent instance — clone a repo or register an existing path."""
    url = args.url
    name = args.name
    branch = args.branch
    local_path = args.path

    if local_path:
        # Register an existing directory in-place (no clone)
        dest = Path(local_path).expanduser().resolve()
        if not dest.is_dir():
            print(f"Error: {dest} is not an existing directory.")
            sys.exit(1)
        if not name:
            name = dest.name
        remote = url  # optional — record the remote if provided
    else:
        # Clone from URL
        if not url:
            print("Error: Provide a git URL or --path to an existing directory.")
            sys.exit(1)
        if not name:
            name = url.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".git"):
                name = name[:-4]
        dest = DATA_BASE_DIR / name
        remote = url

        if dest.exists():
            print(f"Error: Instance '{name}' already exists at {dest}")
            print("Use a different --name or remove the existing directory.")
            sys.exit(1)

        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning into {dest}...")
        clone_cmd = ["git", "clone", url, str(dest)]
        if branch:
            clone_cmd.extend(["--branch", branch])
        result = subprocess.run(clone_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Git clone failed: {result.stderr.strip()}")
            sys.exit(1)

    # Check registry for name collision
    if get_instance_info(name):
        print(f"Error: Instance '{name}' is already registered.")
        print("Use a different --name.")
        sys.exit(1)

    register(name, dest, remote=remote)

    print("\nReady.")
    print(f"  agent-kernel tick {name}")
    print(f"  agent-kernel watch {name}")
    print(f"  agent-kernel install {name}")


def cmd_install(args):
    """Install systemd user services for an instance."""
    name = args.name
    info = get_instance_info(name)

    if not info:
        print(f"Error: Instance '{name}' not found in registry.")
        sys.exit(1)

    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    service_name = f"agent-kernel-{name}"
    service_file = service_dir / f"{service_name}.service"
    container_name = f"agent-kernel-{name}"

    # Uninstall existing service if present
    if service_file.exists():
        print("Existing service found, reinstalling...")
        subprocess.run(["systemctl", "--user", "stop", f"{service_name}.service"], capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", f"{service_name}.service"], capture_output=True)
        service_file.unlink()

    # Find the kernel binary
    kernel_bin = shutil.which("agent-kernel")
    if not kernel_bin:
        # Fallback: use current Python with module
        kernel_bin = f"{sys.executable} -m src.cli"

    # Watcher service
    watcher_service = f"""[Unit]
Description=Agent Kernel ({name})
After=network.target

[Service]
Type=simple
ExecStart={kernel_bin} watch {name}
ExecStopPost=podman stop --time 5 {container_name}
Restart=always
RestartSec=10
KillMode=process

[Install]
WantedBy=default.target
"""
    service_file.write_text(watcher_service)
    print(f"Wrote {service_file}")

    # Reload and enable
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{service_name}.service"], check=True)
    print(f"Service {service_name} enabled and started.")
    print(f"  Check: systemctl --user status {service_name}")


def cmd_uninstall(args):
    """Remove systemd user services for an instance."""
    name = args.name
    service_name = f"agent-kernel-{name}"
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_file = service_dir / f"{service_name}.service"

    if not service_file.exists():
        print(f"Service {service_name} not found.")
        sys.exit(1)

    subprocess.run(["systemctl", "--user", "stop", f"{service_name}.service"], capture_output=True)
    subprocess.run(["systemctl", "--user", "disable", f"{service_name}.service"], capture_output=True)
    service_file.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print(f"Service {service_name} removed.")


def cmd_remove(args):
    """Remove a registered instance."""
    name = args.name
    info = get_instance_info(name)
    if not info:
        print(f"Error: Instance '{name}' not found in registry.")
        sys.exit(1)

    # Stop and remove systemd service if installed
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_file = service_dir / f"agent-kernel-{name}.service"
    if service_file.exists():
        subprocess.run(
            ["systemctl", "--user", "stop", f"agent-kernel-{name}.service"],
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", f"agent-kernel-{name}.service"],
            capture_output=True,
        )
        service_file.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print(f"Removed service agent-kernel-{name}.")

    unregister(name)
    print(f"Unregistered instance '{name}'.")

    path = info.get("path")
    if path:
        print(f"Data directory left in place: {path}")


def cmd_list(args):
    """List registered agent instances."""
    instances = list_instances()
    if not instances:
        print("No registered instances.")
        print("  Use 'agent-kernel init <url>' to create one.")
        return

    print("=== Registered Instances ===")
    for name, info in instances.items():
        path = info.get("path", "?")
        remote = info.get("remote", "")
        created = info.get("created", "")[:10]

        exists = Path(path).is_dir() if path != "?" else False
        status = "ok" if exists else "MISSING"

        print(f"\n  {name}")
        print(f"    Path:      {path} [{status}]")
        if remote:
            print(f"    Remote:    {remote}")
        if created:
            print(f"    Created:   {created}")


def main():
    parser = argparse.ArgumentParser(
        prog="agent-kernel",
        description="Portable agent kernel — install, point, run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- Core subcommands ---

    tick_parser = subparsers.add_parser("tick", help="Run a single agent tick")
    tick_parser.add_argument("name", help="Instance name")
    tick_parser.set_defaults(func=cmd_tick)

    watch_parser = subparsers.add_parser("watch", help="Watch for triggers and auto-tick")
    watch_parser.add_argument("name", help="Instance name")
    watch_parser.add_argument("--interval", "-i", type=float, default=2.0, help="Poll interval in seconds")
    watch_parser.set_defaults(func=cmd_watch)

    # --- Instance management subcommands ---

    init_parser = subparsers.add_parser("init", help="Initialize a new agent instance")
    init_parser.add_argument("url", nargs="?", default=None, help="Git repository URL to clone")
    init_parser.add_argument("--path", "-p", help="Register an existing directory (no clone)")
    init_parser.add_argument("--name", "-n", help="Instance name (default: derived from URL or directory)")
    init_parser.add_argument("--branch", "-b", help="Git branch to checkout (clone mode only)")
    init_parser.set_defaults(func=cmd_init)

    install_parser = subparsers.add_parser("install", help="Install systemd user services")
    install_parser.add_argument("name", help="Instance name")
    install_parser.set_defaults(func=cmd_install)

    uninstall_parser = subparsers.add_parser("uninstall", help="Remove systemd user services")
    uninstall_parser.add_argument("name", help="Instance name")
    uninstall_parser.set_defaults(func=cmd_uninstall)

    remove_parser = subparsers.add_parser("remove", help="Unregister an instance")
    remove_parser.add_argument("name", help="Instance name")
    remove_parser.set_defaults(func=cmd_remove)

    list_parser = subparsers.add_parser("list", help="List registered instances")
    list_parser.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
