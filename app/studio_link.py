"""Shared, dependency-free link between Script Studio Suite apps and the Hub.

Each suite app calls register_app() once at startup and report_recent_file()
whenever it opens/saves a project. The Hub only ever reads this data - it
never assumes where an app or its projects live on disk, since that's the
whole point: it has to work once these apps are installed on someone else's
machine, not just inside this dev checkout.

This file is intentionally duplicated (not shared via a package) into every
suite app's app/ folder, matching how these apps don't otherwise share any
code or a venv.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

STUDIO_DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "ScriptStudio"
APPS_FILE = STUDIO_DATA_DIR / "apps.json"
RECENTS_FILE = STUDIO_DATA_DIR / "recent_files.json"
MAX_RECENTS = 200


def _documents_dir() -> Path:
    """Resolves the user's real Documents folder, honoring OneDrive Known
    Folder Move redirection - plain Path.home()/"Documents" silently
    returns the wrong (unredirected) folder on a machine where OneDrive
    has moved Documents elsewhere, since Windows doesn't always leave a
    junction behind at the classic path."""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Personal")
                return Path(os.path.expandvars(value))
        except OSError:
            pass
    return Path.home() / "Documents"


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, data) -> None:
    STUDIO_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def ensure_program_files_dir(folder_name: str) -> Path:
    """Creates (if missing) and returns Documents/ScriptStudio/<folder_name>.

    This is deliberately anchored to the user's Documents folder, not to
    wherever this app happens to be installed - it resolves the same way
    whether the app is running from source or a packaged build, and on
    any machine, which a path relative to the app's own install location
    never could."""
    program_dir = _documents_dir() / "ScriptStudio" / folder_name
    program_dir.mkdir(parents=True, exist_ok=True)
    return program_dir


def register_app(
    slug: str,
    display_name: str,
    version: str,
    root_dir,
    project_extensions: tuple,
    icon_path=None,
    logo_path=None,
    exe_path=None,
    python_path=None,
    main_py_path=None,
    program_files_dir=None,
) -> None:
    """Called once at startup by each suite app to advertise itself.

    Pass exe_path when running as a packaged/frozen build (sys.frozen),
    or python_path + main_py_path when running from source - the Hub
    uses whichever pair is present to build its launch command.

    Pass program_files_dir (see ensure_program_files_dir()) so the Hub's
    Save Files view knows where this app's projects default to, without
    having to guess.
    """
    apps = _read_json(APPS_FILE, {})
    apps[slug] = {
        "display_name": display_name,
        "version": version,
        "root_dir": str(root_dir),
        "icon_path": str(icon_path) if icon_path else None,
        "logo_path": str(logo_path) if logo_path else None,
        "project_extensions": list(project_extensions),
        "exe_path": str(exe_path) if exe_path else None,
        "python_path": str(python_path) if python_path else None,
        "main_py_path": str(main_py_path) if main_py_path else None,
        "program_files_dir": str(program_files_dir) if program_files_dir else None,
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        _write_json_atomic(APPS_FILE, apps)
    except OSError:
        pass  # self-registration is best-effort; never block app startup on it


def report_recent_file(app_slug: str, path, title: str | None = None) -> None:
    """Called by a suite app right after it successfully opens or saves
    a project, so the Hub's Save Files view doesn't have to guess where
    projects live."""
    resolved = str(Path(path).resolve())
    recents = _read_json(RECENTS_FILE, [])
    recents = [r for r in recents if r.get("path") != resolved]
    recents.insert(
        0,
        {
            "app_slug": app_slug,
            "path": resolved,
            "title": title or Path(path).stem,
            "mtime": datetime.now().timestamp(),
        },
    )
    try:
        _write_json_atomic(RECENTS_FILE, recents[:MAX_RECENTS])
    except OSError:
        pass


def read_apps() -> dict:
    return _read_json(APPS_FILE, {})


def read_recent_files() -> list:
    return _read_json(RECENTS_FILE, [])
