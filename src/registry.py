"""Instance registry — maps short names to data repo paths.

Registry layout:
  ~/.config/agent-kernel/instances.json   — name → {path, remote, container, created}
  ~/.local/share/agent-kernel/{name}/     — cloned data repos
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

REGISTRY_DIR = Path(os.path.expanduser("~/.config/agent-kernel"))
REGISTRY_FILE = REGISTRY_DIR / "instances.json"
DATA_BASE_DIR = Path(os.path.expanduser("~/.local/share/agent-kernel"))


def load_registry() -> dict[str, Any]:
    """Load the instance registry from disk."""
    if not REGISTRY_FILE.exists():
        return {}
    try:
        return json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_registry(registry: dict[str, Any]) -> None:
    """Save the instance registry to disk."""
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2) + "\n")


def register(
    name: str,
    path: Path,
    remote: str | None = None,
) -> None:
    """Register an instance in the registry."""
    registry = load_registry()
    registry[name] = {
        "path": str(path.resolve()),
        "remote": remote,
        "created": datetime.now().isoformat(),
    }
    save_registry(registry)


def unregister(name: str) -> None:
    """Remove an instance from the registry."""
    registry = load_registry()
    registry.pop(name, None)
    save_registry(registry)


def resolve(name_or_path: str) -> Path | None:
    """Resolve an instance name or path to a data directory.

    Checks the registry first, then treats as a filesystem path.
    Returns None if neither matches.
    """
    # Check registry
    registry = load_registry()
    if name_or_path in registry:
        return Path(registry[name_or_path]["path"])

    # Treat as filesystem path
    p = Path(name_or_path).expanduser().resolve()
    if p.is_dir():
        return p

    return None


def get_instance_info(name: str) -> dict[str, Any] | None:
    """Get info for a registered instance, or None if not found."""
    registry = load_registry()
    return registry.get(name)


def list_instances() -> dict[str, Any]:
    """List all registered instances."""
    return load_registry()
