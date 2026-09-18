"""Script Studio Suite shared design tokens (Master Style Guide).

Colors, font families, and font loading shared across the whole suite.
Values not pinned exactly by the style guide (the grey ramp) were chosen
here to complement Midnight Blue - adjust freely.

This file is intentionally duplicated (not shared via a package) into
every suite app's app/ folder, matching how these apps don't otherwise
share any code or a venv (see app/studio_link.py for the same pattern).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication, QPixmap

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def enable_dpi_awareness() -> None:
    """Declares this process Per-Monitor-v2 DPI aware at the OS level.
    Call this before constructing QApplication - it's what makes the
    app's actual rendered sharpness stop depending on how it was
    launched.

    Without an explicit declaration, Windows decides on its own whether
    to DPI-virtualize a process (silently bitmap-stretch the whole
    window after Qt has already drawn it) based on the parent process
    that launched it. A plain terminal and VS Code's integrated
    terminal can make different calls here, which is why the exact same
    app can render crisp from one and blurry from the other even though
    Qt's own high-DPI scaling code never changes.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except (AttributeError, OSError):
            pass

# ---------------------------------------------------------------- neutrals
BLACK = "#000000"
WHITE = "#ffffff"

GREY_50 = "#f5f6fa"
GREY_100 = "#e8eaf1"
GREY_200 = "#d3d6e3"
GREY_400 = "#9a9fb8"
GREY_600 = "#5c6180"
GREY_800 = "#232a4d"  # elevated dark surface (cards/panels on navy)
GREY_900 = "#0a0e1f"  # deepest dark background

# --------------------------------------------------------- signature colors
MIDNIGHT_BLUE = "#0e1938"  # primary brand surface: sidebars, headers, selected states
MIDNIGHT_BLUE_HOVER = "#16234a"

PURPLE = "#6b41bf"  # primary creative accent
PURPLE_HOVER = "#5934a3"
PURPLE_PRESSED = "#472887"
PURPLE_TINT = "#e4d9f7"  # light-mode disabled/tint background
PURPLE_LIGHT = "#a889e0"  # lighter tint for accent TEXT on dark backgrounds (contrast)

PINK = "#f359d2"  # special/AI accent - used sparingly
SOFT_PINK = "#ffc2c3"  # soft supporting accent - cards, notifications

# -------------------------------------------------------------------- fonts
# These are the exact family names Qt registers the bundled font files
# under (verified via QFontDatabase.applicationFontFamilies) - using them
# directly in QSS font-family is more reliable than requesting a numeric
# font-weight against the variable Montserrat font.
FONT_HEADER = "Montserrat ExtraBold"  # headers, panel titles, buttons, nav
FONT_BODY = "Poppins Light"  # general interface text (Proxima Nova substitute)
FONT_USER_CONTENT = "Courier New"  # anything the filmmaker types

_FONT_FILES = (
    "Montserrat[wght].ttf",
    "Poppins-Light.ttf",
    "Poppins-Regular.ttf",
    "Poppins-Medium.ttf",
)


def load_fonts() -> None:
    """Registers the bundled font files with Qt. Call once, right after
    constructing QApplication and before building any widgets."""
    for filename in _FONT_FILES:
        path = FONTS_DIR / filename
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def _device_pixel_ratio() -> float:
    screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen else 1.0


def load_pixmap(path, width: int | None = None, height: int | None = None, aspect_mode=None) -> QPixmap:
    """Loads an image scaled for crisp rendering on HiDPI displays.

    Plain QPixmap.scaled()/scaledToWidth() produce a bitmap sized for a
    1x display; Qt then stretches that bitmap to fill the same logical
    space on a higher-DPI screen, which is what "blurry" logos actually
    are. This scales to the real physical pixel count first and tags the
    result with the screen's device pixel ratio so Qt paints it 1:1.
    """
    pix = QPixmap(str(path))
    if pix.isNull() or (width is None and height is None):
        return pix
    dpr = _device_pixel_ratio()
    mode = aspect_mode if aspect_mode is not None else Qt.KeepAspectRatio
    if width and height:
        scaled = pix.scaled(round(width * dpr), round(height * dpr), mode, Qt.SmoothTransformation)
    elif width:
        scaled = pix.scaledToWidth(round(width * dpr), Qt.SmoothTransformation)
    else:
        scaled = pix.scaledToHeight(round(height * dpr), Qt.SmoothTransformation)
    scaled.setDevicePixelRatio(dpr)
    return scaled
