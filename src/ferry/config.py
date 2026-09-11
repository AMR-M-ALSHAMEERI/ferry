"""User settings stored in ``~/.ferry/config.json``.

Only preferences live here — never conversation data, and never anything from a
bundle. API keys arrive later, which is why the file is written
with owner-only permissions from the start.

Every read is defensive. A corrupt or unreadable config must degrade to
defaults rather than stopping someone migrating their history.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["CONFIG_PATH", "config_dir", "load_config", "read_setting", "write_setting"]


def config_dir() -> Path:
    """Directory holding Ferry's user settings."""
    return Path.home() / ".ferry"


CONFIG_PATH = config_dir() / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read the settings file.

    Returns an empty mapping if the file is missing, unreadable, corrupt, or
    does not contain a JSON object. Settings are a convenience; a broken file
    must never be fatal.
    """
    target = CONFIG_PATH if path is None else path
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict[str, Any], path: Path | None = None) -> bool:
    """Write the settings file atomically, owner-readable only.

    Written to a temporary file in the same directory and then renamed, so an
    interrupted write cannot leave a half-written config behind.

    Returns:
        ``True`` if the write succeeded, ``False`` if it failed for any reason.
        Failing to save a preference is not worth raising over.
    """
    target = CONFIG_PATH if path is None else path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except OSError:
        return False
    return True


def read_setting(key: str, default: Any = None, *, path: Path | None = None) -> Any:
    """Read one setting, falling back to ``default``."""
    return load_config(path).get(key, default)


def write_setting(key: str, value: Any, *, path: Path | None = None) -> bool:
    """Set one setting, preserving everything else in the file."""
    data = load_config(path)
    data[key] = value
    return save_config(data, path)
