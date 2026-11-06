"""In-app updates for the macOS build, so testers don't have to download
a fresh .dmg every release.

Releases are published as GitHub Releases (see .github/workflows/
build-mac.yml - pushing a "v0.16"-style tag builds and publishes one,
with the app zipped as ScriptLiner-macOS.zip). The running app asks the
GitHub API for the latest release, and if it's newer, downloads that zip,
and swaps the .app bundle in place once the app has quit, then relaunches.

An update downloaded by the app itself (not a browser) never gets macOS's
download-quarantine flag, so the swapped-in copy doesn't trigger the
"unidentified developer" prompt again; the helper script also clears the
flag explicitly just in case.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from app.version import APP_VERSION, UPDATE_REPO

MAC_ASSET_NAME = "ScriptLiner-macOS.zip"
_USER_AGENT = "ScriptLiner-Updater"

# Waits for the running app to exit, swaps the bundle in (keeping the old
# one aside so a failed move can be rolled back), then relaunches.
_SWAP_SCRIPT = """#!/bin/sh
PID="$1"; NEW="$2"; DEST="$3"; WORK="$4"
while kill -0 "$PID" 2>/dev/null; do sleep 0.3; done
OLD="$DEST.old-update"
rm -rf "$OLD"
if mv "$DEST" "$OLD"; then
    if mv "$NEW" "$DEST"; then
        xattr -dr com.apple.quarantine "$DEST" 2>/dev/null
        rm -rf "$OLD"
    else
        mv "$OLD" "$DEST"
    fi
fi
rm -rf "$WORK"
open "$DEST"
"""


class UpdateError(Exception):
    pass


@dataclass
class ReleaseInfo:
    version: str
    notes: str
    page_url: str
    asset_url: Optional[str]
    asset_size: int


def parse_version(text: str):
    """"v0.16" / "0.16.1" -> (0, 16, 1); anything non-numeric -> ()."""
    core = re.split(r"[-+]", text.strip().lstrip("vV"), maxsplit=1)[0]
    return tuple(int(part) for part in re.findall(r"\d+", core))


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    remote_parts, local_parts = parse_version(remote), parse_version(local)
    if not remote_parts:
        return False
    width = max(len(remote_parts), len(local_parts))
    pad = lambda parts: parts + (0,) * (width - len(parts))
    return pad(remote_parts) > pad(local_parts)


def _release_from_json(data: dict) -> ReleaseInfo:
    asset_url, asset_size = None, 0
    for asset in data.get("assets") or []:
        if asset.get("name") == MAC_ASSET_NAME:
            asset_url = asset.get("browser_download_url")
            asset_size = int(asset.get("size") or 0)
    return ReleaseInfo(
        version=str(data.get("tag_name", "")).lstrip("vV"),
        notes=(data.get("body") or "").strip(),
        page_url=data.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases",
        asset_url=asset_url,
        asset_size=asset_size,
    )


def fetch_latest_release(timeout: float = 8.0) -> Optional[ReleaseInfo]:
    """The newest published release, or None if there isn't one yet.
    Raises UpdateError when GitHub can't be reached or answers oddly."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(f"GitHub answered with an error ({exc.code}).")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateError(f"Couldn't reach GitHub: {exc}")
    return _release_from_json(data)


def download_file(url, dest, progress=None, is_cancelled=None, chunk_size=256 * 1024):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, open(dest, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if is_cancelled and is_cancelled():
                    raise UpdateError("Download cancelled.")
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Download failed: {exc}")


def current_app_bundle() -> Optional[Path]:
    """The .app this process is running from, or None (running from
    source, or not on a Mac)."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def can_self_update(bundle: Path) -> bool:
    """False for a bundle in a folder this user can't write to (e.g. an
    Applications folder owned by another account) - the update then has
    to be a manual download."""
    return os.access(bundle, os.W_OK) and os.access(bundle.parent, os.W_OK)


def extract_app(zip_path, work_dir) -> Path:
    """Unzips the downloaded update and returns the .app inside it.
    macOS's own `ditto` is used because plain zip extraction drops the
    symlinks and executable bits an app bundle depends on."""
    work_dir = Path(work_dir)
    if shutil.which("ditto"):
        result = subprocess.run(
            ["ditto", "-x", "-k", str(zip_path), str(work_dir)], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise UpdateError(f"Couldn't unpack the update: {result.stderr.strip() or 'ditto failed'}")
    else:
        try:
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(work_dir)
        except zipfile.BadZipFile:
            raise UpdateError("The downloaded update isn't a valid zip file.")
    apps = sorted(p for p in work_dir.iterdir() if p.suffix == ".app")
    if not apps:
        raise UpdateError("The downloaded update doesn't contain an app.")
    return apps[0]


def launch_swap_and_relaunch(new_app: Path, bundle: Path, work_dir: Path):
    """Starts the detached helper that replaces `bundle` with `new_app`
    once this process exits and then reopens it. The caller must quit
    right after."""
    script_path = Path(work_dir) / "swap_update.sh"
    script_path.write_text(_SWAP_SCRIPT, encoding="utf-8")
    subprocess.Popen(
        ["/bin/sh", str(script_path), str(os.getpid()), str(new_app), str(bundle), str(work_dir)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def prepare_update(zip_path, bundle: Path) -> tuple:
    """Unpacks the update beside the installed app (same volume, so the
    final swap is a cheap rename). Returns (new_app, work_dir)."""
    work_dir = Path(tempfile.mkdtemp(prefix=".ScriptLiner-update-", dir=bundle.parent))
    try:
        return extract_app(zip_path, work_dir), work_dir
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


class UpdateCheckThread(QThread):
    """Asks GitHub for the latest release off the UI thread."""

    result = Signal(object, str)  # (ReleaseInfo | None, error message or "")

    def run(self):
        try:
            self.result.emit(fetch_latest_release(), "")
        except UpdateError as exc:
            self.result.emit(None, str(exc))


class UpdateDownloadThread(QThread):
    progress = Signal(int, int)  # (bytes done, bytes total)
    finished_ok = Signal(str)  # path to the downloaded zip
    failed = Signal(str)

    def __init__(self, url: str, dest: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._dest = dest
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            download_file(
                self._url, self._dest,
                progress=lambda done, total: self.progress.emit(done, total),
                is_cancelled=lambda: self._cancelled,
            )
        except UpdateError as exc:
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(self._dest)
