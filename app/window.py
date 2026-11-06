import re
import sys
import copy
import math
import shutil
import tempfile
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTextEdit,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QSplitter,
    QPushButton,
    QMenu,
    QFileDialog,
    QMessageBox,
    QFrame,
    QStyle,
    QScrollArea,
    QDialog,
    QProgressDialog,
)

from PySide6.QtCore import Qt, QRect, QRectF, QPointF, QTimer, QSettings, QUrl, QEvent
from PySide6.QtGui import (
    QTextCursor,
    QTextDocument,
    QTextBlockFormat,
    QTextFormat,
    QTextCharFormat,
    QColor,
    QPainter,
    QPen,
    QFont,
    QKeySequence,
    QPageSize,
    QGuiApplication,
    QPalette,
    QAbstractTextDocumentLayout,
    QShortcut,
    QIcon,
    QPixmap,
    QDesktopServices,
)
from PySide6.QtPrintSupport import QPrinter

from app.shot import Shot
from app.inspector_panel import ShotInspectorPanel
from app.shortcut_button import make_shortcut_button
from app.script_import import import_fdx, import_fadein, import_fountain, import_rtf, import_celtx
from app.document_import import import_document
from app.project_io import dict_to_shots, save_project, load_project_data
from app.studio_link import ensure_program_files_dir
from app import theme
from app import updater
from app.version import APP_VERSION

SCENE_HEADING_RE = re.compile(r'^\s*(INT|EXT|INT\.?/EXT|I/E)[./]', re.IGNORECASE)
SHOT_ID_RE = re.compile(r'^(\d+)([A-Za-z]*)$')  # e.g. "12A" -> scene 12, letter A

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_LONG_PATH = ASSETS_DIR / "ScriptLinerLogo_Long.png"
LOGO_LONG_DARK_PATH = ASSETS_DIR / "ScriptLinerLogo_Long_Dark.png"  # wordmark recolored light; gradient bar untouched
LOGO_SQUARE_PATH = ASSETS_DIR / "ScriptLinerLogo_Square.png"
# White tick drawn inside a checked checkbox (see the QCheckBox::indicator
# rules in the stylesheets below). Forward slashes + quotes keep a Windows
# path with spaces valid inside a stylesheet url().
CHECK_IMAGE_URL = (ASSETS_DIR / "check.png").as_posix()

# Documents/ScriptStudio/ScriptLinerProgramFiles - the default save/open
# location, anchored to the user's Documents rather than wherever this app
# happens to be installed, so it works the same on any machine.
PROGRAM_FILES_DIR = ensure_program_files_dir("ScriptLinerProgramFiles")

FEEDBACK_FORM_URL = "https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=dh2B_8ocPkmI6_IR7nzFEkwkRPTKK9BCgL34KGPdfr1UNlZXWFBZSFBMNTVHUzc0N1JVMjFHM0NXNS4u"

# Opened automatically on boot whenever there's no project to restore
# (see _restore_last_session) so users land on a walkthrough instead of
# the blank placeholder page.
GUIDE_SCRIPT_PATH = ASSETS_DIR.parent / "Sample Scripts" / "ScriptLiner Guide.fadein"

# Shown once, gating the app, until accepted - see app/terms_dialog.py
# and main.py.
BETA_TERMS_PATH = ASSETS_DIR / "beta_terms.md"

PX_PER_INCH = 96
BASE_FONT_FAMILY = "Courier New"
BASE_FONT_POINT_SIZE = 12
# The same size in the document's own pixel units (12pt at 96px per inch).
BASE_FONT_PX = BASE_FONT_POINT_SIZE * PX_PER_INCH / 72


# Standard screenplay format is 12pt Courier at exactly 6 lines per inch -
# a 12pt pitch, 54 lines in the 9 inches of text on a page. Left to its own
# metrics Courier New is 13.6pt tall per line, which gave about 47 lines a
# page and so ~13% more pages than any other screenwriting software.
SCRIPT_LINE_PITCH_PT = 12.0


def _set_script_line_height(block_fmt, px_per_inch):
    block_fmt.setLineHeight(SCRIPT_LINE_PITCH_PT / 72.0 * px_per_inch, QTextBlockFormat.LineHeightTypes.FixedHeight.value)


def _fixed_line_pitch(block):
    """The block's fixed line pitch in pixels, or None if it has none."""
    fmt = block.blockFormat()
    if fmt.lineHeight() > 0 and fmt.lineHeightType() == QTextBlockFormat.LineHeightTypes.FixedHeight.value:
        return fmt.lineHeight()
    return None


def _flow_bottom(doc, block):
    """Where a block's text really ends in the flow of the page. With a
    fixed line height, Qt still reports each line - and so the block's
    bounding rect - as the font's natural height (13.6pt for Courier New)
    even though lines advance by the fixed pitch (12pt), so the last line
    of every paragraph overshoots its slot by the difference. Judging a
    page's fit by that overshoot made it hold 53 lines instead of 54."""
    rect = doc.documentLayout().blockBoundingRect(block)
    pitch = _fixed_line_pitch(block)
    layout = block.layout()
    if pitch is None or layout is None or layout.lineCount() == 0:
        return rect.bottom()
    return rect.top() + layout.lineCount() * pitch


def _points_for_pixels(pixels):
    """The QFont point size that makes a QTextDocument render text exactly
    `pixels` tall. A document turns a font's points into pixels using the
    screen's logical DPI - 96 on Windows but 72 on a Mac - so a plain "12pt"
    came out 25% smaller on a Mac (9pt on the exported page, and a page
    that held far more lines than a real screenplay page). Sizing by pixels
    through this keeps the on-screen page and the export identical on
    every computer."""
    screen = QGuiApplication.primaryScreen()
    dpi = screen.logicalDotsPerInchY() if screen is not None else 96.0
    return pixels * 72.0 / (dpi or 96.0)


def IN(inches: float) -> int:
    return round(inches * PX_PER_INCH)


PAGE_MARGIN_TOP_PX = IN(1.0)
PAGE_MARGIN_BOTTOM_PX = IN(1.0)
PAGE_MARGIN_LEFT_PX = IN(1.5)
PAGE_MARGIN_RIGHT_PX = IN(1.0)
PAGE_WIDTH_PX = IN(8.5)
PAGE_HEIGHT_PX = IN(11.0)
PAGE_NUMBER_TOP_OFFSET_PX = IN(0.5)
# How far into the bottom/top margin (MORE)/(CONT'D) sit, from the
# content edge they're closest to - well clear of both the content
# itself and (for "gap" page-break style) the canvas-colored gap band
# further out. Only used for "gap" style - see paintEvent/_write_lined_pdf.
MORE_CONTD_MARGIN_OFFSET_PX = IN(0.25)
CONTENT_HEIGHT_PX = PAGE_HEIGHT_PX - PAGE_MARGIN_TOP_PX - PAGE_MARGIN_BOTTOM_PX
PAGE_GAP_PX = 30
DOTTED_GAP_TOTAL_PX = 20  # on-screen page-turn space for the "dotted line" display style

# The dark-grey canvas/mat behind the page (see PageAspectContainer) -
# fixed regardless of theme, matching Fade In/Google Docs. The "gap
# between pages" break style (ScriptTextEdit.paintEvent) fills with this
# same color so the gap reads as the canvas showing through, not a
# separate stripe painted on the page.
PAGE_CANVAS_BG_COLOR = "#4a4a4a"

# Dark Mode ("invert page too") colors for the interactive page itself -
# export (_write_lined_pdf) never uses these, so a PDF always renders on
# a plain white page regardless of the app's theme. See
# ScriptTextEdit.set_dark_page/paintEvent and MainWindow._apply_theme.
PAGE_DARK_BG = "#1e1e1e"
PAGE_DARK_INK = "#e6e6e6"
PAGE_DARK_SECONDARY_INK = "#a0a0a0"

SCENE_NUMBER_MARGIN_GAP_PX = 8  # gap between a scene number and the text it flanks

KEY_LEGEND_COLOR = "#595959"
KEY_LEGEND_FONT_SHRINK_PT = 2
KEY_LEGEND_TOP_LIMIT_IN = 0.08    # the box's top edge can't start higher than this
KEY_LEGEND_BOTTOM_LIMIT_IN = 0.75  # the box's bottom edge can't extend past this
KEY_LEGEND_COLUMN_GAP_PX = 16
KEY_LEGEND_BOX_PADDING_PX = 4
KEY_LEGEND_TITLE_PADDING_PX = 4  # horizontal clearance around the title where it straddles the border
KEY_LEGEND_TITLE = "Key"
KEY_LEGEND_SIDE_MARGIN_PX = 4  # gap from the page edge when left/right-aligned

KEY_BOX_POSITIONS = {
    "top_center": "Top margin, centered",
    "top_left": "Top margin, left",
    "bottom_left": "Bottom margin, left",
    "bottom_center": "Bottom margin, centered",
    "bottom_right": "Bottom margin, right",
}
KEY_BOX_POSITION_DEFAULT = "bottom_center"

# Chrome (buttons/panels/dialogs/lists/menus - everything except the
# page itself, which stays governed separately by ScriptTextEdit's own
# dark_page/paintEvent logic) is styled to match Script Forge's palette:
# a purple accent (#6d5bd0 light / #a08cf0 dark) on an otherwise neutral
# grey chrome, rounded 6px controls. See MainWindow._apply_theme, which
# picks LIGHT_STYLESHEET for "light" and DARK_STYLESHEET for the two dark
# variants. #primaryButton marks the app's main call-to-action buttons
# (Update Script, Merge Projects, Confirm Placement) for the solid accent
# treatment - see _build_ui.
LIGHT_STYLESHEET = f"""
QWidget {{
    background-color: {theme.WHITE};
    color: {theme.GREY_900};
    font-family: "{theme.FONT_BODY}";
}}
QMainWindow, QDialog {{
    background-color: {theme.WHITE};
}}
QLabel {{
    background: transparent;
    color: {theme.GREY_900};
}}
QMenuBar {{
    background-color: {theme.WHITE};
    color: {theme.GREY_900};
    font-family: "{theme.FONT_HEADER}";
    border-bottom: 1px solid {theme.GREY_200};
}}
QMenuBar::item:selected {{
    background-color: {theme.GREY_100};
}}
QMenu {{
    background-color: {theme.WHITE};
    color: {theme.GREY_900};
    border: 1px solid {theme.GREY_200};
}}
QMenu::item {{
    padding: 6px 28px 6px 14px;
}}
QMenu::item:selected {{
    background-color: {theme.GREY_100};
}}
QPushButton {{
    background-color: {theme.WHITE};
    color: {theme.GREY_900};
    font-family: "{theme.FONT_HEADER}";
    border: 1px solid {theme.GREY_200};
    border-radius: 6px;
    padding: 5px 12px;
}}
QPushButton:hover {{
    background-color: {theme.GREY_100};
}}
QPushButton:pressed {{
    background-color: {theme.GREY_200};
}}
QPushButton:checked {{
    background-color: {theme.PURPLE};
    color: {theme.WHITE};
    border: 1px solid {theme.PURPLE_HOVER};
}}
QPushButton:disabled {{
    color: {theme.GREY_400};
    background-color: {theme.GREY_50};
    border: 1px solid {theme.GREY_200};
}}
QPushButton#primaryButton {{
    background-color: {theme.PURPLE};
    color: {theme.WHITE};
    border: none;
    font-weight: 600;
}}
QPushButton#primaryButton:hover {{
    background-color: {theme.PURPLE_HOVER};
}}
QPushButton#primaryButton:pressed {{
    background-color: {theme.PURPLE_PRESSED};
}}
QPushButton#primaryButton:disabled {{
    background-color: {theme.PURPLE_TINT};
    color: {theme.GREY_400};
}}
QLineEdit, QTextEdit, QListWidget, QComboBox, QAbstractSpinBox, QKeySequenceEdit {{
    background-color: {theme.WHITE};
    color: {theme.GREY_900};
    border: 1px solid {theme.GREY_200};
    border-radius: 4px;
    selection-background-color: {theme.PURPLE};
    selection-color: {theme.WHITE};
}}
QLineEdit, QTextEdit {{
    font-family: "{theme.FONT_USER_CONTENT}";
}}
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled, QKeySequenceEdit:disabled {{
    color: {theme.GREY_400};
    background-color: {theme.GREY_50};
    border: 1px solid {theme.GREY_200};
}}
QComboBox QAbstractItemView {{
    background-color: {theme.WHITE};
    color: {theme.GREY_900};
    selection-background-color: {theme.PURPLE};
    selection-color: {theme.WHITE};
}}
QListWidget::item {{
    padding: 4px 6px;
    border-radius: 4px;
}}
QListWidget::item:selected {{
    background-color: {theme.PURPLE};
    color: {theme.WHITE};
}}
QListWidget::item:hover {{
    background-color: {theme.GREY_100};
}}
QGroupBox {{
    border: 1px solid {theme.GREY_200};
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 6px;
    font-family: "{theme.FONT_HEADER}";
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {theme.GREY_900};
}}
QCheckBox, QRadioButton {{
    color: {theme.GREY_900};
    spacing: 6px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {theme.GREY_400};
    border-radius: 4px;
    background: {theme.WHITE};
}}
QCheckBox::indicator:checked {{
    background: {theme.PURPLE};
    border: 1px solid {theme.PURPLE};
    image: url("{CHECK_IMAGE_URL}");
}}
QCheckBox::indicator:disabled {{
    background: {theme.GREY_50};
    border: 1px solid {theme.GREY_200};
}}
QCheckBox::indicator:checked:disabled {{
    background: {theme.PURPLE_TINT};
    border: 1px solid {theme.PURPLE_TINT};
}}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: {theme.WHITE};
    border: none;
}}
QScrollBar::handle {{
    background: {theme.GREY_200};
    border-radius: 4px;
}}
QScrollBar::handle:hover {{
    background: {theme.GREY_400};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    background: none;
    border: none;
}}
QSplitter::handle {{
    background-color: {theme.GREY_200};
}}
QStatusBar {{
    background-color: {theme.WHITE};
    color: {theme.GREY_600};
    border-top: 1px solid {theme.GREY_200};
}}
QToolTip {{
    background-color: {theme.GREY_900};
    color: {theme.WHITE};
    border: 1px solid {theme.GREY_900};
}}
"""

DARK_STYLESHEET = f"""
QWidget {{
    background-color: {theme.GREY_900};
    color: {theme.GREY_100};
    font-family: "{theme.FONT_BODY}";
}}
QMainWindow, QDialog {{
    background-color: {theme.GREY_900};
}}
QLabel {{
    background: transparent;
    color: {theme.GREY_100};
}}
QMenuBar {{
    background-color: {theme.MIDNIGHT_BLUE};
    color: {theme.GREY_100};
    font-family: "{theme.FONT_HEADER}";
    border-bottom: 1px solid {theme.MIDNIGHT_BLUE_HOVER};
}}
QMenuBar::item:selected {{
    background-color: {theme.GREY_800};
}}
QMenu {{
    background-color: {theme.GREY_800};
    color: {theme.GREY_100};
    border: 1px solid {theme.MIDNIGHT_BLUE_HOVER};
}}
QMenu::item {{
    padding: 6px 28px 6px 14px;
}}
QMenu::item:selected {{
    background-color: {theme.PURPLE};
}}
QPushButton {{
    background-color: {theme.GREY_800};
    color: {theme.GREY_100};
    font-family: "{theme.FONT_HEADER}";
    border: 1px solid {theme.MIDNIGHT_BLUE_HOVER};
    border-radius: 6px;
    padding: 5px 12px;
}}
QPushButton:hover {{
    background-color: {theme.MIDNIGHT_BLUE_HOVER};
}}
QPushButton:pressed {{
    background-color: {theme.GREY_900};
}}
QPushButton:checked {{
    background-color: {theme.PURPLE};
    color: {theme.WHITE};
    border: 1px solid {theme.PURPLE_LIGHT};
}}
QPushButton:disabled {{
    color: {theme.GREY_600};
    background-color: {theme.GREY_900};
    border: 1px solid {theme.GREY_800};
}}
QPushButton#primaryButton {{
    background-color: {theme.PURPLE};
    color: {theme.WHITE};
    border: none;
    font-weight: 600;
}}
QPushButton#primaryButton:hover {{
    background-color: {theme.PURPLE_HOVER};
}}
QPushButton#primaryButton:pressed {{
    background-color: {theme.PURPLE_PRESSED};
}}
QPushButton#primaryButton:disabled {{
    background-color: {theme.GREY_800};
    color: {theme.GREY_600};
}}
QLineEdit, QTextEdit, QListWidget, QComboBox, QAbstractSpinBox, QKeySequenceEdit {{
    background-color: {theme.GREY_800};
    color: {theme.GREY_100};
    border: 1px solid {theme.MIDNIGHT_BLUE_HOVER};
    border-radius: 4px;
    selection-background-color: {theme.PURPLE};
    selection-color: {theme.WHITE};
}}
QLineEdit, QTextEdit {{
    font-family: "{theme.FONT_USER_CONTENT}";
}}
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled, QKeySequenceEdit:disabled {{
    color: {theme.GREY_600};
    background-color: {theme.GREY_900};
    border: 1px solid {theme.GREY_800};
}}
QComboBox QAbstractItemView {{
    background-color: {theme.GREY_800};
    color: {theme.GREY_100};
    selection-background-color: {theme.PURPLE};
    selection-color: {theme.WHITE};
}}
QListWidget::item {{
    padding: 4px 6px;
    border-radius: 4px;
}}
QListWidget::item:selected {{
    background-color: {theme.PURPLE};
    color: {theme.WHITE};
}}
QListWidget::item:hover {{
    background-color: {theme.MIDNIGHT_BLUE_HOVER};
}}
QGroupBox {{
    border: 1px solid {theme.MIDNIGHT_BLUE_HOVER};
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 6px;
    font-family: "{theme.FONT_HEADER}";
    color: {theme.GREY_100};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {theme.GREY_100};
}}
QCheckBox, QRadioButton {{
    color: {theme.GREY_100};
    spacing: 6px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {theme.GREY_400};
    border-radius: 4px;
    background: {theme.GREY_800};
}}
QCheckBox::indicator:checked {{
    background: {theme.PURPLE};
    border: 1px solid {theme.PURPLE_LIGHT};
    image: url("{CHECK_IMAGE_URL}");
}}
QCheckBox::indicator:disabled {{
    background: {theme.GREY_900};
    border: 1px solid {theme.GREY_800};
}}
QCheckBox::indicator:checked:disabled {{
    background: {theme.GREY_800};
    border: 1px solid {theme.GREY_600};
}}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: {theme.GREY_900};
    border: none;
}}
QScrollBar::handle {{
    background: {theme.MIDNIGHT_BLUE_HOVER};
    border-radius: 4px;
}}
QScrollBar::handle:hover {{
    background: {theme.GREY_600};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    background: none;
    border: none;
}}
QSplitter::handle {{
    background-color: {theme.MIDNIGHT_BLUE};
}}
QStatusBar {{
    background-color: {theme.MIDNIGHT_BLUE};
    color: {theme.GREY_400};
    border-top: 1px solid {theme.MIDNIGHT_BLUE_HOVER};
}}
QToolTip {{
    background-color: {theme.GREY_800};
    color: {theme.GREY_100};
    border: 1px solid {theme.MIDNIGHT_BLUE_HOVER};
}}
"""

LINE_STYLE_MAP = {
    "solid": Qt.SolidLine,
    "dashed": Qt.DashLine,
    "dotted": Qt.DotLine,
    "dash-dot": Qt.DashDotLine,
    "double": Qt.SolidLine,
}

# Paired 1:1 by index with MainWindow.colors (see _shape_for_color) so
# each shot's tick-mark shape tracks its color assignment automatically -
# used in place of color to tell shots apart in a black-and-white export
# (see _draw_tick_mark/_write_lined_pdf), since color alone can't do that
# once everything renders in black ink. "diagonal" is first so an old
# shot loaded from a project saved before this feature (Shot.tick_shape
# defaults to "diagonal") still gets the classic look.
TICK_SHAPES = ["diagonal", "circle", "square", "triangle", "diamond", "plus"]

SCREENPLAY_STYLE_MAP = {
    # space_before_pt is whole blank lines at the 12pt script line pitch
    # (SCRIPT_LINE_PITCH_PT): one before action/character/transition, two
    # before a scene heading or shot.
    "Scene Heading": dict(left_in=0.0, right_in=0.0, align=Qt.AlignLeft, case="upper", space_before_pt=24),
    "Action":        dict(left_in=0.0, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=12),
    "Character":     dict(left_in=2.0, right_in=0.0, align=Qt.AlignLeft, case="upper", space_before_pt=12),
    "Parenthetical": dict(left_in=1.5, right_in=2.0, align=Qt.AlignLeft, case="lower_paren", space_before_pt=0),
    "Dialogue":      dict(left_in=1.0, right_in=1.5, align=Qt.AlignLeft, case="as_is", space_before_pt=0),
    "Transition":    dict(left_in=4.0, right_in=0.4, align=Qt.AlignRight, case="upper", space_before_pt=12),
    "Shot":          dict(left_in=0.0, right_in=0.0, align=Qt.AlignLeft, case="upper", space_before_pt=24),
    "General":       dict(left_in=0.0, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=0),
    # Non-screenplay documents (beat sheets, treatments, outlines) from
    # File > Import Document - see app/document_import.py. "hanging_in"
    # indents wrapped lines past a list marker instead of back under it.
    "Doc Title":     dict(left_in=0.0, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=22),
    "Doc Section":   dict(left_in=0.0, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=22),
    "Doc Heading":   dict(left_in=0.0, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=14),
    "Doc Text":      dict(left_in=0.0, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=11),
    "Doc List 1":    dict(left_in=0.2, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=5, hanging_in=0.3),
    "Doc List 2":    dict(left_in=0.6, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=3, hanging_in=0.3),
    "Doc List 3":    dict(left_in=1.0, right_in=0.0, align=Qt.AlignLeft, case="as_is", space_before_pt=3, hanging_in=0.3),
}
DEFAULT_SCREENPLAY_STYLE = SCREENPLAY_STYLE_MAP["Action"]
# Paragraph types that start a new numbered scene (screenplay) or section
# (imported document) - shot ids like "12A" are numbered off these.
SECTION_HEADING_TYPES = ("Scene Heading", "Doc Section")

LANE_START_X = 15
LANE_WIDTH = 20
MAX_LANES = max(1, (PAGE_MARGIN_LEFT_PX - LANE_START_X) // LANE_WIDTH)
# Lanes a project starts with (what fits at the normal line spacing), and
# the most Settings will let you add. Every lane beyond what fits at the
# normal spacing makes ALL the lines narrower - see _fit_lane_width_to_count.
DEFAULT_LANE_COUNT = MAX_LANES
MAX_LANE_COUNT = 10
CROSS_SIZE = 7
MIN_LANE_WIDTH = 8
# Minimum vertical breathing room (unscaled px, at scale=1.0) required
# between one shot's end-tick and the next shot's start-tick before
# they're allowed to share a lane. Without it, two shots that are
# textually adjacent (e.g. one ends right where the next scene's shot
# begins, with no blank line between) get packed into the same lane and
# their tick marks collide with the next shot's ID label, which is
# drawn just above its start point (see the `label_y = ... - cross - 4`
# lines in ScriptTextEdit.paintEvent / MainWindow._write_lined_pdf).
# Calibrated to the end-tick's own downward reach, the label's line
# height, and the start-tick's upward reach.
MIN_LANE_GAP_PX = CROSS_SIZE * 2 + round(BASE_FONT_POINT_SIZE * (PX_PER_INCH / 72) * 1.5) + 4
MAX_UNDO_STEPS = 50


def _lane_disabled(index: int, active_lanes, lane_count: int = DEFAULT_LANE_COUNT) -> bool:
    """True if `index` is one of the numbered lanes (1 to lane_count, set in
    Settings) and the user turned it off there. Indices at or beyond
    lane_count are always usable - they only exist as overflow when more
    shots overlap than the numbered lanes hold, so "disabled" (a concept
    scoped to the lanes shown in Settings) doesn't apply to them."""
    return index < lane_count and index not in active_lanes

PROJECT_FILE_FILTER = "ScriptLiner Project (*.slproj)"
AUTOSAVE_INTERVAL_MS = 30_000
DEFAULT_AUTOSAVE_PATH = Path.home() / ".scriptliner" / "autosave.slproj"


def _apply_case(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "lower_paren":
        stripped = text.strip()
        if not stripped:
            return stripped
        if stripped.startswith("(") and stripped.endswith(")"):
            return stripped.lower()
        return f"({stripped.lower()})"
    return text


def _apply_case_to_runs(runs, case, original_text):
    """Same transform as _apply_case, spread across formatting runs
    instead of one plain string - "upper" is a per-character transform
    so splitting it across runs is exactly equivalent; "lower_paren"
    lowercases every run the same way, then adds plain (unformatted)
    "(" / ")" runs to match _apply_case's own wrap-unless-both-already-
    present rule, evaluated against the ORIGINAL text (matching
    _apply_case, which never re-checks its own output)."""
    if case == "upper":
        return [(t.upper(), b, i, u, s) for t, b, i, u, s in runs]
    if case == "lower_paren":
        lowered = [(t.lower(), b, i, u, s) for t, b, i, u, s in runs]
        stripped = original_text.strip()
        if not stripped or (stripped.startswith("(") and stripped.endswith(")")):
            return lowered
        return [("(", False, False, False, False)] + lowered + [(")", False, False, False, False)]
    return runs


def _char_format_for_run(bold, italic, underline, strike):
    fmt = QTextCharFormat()
    if bold:
        fmt.setFontWeight(QFont.Bold)
    if italic:
        fmt.setFontItalic(True)
    if underline:
        fmt.setFontUnderline(True)
    if strike:
        fmt.setFontStrikeOut(True)
    return fmt


def _render_paragraphs_into_document(doc, paragraphs, px_per_inch):
    def in_(inches):
        return round(inches * px_per_inch)

    def pt_(points):
        return round(points / 72 * px_per_inch)

    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.Start)

    wrote_content = False
    for para in paragraphs:
        p_type, text = para[0], para[1]
        runs = para[3] if len(para) > 3 else None
        style = SCREENPLAY_STYLE_MAP.get(p_type, DEFAULT_SCREENPLAY_STYLE)

        block_fmt = QTextBlockFormat()
        _set_script_line_height(block_fmt, px_per_inch)
        hanging_in = style.get("hanging_in", 0.0)
        block_fmt.setLeftMargin(in_(style["left_in"] + hanging_in))
        if hanging_in:
            block_fmt.setTextIndent(-in_(hanging_in))
        block_fmt.setRightMargin(in_(style["right_in"]))
        block_fmt.setAlignment(style["align"])
        block_fmt.setTopMargin(pt_(style["space_before_pt"]) if wrote_content else 0)
        # Lets pagination (_apply_page_gaps_to_document) recognize
        # Character/Parenthetical/Dialogue blocks for widow control and
        # (MORE)/(CONT'D) - see _PARA_TYPE_PROPERTY.
        block_fmt.setProperty(_PARA_TYPE_PROPERTY, p_type)

        # Reset to a blank char format for every paragraph - otherwise a
        # bold/italic/etc. run at the end of one paragraph would leak
        # into the start of the next (insertText with no explicit
        # format just uses whatever the cursor's format already is).
        blank_fmt = QTextCharFormat()
        if not wrote_content:
            cursor.setBlockFormat(block_fmt)
            cursor.setCharFormat(blank_fmt)
        else:
            cursor.insertBlock(block_fmt, blank_fmt)

        if runs:
            for run_text, bold, italic, underline, strike in _apply_case_to_runs(runs, style["case"], text):
                cursor.insertText(run_text, _char_format_for_run(bold, italic, underline, strike))
        else:
            cursor.insertText(_apply_case(text, style["case"]), blank_fmt)
        wrote_content = True


def _absolute_y_for_position(doc, position):
    """Document-space Y for a character position, independent of any
    QTextEdit's current scroll offset (unlike ScriptTextEdit._y_for_position)."""
    cursor = QTextCursor(doc)
    cursor.setPosition(position)
    block = cursor.block()
    rect = doc.documentLayout().blockBoundingRect(block)
    layout = block.layout()
    if layout is not None:
        line = layout.lineForTextPosition(cursor.positionInBlock())
        if line.isValid():
            return rect.top() + line.y()
    return rect.top()


def _compute_scene_numbers(doc, loaded_paragraphs):
    """[(block_start_position, scene_number_str), ...] for every Scene
    Heading in the document, in order - the character position lets
    callers find each heading's line via _absolute_y_for_position or
    ScriptTextEdit._y_for_position, for drawing a scene number beside it.
    Mirrors the same detection/numbering rules as
    MainWindow.get_scene_number_for_position: when loaded_paragraphs
    (from a structured import) is available, its authoritative "Scene
    Heading" type tags are used - paragraph i corresponds 1:1 to the
    document's block i, since that's the only thing that renders this
    document - and an explicit numeric scene number carried from the
    source file is honored over the auto-incremented count. Falls back
    to regex-detecting INT/EXT lines directly for plain pasted text."""
    results = []
    scene_count = 0

    if loaded_paragraphs:
        block = doc.begin()
        for para in loaded_paragraphs:
            # A real (MORE)/(CONT'D) split (see _try_split_dialogue_block)
            # inserts 3 extra blocks with no counterpart in
            # loaded_paragraphs at all - skip them to keep block i
            # lined up with paragraph i.
            while block.isValid() and block.blockFormat().property(_SYNTHETIC_BLOCK_PROPERTY):
                block = block.next()
            if not block.isValid():
                break
            if para[0] in SECTION_HEADING_TYPES:
                scene_count += 1
                explicit_number = para[2] if len(para) > 2 else None
                if explicit_number and str(explicit_number).isdigit():
                    number = str(explicit_number)
                else:
                    number = str(scene_count)
                results.append((block.position(), number))
            block = block.next()
        return results

    block = doc.begin()
    while block.isValid():
        if SCENE_HEADING_RE.match(block.text()):
            scene_count += 1
            results.append((block.position(), str(scene_count)))
        block = block.next()
    return results


def _scene_numbers_on_page(scene_number_ys, page_top, page_bottom):
    """Every scene number that appears anywhere on a page spanning
    [page_top, page_bottom] - both a scene heading that starts partway
    through the page AND whichever scene was already in progress at the
    page's own top (its heading may have been several pages earlier).
    `scene_number_ys` is [(y, number), ...] with y already converted to
    the same absolute pixel-Y coordinate space as page_top/page_bottom
    via _absolute_y_for_position - NOT the raw character-offset
    positions _compute_scene_numbers returns; comparing those directly
    against a pixel range would be comparing two different units. Used
    by the PDF export's scene/date-based Selection filter (see
    _write_lined_pdf) to decide whether a page belongs to the batch
    being exported."""
    on_page = set()
    active = None
    for y, number in scene_number_ys:
        if y <= page_top + 0.5:
            active = number
        if page_top - 0.5 <= y <= page_bottom + 0.5:
            on_page.add(number)
    if active is not None:
        on_page.add(active)
    return on_page


def _parse_scene_selector(text):
    """Parses a Fade In-style "Scenes:" field (e.g. "12, 14, 15-18") into
    a set of scene-number strings. A token that isn't a plain integer or
    an integer range (e.g. a lettered/inserted scene like "14A") is kept
    as an exact-match string - ranges only ever expand across plain
    numeric tokens, matching how scene numbering itself works. Returns
    None for blank input, meaning "no filter, include every page" -
    distinct from an empty set, which would exclude everything."""
    text = (text or "").strip()
    if not text:
        return None
    scenes = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, _, end = token.partition("-")
            start, end = start.strip(), end.strip()
            if start.isdigit() and end.isdigit():
                for n in range(int(start), int(end) + 1):
                    scenes.add(str(n))
                continue
        scenes.add(token)
    return scenes


def _draw_scene_numbers(painter, page_width, rows, scale=1.0):
    """Draws each (line_top_y, number_str) in `rows` in BOTH the left and
    right margins, vertically aligned with its Scene Heading line -
    matching Final Draft/Fade In's standard "scene numbers in both
    margins" formatting. `page_width` and the coordinate space of
    line_top_y must already be in the same units the painter is
    currently drawing in (scaled for export, unscaled for the
    interactive view)."""
    if not rows:
        return
    metrics = painter.fontMetrics()
    ascent = metrics.ascent()
    gap = SCENE_NUMBER_MARGIN_GAP_PX * scale
    left_edge = PAGE_MARGIN_LEFT_PX * scale - gap
    right_edge = page_width - PAGE_MARGIN_RIGHT_PX * scale + gap
    for line_top, number in rows:
        text_y = round(line_top + ascent)
        width = metrics.horizontalAdvance(number)
        painter.drawText(round(left_edge - width), text_y, number)
        painter.drawText(round(right_edge), text_y, number)


# A conventional drafting "break line" symbol: a short kick to the right,
# a steep diagonal crossing back past center, another short kick to the
# right, then a straight run down to rejoin the line. Angles are measured
# clockwise from the positive x-axis (0 deg = right, 90 deg = down);
# lengths are relative "measures" scaled to a concrete pixel size below.
_BREAK_MARK_SEGMENTS = [(0, 1.5), (147, 3.8), (0, 1.5), (90, 2.0)]
_BREAK_MARK_TOTAL_DY = sum(
    length * math.sin(math.radians(angle)) for angle, length in _BREAK_MARK_SEGMENTS
)


def _break_mark_height(size):
    """Total vertical span (in px) the break mark occupies for a given size."""
    return 4.0 * max(3, size)


def _mark_size_for_gap(size, gap):
    """Shrinks the break-mark size so an "end" mark and a flipped "start"
    mark - drawn back to back across a small gap between two segments -
    fit within that gap instead of overlapping into a squashed mess."""
    if gap >= 2 * _break_mark_height(size):
        return size
    return max(1.0, min(size, gap / 8.0))


def _draw_break_zigzag(painter, x, anchor_y, size, flip=False):
    """Draws the break mark anchored at (x, anchor_y), used wherever a
    shot's line is cut - a page edge or a same-page gap between split-shot
    segments - instead of a plain line.

    flip=False: the "end" mark - anchor_y is its top, and it runs downward.
    Used wherever a line segment ends (bottom of a page, end of a segment
    right before a same-page gap).

    flip=True: the "start" mark - the same shape rotated 180 degrees,
    anchor_y is its bottom, and it runs upward from there. Used wherever a
    line segment begins (top of a page, start of a segment right after a
    same-page gap) so the two ends of a break look like complementary
    torn edges rather than identical copies.
    """
    unit = _break_mark_height(size) / _BREAK_MARK_TOTAL_DY
    sign = -1 if flip else 1
    points = [QPointF(x, anchor_y)]
    cx, cy = x, anchor_y
    for angle_deg, length in _BREAK_MARK_SEGMENTS:
        rad = math.radians(angle_deg)
        cx += sign * math.cos(rad) * length * unit
        cy += sign * math.sin(rad) * length * unit
        points.append(QPointF(cx, cy))
    painter.drawPolyline(points)


def _draw_tick_mark(painter, x, y, size, shape):
    """Draws a shot's start/end tick mark as `shape` (see TICK_SHAPES)
    centered at (x, y), sized to match the plain diagonal tick's own
    reach (`size` == `cross`, the same value used for the diagonal line
    everywhere else). Only used for a black-and-white export (see
    _write_lined_pdf) - every other tick in the app (the interactive
    view, a normal color export) keeps the classic single-diagonal-line
    look regardless of a shot's own tick_shape, since color alone
    already distinguishes shots there."""
    if shape == "diagonal" or shape not in TICK_SHAPES:
        painter.drawLine(round(x - size), round(y + size), round(x + size), round(y - size))
        return
    # A filled shape covers far more ink than the diagonal's single
    # stroke at the same nominal radius, reading much bigger/heavier
    # even though `size` matches - shrink it so it lands at a similar
    # visual weight instead of dominating the margin.
    size = size * 0.6
    painter.save()
    painter.setBrush(painter.pen().color())
    if shape == "circle":
        painter.drawEllipse(QPointF(x, y), size, size)
    elif shape == "square":
        painter.drawRect(QRectF(x - size, y - size, size * 2, size * 2))
    elif shape == "triangle":
        painter.drawPolygon([QPointF(x, y - size), QPointF(x + size, y + size), QPointF(x - size, y + size)])
    elif shape == "diamond":
        painter.drawPolygon([QPointF(x, y - size), QPointF(x + size, y), QPointF(x, y + size), QPointF(x - size, y)])
    elif shape == "plus":
        wide_pen = QPen(painter.pen())
        wide_pen.setWidth(max(painter.pen().width(), max(1, round(size * 0.6))))
        painter.setPen(wide_pen)
        painter.drawLine(round(x - size), round(y), round(x + size), round(y))
        painter.drawLine(round(x), round(y - size), round(x), round(y + size))
    painter.restore()


MAX_PALETTE_COLORS = 30
DEFAULT_OVERLAY_OPACITY = 40

# The colors "give every new shot its own color" hands out, in this order.
# Fixed rather than generated: random/computed colors drift into near-
# duplicates, so these 30 were picked to be as different from one another
# as possible while ALL staying readable on a white page (each has at
# least 3:1 contrast against white - so no pale yellows or cyans - and
# none is so dark it gets mistaken for the script's black text). The
# first few are the most mutually distinct, so a short script gets the
# clearest set. Aqua, Brass and Burnt Sienna are slightly darker than
# their textbook values for exactly that white-page reason.
SHOT_COLOR_SET = [
    ("Aqua", "#00a3a3"),
    ("Blue", "#0000ff"),
    ("Jade", "#00a36c"),
    ("Brass", "#a68f51"),
    ("Brown", "#a52a2a"),
    ("Burnt Sienna", "#e4724f"),
    ("Olive Green", "#808000"),
    ("Cerise", "#de3163"),
    ("Azure", "#0085fa"),
    ("Electric Orchid", "#e229ff"),
    ("Forest Green", "#165a28"),
    ("Plum", "#732b64"),
    ("Umber", "#704910"),
    ("Sapphire", "#085496"),
    ("Periwinkle", "#7764c4"),
    ("Amber", "#b36409"),
    ("Apple Green", "#5ca509"),
    ("Grape", "#a400bd"),
    ("Mauve", "#c464a4"),
    ("Moss", "#555a16"),
    ("Green", "#008000"),
    ("Red", "#eb0000"),
    ("Indigo", "#573894"),
    ("Raspberry", "#bd007e"),
    ("Burgundy", "#960841"),
    ("Emerald", "#127d59"),
    ("Sky Blue", "#0b9cd0"),
    ("Sienna", "#aa5d41"),
    ("Royal Blue", "#0053fa"),
    ("Rust", "#802a00"),
]
SHOT_COLORS = [hex_color for _name, hex_color in SHOT_COLOR_SET]


def _shot_line_x(shot, lane, lane_start_x, lane_width, max_lanes, text_left_x):
    """Horizontal position of a shot's line: a margin lane's column, or -
    for a shot drawn over the text (Shot.overlay) - its own column inside
    the text area, counted from the text's left edge."""
    if shot.overlay:
        return round(text_left_x + (lane + 0.5) * lane_width)
    return lane_start_x + (lane % max_lanes) * lane_width


def _overlay_alpha(shot):
    """0-255 alpha for a shot drawn over the text, or None for a normal
    margin line (always fully opaque)."""
    if not shot.overlay:
        return None
    return round(255 * max(10, min(100, shot.overlay_opacity)) / 100)


def _draw_page_note(painter, text, x_left, bottom_y, max_width, ink, backdrop=None):
    """Free-text note printed above a shot's start: wrapped to `max_width`,
    smaller than the script text, with its bottom edge at `bottom_y`. A
    translucent `backdrop` keeps it readable when it crosses other shots'
    lines in the margin. Returns the note's top edge."""
    base = painter.font()
    note_font = QFont(base)
    point_size = base.pointSizeF()
    if point_size > 0:
        note_font.setPointSizeF(max(5.0, point_size * 0.75))
    painter.save()
    painter.setFont(note_font)
    flags = Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop
    bounds = painter.fontMetrics().boundingRect(QRect(0, 0, max(1, int(max_width)), 100000), flags, text)
    box = QRectF(x_left, bottom_y - bounds.height(), max_width, bounds.height())
    if backdrop is not None:
        painter.fillRect(box.adjusted(-2, -1, 2, 1), backdrop)
    painter.setPen(ink)
    painter.drawText(box, flags, text)
    painter.restore()
    return box.top()


def _draw_handle_arrows(painter, cx, cy, radius, horizontal):
    """Draws a small double-headed arrow icon (in white) centered at
    (cx, cy), sized to fit inside a handle circle of the given radius -
    horizontal (left/right) for the duplicate preview's side-to-side
    lane-picker handle, vertical (up/down) for its resize handles."""
    painter.save()
    pen = QPen(QColor(255, 255, 255, 230))
    pen.setWidth(2)
    painter.setPen(pen)
    shaft = max(3.0, radius * 0.75)
    head = max(2.0, radius * 0.4)
    if horizontal:
        painter.drawLine(QPointF(cx - shaft, cy), QPointF(cx + shaft, cy))
        painter.drawLine(QPointF(cx - shaft, cy), QPointF(cx - shaft + head, cy - head))
        painter.drawLine(QPointF(cx - shaft, cy), QPointF(cx - shaft + head, cy + head))
        painter.drawLine(QPointF(cx + shaft, cy), QPointF(cx + shaft - head, cy - head))
        painter.drawLine(QPointF(cx + shaft, cy), QPointF(cx + shaft - head, cy + head))
    else:
        painter.drawLine(QPointF(cx, cy - shaft), QPointF(cx, cy + shaft))
        painter.drawLine(QPointF(cx, cy - shaft), QPointF(cx - head, cy - shaft + head))
        painter.drawLine(QPointF(cx, cy - shaft), QPointF(cx + head, cy - shaft + head))
        painter.drawLine(QPointF(cx, cy + shaft), QPointF(cx - head, cy + shaft - head))
        painter.drawLine(QPointF(cx, cy + shaft), QPointF(cx + head, cy + shaft - head))
    painter.restore()


def _key_legend_row_height(scale=1.0):
    # Tightened from the body-text row height to match the Key's smaller
    # font (KEY_LEGEND_FONT_SHRINK_PT below the page font).
    return 14 * scale


def _key_legend_box_reserved_height(scale=1.0):
    """Vertical space inside the box that ISN'T available for rows - just
    the padding above the first row and below the last one. The "Key"
    title sits straddling the top border itself (like a groupbox legend)
    rather than occupying a row of its own, so it costs next to nothing
    here - that's what lets more rows fit per column."""
    return KEY_LEGEND_BOX_PADDING_PX * scale * 2


def _key_legend_columns(rows, scale=1.0):
    """Splits Key rows into columns that each fit within the fixed
    vertical band the legend box is allowed to occupy (between
    KEY_LEGEND_TOP_LIMIT_IN and KEY_LEGEND_BOTTOM_LIMIT_IN inches from
    the page's top edge, minus the title/padding reserved above) - if
    there isn't room for everything in one column, the overflow
    continues in a second (or third, ...) column."""
    row_height = _key_legend_row_height(scale)
    band_height_px = IN(KEY_LEGEND_BOTTOM_LIMIT_IN - KEY_LEGEND_TOP_LIMIT_IN) * scale
    available_for_rows = max(row_height, band_height_px - _key_legend_box_reserved_height(scale))
    max_rows = max(1, int(available_for_rows // row_height))
    return [rows[i:i + max_rows] for i in range(0, len(rows), max_rows)]


def _group_key_matches(shots, shot_type_style_map):
    """Groups each shot's Key match by line style, preserving first-seen
    order. Returns (order, groups) where groups[style] is the list of
    shot types matched to that style (None entries mean no shot type
    could be pinned down for this style at all).
    A shot only counts as matched through its Shot Type mapping if its
    OWN Line Style still agrees with what that mapping currently says -
    otherwise (a manual per-shot override, or a mapping that changed
    after the shot was created and was never propagated to it) it's
    unresolved for this style.
    A style that ends up with NO individually-attributable shot (every
    shot rendering it is typeless, or has drifted from the mapping)
    falls back to every Shot Type currently mapped to that style - the
    same names Settings would show - rather than the bare style word,
    which is what used to render and disagreed with what Settings shows
    for a typeless shot. A style that already has at least one real
    attributable shot type does NOT also get this fallback - unused
    sibling types mapped to the same style stay hidden, same as before."""
    order = []
    groups = {}
    seen = set()

    def add(style, shot_type):
        pair = (style, shot_type)
        if pair in seen:
            return
        seen.add(pair)
        if style not in groups:
            groups[style] = []
            order.append(style)
        groups[style].append(shot_type)

    unresolved_styles = []
    seen_unresolved = set()
    for shot in shots:
        mapped_style = shot_type_style_map.get(shot.shot_type) if shot.shot_type else None
        if mapped_style is not None and mapped_style == shot.line_style:
            add(mapped_style, shot.shot_type)
        elif shot.line_style not in seen_unresolved:
            seen_unresolved.add(shot.line_style)
            unresolved_styles.append(shot.line_style)

    for style in unresolved_styles:
        if style in groups:
            continue  # already has a real, attributable shot type
        named_types = [st for st, mapped in shot_type_style_map.items() if st and mapped == style]
        if named_types:
            for shot_type in named_types:
                add(style, shot_type)
        else:
            add(style, None)

    return order, groups


def _rows_from_groups(order, groups, multi_style_mode=None):
    """Turns a (order, groups) pair - as produced by _group_key_matches or
    _group_settings_key_matches - into (style, row_key, default_label)
    rows. When a style is matched by more than one shot type, its entry
    in multi_style_mode (style -> "merge"/"overwrite") picks how it's
    shown:
      - "merge": one row, auto-labeled "Wide/Medium/..." - a single
        sample line standing in for all of them.
      - "overwrite" (default): one row per shot type - "solid means:
        Wide" and "solid means: Medium" as separate lines, each
        individually editable/overridable.
    row_key is what a custom override in line_style_labels is keyed by;
    default_label is what's shown/used absent an override."""
    multi_style_mode = multi_style_mode or {}
    rows = []
    for style in order:
        shot_types = groups[style]
        named_types = [t for t in shot_types if t]
        mode = multi_style_mode.get(style, "overwrite") if len(named_types) > 1 else "overwrite"
        if mode == "merge":
            rows.append((style, style, "/".join(named_types)))
        else:
            for shot_type in shot_types:
                row_key = shot_type if shot_type else style
                rows.append((style, row_key, row_key))
    return rows


def _compute_key_rows(shots, shot_type_style_map, multi_style_mode=None):
    """Rows for the actual rendered/printed Key (on-screen and PDF) -
    shot-grounded, so it only ever shows what's really on the page right
    now. See _group_key_matches for the matching rules."""
    order, groups = _group_key_matches(shots, shot_type_style_map)
    return _rows_from_groups(order, groups, multi_style_mode)


def _group_settings_key_matches(shot_type_style_map, shots):
    """Groups matches for the Settings dialog's Key section. Unlike
    _group_key_matches (which drives the printed Key and requires an
    actual shot), every Shot Type -> Line Style mapping counts on its own
    here, regardless of whether any shot uses it yet - so the Merge/
    Overwrite controls and rows are visible immediately while configuring
    the mapping, before a single shot exists. A real shot whose own Line
    Style isn't explained by any current mapping (unmapped shot type, or
    one that's drifted from what its shot type now maps to) still
    contributes a generic, style-keyed match, same as the printed Key."""
    order = []
    groups = {}
    seen = set()

    def add(style, shot_type):
        key = (style, shot_type)
        if key in seen:
            return
        seen.add(key)
        if style not in groups:
            groups[style] = []
            order.append(style)
        groups[style].append(shot_type)

    for shot_type, style in shot_type_style_map.items():
        if shot_type:
            add(style, shot_type)

    for shot in shots:
        mapped_style = shot_type_style_map.get(shot.shot_type) if shot.shot_type else None
        if mapped_style is None or mapped_style != shot.line_style:
            add(shot.line_style, None)

    return order, groups


def _compute_settings_key_rows(shot_type_style_map, shots, multi_style_mode=None):
    """Rows for the Settings dialog's Key section - mapping-driven, see
    _group_settings_key_matches."""
    order, groups = _group_settings_key_matches(shot_type_style_map, shots)
    return _rows_from_groups(order, groups, multi_style_mode)


def _draw_key_legend(painter, page_width, page_top_y, rows, line_style_labels, scale=1.0,
                      position=KEY_BOX_POSITION_DEFAULT, ink_color=None, bg_color=None,
                      page_bottom_y=None):
    """Draws the Key legend inside a bordered box titled "Key", positioned
    per `position` (one of KEY_BOX_POSITIONS): horizontally left/centered/
    right on the page, and vertically anchored inside either the page's
    TOP margin ("top_left"/"top_center") or its BOTTOM margin
    ("bottom_left"/"bottom_center"/"bottom_right"). Either way the box
    sits within a fixed KEY_LEGEND_TOP_LIMIT_IN inch offset from whichever
    page edge it's anchored to, growing toward the content as more rows
    are needed - never past `page_bottom_y` (for bottom-anchored
    positions). `page_bottom_y` should be the actual bottom of THIS
    page's own rendered area - defaults to a full nominal
    PAGE_HEIGHT_PX-tall page below page_top_y if not given, but callers
    that clip/capture a shorter actual page (a page whose content ends
    before the nominal page height, which is the common case) MUST pass
    the real value, or the box can be positioned past what's actually
    visible/printed for that page. If the rows don't all fit in the
    available band, the overflow continues in a second (or third, ...)
    column inside the same box. Returns True if more than one column
    was needed, so callers can nudge the user toward "Merge Multiples"
    to shrink it back down to one."""
    if not rows:
        return False

    original_font = painter.font()
    key_font = QFont(original_font)
    # Font point sizes are never scaled by `scale` - unlike pixel-based
    # positions/dimensions in this function, a QFont's point size is
    # already resolution-independent once set via direct
    # painter.setFont()+drawText() (see the comment in _write_lined_pdf
    # where base_font/scaled_font are defined for why multiplying point
    # size by `scale` badly breaks direct text drawing).
    shrink = KEY_LEGEND_FONT_SHRINK_PT
    if key_font.pointSizeF() > 0:
        key_font.setPointSizeF(max(1.0, key_font.pointSizeF() - shrink))
    else:
        key_font.setPointSize(max(1, key_font.pointSize() - round(shrink)))
    title_font = QFont(key_font)
    title_font.setBold(True)

    painter.setFont(key_font)
    metrics = painter.fontMetrics()
    painter.setFont(title_font)
    title_metrics = painter.fontMetrics()

    row_height = _key_legend_row_height(scale)
    sample_len = 28 * scale
    pad = KEY_LEGEND_BOX_PADDING_PX * scale
    columns = _key_legend_columns(rows, scale)
    color = QColor(ink_color) if ink_color is not None else QColor(KEY_LEGEND_COLOR)
    box_bg_color = QColor(bg_color) if bg_color is not None else QColor("white")

    # First pass (no drawing yet): measure each column's content width so
    # the box's total size is known up front and can be centered.
    painter.setFont(key_font)
    col_widths = []
    for col_rows in columns:
        max_label_width = 0.0
        for style, row_key, default_label in col_rows:
            label = line_style_labels.get(row_key, default_label)
            max_label_width = max(max_label_width, metrics.horizontalAdvance(label))
        col_widths.append(sample_len + 6 * scale + max_label_width)

    columns_width = sum(col_widths) + KEY_LEGEND_COLUMN_GAP_PX * scale * (len(columns) - 1)
    title_width = title_metrics.horizontalAdvance(KEY_LEGEND_TITLE)
    title_pad = KEY_LEGEND_TITLE_PADDING_PX * scale
    box_width = max(columns_width, title_width + title_pad * 2) + pad * 2

    side_margin = KEY_LEGEND_SIDE_MARGIN_PX * scale
    if position in ("top_left", "bottom_left"):
        box_x = side_margin
    elif position == "bottom_right":
        box_x = page_width - box_width - side_margin
    else:  # top_center, bottom_center
        box_x = (page_width - box_width) / 2

    row_area_height = max(len(c) for c in columns) * row_height
    box_height = _key_legend_box_reserved_height(scale) + row_area_height
    if position in ("top_left", "top_center"):
        # Anchored in the page's TOP margin: the box's top edge always
        # touches KEY_LEGEND_TOP_LIMIT_IN from the page's top edge,
        # growing downward (toward the content start) as more rows are
        # needed.
        box_top = page_top_y + IN(KEY_LEGEND_TOP_LIMIT_IN) * scale
    else:  # bottom_left, bottom_center, bottom_right
        # Mirror image, anchored in the page's BOTTOM margin instead:
        # the box's BOTTOM edge always touches KEY_LEGEND_TOP_LIMIT_IN
        # from the page's bottom edge, growing UPWARD (toward the
        # content end) as more rows are needed. Pinning the bottom edge
        # (not the top) is what matters here - anchoring the top edge a
        # fixed distance from the page bottom instead would let a
        # multi-row box's bottom edge run past the page's actual bottom
        # edge into the next page's territory once it's taller than
        # that fixed distance.
        effective_bottom_y = page_bottom_y if page_bottom_y is not None else page_top_y + PAGE_HEIGHT_PX * scale
        box_bottom = effective_bottom_y - IN(KEY_LEGEND_TOP_LIMIT_IN) * scale
        box_top = box_bottom - box_height

    border_pen = QPen(color)
    border_pen.setWidthF(max(0.75, scale * 0.6))
    border_pen.setStyle(Qt.SolidLine)
    painter.setPen(border_pen)
    painter.drawRect(QRectF(box_x, box_top, box_width, box_height))

    # "Key" title straddles the top border instead of sitting inside it -
    # erase a gap in the line behind the text (matching the page's white
    # background), then draw the title centered on the border, like a
    # classic groupbox legend. This is what reclaims the vertical space a
    # dedicated title row used to cost, letting more rows fit per column.
    painter.setFont(title_font)
    title_x = box_x + (box_width - title_width) / 2
    title_text_height = title_metrics.ascent() + title_metrics.descent()
    gap_rect = QRectF(
        title_x - title_pad, box_top - title_text_height / 2 - 1 * scale,
        title_width + title_pad * 2, title_text_height + 2 * scale,
    )
    painter.fillRect(gap_rect, box_bg_color)
    painter.setPen(color)
    title_baseline_y = box_top + (title_metrics.ascent() - title_metrics.descent()) / 2
    painter.drawText(round(title_x), round(title_baseline_y), KEY_LEGEND_TITLE)

    painter.setFont(key_font)
    rows_top = box_top + pad
    col_x = box_x + pad
    for col_index, col_rows in enumerate(columns):
        for i, (style, row_key, default_label) in enumerate(col_rows):
            row_y = rows_top + i * row_height + row_height / 2
            label = line_style_labels.get(row_key, default_label)

            pen = QPen(color)
            pen.setWidth(max(1, round(2 * scale)))
            if style == "double":
                # Qt has no native "double line" pen style - draw two
                # parallel lines, matching how a double-style shot's own
                # line is drawn on the page.
                pen.setStyle(Qt.SolidLine)
                painter.setPen(pen)
                offset = max(1, round(2 * scale))
                painter.drawLine(round(col_x), round(row_y - offset), round(col_x + sample_len), round(row_y - offset))
                painter.drawLine(round(col_x), round(row_y + offset), round(col_x + sample_len), round(row_y + offset))
            else:
                pen.setStyle(LINE_STYLE_MAP.get(style, Qt.SolidLine))
                painter.setPen(pen)
                painter.drawLine(round(col_x), round(row_y), round(col_x + sample_len), round(row_y))

            painter.setPen(color)
            painter.drawText(round(col_x + sample_len + 6 * scale), round(row_y + metrics.ascent() / 2), label)

        col_x += col_widths[col_index] + KEY_LEGEND_COLUMN_GAP_PX * scale

    painter.setFont(original_font)
    return len(columns) > 1


_ORIGINAL_MARGIN_PROPERTY = QTextFormat.UserProperty + 1
# The screenplay element type ("Character", "Dialogue", ...) a block
# was rendered as - set by _render_paragraphs_into_document, read by
# _apply_page_gaps_to_document for widow control and (MORE)/(CONT'D).
# Blocks from plain-text (non-structured) scripts never carry this, so
# both features simply don't apply there - there's no element-type
# information to reason about in that case.
_PARA_TYPE_PROPERTY = QTextFormat.UserProperty + 2
# Marks the first block of a real mid-paragraph (MORE)/(CONT'D) split
# (see _try_split_dialogue_block) - its value is the block's ORIGINAL,
# pre-split text, so _undo_dialogue_splits can restore it exactly.
_SPLIT_TEXT_PROPERTY = QTextFormat.UserProperty + 3
# Marks the 3 NEW blocks a split inserts ("(MORE)", "SPEAKER (CONT'D)",
# and the rest of the dialogue) - unlike the (truncated) first block,
# these have no corresponding entry in loaded_paragraphs at all, so
# anything walking the document in lockstep with loaded_paragraphs
# (_compute_scene_numbers, get_scene_number_for_position) must skip
# them or the block-index/paragraph-index correspondence they depend
# on drifts by 3 for every split.
_SYNTHETIC_BLOCK_PROPERTY = QTextFormat.UserProperty + 4
# Element types that stay attached to the dialogue immediately
# following them - a page break is never allowed to leave one of these
# stranded alone at the bottom of a page (see _apply_page_gaps_to_document).
CUE_TYPES = {"Character", "Parenthetical"}
# Same idea for imported-document headings: a heading is never left
# alone at the bottom of a page with its first paragraph on the next.
KEEP_WITH_NEXT_TYPES = CUE_TYPES | {"Doc Title", "Doc Section", "Doc Heading"}
# A split that would leave fewer than this many characters on either
# side isn't worth it - reject it (fall back to moving the whole
# paragraph) rather than producing a "(MORE)" after two words.
MIN_DIALOGUE_SPLIT_CHARS = 20
_CONTD_SUFFIX = " (CONT'D)"


def _bare_speaker_name(text: str) -> str:
    """Strips a trailing " (CONT'D)" - a speech split more than once
    (a long enough monologue to cross 3+ pages) walks back and finds
    the PREVIOUS split's synthetic "SPEAKER (CONT'D)" block (tagged
    Character for margin/rendering purposes) as its nearest cue;
    without this it would relabel the next break "SPEAKER (CONT'D)
    (CONT'D)" instead of just "SPEAKER (CONT'D)" again."""
    return text[: -len(_CONTD_SUFFIX)] if text.endswith(_CONTD_SUFFIX) else text


class SplitMap:
    """Tracks the character-offset difference between a document's
    "clean" text (what was actually imported/typed - what shot
    positions, script diffing, and saved project files are always
    measured against) and its "rendered" text (what's currently on
    screen/printed, which may have real "(MORE)"/"CHARACTER (CONT'D)"
    text spliced in by _try_split_dialogue_block - see
    _apply_page_gaps_to_document). Rebuilt from scratch every time
    pagination recomputes, since resizing/zooming/editing Settings can
    change where - or whether - a paragraph gets split.

    Every ScriptTextEdit carries its OWN SplitMap (self.split_map),
    matching its OWN document at its OWN scale - a position from one
    view's rendered text must never be passed through another view's
    map, or through a different document's map (e.g. the PDF export's
    own document, built independently at export DPI)."""

    def __init__(self):
        self._entries = []  # sorted [(clean_pos, cumulative_before, inserted_len)]

    def add_split(self, clean_pos: int, inserted_len: int):
        cumulative_before = self._entries[-1][1] + self._entries[-1][2] if self._entries else 0
        self._entries.append((clean_pos, cumulative_before, inserted_len))

    def clean_to_rendered(self, clean_pos: int) -> int:
        delta = 0
        for split_pos, cumulative_before, inserted_len in self._entries:
            if split_pos <= clean_pos:
                delta = cumulative_before + inserted_len
            else:
                break
        return clean_pos + delta

    def rendered_to_clean(self, rendered_pos: int) -> int:
        delta = 0
        for split_pos, cumulative_before, inserted_len in self._entries:
            rendered_start = split_pos + cumulative_before
            rendered_end = rendered_start + inserted_len
            if rendered_pos >= rendered_end:
                delta = cumulative_before + inserted_len
            elif rendered_pos >= rendered_start:
                # Falls inside the synthetic "(MORE)"/"CONT'D" text
                # itself (e.g. a click landed there) - there's no
                # sensible clean position "inside" text that doesn't
                # exist in the source, so clamp to the split point.
                return split_pos
            else:
                break
        return rendered_pos - delta


def _block_fragments(block):
    """[(text, QTextCharFormat), ...] for a block's content, in the
    order it appears - preserves each fragment's own rich-text
    formatting (bold/italic/underline/strikethrough, see
    _render_paragraphs_into_document), unlike block.text() alone."""
    fragments = []
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid():
            fragments.append((frag.text(), frag.charFormat()))
        it += 1
    return fragments


def _split_fragments_at(fragments, local_pos):
    """(before, after) - splits a fragment list (see _block_fragments)
    at character offset `local_pos` within their combined text,
    dividing the one fragment that straddles the boundary (if any) so
    neither side loses its original formatting."""
    before, after = [], []
    pos = 0
    for text, fmt in fragments:
        frag_start, frag_end = pos, pos + len(text)
        pos = frag_end
        if frag_end <= local_pos:
            before.append((text, fmt))
        elif frag_start >= local_pos:
            after.append((text, fmt))
        else:
            cut = local_pos - frag_start
            before.append((text[:cut], fmt))
            after.append((text[cut:], fmt))
    return before, after


def _rstrip_fragments(fragments):
    if not fragments:
        return fragments
    text, fmt = fragments[-1]
    stripped = text.rstrip()
    return fragments[:-1] + ([(stripped, fmt)] if stripped else [])


def _lstrip_fragments(fragments):
    if not fragments:
        return fragments
    text, fmt = fragments[0]
    stripped = text.lstrip()
    return ([(stripped, fmt)] if stripped else []) + fragments[1:]


def _find_dialogue_split_position(doc, block, current_page_top, content_height):
    """Local character offset (within `block`'s own text) where its
    wrapped lines first cross `content_height`, measured from
    current_page_top - the natural place to break for (MORE)/(CONT'D).
    None if the block has only one line (nothing to split), if every
    line already fits (the overflow test that got us here was a false
    positive), or if NO line fits at all (the whole block should just
    move, same as before - there's nothing to leave behind).

    If the natural boundary would leave too little text on the
    continuation side (e.g. the paragraph's last wrapped line trails
    off after just a few characters), back off to the latest earlier
    line boundary that gives both halves at least
    MIN_DIALOGUE_SPLIT_CHARS - this sacrifices a line or two of
    current-page fill in exchange for a real, worthwhile split instead
    of abandoning it and pushing the ENTIRE paragraph to the next page
    (which is what created the big gaps this feature exists to fix).
    Returns None only when even the earliest possible split (line 1)
    can't satisfy the minimum on the "before" side - i.e. almost
    nothing fits on the current page anyway, so moving the whole block
    is genuinely the best option."""
    layout = block.layout()
    if layout is None or layout.lineCount() <= 1:
        return None
    block_top = doc.documentLayout().blockBoundingRect(block).top()
    pitch = _fixed_line_pitch(block)
    split_line_index = None
    for i in range(layout.lineCount()):
        line = layout.lineAt(i)
        # Fixed-pitch lines end where the next one starts - see _flow_bottom.
        line_bottom = block_top + (i + 1) * pitch if pitch else block_top + line.y() + line.height()
        if line_bottom - current_page_top > content_height:
            split_line_index = i
            break
    if not split_line_index:  # None, or 0 (nothing at all fits)
        return None
    full_text = block.text()
    for i in range(split_line_index, 0, -1):
        local_pos = layout.lineAt(i).textStart()
        if local_pos >= MIN_DIALOGUE_SPLIT_CHARS and (len(full_text) - local_pos) >= MIN_DIALOGUE_SPLIT_CHARS:
            return local_pos
    return None


def _try_split_dialogue_block(doc, block, current_page_top, content_height, total_gap, px_per_inch):
    """Attempts a real mid-paragraph (MORE)/(CONT'D) split of a
    Dialogue block that doesn't fit in the remaining page space,
    turning ONE paragraph into FOUR blocks: the truncated original
    (kept on the current page), a centered "(MORE)" line, a
    "SPEAKER (CONT'D)" line starting the new page (this becomes the
    actual page-break block), and the rest of the dialogue continuing
    normally. Returns (contd_block, local_split_pos) on success - the
    caller adds contd_block to break_blocks so the usual margin-gap
    step turns its top margin into the page break, and uses
    local_split_pos (the split point within the ORIGINAL block's own
    text) to record where this happened in SplitMap terms. Returns
    None if splitting isn't possible/worthwhile here - the caller
    falls back to moving the whole block,
    exactly like before this feature existed."""
    local_split_pos = _find_dialogue_split_position(doc, block, current_page_top, content_height)
    if local_split_pos is None:
        return None

    speaker = None
    cue = block.previous()
    while cue.isValid() and cue.blockFormat().property(_PARA_TYPE_PROPERTY) != "Character":
        cue = cue.previous()
    if cue.isValid():
        speaker = _bare_speaker_name(cue.text())
    if not speaker:
        return None  # can't label CONT'D without knowing who's speaking

    full_text = block.text()
    before, after = _split_fragments_at(_block_fragments(block), local_split_pos)
    before = _rstrip_fragments(before)
    after = _lstrip_fragments(after)
    if not before or not after:
        return None

    def in_(inches):
        return round(inches * px_per_inch)

    dialogue_style = SCREENPLAY_STYLE_MAP["Dialogue"]
    character_style = SCREENPLAY_STYLE_MAP["Character"]
    blank_fmt = QTextCharFormat()

    cursor = QTextCursor(block)
    cursor.setPosition(block.position())
    cursor.setPosition(block.position() + len(full_text), QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    cursor.setCharFormat(blank_fmt)
    for text, fmt in before:
        cursor.insertText(text, fmt)
    truncated_fmt = QTextBlockFormat(block.blockFormat())
    truncated_fmt.setProperty(_SPLIT_TEXT_PROPERTY, full_text)
    cursor.setBlockFormat(truncated_fmt)

    more_fmt = QTextBlockFormat()
    _set_script_line_height(more_fmt, px_per_inch)
    more_fmt.setAlignment(Qt.AlignCenter)
    more_fmt.setProperty(_SYNTHETIC_BLOCK_PROPERTY, True)
    cursor.insertBlock(more_fmt, blank_fmt)
    cursor.insertText("(MORE)")

    contd_fmt = QTextBlockFormat()
    _set_script_line_height(contd_fmt, px_per_inch)
    contd_fmt.setLeftMargin(in_(character_style["left_in"]))
    contd_fmt.setRightMargin(in_(character_style["right_in"]))
    contd_fmt.setAlignment(character_style["align"])
    contd_fmt.setTopMargin(total_gap)
    contd_fmt.setProperty(_PARA_TYPE_PROPERTY, "Character")
    contd_fmt.setProperty(_SYNTHETIC_BLOCK_PROPERTY, True)
    cursor.insertBlock(contd_fmt, blank_fmt)
    cursor.insertText(f"{speaker} (CONT'D)")
    contd_block = cursor.block()

    cont_fmt = QTextBlockFormat()
    _set_script_line_height(cont_fmt, px_per_inch)
    cont_fmt.setLeftMargin(in_(dialogue_style["left_in"]))
    cont_fmt.setRightMargin(in_(dialogue_style["right_in"]))
    cont_fmt.setAlignment(dialogue_style["align"])
    cont_fmt.setProperty(_PARA_TYPE_PROPERTY, "Dialogue")
    cont_fmt.setProperty(_SYNTHETIC_BLOCK_PROPERTY, True)
    cursor.insertBlock(cont_fmt, blank_fmt)
    for text, fmt in after:
        cursor.insertText(text, fmt)

    return contd_block, local_split_pos


def _undo_dialogue_splits(doc):
    """Reverses _try_split_dialogue_block's mutations, merging each
    split group (truncated original + "(MORE)" + "CONT'D" + rest of
    the dialogue) back into the ONE original Dialogue block so
    pagination can be recomputed fresh - same idempotency pattern as
    the margin-gap undo below, needed because split points shift with
    page geometry (zoom, window resize, Settings) just like everything
    else here."""
    group_starts = []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().hasProperty(_SPLIT_TEXT_PROPERTY):
            group_starts.append(block)
        block = block.next()

    for start_block in group_starts:
        original_text = start_block.blockFormat().property(_SPLIT_TEXT_PROPERTY)
        end_block = start_block
        for _ in range(3):  # "(MORE)", "CONT'D", and the rest of the dialogue
            end_block = end_block.next()
        cursor = QTextCursor(doc)
        cursor.setPosition(start_block.position())
        cursor.setPosition(end_block.position() + len(end_block.text()), QTextCursor.MoveMode.KeepAnchor)
        restored_fmt = QTextBlockFormat(start_block.blockFormat())
        restored_fmt.clearProperty(_SPLIT_TEXT_PROPERTY)
        cursor.removeSelectedText()
        cursor.setCharFormat(QTextCharFormat())
        cursor.insertText(original_text)
        cursor.setBlockFormat(restored_fmt)


def _apply_page_gaps_to_document(doc, content_height, total_gap, px_per_inch=PX_PER_INCH):
    """Paginates `doc` in place: undoes any splits/gaps from a
    previous call (idempotent, since this gets recomputed on every
    resize/zoom/Settings change), then re-walks the document deciding
    where each page breaks - splitting a Dialogue paragraph mid-line
    with real (MORE)/(CONT'D) text where that lets a page pack tighter
    (see _try_split_dialogue_block), applying widow control otherwise
    (see CUE_TYPES), and finally stamping the actual page-break gap
    margins. Returns (content_starts, more_cont_speakers, split_map):
    content_starts/more_cont_speakers are unchanged in shape from
    before this feature (one page-number-label-worthy speaker or None
    per break, for the OLDER whole-paragraph-move case where a speech
    was already spread across separate Dialogue paragraphs); split_map
    is a SplitMap converting between this document's clean and
    rendered character positions - see SplitMap's docstring."""
    _undo_dialogue_splits(doc)

    block = doc.begin()
    while block.isValid():
        fmt = block.blockFormat()
        if fmt.hasProperty(_ORIGINAL_MARGIN_PROPERTY):
            cursor = QTextCursor(block)
            restored_fmt = QTextBlockFormat(fmt)
            restored_fmt.setTopMargin(fmt.property(_ORIGINAL_MARGIN_PROPERTY))
            restored_fmt.clearProperty(_ORIGINAL_MARGIN_PROPERTY)
            cursor.setBlockFormat(restored_fmt)
        block = block.next()

    def para_type(b):
        return b.blockFormat().property(_PARA_TYPE_PROPERTY)

    split_map = SplitMap()
    length_before_splits = doc.characterCount()

    break_blocks = []
    # Parallel to break_blocks: the speaking character's name if that
    # break is the OLDER "whole paragraph moved" case for an
    # already-in-progress speech (so a paint-only (MORE)/(CONT'D)
    # overlay should be drawn there - see paintEvent/_write_lined_pdf),
    # else None. A REAL split (_try_split_dialogue_block) already has
    # literal "(MORE)"/"CONT'D" text in the document, so its entry is
    # always None here - drawing the overlay too would double it up.
    more_cont_speakers = []
    current_page_top = 0.0
    first_block = True
    block = doc.begin()
    while block.isValid():
        rect = doc.documentLayout().blockBoundingRect(block)
        if first_block:
            current_page_top = rect.top()
            first_block = False
        elif _flow_bottom(doc, block) - current_page_top > content_height and rect.top() > current_page_top:
            if para_type(block) == "Dialogue":
                block_position = block.position()
                length_before_this_split = doc.characterCount()
                # This block's own start, translated back to clean
                # coordinates by undoing every insertion made by any
                # EARLIER split in this same pass.
                block_clean_pos = block_position - (length_before_this_split - length_before_splits)
                split_result = _try_split_dialogue_block(
                    doc, block, current_page_top, content_height, total_gap, px_per_inch
                )
                if split_result is not None:
                    contd_block, local_split_pos = split_result
                    inserted_len = doc.characterCount() - length_before_this_split
                    split_map.add_split(block_clean_pos + local_split_pos, inserted_len)
                    break_blocks.append(contd_block)
                    more_cont_speakers.append(None)
                    current_page_top = doc.documentLayout().blockBoundingRect(contd_block).top()
                    block = contd_block
                    continue

            # Widow control: never strand a Character/Parenthetical cue
            # alone at the bottom of a page - pull it (and any earlier
            # contiguous cue) onto the new page with its dialogue,
            # Fade In-style.
            adjusted = block
            prev = adjusted.previous()
            while (
                prev.isValid()
                and para_type(prev) in KEEP_WITH_NEXT_TYPES
                # Never pull back the page's own first block - that would
                # put a break at the very top of a page and waste it.
                and doc.documentLayout().blockBoundingRect(prev).top() > current_page_top
            ):
                adjusted = prev
                prev = adjusted.previous()

            # (MORE)/(CONT'D) overlay: only for the whole-paragraph-move
            # case - triggers when the speech is ALREADY spread across
            # multiple Character/Parenthetical/Dialogue paragraphs and
            # the split lands between two of them, not at the speech's
            # very start (which is exactly the widow case handled
            # above, with no earlier content to "continue" from).
            speaker = None
            if para_type(adjusted) == "Dialogue" and prev.isValid() and para_type(prev) in ({"Dialogue"} | CUE_TYPES):
                cue = prev
                while cue.isValid() and para_type(cue) != "Character":
                    cue = cue.previous()
                if cue.isValid():
                    speaker = _bare_speaker_name(cue.text())

            break_blocks.append(adjusted)
            more_cont_speakers.append(speaker)
            current_page_top = doc.documentLayout().blockBoundingRect(adjusted).top()
        block = block.next()

    for block in break_blocks:
        cursor = QTextCursor(block)
        block_fmt = QTextBlockFormat(block.blockFormat())
        block_fmt.setProperty(_ORIGINAL_MARGIN_PROPERTY, block_fmt.topMargin())
        block_fmt.setTopMargin(total_gap)
        cursor.setBlockFormat(block_fmt)

    doc.documentLayout().documentSize()

    content_starts = []
    first_block_obj = doc.begin()
    if first_block_obj.isValid():
        content_starts.append(doc.documentLayout().blockBoundingRect(first_block_obj).top())
    for block in break_blocks:
        content_starts.append(doc.documentLayout().blockBoundingRect(block).top())

    return content_starts, more_cont_speakers, split_map


def _draw_document_in_ink(painter, doc, rect, ink="#000000"):
    """QTextDocument.drawContents, but with the text color fixed instead
    of taken from the computer's palette. drawContents paints any text
    that doesn't carry its own color in the application palette's Text
    color - which is WHITE on a Mac (or PC) in Dark Mode, so an exported
    PDF came out with white-on-white script text: invisible on paper, yet
    still selectable. A PDF is always black ink on white paper (see
    MainWindow._write_lined_pdf), whatever the system appearance."""
    context = QAbstractTextDocumentLayout.PaintContext()
    palette = QPalette()
    ink_color = QColor(ink)
    for role in (
        QPalette.Text, QPalette.WindowText, QPalette.ButtonText, QPalette.BrightText,
        QPalette.PlaceholderText,
    ):
        palette.setColor(role, ink_color)
    for role in (QPalette.Base, QPalette.Window, QPalette.Button):
        palette.setColor(role, QColor("#ffffff"))
    context.palette = palette
    context.clip = rect
    doc.documentLayout().draw(painter, context)


def _shot_notes_html(shot):
    """Rich-text block for one shot on a back-of-page notes page: the id
    in bold, its type/lens/movement on one line, then description,
    notes, and (greyed, italic) an excerpt of the lines it covers."""
    from html import escape

    def block(text):
        return escape(text.strip()).replace("\n", "<br>")

    details = " / ".join(
        part.strip() for part in (shot.shot_type, shot.lens, shot.camera_movement) if part and part.strip()
    )
    parts = [f"<b>{escape(shot.shot_id)}</b>" + (f" &nbsp;&nbsp;{escape(details)}" if details else "")]
    if shot.description and shot.description.strip():
        parts.append(block(shot.description))
    if shot.notes and shot.notes.strip():
        parts.append("<i>Notes:</i> " + block(shot.notes))
    excerpt = " ".join((shot.text or "").split())
    if excerpt:
        if len(excerpt) > 110:
            excerpt = excerpt[:107].rstrip() + "..."
        parts.append(f'<span style="color:#888888"><i>\u201c{escape(excerpt)}\u201d</i></span>')
    return "<p style='margin:0'>" + "<br>".join(parts) + "</p>"


class ScriptTextEdit(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Deterministic outer size: no frame border, and the vertical
        # scrollbar always reserved (not "as needed") so its width doesn't
        # change - and therefore doesn't feed back into the scale
        # computation - as the document crosses the single-page/
        # multi-page boundary. PageAspectContainer accounts for this
        # reserved width when it fits the page's geometry. No horizontal
        # scrollbar here: script_view's own fixed outer width and its
        # document's text width are always set to the same value (see
        # PageAspectContainer._fit_geometry), so internal horizontal
        # overflow should never occur - PageAspectContainer's own
        # QScrollArea is what pans when a zoomed-in page is wider than
        # the available pane.
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        # Explicit, not inherited: Qt stylesheets cascade an unscoped
        # "background-color" rule down to every child widget that lacks
        # its own stylesheet, so PageAspectContainer's dark-grey canvas
        # background (and, in "dark_ui" mode, the app-wide dark
        # stylesheet) would otherwise bleed through onto the page itself.
        # This override keeps the page a light page-color regardless of
        # what the container/app is styled with, unless dark_page is on
        # (Dark Mode's "invert page too" option) - see set_dark_page.
        self.dark_page = False
        self._apply_page_color()
        self.position_pick_callback = None
        self.shots_provider = lambda: []
        self.scale = 1.0  # current interactive zoom factor; see PageAspectContainer
        self.zoom_callback = None  # (factor: float) -> None; see wheelEvent/event and MainWindow._build_ui
        self._wheel_zoom_accum = 0  # accumulated Ctrl+wheel angleDelta.y() between whole "clicks" - see wheelEvent
        self.base_lane_width = LANE_WIDTH  # unscaled reference; only set_lane_capacity() changes this
        self.lane_start_x = LANE_START_X
        self.lane_width = LANE_WIDTH
        self.max_lanes = MAX_LANES
        self.lane_count = DEFAULT_LANE_COUNT  # numbered lanes shown in Settings - see _lane_disabled
        self.cross_size = CROSS_SIZE
        self.line_width = 2
        self._apply_scale_to_lane_geometry()
        self.show_page_markers = True
        self.page_content_starts = [0]
        self.page_more_speakers = []  # parallel to page_content_starts[1:] - see _apply_page_gaps_to_document
        self.split_map = SplitMap()  # clean<->rendered position conversion - see SplitMap
        self.scene_numbers = []  # [(block_start_position, scene_number_str), ...]
        self.page_gap_style = "gap"  # or "dotted"
        self.key_enabled = False
        self.key_placement = "first_page"  # or "every_page"
        self.key_box_position = KEY_BOX_POSITION_DEFAULT
        self.line_style_labels = {}
        self.shot_type_style_map = {}
        self.multi_style_mode = {}
        self.show_lanes = False
        self.active_lanes = set(range(MAX_LANES))  # which of the 6 nominal lanes are enabled (Settings)
        self.auto_sort_shots = False  # an automatic lane order is on (alphabetical or by length) - manual drag-to-reassign is a no-op while this is on
        self.label_every_segment = True  # shot number above every section of a split shot
        self.lane_drag_callback = None  # (shot_id, new_lane) -> None
        self.key_overflow_callback = None  # (needs_two_columns: bool) -> None
        self._dragging_shot_id = None
        self._drag_lane_preview = None
        # Placement preview: {"start": int, "end": int, "lane": int}
        # while the user is positioning either a not-yet-created copy
        # ("duplicate" mode) or an existing shot's own start/end/lane
        # ("edit" mode) - see MainWindow._begin_placement/
        # _place_duplicate_preview. None when no placement is in progress.
        self.duplicate_preview = None
        # In "edit" mode, this is the shot_id being edited - it's excluded
        # from the other-shots opacity fade below (it's still the current
        # subject, not just a reference), unlike "duplicate" mode where
        # every existing shot (including the source) fades equally.
        self.editing_shot_id = None
        self._dragging_duplicate_edge = None  # "start" | "end" | "move" | None
        # Needed so mouseMoveEvent fires (for hover cursor feedback over
        # the duplicate preview's handles) even while no button is held.
        # Mouse events over the content area actually arrive on the
        # viewport widget, not this outer QTextEdit - both need tracking
        # enabled, or hover moves never get delivered at all.
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def _apply_page_color(self):
        bg = PAGE_DARK_BG if self.dark_page else "white"
        ink = PAGE_DARK_INK if self.dark_page else "black"
        self.setStyleSheet(f"background-color: {bg}; color: {ink};")

    def set_dark_page(self, dark: bool):
        """Dark Mode's "invert page too" option - flips the page itself
        (background and text) dark, on top of whatever the app-wide
        chrome theme already did. Never used for export (_write_lined_pdf
        always renders a plain light page) - purely an interactive-view
        setting. See MainWindow._apply_theme."""
        if dark == self.dark_page:
            return
        self.dark_page = dark
        self._apply_page_color()
        self.viewport().update()

    def _apply_scale_to_lane_geometry(self):
        """Recomputes lane_start_x/lane_width/max_lanes/cross_size/
        line_width from self.scale and self.base_lane_width - the exact
        same formulas _write_lined_pdf uses for export at an arbitrary
        DPI, just driven by the interactive zoom factor instead. Call
        whenever self.scale changes, or after set_lane_capacity() changes
        base_lane_width (a user's manual "Make Lines Smaller" choice)."""
        self.lane_start_x = round(LANE_START_X * self.scale)
        # The floor scales down with the zoom (but never above its full
        # size): a flat 8px minimum would stop every lane from fitting once
        # the view is zoomed far out, since the margin shrinks but the
        # floor wouldn't.
        self.lane_width = max(
            round(MIN_LANE_WIDTH * min(1.0, self.scale)), round(self.base_lane_width * self.scale), 2
        )
        left_margin_scaled = PAGE_MARGIN_LEFT_PX * self.scale
        self.max_lanes = max(1, int((left_margin_scaled - self.lane_start_x) // self.lane_width))
        self.cross_size = max(3, round(CROSS_SIZE * self.scale))
        self.line_width = max(1, round(2 * self.scale))

    def apply_page_gaps(self):
        # Pin to the fixed (scaled) page width rather than trusting the
        # widget's current viewport width, which Qt hasn't necessarily
        # settled to a real value for yet (e.g. when a project is
        # restored on launch, before the window has ever been shown/laid
        # out - or at any DPI scaling where font metrics aren't final
        # until actually on screen).
        self.document().setTextWidth(PAGE_WIDTH_PX * self.scale)
        if self.page_gap_style == "dotted":
            total_gap = DOTTED_GAP_TOTAL_PX * self.scale
        else:
            total_gap = (PAGE_MARGIN_BOTTOM_PX + PAGE_GAP_PX + PAGE_MARGIN_TOP_PX) * self.scale
        self.page_content_starts, self.page_more_speakers, self.split_map = _apply_page_gaps_to_document(
            self.document(), CONTENT_HEIGHT_PX * self.scale, total_gap, PX_PER_INCH * self.scale
        )

    def _ensure_page_gaps_current(self):
        # Qt's own layout machinery can silently reset the document's text
        # width (e.g. once the widget is actually shown/resized after
        # apply_page_gaps() was called too early). Paint time is the one
        # moment geometry and font metrics are guaranteed to be real and
        # final, so re-pin and recompute here whenever that's happened.
        target_width = PAGE_WIDTH_PX * self.scale
        if abs(self.document().textWidth() - target_width) > 0.5:
            self.apply_page_gaps()

    def set_lane_capacity(self, needed_lanes: int):
        """User-triggered lane shrink (the "Make Lines Smaller" choice in
        the overlap-warning dialog). Sets base_lane_width from the
        UNSCALED reference so the choice scales proportionally with the
        view instead of reverting to the default density on the next
        resize."""
        needed_lanes = max(1, needed_lanes)
        available_width = PAGE_MARGIN_LEFT_PX - LANE_START_X
        self.base_lane_width = max(MIN_LANE_WIDTH, available_width // needed_lanes)
        self._apply_scale_to_lane_geometry()
        self.viewport().update()

    def _hit_test_shot_line(self, pos):
        """Returns the shot_id whose line is under the given viewport
        position, or None. Used for Show Lanes drag-to-reassign."""
        x, y = pos.x(), pos.y()
        tolerance = max(6, self.lane_width // 2)
        for shot, lane in self.shots_provider():
            if shot.overlay:
                continue  # drawn over the text, not in a draggable margin lane
            lane_x = self.lane_start_x + (lane % self.max_lanes) * self.lane_width
            if abs(x - lane_x) > tolerance:
                continue
            for seg_start, seg_end in shot.all_ranges():
                # shot.all_ranges() is always in "clean" (stable, saved/
                # diffed) coordinates - convert to this document's
                # current rendered positions before using them, since a
                # real (MORE)/(CONT'D) split may have shifted everything
                # after it (see SplitMap).
                top = self._y_for_position(self.split_map.clean_to_rendered(seg_start))
                bottom = self._y_for_position(self.split_map.clean_to_rendered(seg_end))
                if top is None or bottom is None:
                    continue
                top, bottom = min(top, bottom), max(top, bottom)
                if top - tolerance <= y <= bottom + tolerance:
                    return shot.shot_id
        return None

    def _duplicate_preview_x(self):
        lane = self.duplicate_preview.get("lane", 0)
        return self.lane_start_x + (lane % self.max_lanes) * self.lane_width

    def _snap_to_active_lane(self, lane):
        """Nudges a mouse-derived lane index off a disabled lane (see
        _lane_disabled) onto the nearest enabled one, so a drag can never
        drop a shot somewhere Settings says it shouldn't go."""
        lane = max(0, min(self.max_lanes - 1, lane))
        if not _lane_disabled(lane, self.active_lanes, self.lane_count):
            return lane
        for offset in range(1, MAX_LANE_COUNT + 1):
            for candidate in (lane - offset, lane + offset):
                if 0 <= candidate < self.max_lanes and not _lane_disabled(candidate, self.active_lanes, self.lane_count):
                    return candidate
        return lane

    def _hit_test_duplicate_handle(self, pos):
        """Returns "start", "end", "move", or None: which (if any) of the
        duplicate preview's three draggable handles pos is near - the two
        end handles (resize that end, up/down only) or the center handle
        (reassign the lane, side to side only). A bigger tolerance than
        _hit_test_shot_line's - these are meant to be an obvious,
        easy-to-grab target, not a precision hit."""
        if self.duplicate_preview is None:
            return None
        x = self._duplicate_preview_x()
        tolerance = max(10, self.cross_size * 2)
        if abs(pos.x() - x) > tolerance:
            return None
        top = self._y_for_position(self.duplicate_preview["start"])
        bottom = self._y_for_position(self.duplicate_preview["end"])
        if top is None or bottom is None:
            return None
        top, bottom = min(top, bottom), max(top, bottom)
        if abs(pos.y() - top) <= tolerance:
            return "start"
        if abs(pos.y() - bottom) <= tolerance:
            return "end"
        if abs(pos.y() - (top + bottom) / 2) <= tolerance:
            return "move"
        return None

    def mousePressEvent(self, event):
        if self.position_pick_callback is not None:
            cursor = self.cursorForPosition(event.pos())
            callback = self.position_pick_callback
            self.position_pick_callback = None
            self.viewport().unsetCursor()
            callback(cursor.position())
            return
        if self.duplicate_preview is not None and event.button() == Qt.LeftButton:
            edge = self._hit_test_duplicate_handle(event.pos())
            if edge is not None:
                self._dragging_duplicate_edge = edge
                self.viewport().setCursor(Qt.ClosedHandCursor if edge == "move" else Qt.SizeVerCursor)
                return
        if self.show_lanes and not self.auto_sort_shots and event.button() == Qt.LeftButton:
            shot_id = self._hit_test_shot_line(event.pos())
            if shot_id is not None:
                self._dragging_shot_id = shot_id
                self._drag_lane_preview = None
                self.viewport().setCursor(Qt.ClosedHandCursor)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging_duplicate_edge is not None:
            preview = self.duplicate_preview
            if self._dragging_duplicate_edge == "move":
                # Side to side only - reassigns which lane the duplicate
                # will occupy, exactly like Show Lanes' drag-to-reassign.
                lane = round((event.pos().x() - self.lane_start_x) / self.lane_width)
                lane = self._snap_to_active_lane(lane)
                if lane != preview.get("lane"):
                    preview["lane"] = lane
                    self.viewport().update()
                return
            position = self.cursorForPosition(event.pos()).position()
            if self._dragging_duplicate_edge == "start":
                preview["start"] = min(position, preview["end"] - 1)
            else:
                preview["end"] = max(position, preview["start"] + 1)
            self.viewport().update()
            return
        if self._dragging_shot_id is not None:
            lane = round((event.pos().x() - self.lane_start_x) / self.lane_width)
            lane = self._snap_to_active_lane(lane)
            if lane != self._drag_lane_preview:
                self._drag_lane_preview = lane
                self.viewport().update()
            return
        if self.duplicate_preview is not None:
            # Hover feedback (no button held) so the up/down and move
            # cursors show up before the user commits to a drag, not just
            # during one. Must return here rather than falling through to
            # super() - QTextEdit's own mouseMoveEvent resets the cursor
            # to an I-beam for hovering over text on every move, which
            # would immediately stomp on whatever we just set.
            edge = self._hit_test_duplicate_handle(event.pos())
            if edge in ("start", "end"):
                self.viewport().setCursor(Qt.SizeVerCursor)
                return
            elif edge == "move":
                self.viewport().setCursor(Qt.OpenHandCursor)
                return
            else:
                self.viewport().unsetCursor()
        if self.show_lanes and not self.auto_sort_shots:
            # Same hover-before-drag feedback for Show Lanes' own
            # per-shot handles - matches the ClosedHandCursor already
            # shown once a drag actually starts (see mousePressEvent).
            # Suppressed while auto-sort owns lane assignment, since a
            # manual drag would just get overridden on the next recompute.
            if self._hit_test_shot_line(event.pos()) is not None:
                self.viewport().setCursor(Qt.OpenHandCursor)
                return
            else:
                self.viewport().unsetCursor()
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        # Ctrl+wheel is how a trackpad's pinch-to-zoom gesture typically
        # reaches Qt on this platform - route it into the app's OWN zoom
        # system (self.zoom_callback, wired to
        # PageAspectContainer.apply_zoom_factor in MainWindow._build_ui)
        # instead of letting it fall through to QAbstractScrollArea's
        # default wheelEvent, which would just scroll the document and
        # make Ctrl+scroll feel like it's fighting the +/- zoom buttons.
        # event.accept() below keeps it from also bubbling up to a
        # parent widget/the window. angleDelta is accumulated so a
        # trackpad's much finer-grained deltas still zoom smoothly
        # instead of needing to cross a full 120-unit "click" each time.
        if event.modifiers() & Qt.ControlModifier and self.zoom_callback is not None:
            self._wheel_zoom_accum += event.angleDelta().y()
            step = 120  # one notional "wheel click", matching QWheelEvent's angleDelta convention
            zoom_step = PageAspectContainer.ZOOM_STEP
            while self._wheel_zoom_accum >= step:
                self.zoom_callback(zoom_step)
                self._wheel_zoom_accum -= step
            while self._wheel_zoom_accum <= -step:
                self.zoom_callback(1 / zoom_step)
                self._wheel_zoom_accum += step
            event.accept()
            return
        super().wheelEvent(event)

    def event(self, event):
        # Some trackpad drivers report pinch-to-zoom as a native gesture
        # event instead of (or in addition to) Ctrl+wheel - handled here
        # rather than in wheelEvent since QNativeGestureEvent isn't a
        # QWheelEvent. value() is the incremental scale delta for this
        # tick (e.g. 0.02 for a small pinch-out step), applied directly
        # as a continuous zoom-multiplier factor for a smooth feel,
        # unlike the fixed-step Ctrl+wheel/button handling above.
        if event.type() == QEvent.NativeGesture and event.gestureType() == Qt.ZoomNativeGesture:
            if self.zoom_callback is not None:
                self.zoom_callback(1 + event.value())
            event.accept()
            return True
        return super().event(event)

    def mouseReleaseEvent(self, event):
        if self._dragging_duplicate_edge is not None:
            self._dragging_duplicate_edge = None
            self.viewport().unsetCursor()
            self.viewport().update()
            return
        if self._dragging_shot_id is not None:
            shot_id = self._dragging_shot_id
            lane = self._drag_lane_preview
            self._dragging_shot_id = None
            self._drag_lane_preview = None
            self.viewport().unsetCursor()
            if lane is not None and self.lane_drag_callback is not None:
                self.lane_drag_callback(shot_id, lane)
            else:
                self.viewport().update()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)

        self._ensure_page_gaps_current()

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing)

        if self.scene_numbers:
            painter.setFont(self.font())
            painter.setPen(QColor(PAGE_DARK_INK) if self.dark_page else QColor("#000000"))
            viewport_height = self.viewport().height()
            rows = []
            for position, number in self.scene_numbers:
                y = self._y_for_position(position)
                if y is not None and -50 <= y <= viewport_height + 50:
                    rows.append((y, number))
            _draw_scene_numbers(painter, self.viewport().width(), rows, scale=self.scale)

        scroll_y = self.verticalScrollBar().value()
        if self.page_gap_style == "dotted":
            total_gap = DOTTED_GAP_TOTAL_PX * self.scale
        else:
            total_gap = (PAGE_MARGIN_BOTTOM_PX + PAGE_GAP_PX + PAGE_MARGIN_TOP_PX) * self.scale
        page_cuts = []  # (prev page's content-bottom, next page's content-start), for break marks
        for page_index, content_start in enumerate(self.page_content_starts):
            if page_index == 0:
                continue
            content_start_v = content_start - scroll_y
            page_cuts.append((content_start_v - total_gap, content_start_v))

        if self.show_page_markers:
            for boundary_index, (content_bottom_v, content_start_v) in enumerate(page_cuts):
                if self.page_gap_style == "dotted":
                    line_y = (content_bottom_v + content_start_v) / 2
                    if -10 <= line_y <= self.viewport().height() + 10:
                        dotted_pen = QPen(QColor("#999999"))
                        dotted_pen.setStyle(Qt.DotLine)
                        dotted_pen.setWidth(2)
                        painter.setPen(dotted_pen)
                        painter.drawLine(0, round(line_y), self.viewport().width(), round(line_y))
                    number_y = content_start_v + 4
                else:
                    gap_top = content_start_v - PAGE_MARGIN_TOP_PX * self.scale - PAGE_GAP_PX * self.scale
                    gap_bottom = content_start_v - PAGE_MARGIN_TOP_PX * self.scale
                    if gap_bottom >= 0 and gap_top <= self.viewport().height():
                        # Matches the canvas/mat behind the page (same
                        # color regardless of theme - see
                        # PAGE_CANVAS_BG_COLOR), so the gap between pages
                        # reads as the canvas showing through rather than
                        # a stripe drawn on top of the page.
                        painter.fillRect(
                            0, round(gap_top), self.viewport().width(),
                            round(gap_bottom - gap_top), QColor(PAGE_CANVAS_BG_COLOR),
                        )
                    number_y = (content_start_v - PAGE_MARGIN_TOP_PX * self.scale) + PAGE_NUMBER_TOP_OFFSET_PX * self.scale

                if -20 <= number_y <= self.viewport().height() + 20:
                    label = f"{boundary_index + 2}."
                    painter.setPen(QColor(PAGE_DARK_SECONDARY_INK) if self.dark_page else QColor("#666666"))
                    label_width = painter.fontMetrics().horizontalAdvance(label)
                    label_x = self.viewport().width() - PAGE_MARGIN_RIGHT_PX * self.scale - label_width
                    painter.drawText(round(label_x), round(number_y), label)

                # (MORE)/(CONT'D): visual-only (never inserted into the
                # real document text - see _apply_page_gaps_to_document)
                # markers for a speech that's already in progress and
                # continues past this break. Skipped in "dotted" style,
                # whose deliberately compact page-turn space (tens of
                # pixels, not a full margin) has no room for two extra
                # readable lines of text.
                if self.page_gap_style != "dotted" and boundary_index < len(self.page_more_speakers):
                    speaker = self.page_more_speakers[boundary_index]
                    if speaker:
                        ink = QColor(PAGE_DARK_INK) if self.dark_page else QColor("#000000")
                        painter.setPen(ink)
                        more_y = content_bottom_v + MORE_CONTD_MARGIN_OFFSET_PX * self.scale
                        if -20 <= more_y <= self.viewport().height() + 20:
                            more_label = "(MORE)"
                            more_width = painter.fontMetrics().horizontalAdvance(more_label)
                            more_x = (self.viewport().width() - more_width) / 2
                            painter.drawText(round(more_x), round(more_y), more_label)

                        contd_y = content_start_v - MORE_CONTD_MARGIN_OFFSET_PX * self.scale
                        if -20 <= contd_y <= self.viewport().height() + 20:
                            contd_x = (PAGE_MARGIN_LEFT_PX + IN(2.0)) * self.scale
                            painter.drawText(round(contd_x), round(contd_y), f"{speaker} (CONT'D)")

        shots_with_lanes = self.shots_provider()
        cross = min(self.cross_size, max(3, self.lane_width // 2 - 1))

        key_needs_two_columns = False
        if self.key_enabled:
            key_rows = _compute_key_rows(
                [s for s, _lane in shots_with_lanes], self.shot_type_style_map,
                self.multi_style_mode,
            )
            if key_rows:
                content_starts_v = [self.page_content_starts[0] - scroll_y]
                if self.key_placement == "every_page":
                    content_starts_v.extend(cs for _cb, cs in page_cuts)
                # The box may be anchored near the page's top OR bottom
                # margin (see _draw_key_legend) - so visibility has to be
                # checked against the WHOLE page's span, not just a small
                # offset from page_top_v, or a bottom-anchored box would
                # get culled as soon as the page's top scrolls out of a
                # narrow window even though its bottom (where the box
                # actually sits) is still on screen.
                for i, content_start_v in enumerate(content_starts_v):
                    page_top_v = content_start_v - PAGE_MARGIN_TOP_PX * self.scale
                    page_bottom_v = page_top_v + PAGE_HEIGHT_PX * self.scale
                    if page_bottom_v >= -50 and page_top_v <= self.viewport().height() + 50:
                        # This page's OWN actual bottom edge (content-
                        # bottom + its own bottom margin), not the full
                        # nominal page height - only meaningful (and
                        # only available) for a page followed by
                        # another one; the last page falls back to
                        # _draw_key_legend's own nominal-height default.
                        actual_bottom_v = (
                            page_cuts[i][0] + PAGE_MARGIN_BOTTOM_PX * self.scale
                            if i < len(page_cuts) else None
                        )
                        overflowed = _draw_key_legend(
                            painter, PAGE_WIDTH_PX * self.scale, page_top_v, key_rows, self.line_style_labels,
                            scale=self.scale, position=self.key_box_position,
                            ink_color=PAGE_DARK_INK if self.dark_page else None,
                            bg_color=PAGE_DARK_BG if self.dark_page else None,
                            page_bottom_y=actual_bottom_v,
                        )
                        key_needs_two_columns = key_needs_two_columns or overflowed
        if self.key_overflow_callback:
            self.key_overflow_callback(key_needs_two_columns)

        # Collected while drawing each shot below, then all drawn in one
        # pass afterward (see show_lane_handles below the loop) so a
        # circle can never end up UNDER another shot's break-zigzag mark
        # just because that shot happened to be drawn later in the loop.
        show_lane_handles = []  # [(x, mid_y, color), ...]

        for shot, lane in shots_with_lanes:
            x = _shot_line_x(
                shot, lane, self.lane_start_x, self.lane_width, self.max_lanes,
                PAGE_MARGIN_LEFT_PX * self.scale,
            )

            line_color = QColor(shot.color)
            overlay_alpha = _overlay_alpha(shot)
            if overlay_alpha is not None:
                line_color.setAlpha(overlay_alpha)
            if self.duplicate_preview is not None and shot.shot_id != self.editing_shot_id:
                # Fade every OTHER existing shot during placement, so the
                # preview (solid dodger blue, drawn after this loop) reads
                # clearly as the one thing actively being positioned - but
                # not the shot actually being edited in place (editing_shot_id),
                # which stays at full opacity since it's still the current
                # subject, not just a reference like every other shot.
                line_color.setAlpha(min(line_color.alpha(), 70))
            pen = QPen(line_color)
            pen.setWidth(self.line_width)
            pen.setStyle(LINE_STYLE_MAP.get(shot.line_style, Qt.SolidLine))
            painter.setPen(pen)

            segs = []
            for seg_start, seg_end in sorted(shot.all_ranges()):
                # shot.all_ranges() is in "clean" coordinates - see the
                # matching comment in _hit_test_shot_line.
                top = self._y_for_position(self.split_map.clean_to_rendered(seg_start))
                bottom = self._y_for_position(self.split_map.clean_to_rendered(seg_end))
                if top is None or bottom is None:
                    continue
                segs.append((top, max(bottom, top + 2)))

            # Every actual drawn piece of this shot's line - (top, bottom)
            # - in the order drawn below. The Show Lanes handle (further
            # down) is placed somewhere inside one of these, never in a
            # gap or on a break mark - otherwise it isn't obviously "on"
            # this particular line.
            line_spans = []

            first_top = None
            last_bottom = None
            for top, seg_bottom in segs:
                if first_top is None or top < first_top:
                    first_top = top
                if last_bottom is None or seg_bottom > last_bottom:
                    last_bottom = seg_bottom

                # Break the line at each page-boundary crossing so an
                # "end" mark and a flipped "start" mark sit just inside
                # each page's content area (matching the PDF export)
                # instead of overlapping the gray page-break band.
                crossings = sorted(
                    (content_bottom, content_start)
                    for content_bottom, content_start in page_cuts
                    if top < content_bottom < seg_bottom
                )
                cursor_y = top
                for content_bottom, content_start in crossings:
                    fitted_cross = _mark_size_for_gap(cross, content_start - content_bottom)
                    fitted_height = _break_mark_height(fitted_cross)
                    end_top = content_bottom - fitted_height
                    if end_top > cursor_y:
                        painter.drawLine(x, cursor_y, x, end_top)
                        if shot.line_style == "double":
                            painter.drawLine(x + 4, cursor_y, x + 4, end_top)
                        line_spans.append((cursor_y, end_top))
                    _draw_break_zigzag(painter, x, end_top, fitted_cross)
                    flip_anchor = content_start + fitted_height
                    _draw_break_zigzag(painter, x, flip_anchor, fitted_cross, flip=True)
                    cursor_y = content_start + fitted_height
                if seg_bottom > cursor_y:
                    painter.drawLine(x, cursor_y, x, seg_bottom)
                    if shot.line_style == "double":
                        painter.drawLine(x + 4, cursor_y, x + 4, seg_bottom)
                    line_spans.append((cursor_y, seg_bottom))

            # Baselines for the shot number repeated above every later
            # section of a split shot (see label_every_segment) - drawn
            # together with the first label below, after the ticks, so
            # changing the pen for text can't thin out a mark.
            extra_label_ys = []
            for (_, bottom_a), (top_b, _) in zip(segs, segs[1:]):
                fitted_size = _mark_size_for_gap(cross, top_b - bottom_a)
                _draw_break_zigzag(painter, x, bottom_a, fitted_size)
                _draw_break_zigzag(painter, x, top_b, fitted_size, flip=True)
                if self.label_every_segment:
                    extra_label_ys.append(top_b - _break_mark_height(fitted_size) - 3)

            if first_top is not None:
                if not shot.suppress_start_tick:
                    painter.drawLine(x - cross, first_top + cross, x + cross, first_top - cross)
                if last_bottom is not None and not shot.suppress_end_tick:
                    painter.drawLine(x - cross, last_bottom + cross, x + cross, last_bottom - cross)

                painter.setPen(line_color)
                metrics = painter.fontMetrics()
                text_width = metrics.horizontalAdvance(shot.shot_id)
                label_x = x - (text_width // 2)
                if not shot.suppress_start_tick:
                    label_y = first_top - cross - 4
                    painter.drawText(label_x, label_y, shot.shot_id)
                    note = (shot.page_note or "").strip()
                    if note:
                        backdrop = QColor(PAGE_DARK_BG if self.dark_page else "#ffffff")
                        backdrop.setAlpha(215)
                        _draw_page_note(
                            painter, note, 3 * self.scale, label_y - metrics.ascent() - 2,
                            PAGE_MARGIN_LEFT_PX * self.scale - 6 * self.scale, line_color, backdrop,
                        )
                for label_y in extra_label_ys:
                    painter.drawText(label_x, round(label_y), shot.shot_id)

                if self.show_lanes and last_bottom is not None and not shot.overlay:
                    # Just the center side-to-side handle (not the
                    # top/bottom resize ones - Show Lanes only ever
                    # reassigns which lane a shot occupies, it doesn't
                    # resize it), in the shot's own color at 80% opacity.
                    # Drawn later, in one pass after every shot - see
                    # show_lane_handles above the loop.
                    handle_color = QColor(shot.color)
                    handle_color.setAlpha(round(255 * 0.8))
                    mid = (first_top + last_bottom) / 2
                    handle_r = max(7, self.cross_size)

                    # The handle must sit somewhere ON an actual drawn
                    # piece of the line - never floating in a gap (a
                    # page-break band, a Split Shots gap) and never
                    # overlapping a break mark - so it's unambiguous
                    # which line it belongs to. If the naive midpoint
                    # isn't already on a drawn piece, snap to whichever
                    # piece is closest; either way, stay clear of that
                    # piece's own ends (where a break mark sits) when
                    # the piece is long enough to allow it.
                    containing = next((s for s in line_spans if s[0] <= mid <= s[1]), None)
                    target_span = containing
                    if target_span is None and line_spans:
                        target_span = min(
                            line_spans,
                            key=lambda s: min(abs(mid - s[0]), abs(mid - s[1])),
                        )
                    if target_span is not None:
                        span_lo, span_hi = target_span
                        # A span's own two ends are exactly where a break
                        # mark is anchored (a tick, or a zigzag never
                        # bigger than `cross`) - clearing it takes BOTH
                        # the mark's own reach (cross) AND the circle's
                        # radius, not just the circle's radius alone.
                        margin = handle_r + cross + 3
                        if span_hi - span_lo > 2 * margin:
                            mid = min(max(mid, span_lo + margin), span_hi - margin)
                        else:
                            mid = (span_lo + span_hi) / 2

                    show_lane_handles.append((x, mid, handle_color))

        for handle_x, handle_mid, handle_color in show_lane_handles:
            handle_r = max(7, self.cross_size)
            painter.setBrush(handle_color)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(round(handle_x - handle_r), round(handle_mid - handle_r), handle_r * 2, handle_r * 2)
            painter.setBrush(Qt.NoBrush)
            _draw_handle_arrows(painter, handle_x, handle_mid, handle_r, horizontal=True)

        # Duplicate placement always shows the lane guide lines too - the
        # user needs the same spatial reference Show Lanes gives them,
        # whether or not they happened to have that toggle on already.
        if self.show_lanes or self.duplicate_preview is not None:
            guide_pen = QPen(QColor(255, 0, 0, 130))
            guide_pen.setWidth(1)
            disabled_guide_pen = QPen(QColor(120, 120, 120, 90))
            disabled_guide_pen.setWidth(1)
            disabled_guide_pen.setStyle(Qt.DashLine)
            for lane_index in range(self.max_lanes):
                lane_x = self.lane_start_x + lane_index * self.lane_width
                painter.setPen(
                    disabled_guide_pen if _lane_disabled(lane_index, self.active_lanes, self.lane_count) else guide_pen
                )
                painter.drawLine(lane_x, 0, lane_x, self.viewport().height())

            if self._dragging_shot_id is not None and self._drag_lane_preview is not None:
                preview_x = self.lane_start_x + self._drag_lane_preview * self.lane_width
                highlight_pen = QPen(QColor(255, 0, 0, 220))
                highlight_pen.setWidth(3)
                painter.setPen(highlight_pen)
                painter.drawLine(preview_x, 0, preview_x, self.viewport().height())

            if self._dragging_duplicate_edge == "move" and self.duplicate_preview is not None:
                preview_x = self._duplicate_preview_x()
                highlight_pen = QPen(QColor(255, 0, 0, 220))
                highlight_pen.setWidth(3)
                painter.setPen(highlight_pen)
                painter.drawLine(preview_x, 0, preview_x, self.viewport().height())

        if self.duplicate_preview is not None:
            top = self._y_for_position(self.duplicate_preview["start"])
            bottom = self._y_for_position(self.duplicate_preview["end"])
            if top is not None and bottom is not None:
                top, bottom = min(top, bottom), max(top, bottom)
                mid = (top + bottom) / 2
                x = self._duplicate_preview_x()
                handle_r = max(7, self.cross_size)
                preview_color = QColor(30, 144, 255, 220)  # dodger blue - distinct from any shot color
                preview_pen = QPen(preview_color)
                preview_pen.setWidth(self.line_width + 1)
                preview_pen.setStyle(Qt.DashLine)
                painter.setPen(preview_pen)
                painter.drawLine(x, top, x, bottom)
                painter.setBrush(preview_color)
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(x - handle_r, top - handle_r, handle_r * 2, handle_r * 2)
                painter.drawEllipse(x - handle_r, bottom - handle_r, handle_r * 2, handle_r * 2)
                # Center handle - drags side to side only, to reassign
                # which lane the duplicate lands in (see mouseMoveEvent's
                # "move" branch); its left/right arrow icon (vs. the
                # top/bottom handles' up/down one) makes that distinction
                # visible before the user even has to try dragging it.
                painter.drawEllipse(round(x - handle_r), round(mid - handle_r), handle_r * 2, handle_r * 2)
                painter.setBrush(Qt.NoBrush)
                _draw_handle_arrows(painter, x, top, handle_r, horizontal=False)
                _draw_handle_arrows(painter, x, bottom, handle_r, horizontal=False)
                _draw_handle_arrows(painter, x, mid, handle_r, horizontal=True)

        painter.end()

    def get_page_breaks(self):
        doc = self.document()
        breaks = [0]
        current_page_top = 0.0

        block = doc.begin()
        while block.isValid():
            rect = doc.documentLayout().blockBoundingRect(block)
            if _flow_bottom(doc, block) - current_page_top > CONTENT_HEIGHT_PX and rect.top() > current_page_top:
                current_page_top = rect.top()
                breaks.append(block.position())
            block = block.next()

        return breaks

    def _y_for_position(self, position):
        try:
            cursor = self.textCursor()
            cursor.setPosition(position)
        except Exception:
            return None
        return self.cursorRect(cursor).top()


class PageAspectContainer(QWidget):
    """Wraps ScriptTextEdit (via an inner QScrollArea) and forces it to
    always render the page at the true 8.5:11 (1:1.2941) aspect ratio.
    The page's WIDTH tracks zoom_multiplier directly - so the box,
    margins, and text all scale together as one unit, Google-Docs-style -
    while its HEIGHT always fills the available pane exactly. Since
    script_view's own paintEvent lays out pages purely in terms of its
    viewport size and scroll position (never its outer geometry), a
    fixed viewport height with a shrunk page width naturally reveals
    more of the continuous document - including multiple pages at once -
    as you zoom out, and the QScrollArea lets you pan across a page
    that's wider than the pane when you zoom in. This keeps the
    interactive view a genuine WYSIWYG preview of the PDF export at
    whatever zoom level the window ends up at."""

    RESCALE_DEBOUNCE_MS = 180
    ZOOM_STEP = 1.1
    MIN_ZOOM_MULTIPLIER = 0.3
    MAX_ZOOM_MULTIPLIER = 4.0

    def __init__(self, script_view, on_scale_changed, parent=None):
        super().__init__(parent)
        self.script_view = script_view
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidget(self.script_view)
        # script_view keeps an explicit fixed size, set every time by
        # _fit_geometry - the scroll area just frames/scrolls it, it
        # never resizes it itself.
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        # Both reserved unconditionally (AlwaysOn), never "as needed" -
        # matches the existing convention on script_view's own vertical
        # scrollbar (see ScriptTextEdit.__init__): an as-needed scrollbar
        # that only appears sometimes would eat into the geometry
        # _fit_geometry() already committed to, silently clipping a strip
        # of the page.
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        # Vertical scroll stays owned entirely by script_view itself
        # (paintEvent reads its own verticalScrollBar(), not this one's).
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Dark grey canvas around the page (Fade In-style) so the white
        # page itself reads as a distinct sheet of paper rather than
        # blending into the pane - this is the space visible in the
        # letterbox/gutter area whenever the page doesn't exactly fill
        # the viewport (zoomed out, or narrower than the pane).
        self.scroll_area.setStyleSheet(f"background-color: {PAGE_CANVAS_BG_COLOR}; border: none;")
        self.scroll_area.viewport().setStyleSheet(f"background-color: {PAGE_CANVAS_BG_COLOR};")
        self._on_scale_changed = on_scale_changed
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._commit_pending_scale)
        self._pending_scale = None
        # User-controlled zoom on top of the auto-fit-to-pane scale (see
        # zoom_in/zoom_out) - 1.0 means "exactly fill the available
        # space", matching the original stretch-to-fit behavior.
        self.zoom_multiplier = 1.0
        # Dark grey canvas, matching the scroll area above - keeps the
        # white page reading as a sheet of paper sitting on a distinct
        # background, Fade In/Google Docs-style, instead of blending
        # into the pane.
        self.setStyleSheet(f"background-color: {PAGE_CANVAS_BG_COLOR};")

    def _fit_geometry(self):
        """Computes the page width for the current zoom_multiplier (1.0
        == the page's width/height both exactly fill the pane, matching
        the original auto-fit behavior) and sizes script_view to
        (that width) x (the full available height) - always the full
        pane height, regardless of zoom, which is what makes zooming out
        reveal more of the document at once rather than just shrinking a
        fixed viewport. Both the inner vertical scrollbar (owned by
        script_view - see ScriptTextEdit.__init__) and the outer
        horizontal scrollbar (owned by self.scroll_area) are always
        reserved space, never "as needed", so neither can silently clip
        a strip of the page by appearing/disappearing after this
        geometry is already committed. Returns the resulting scale
        factor (1.0 == the natural 816x1056px page filling the available
        space)."""
        v_scrollbar_extent = self.script_view.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        h_scrollbar_extent = self.scroll_area.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        available_w = max(1, self.width() - v_scrollbar_extent)
        available_h = max(1, self.height() - h_scrollbar_extent)
        page_ratio = PAGE_WIDTH_PX / PAGE_HEIGHT_PX
        if available_w / available_h > page_ratio:
            fit_h = available_h
            fit_w = round(fit_h * page_ratio)
        else:
            fit_w = available_w
            fit_h = round(fit_w / page_ratio)
        fit_scale = fit_w / PAGE_WIDTH_PX
        effective_scale = max(0.2, fit_scale * self.zoom_multiplier)

        target_w = round(PAGE_WIDTH_PX * effective_scale)
        widget_w = target_w + v_scrollbar_extent
        widget_h = available_h
        self.script_view.setFixedSize(widget_w, widget_h)
        return effective_scale

    def resizeEvent(self, event):
        # Manual geometry, not a QLayout (consistent with why this
        # container never used one - it avoids Qt's layout-timing
        # ambiguity), so the scroll area's geometry must be settled
        # synchronously here before _fit_geometry() measures against it.
        self.scroll_area.setGeometry(0, 0, self.width(), self.height())
        # Re-fit geometry immediately (cheap - it's just setFixedSize),
        # but debounce the expensive part (re-laying-out the whole
        # document at the new scale) so dragging the splitter doesn't
        # rebuild it on every intermediate tick, only once resizing
        # settles.
        self._pending_scale = self._fit_geometry()
        self._debounce_timer.start(self.RESCALE_DEBOUNCE_MS)
        super().resizeEvent(event)

    def _commit_pending_scale(self):
        if self._pending_scale is not None:
            self._on_scale_changed(self._pending_scale)

    def showEvent(self, event):
        # Synchronous and undebounced: MainWindow() is constructed before
        # .show() in main.py, so nothing has resized yet on launch - do
        # the first fit immediately so the page renders at the correct
        # scale on first paint instead of visibly snapping to it a
        # moment later. Safe to call more than once (idempotent).
        self.scroll_area.setGeometry(0, 0, self.width(), self.height())
        self._on_scale_changed(self._fit_geometry())
        super().showEvent(event)

    def apply_zoom_factor(self, factor):
        """Multiplies zoom_multiplier by `factor` (clamped to the usual
        min/max) and re-fits geometry - the single path zoom_in/zoom_out
        and ScriptTextEdit's Ctrl+wheel/pinch-gesture handling (see
        ScriptTextEdit.wheelEvent/event) all funnel through, so a
        trackpad's continuous pinch and the +/- buttons' fixed
        ZOOM_STEP clicks behave identically from here on."""
        self.zoom_multiplier = min(
            self.MAX_ZOOM_MULTIPLIER, max(self.MIN_ZOOM_MULTIPLIER, self.zoom_multiplier * factor)
        )
        self._on_scale_changed(self._fit_geometry())

    def zoom_in(self):
        self.apply_zoom_factor(self.ZOOM_STEP)

    def zoom_out(self):
        self.apply_zoom_factor(1 / self.ZOOM_STEP)


# (action_id, display label for the Settings dialog, default key
# sequence) - the default is either a QKeySequence.StandardKey (for the
# handful of actions with a real platform-standard binding) or a plain
# string for everything else. Every entry here is user-customizable from
# Settings > Keyboard Shortcuts (see SettingsDialog) and persisted via
# QSettings under "shortcuts/<action_id>" (see MainWindow._load_shortcut/
# _set_shortcut).
SHORTCUT_ACTIONS = [
    ("open_project", "Open Project", QKeySequence.StandardKey.Open),
    ("save_project", "Save Project", QKeySequence.StandardKey.Save),
    ("save_project_as", "Save Project As", QKeySequence.StandardKey.SaveAs),
    ("undo", "Undo", QKeySequence.StandardKey.Undo),
    ("redo", "Redo", QKeySequence.StandardKey.Redo),
    ("new_shot", "New Shot (Create Shot from Selection)", "N"),
    ("duplicate_shot", "Duplicate Shot", "D"),
    ("show_lanes", "Move Shots", "S"),
    ("edit_placement", "Edit Start/End", "Q"),
    ("settings", "Settings", "Ctrl+,"),
    ("export_pdf", "Export Lined Script as PDF", "Ctrl+E"),
]


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("ScriptLiner")
        if LOGO_SQUARE_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_SQUARE_PATH)))
        self.resize(1400, 900)

        self.shots = []
        self.shots_by_id = {}
        self.scene_used_letters = {}
        self.shot_lanes = {}
        self.loaded_paragraphs = None

        self.undo_stack = []
        self.redo_stack = []

        self.selected_shot = None
        self.selected_shot_item = None

        self.colors = [
            "#ff4d4d",
            "#4da6ff",
            "#43a047",
            "#e0b400",
            "#b84dff",
            "#ff7ad9",
        ]
        self.camera_letter_colors = {}

        # Settings
        self.shot_type_style_map = {
            st: "solid" for st in ShotInspectorPanel.SHOT_TYPES if st
        }
        self.shot_type_affects_style = False
        self.key_enabled = False
        self.key_placement = "first_page"  # or "every_page"
        self.key_box_position = KEY_BOX_POSITION_DEFAULT  # see KEY_BOX_POSITIONS
        self.line_style_labels = {}  # explicit label overrides; unset styles use the derived label
        self.multi_style_mode = {}  # style -> "merge"/"overwrite" ("overwrite" is default), only relevant when >1 shot type matches it
        self.page_gap_style = "gap"  # or "dotted"
        self.show_lanes = False
        self.pinned_lanes = set()  # shot_ids manually assigned to a lane via drag
        # shot_id -> the lane it was dragged to. shot_lanes itself is
        # overwritten by an automatic lane order (alphabetical/by length),
        # so this remembers the manual layout for when that order is
        # switched off again - see _recompute_lanes.
        self.manual_lanes = {}
        self.lane_count = DEFAULT_LANE_COUNT  # numbered lanes (6 by default, up to MAX_LANE_COUNT) - see Settings
        self.active_lanes = set(range(DEFAULT_LANE_COUNT))  # which of those lanes are enabled
        self.auto_sort_shots = False  # "Shots in Alphabetical Order" - see _recompute_lanes
        self.length_sort_shots = False  # "Shots by Length" (longest outermost) - see _recompute_lanes
        self.unique_shot_colors = False  # every new shot gets a color nothing else uses yet
        self.label_every_segment = True  # shot number above every section of a split shot
        self.overlay_default_opacity = DEFAULT_OVERLAY_OPACITY

        # Dark Mode is a personal display preference, not a project
        # attribute - it's loaded from/saved to QSettings (like keyboard
        # shortcuts), never into the .slproj file, and never affects
        # export (_write_lined_pdf always renders light/black-on-white).
        # "light": normal. "dark_ui": everything but the page goes dark.
        # "dark_full": the page inverts too. See _apply_theme.
        self.theme = QSettings("ScriptLiner", "ScriptLiner").value("theme", "light")
        if self.theme not in ("light", "dark_ui", "dark_full"):
            self.theme = "light"

        self.current_project_path = None
        self._dirty = False
        self._key_overflow_notified = False

        self._placement_mode = None  # "duplicate" | "edit" | None, while placement is in progress
        self._placement_source = None  # shot being copied ("duplicate") or edited in place ("edit")
        self._duplicate_fields = None  # set of field names to copy, chosen in DuplicateShotDialog ("duplicate" mode only)

        # action_id -> the live QAction/QShortcut object, so a customized
        # shortcut (from Settings > Keyboard Shortcuts) can be applied to
        # it directly instead of rebuilding the menu/buttons. Populated by
        # _build_ui()/_build_menu() below.
        self._shortcut_objects = {}
        # action_id -> the button a bare-letter shortcut clicks, for the
        # enabled/text-field-focus safety check in _sync_button_shortcuts.
        self._button_shortcuts = {}

        self._build_ui()
        self._build_menu()
        self._apply_theme()

        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start(AUTOSAVE_INTERVAL_MS)

        self._restore_last_session()
        QTimer.singleShot(4000, self._auto_check_for_updates)

    def _build_ui(self):
        self.script_view = ScriptTextEdit()
        initial_font = QFont(BASE_FONT_FAMILY)
        initial_font.setPointSizeF(_points_for_pixels(BASE_FONT_PX))
        self.script_view.setFont(initial_font)
        self.script_view.shots_provider = lambda: [
            (s, self.shot_lanes.get(s.shot_id, 0)) for s in self.shots
        ]
        self.script_view.lane_drag_callback = self._pin_shot_lane
        self.script_view.key_overflow_callback = self._on_key_overflow_changed
        self._apply_page_margins()
        self.script_view.setPlainText(
            "INT. KITCHEN - DAY\n\n"
            "Sarah enters the room.\n"
            "She looks around.\n"
            "A coffee mug sits on the table.\n"
        )
        self.script_view.setReadOnly(True)

        self.script_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.script_view.customContextMenuRequested.connect(
            self.show_script_context_menu
        )

        self.shot_list = QListWidget()
        self.shot_list.itemClicked.connect(self.on_shot_selected)
        # Scoped to shot_list itself (WidgetShortcut context) so Delete/
        # Backspace only deletes a shot while the list has focus - it
        # must NOT fire while editing text in the inspector's fields.
        for key in (Qt.Key_Delete, Qt.Key_Backspace):
            shortcut = QShortcut(QKeySequence(key), self.shot_list)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(self.delete_selected_shot)
        self.shot_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.shot_list.customContextMenuRequested.connect(self._show_shot_list_context_menu)

        self.inspector = ShotInspectorPanel()
        self.inspector.field_changed.connect(self.on_shot_field_changed)
        self.inspector.edit_placement_requested.connect(self.start_edit_placement)
        self.inspector.duplicate_requested.connect(self._duplicate_selected_shot)

        self.update_script_btn = QPushButton("Update Script...")
        self.update_script_btn.setObjectName("primaryButton")
        self.update_script_btn.clicked.connect(self.start_update_script)

        self.merge_projects_btn = QPushButton("Merge Projects...")
        self.merge_projects_btn.setObjectName("primaryButton")
        self.merge_projects_btn.clicked.connect(self.start_merge_projects)

        self.create_shot_btn = make_shortcut_button("Create Shot from Selection", "N")
        self.create_shot_btn.clicked.connect(self.create_shot)

        self.show_lanes_btn = make_shortcut_button("Move Shots", "S")
        self.show_lanes_btn.setCheckable(True)
        self.show_lanes_btn.toggled.connect(self._toggle_show_lanes)

        # Only shown while a duplicate or an in-place edit is being
        # positioned (see _begin_placement/_show_placement_confirm_controls).
        self.confirm_duplicate_btn = QPushButton("Confirm Placement")
        self.confirm_duplicate_btn.setObjectName("primaryButton")
        self.confirm_duplicate_btn.clicked.connect(self._confirm_placement)
        self.confirm_duplicate_btn.setVisible(False)
        self.cancel_duplicate_btn = QPushButton("Cancel Placement")
        self.cancel_duplicate_btn.clicked.connect(self._cancel_placement)
        self.cancel_duplicate_btn.setVisible(False)

        # Enter confirms placement, matching the Confirm Placement button -
        # both Key_Return (main keyboard) and Key_Enter (numpad) map here.
        # Disabled outside placement mode (see
        # _show_placement_confirm_controls) so it never steals a plain
        # Enter/newline keypress anywhere else in the app (e.g. the
        # inspector's multi-line Description/Notes fields).
        self._duplicate_confirm_shortcuts = [
            QShortcut(QKeySequence(Qt.Key_Return), self),
            QShortcut(QKeySequence(Qt.Key_Enter), self),
        ]
        for shortcut in self._duplicate_confirm_shortcuts:
            shortcut.activated.connect(self._confirm_placement)
            shortcut.setEnabled(False)

        # N/D/S/Q trigger their buttons directly (click(), so enablement
        # and the button's own visual press-state are respected
        # automatically) - but bare letter keys would otherwise swallow
        # normal typing in the inspector's text fields, so each one is
        # only enabled while its button itself is enabled AND no
        # text-editing widget currently has focus (see
        # _sync_button_shortcuts, wired to QApplication.focusChanged).
        for action_id, button in (
            ("new_shot", self.create_shot_btn),
            ("duplicate_shot", self.inspector.duplicate_btn),
            ("show_lanes", self.show_lanes_btn),
            ("edit_placement", self.inspector.edit_placement_btn),
        ):
            shortcut = QShortcut(self._load_shortcut(action_id), self)
            shortcut.activated.connect(button.click)
            self._shortcut_objects[action_id] = shortcut
            self._button_shortcuts[action_id] = button

        QApplication.instance().focusChanged.connect(self._sync_button_shortcuts)
        self._sync_button_shortcuts(None, QApplication.instance().focusWidget())

        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_layout.addWidget(self.update_script_btn)
        left_layout.addWidget(self.merge_projects_btn)
        left_layout.addWidget(self.create_shot_btn)
        left_layout.addWidget(self.show_lanes_btn)
        left_layout.addWidget(QLabel("Shots"))
        left_layout.addWidget(self.shot_list)
        left_layout.addWidget(self.confirm_duplicate_btn)
        left_layout.addWidget(self.cancel_duplicate_btn)
        left_widget.setLayout(left_layout)

        right_widget = QWidget()
        right_layout = QVBoxLayout()
        right_layout.addWidget(self.inspector)
        right_widget.setLayout(right_layout)

        self.page_container = PageAspectContainer(self.script_view, self._apply_interactive_scale)
        self.script_view.zoom_callback = self.page_container.apply_zoom_factor

        self.zoom_out_btn = QPushButton("−")
        self.zoom_out_btn.setFixedWidth(28)
        self.zoom_out_btn.setToolTip("Zoom Out")
        self.zoom_out_btn.clicked.connect(self._on_zoom_out_clicked)

        self.zoom_in_btn = QPushButton("+")
        self.zoom_in_btn.setFixedWidth(28)
        self.zoom_in_btn.setToolTip("Zoom In")
        self.zoom_in_btn.clicked.connect(self._on_zoom_in_clicked)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setFixedWidth(48)

        zoom_bar = QHBoxLayout()
        zoom_bar.addStretch()
        zoom_bar.addWidget(self.zoom_out_btn)
        zoom_bar.addWidget(self.zoom_label)
        zoom_bar.addWidget(self.zoom_in_btn)
        zoom_bar.addStretch()

        page_pane = QWidget()
        page_pane_layout = QVBoxLayout()
        page_pane_layout.setContentsMargins(0, 0, 0, 0)
        page_pane_layout.addLayout(zoom_bar)
        page_pane_layout.addWidget(self.page_container)
        page_pane.setLayout(page_pane_layout)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(page_pane)
        splitter.addWidget(right_widget)
        splitter.setSizes([250, 800, 300])

        self.header = QLabel()
        self.header.setContentsMargins(12, 8, 12, 8)
        self.header.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._apply_header_theme()
        header = self.header
        # Without this, QLabel reports an Expanding vertical size policy
        # once it holds only a pixmap (no text), so the layout below
        # would split space with the splitter roughly evenly instead of
        # giving this a thin banner strip and the splitter everything else.
        header.setFixedHeight(36 + 8 + 8)

        central = QWidget()
        central_layout = QVBoxLayout()
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(header)
        central_layout.addWidget(splitter)
        central.setLayout(central_layout)

        self.setCentralWidget(central)
        self.statusBar()

    @staticmethod
    def _default_shortcut(action_id: str) -> QKeySequence:
        for aid, _label, default in SHORTCUT_ACTIONS:
            if aid == action_id:
                return QKeySequence(default)
        return QKeySequence()

    def _load_shortcut(self, action_id: str) -> QKeySequence:
        """The key sequence to actually assign for this action - a saved
        customization from Settings > Keyboard Shortcuts if there is one,
        otherwise the built-in default."""
        saved = QSettings("ScriptLiner", "ScriptLiner").value(f"shortcuts/{action_id}")
        if saved:
            return QKeySequence(saved)
        return self._default_shortcut(action_id)

    def _set_shortcut(self, action_id: str, key_sequence: QKeySequence):
        """Applies a new key sequence to the live action/shortcut object
        (so it takes effect immediately) and persists it, so it's loaded
        again on next launch."""
        obj = self._shortcut_objects.get(action_id)
        if obj is not None:
            # QAction (menu items) uses setShortcut(); QShortcut (the
            # bare-letter button shortcuts) uses setKey() instead.
            if isinstance(obj, QShortcut):
                obj.setKey(key_sequence)
            else:
                obj.setShortcut(key_sequence)
        button = self._button_shortcuts.get(action_id)
        if button is not None and hasattr(button, "update_shortcut_hint"):
            button.update_shortcut_hint(key_sequence.toString())
        QSettings("ScriptLiner", "ScriptLiner").setValue(f"shortcuts/{action_id}", key_sequence.toString())

    def _sync_button_shortcuts(self, old_widget, new_widget):
        """Keeps each bare-letter button shortcut (N/D/S/Q by default)
        enabled only when its button is itself enabled AND no
        text-editing widget currently has focus - otherwise the shortcut
        would swallow that letter before it ever reaches a QLineEdit/
        QTextEdit the user is typing into. Called on every focus change,
        and should also be called wherever button enablement changes
        (see select_shot, _show_placement_confirm_controls)."""
        is_text_input = isinstance(new_widget, (QLineEdit, QTextEdit))
        for action_id, button in self._button_shortcuts.items():
            shortcut = self._shortcut_objects.get(action_id)
            if shortcut is not None:
                shortcut.setEnabled(button.isEnabled() and not is_text_input)

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("File")

        new_project_action = file_menu.addAction("New Project")
        new_project_action.triggered.connect(self.new_project)

        open_project_action = file_menu.addAction("Open Project...")
        open_project_action.setShortcut(self._load_shortcut("open_project"))
        open_project_action.triggered.connect(self.open_project)
        self._shortcut_objects["open_project"] = open_project_action

        save_project_action = file_menu.addAction("Save Project")
        save_project_action.setShortcut(self._load_shortcut("save_project"))
        save_project_action.triggered.connect(self.save_project)
        self._shortcut_objects["save_project"] = save_project_action

        save_project_as_action = file_menu.addAction("Save Project As...")
        save_project_as_action.setShortcut(self._load_shortcut("save_project_as"))
        save_project_as_action.triggered.connect(self.save_project_as)
        self._shortcut_objects["save_project_as"] = save_project_as_action

        file_menu.addSeparator()

        open_action = file_menu.addAction("Import Script...")
        open_action.triggered.connect(self.open_script)

        document_action = file_menu.addAction("Import Document...")
        document_action.setToolTip(
            "Import a Word document, PDF, or text file that isn't screenplay-formatted - "
            "a beat sheet, treatment, or outline."
        )
        document_action.triggered.connect(self.open_document)

        pdf_action = file_menu.addAction("Open Screenplay PDF  [coming soon]")
        pdf_action.triggered.connect(self.open_unsupported_format)

        mmx_action = file_menu.addAction("Open Movie Magic (.mmx)  [coming soon]")
        mmx_action.triggered.connect(self.open_unsupported_format)

        export_action = file_menu.addAction("Export Lined Script as PDF...")
        export_action.setShortcut(self._load_shortcut("export_pdf"))
        export_action.triggered.connect(self.export_lined_pdf)
        self._shortcut_objects["export_pdf"] = export_action

        edit_menu = self.menuBar().addMenu("Edit")

        undo_action = edit_menu.addAction("Undo")
        undo_action.setShortcut(self._load_shortcut("undo"))
        undo_action.triggered.connect(self.undo)
        self._shortcut_objects["undo"] = undo_action

        redo_action = edit_menu.addAction("Redo")
        redo_action.setShortcut(self._load_shortcut("redo"))
        redo_action.triggered.connect(self.redo)
        self._shortcut_objects["redo"] = redo_action

        settings_menu = self.menuBar().addMenu("Settings")
        settings_action = settings_menu.addAction("Settings...")
        settings_action.setShortcut(self._load_shortcut("settings"))
        settings_action.triggered.connect(self._open_settings_dialog)
        self._shortcut_objects["settings"] = settings_action

        help_menu = self.menuBar().addMenu("Help")
        guide_action = help_menu.addAction("View Guide")
        guide_action.setToolTip("Read the walkthrough that opens the first time ScriptLiner launches.")
        guide_action.triggered.connect(self.show_guide)
        feedback_action = help_menu.addAction("Send Feedback...")
        feedback_action.triggered.connect(self._open_feedback_form)

        # In-app updating is only built for the macOS app (see
        # app/updater.py); other platforms don't get the menu items.
        if sys.platform == "darwin":
            help_menu.addSeparator()
            update_action = help_menu.addAction("Check for Updates...")
            update_action.triggered.connect(lambda: self.check_for_updates(manual=True))
            self.auto_update_action = help_menu.addAction("Check for Updates on Startup")
            self.auto_update_action.setCheckable(True)
            self.auto_update_action.setChecked(
                QSettings("ScriptLiner", "ScriptLiner").value("updates/auto_check", True, type=bool)
            )
            self.auto_update_action.toggled.connect(
                lambda on: QSettings("ScriptLiner", "ScriptLiner").setValue("updates/auto_check", on)
            )

    def _auto_check_for_updates(self):
        """Quiet startup check - only in the packaged Mac app, only if the
        user hasn't turned it off, and it says nothing unless there's an
        update (or a failure worth mentioning, which it isn't)."""
        if updater.current_app_bundle() is None:
            return
        if not QSettings("ScriptLiner", "ScriptLiner").value("updates/auto_check", True, type=bool):
            return
        self.check_for_updates(manual=False)

    def check_for_updates(self, manual=True):
        if getattr(self, "_update_check_thread", None) is not None:
            return
        thread = updater.UpdateCheckThread(self)
        thread.result.connect(lambda release, error: self._on_update_check_done(manual, release, error))
        thread.finished.connect(self._clear_update_check_thread)
        self._update_check_thread = thread
        if manual:
            self.statusBar().showMessage("Checking for updates...", 3000)
        thread.start()

    def _clear_update_check_thread(self):
        thread = getattr(self, "_update_check_thread", None)
        self._update_check_thread = None
        if thread is not None:
            thread.deleteLater()

    def _on_update_check_done(self, manual, release, error):
        if error:
            if manual:
                QMessageBox.warning(self, "Couldn't check for updates", error)
            return
        if release is None or not updater.is_newer(release.version):
            if manual:
                QMessageBox.information(
                    self, "You're up to date", f"ScriptLiner {APP_VERSION} is the latest version."
                )
            return
        self._offer_update(release)

    def _offer_update(self, release):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Update available")
        box.setText(f"ScriptLiner {release.version} is available (you have {APP_VERSION}).")
        if release.notes:
            box.setDetailedText(release.notes)
        can_install = release.asset_url is not None and updater.current_app_bundle() is not None
        install_button = box.addButton(
            "Update Now" if can_install else "Open Download Page", QMessageBox.AcceptRole
        )
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not install_button:
            return
        if can_install:
            self._download_and_install_update(release)
        else:
            QDesktopServices.openUrl(QUrl(release.page_url))

    def _download_and_install_update(self, release):
        bundle = updater.current_app_bundle()
        if bundle is None:
            return
        if not updater.can_self_update(bundle):
            QMessageBox.information(
                self, "Update needs a manual install",
                "ScriptLiner is installed somewhere this account can't modify, so it can't "
                "update itself. The download page will open instead.",
            )
            QDesktopServices.openUrl(QUrl(release.page_url))
            return

        download_dir = tempfile.mkdtemp(prefix="scriptliner-update-")
        zip_path = str(Path(download_dir) / updater.MAC_ASSET_NAME)
        progress = QProgressDialog("Downloading update...", "Cancel", 0, 100, self)
        progress.setWindowTitle("Updating ScriptLiner")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        thread = updater.UpdateDownloadThread(release.asset_url, zip_path, self)
        self._update_download_thread = thread

        def on_progress(done, total):
            total = total or release.asset_size
            if total:
                progress.setValue(min(99, int(done * 100 / total)))

        def cleanup():
            progress.close()
            self._update_download_thread = None
            thread.deleteLater()

        def on_failed(message):
            cleanup()
            shutil.rmtree(download_dir, ignore_errors=True)
            if "cancelled" not in message.lower():
                QMessageBox.warning(self, "Update failed", message)

        def on_downloaded(path):
            cleanup()
            self._install_downloaded_update(path, bundle, download_dir)

        progress.canceled.connect(thread.cancel)
        thread.progress.connect(on_progress)
        thread.failed.connect(on_failed)
        thread.finished_ok.connect(on_downloaded)
        thread.start()

    def _install_downloaded_update(self, zip_path, bundle, download_dir):
        try:
            new_app, work_dir = updater.prepare_update(zip_path, bundle)
        except (updater.UpdateError, OSError) as exc:
            shutil.rmtree(download_dir, ignore_errors=True)
            QMessageBox.warning(self, "Update failed", str(exc))
            return
        shutil.rmtree(download_dir, ignore_errors=True)
        # Save first so nothing is lost - the relaunched app reopens the
        # last project automatically (see _restore_last_session).
        self._autosave()
        try:
            updater.launch_swap_and_relaunch(new_app, bundle, work_dir)
        except OSError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            QMessageBox.warning(self, "Update failed", str(exc))
            return
        self.close()
        QApplication.quit()

    def show_guide(self):
        """Help > View Guide: a read-only copy of the first-launch guide
        that never touches the open project. Its "Open as Practice
        Script" button is the only way it replaces the current script,
        and that confirms first if there's anything to lose."""
        from app.guide_dialog import GuideViewerDialog

        if not GUIDE_SCRIPT_PATH.exists():
            QMessageBox.information(self, "Guide not found", "This build doesn't include the guide file.")
            return
        try:
            paragraphs = self._import_paragraphs_from_path(str(GUIDE_SCRIPT_PATH))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not open the guide", str(exc))
            return
        if not paragraphs:
            return

        dialog = GuideViewerDialog(paragraphs, dark_page=(self.theme == "dark_full"), parent=self)
        dialog.exec()
        if not dialog.practice_requested:
            return
        if self._dirty or self.shots:
            answer = QMessageBox.question(
                self, "Replace the current script?",
                "Opening the guide as a practice script replaces the script and shots currently "
                "open. Unsaved changes will be lost.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._load_structured_script(paragraphs)
        self.statusBar().showMessage("Opened the guide as a practice script.", 4000)

    def _open_feedback_form(self):
        """Opens the Microsoft Forms feedback form (bug report / feature
        idea / general feedback) in the user's default browser. Until
        FEEDBACK_FORM_URL is filled in above, shows a heads-up instead of
        opening a dead link."""
        if not FEEDBACK_FORM_URL:
            QMessageBox.information(
                self, "Feedback Form Not Set Up",
                "The feedback form link hasn't been configured yet.",
            )
            return
        QDesktopServices.openUrl(QUrl(FEEDBACK_FORM_URL))

    def _open_settings_dialog(self):
        from app.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self, self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            before = self._lane_setup()
            dialog.apply_to_window()
            if self.lane_count != before[2]:
                self._fit_lane_width_to_count(before[2])
            self._apply_settings_to_view()
            self._dirty = True
            # Switching an automatic lane order on, off, or between kinds -
            # or changing the number of lanes - re-packs every shot, which
            # can need more lanes than the line size chosen earlier ("Make
            # Lines Smaller") allows for. Ask again rather than leaving
            # lines silently stacked on top of each other.
            if self._lane_setup() != before:
                self._check_lane_overflow(None)

    def _lane_setup(self):
        return (
            self.auto_sort_shots, self.length_sort_shots, self.lane_count, frozenset(self.active_lanes),
        )

    def _fit_lane_width_to_count(self, old_count):
        """Sizes the lines so `lane_count` lanes fit the margin: more lanes
        than before can only make lines narrower (never wider than a
        smaller size already chosen via "Make Lines Smaller"), fewer
        lanes go back to the normal width."""
        available = PAGE_MARGIN_LEFT_PX - LANE_START_X
        fitted = max(MIN_LANE_WIDTH, min(LANE_WIDTH, available // max(1, self.lane_count)))
        view = self.script_view
        if self.lane_count > old_count:
            view.base_lane_width = min(view.base_lane_width, fitted)
        else:
            view.base_lane_width = fitted
        view._apply_scale_to_lane_geometry()

    def _apply_theme(self):
        """Applies self.theme: "dark_ui" and "dark_full" both switch the
        app-wide chrome (buttons, panels, dialogs, lists...) to the dark
        stylesheet; "dark_full" additionally inverts the page itself
        (ScriptTextEdit.set_dark_page) - "dark_ui" leaves the page a
        normal light page, matching a paper preview. Export is
        completely unaffected either way (_write_lined_pdf never reads
        self.theme). Persisted to QSettings immediately so the choice is
        remembered next launch."""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(DARK_STYLESHEET if self.theme != "light" else LIGHT_STYLESHEET)
        self.script_view.set_dark_page(self.theme == "dark_full")
        self._apply_header_theme()
        QSettings("ScriptLiner", "ScriptLiner").setValue("theme", self.theme)

    def _apply_header_theme(self):
        """The header banner is always Midnight Blue - the suite's brand
        surface for headers/navigation (Master Style Guide) - regardless
        of the app's own light/dark page theme, matching how the sidebar
        stays dark in the other suite apps. It uses the separately
        recolored logo asset (wordmark -> light grey, gradient bar left
        untouched) rather than the normal black-on-transparent one, since
        the black wordmark would be unreadable on the dark banner."""
        logo_path = LOGO_LONG_DARK_PATH if LOGO_LONG_DARK_PATH.exists() else LOGO_LONG_PATH
        if logo_path.exists():
            self.header.setPixmap(theme.load_pixmap(logo_path, height=36))
        self.header.setStyleSheet(f"background-color: {theme.MIDNIGHT_BLUE};")

    def _apply_settings_to_view(self):
        self._apply_theme()
        effective_labels = self._effective_line_style_labels()
        self.script_view.page_gap_style = self.page_gap_style
        self.script_view.key_enabled = self.key_enabled
        self.script_view.key_placement = self.key_placement
        self.script_view.key_box_position = self.key_box_position
        # The legend is drawn per-row (one row per shot type mapped to a
        # style, or a generic row for an unmapped style), so it looks up
        # labels by row key (shot type, or style as a fallback) - the raw
        # override dict, not the per-style joined one used by the dropdown.
        self.script_view.line_style_labels = self.line_style_labels
        self.script_view.shot_type_style_map = self.shot_type_style_map
        self.script_view.multi_style_mode = self.multi_style_mode
        self.script_view.active_lanes = self.active_lanes
        self.script_view.lane_count = self.lane_count
        self.script_view.auto_sort_shots = self.auto_sort_shots or self.length_sort_shots
        self.script_view.label_every_segment = self.label_every_segment
        self._update_move_shots_availability()
        self.script_view.apply_page_gaps()
        # The Line Style dropdown always reflects the current effective
        # labels - key_enabled only controls whether the legend is
        # printed/drawn, not whether the labels themselves are in effect.
        self.inspector.set_line_style_labels(effective_labels)
        self.update_highlights()

    def _apply_page_margins(self):
        scale = self.script_view.scale
        doc = self.script_view.document()
        doc.setDocumentMargin(0)
        frame_fmt = doc.rootFrame().frameFormat()
        frame_fmt.setLeftMargin(PAGE_MARGIN_LEFT_PX * scale)
        frame_fmt.setRightMargin(PAGE_MARGIN_RIGHT_PX * scale)
        frame_fmt.setTopMargin(PAGE_MARGIN_TOP_PX * scale)
        frame_fmt.setBottomMargin(PAGE_MARGIN_BOTTOM_PX * scale)
        doc.rootFrame().setFrameFormat(frame_fmt)

    def _apply_interactive_scale(self, new_scale):
        """Called by PageAspectContainer whenever the script pane's fitted
        size changes - re-lays-out the document (and everything overlaid
        on it) at the new zoom factor, mirroring exactly what
        _write_lined_pdf does at an arbitrary export DPI, just driven by
        widget size instead of printer resolution."""
        if abs(new_scale - self.script_view.scale) < 0.001:
            return
        self.script_view.scale = new_scale
        scaled_font = QFont(BASE_FONT_FAMILY)
        scaled_font.setPointSizeF(_points_for_pixels(BASE_FONT_PX * new_scale))
        self.script_view.setFont(scaled_font)
        if self.loaded_paragraphs:
            self.script_view.clear()
            _render_paragraphs_into_document(
                self.script_view.document(), self.loaded_paragraphs, PX_PER_INCH * new_scale
            )
        self._apply_page_margins()
        self.script_view._apply_scale_to_lane_geometry()
        self.script_view.apply_page_gaps()
        self._recompute_scene_numbers()
        self.update_highlights()

    def _on_zoom_in_clicked(self):
        self.page_container.zoom_in()
        self.zoom_label.setText(f"{round(self.page_container.zoom_multiplier * 100)}%")

    def _on_zoom_out_clicked(self):
        self.page_container.zoom_out()
        self.zoom_label.setText(f"{round(self.page_container.zoom_multiplier * 100)}%")

    def new_project(self):
        self._load_script_text("")
        self.current_project_path = None
        self._dirty = False
        self.statusBar().showMessage("Started a new project.", 3000)

    def save_project(self):
        if self.current_project_path is None:
            self.save_project_as()
            return
        self._write_project(self.current_project_path)

    def save_project_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", self._default_dialog_dir(), PROJECT_FILE_FILTER
        )
        if not path:
            return
        if not path.lower().endswith(".slproj"):
            path += ".slproj"
        self._write_project(path)

    def _default_dialog_dir(self) -> str:
        if self.current_project_path:
            return str(Path(self.current_project_path).parent)
        return str(PROGRAM_FILES_DIR)

    def _write_project(self, path: str):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            save_project(self, path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", f"Could not save project:\n{exc}")
            return
        self.current_project_path = path
        self._dirty = False
        self._remember_last_project(path)
        self.statusBar().showMessage(f"Saved project to {path}", 3000)

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", self._default_dialog_dir(), PROJECT_FILE_FILTER
        )
        if not path:
            return
        if self._open_project_path(path):
            self.statusBar().showMessage(f"Opened project {path}", 3000)

    def _open_project_path(self, path: str) -> bool:
        try:
            data = load_project_data(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not open project", str(exc))
            return False

        self.loaded_paragraphs = data.get("loaded_paragraphs")
        if self.loaded_paragraphs:
            self.script_view.clear()
            self._apply_page_margins()
            _render_paragraphs_into_document(
                self.script_view.document(), self.loaded_paragraphs, PX_PER_INCH
            )
        else:
            self._apply_page_margins()
            self.script_view.setPlainText(data.get("script_text") or "")
        self.script_view.apply_page_gaps()
        self._recompute_scene_numbers()

        self.shots = dict_to_shots(data)
        self.shot_lanes = data.get("shot_lanes", {})
        self.scene_used_letters = {
            key: set(letters) for key, letters in data.get("scene_used_letters", {}).items()
        }
        self.camera_letter_colors = data.get("camera_letter_colors", {})
        if data.get("colors"):
            self.colors = data["colors"]
        self.pinned_lanes = set(data.get("pinned_lanes", []))
        self.manual_lanes = {k: int(v) for k, v in (data.get("manual_lanes") or {}).items()}
        self.script_view.base_lane_width = max(
            MIN_LANE_WIDTH, int(data.get("base_lane_width") or LANE_WIDTH)
        )
        self.script_view._apply_scale_to_lane_geometry()
        self.unique_shot_colors = bool(data.get("unique_shot_colors", False))
        self.length_sort_shots = bool(data.get("length_sort_shots", False))
        self.label_every_segment = bool(data.get("label_every_segment", True))
        self.overlay_default_opacity = int(data.get("overlay_default_opacity") or DEFAULT_OVERLAY_OPACITY)

        self.shot_type_style_map = data.get("shot_type_style_map") or {
            st: "solid" for st in ShotInspectorPanel.SHOT_TYPES if st
        }
        self.shot_type_affects_style = data.get("shot_type_affects_style", False)
        self.key_enabled = data.get("key_enabled", False)
        self.key_placement = data.get("key_placement", "first_page")
        self.key_box_position = data.get("key_box_position", KEY_BOX_POSITION_DEFAULT)
        self.line_style_labels = data.get("line_style_labels") or {}
        self.multi_style_mode = data.get("multi_style_mode") or {}
        self.page_gap_style = data.get("page_gap_style", "gap")
        self.lane_count = max(1, min(MAX_LANE_COUNT, int(data.get("lane_count") or DEFAULT_LANE_COUNT)))
        active_lanes = data.get("active_lanes")
        self.active_lanes = set(active_lanes) if active_lanes else set(range(self.lane_count))
        self.auto_sort_shots = data.get("auto_sort_shots", False)
        self._apply_settings_to_view()

        self.undo_stack = []
        self.redo_stack = []
        self._rebuild_shot_list()

        self.current_project_path = path
        self._dirty = False
        self._remember_last_project(path)
        return True

    def _remember_last_project(self, path: str):
        QSettings("ScriptLiner", "ScriptLiner").setValue("last_project_path", str(path))
        # The autosave fallback path isn't a project the user chose to save -
        # don't surface it in Script Studio Hub's Save Files list.
        if Path(path).resolve() == DEFAULT_AUTOSAVE_PATH.resolve():
            return
        try:
            from app.studio_link import report_recent_file

            report_recent_file("scriptliner", path)
        except OSError:
            pass

    def _restore_last_session(self):
        settings = QSettings("ScriptLiner", "ScriptLiner")
        geometry = settings.value("window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

        last_path = settings.value("last_project_path")
        if last_path and Path(last_path).exists():
            if self._open_project_path(last_path):
                self.statusBar().showMessage(f"Restored last project: {last_path}", 4000)
            return

        self._open_guide_script()

    def _open_guide_script(self):
        """Loads the bundled walkthrough script in place of the blank
        placeholder page - called from _restore_last_session every time
        the app boots with no project to restore (not just the first
        launch ever - a one-time flag would mean the guide is only ever
        seen once per machine, then a fresh boot with nothing to restore
        falls back to the old hardcoded placeholder forever after).
        Silently does nothing if the guide file isn't present, e.g. a
        stripped-down build that didn't bundle it."""
        if not GUIDE_SCRIPT_PATH.exists():
            return
        try:
            paragraphs = self._import_paragraphs_from_path(str(GUIDE_SCRIPT_PATH))
        except (OSError, ValueError):
            return
        if paragraphs is not None:
            self._load_structured_script(paragraphs)
            self.statusBar().showMessage("Welcome to ScriptLiner - here's how it works", 5000)

    def _autosave(self):
        if not self._dirty:
            return
        path = self.current_project_path or str(DEFAULT_AUTOSAVE_PATH)
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            save_project(self, path)
        except OSError:
            return
        if self.current_project_path is None:
            self.current_project_path = path
        self._dirty = False
        self._remember_last_project(path)
        self.statusBar().showMessage("Autosaved", 1500)

    def closeEvent(self, event):
        if self._dirty:
            self._autosave()
        QSettings("ScriptLiner", "ScriptLiner").setValue("window_geometry", self.saveGeometry())
        super().closeEvent(event)

    SCRIPT_FILE_FILTER = "Screenplay Files (*.fountain *.txt *.fdx *.fadein *.xml *.rtf *.celtx);;All Files (*)"
    DOCUMENT_FILE_FILTER = "Documents (*.docx *.pdf *.txt);;All Files (*)"
    # Update Script has to accept either kind - a project built from an
    # imported document is revised with a newer version of that document.
    UPDATE_FILE_FILTER = (
        "Screenplay and Document Files (*.fountain *.txt *.fdx *.fadein *.xml *.rtf *.celtx *.docx *.pdf);;"
        "All Files (*)"
    )

    @staticmethod
    def _import_paragraphs_from_path(path: str):
        """Structured paragraphs for FDX/Fade In/Fountain/RTF/Celtx, or
        None for plain text (the caller should just read the file)."""
        lower = path.lower()
        if lower.endswith(".fdx"):
            return import_fdx(path)
        if lower.endswith((".fadein", ".xml")):
            # Fade In's Open Screenplay Format is sometimes saved with a
            # bare .xml extension instead of .fadein - same parser, since
            # import_fadein() already handles both a zipped .fadein
            # archive and a plain (unzipped) XML file.
            return import_fadein(path)
        if lower.endswith(".fountain"):
            return import_fountain(path)
        if lower.endswith(".rtf"):
            return import_rtf(path)
        if lower.endswith(".celtx"):
            return import_celtx(path)
        if lower.endswith((".docx", ".pdf")):
            # Not screenplay-formatted - imported as a plain document
            # (see app/document_import.py).
            return import_document(path)
        return None

    def _read_script_text_from_path(self, path: str):
        """(text, paragraphs_or_None) for any supported script file -
        `text` is the exact formatted plain-text representation the app
        would display for it, built via the same rendering path used to
        actually load a structured script (_render_paragraphs_into_document),
        so it lines up character-for-character with what open_script()
        would show. Used by Update Script to diff against the current
        document on an apples-to-apples basis."""
        paragraphs = self._import_paragraphs_from_path(path)
        if paragraphs is not None:
            temp_doc = QTextDocument()
            _render_paragraphs_into_document(temp_doc, paragraphs, PX_PER_INCH)
            return temp_doc.toPlainText(), paragraphs
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), None

    def clean_script_text(self) -> str:
        """The script's stable, un-paginated text - what shot positions
        are actually stored/saved/diffed against, as opposed to
        self.script_view.toPlainText(), which can include real
        "(MORE)"/"CHARACTER (CONT'D)" text spliced into a Dialogue
        paragraph that happens not to fit the current page (see
        SplitMap) - content that depends on the window's current size/
        zoom and isn't part of the actual script. Rebuilding fresh from
        loaded_paragraphs (no pagination applied) is the same technique
        _read_script_text_from_path already uses to diff an about-to-be-
        imported file on an apples-to-apples basis. Plain-text
        (unstructured) scripts never get split in the first place, so
        script_view's own text is already clean there."""
        if self.loaded_paragraphs:
            temp_doc = QTextDocument()
            _render_paragraphs_into_document(temp_doc, self.loaded_paragraphs, PX_PER_INCH)
            return temp_doc.toPlainText()
        return self.script_view.toPlainText()

    def open_script(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Script", "", self.SCRIPT_FILE_FILTER)
        if not path:
            return

        try:
            paragraphs = self._import_paragraphs_from_path(path)
            if paragraphs is not None:
                self._load_structured_script(paragraphs)
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                self._load_script_text(text)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not open file", str(exc))
            return

    def open_document(self):
        """File > Import Document...: brings in a Word (.docx), PDF, or
        plain-text document that ISN'T screenplay-formatted - a beat
        sheet, treatment, outline, shot list - so it can be lined with
        shots the same way a script is. Each top-level heading becomes a
        numbered section (the equivalent of a scene) that shot ids like
        "2B" are numbered off."""
        path, _ = QFileDialog.getOpenFileName(self, "Import Document", "", self.DOCUMENT_FILE_FILTER)
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            paragraphs = import_document(path)
        except (OSError, ValueError) as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Could not import document", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self._load_structured_script(paragraphs)
        self.statusBar().showMessage(f"Imported document {Path(path).name}", 4000)

    def start_update_script(self):
        """"Update Script..." entry point: pick a new revision of the
        script, then review it side by side with the current one -
        shots whose own text is untouched transfer automatically, and
        anything else has to be placed by hand before the swap can be
        confirmed (see UpdateScriptDialog)."""
        path, _ = QFileDialog.getOpenFileName(self, "Update Script - Select New Version", "", self.UPDATE_FILE_FILTER)
        if not path:
            return

        try:
            new_text, new_paragraphs = self._read_script_text_from_path(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not open file", str(exc))
            return

        from app.update_script_dialog import UpdateScriptDialog

        dialog = UpdateScriptDialog(self, new_text, new_paragraphs, self)
        if dialog.exec() == QDialog.Accepted:
            self._apply_script_update(new_text, new_paragraphs, dialog.result_shots())

    def _apply_script_update(self, new_text, new_paragraphs, shot_lane_pairs):
        """Replaces the document text with the reviewed new version and
        adopts the remapped shot positions/lanes from UpdateScriptDialog.
        Not part of the undo/redo stack - it's a deliberate, reviewed
        document replacement, not an incremental edit."""
        if new_paragraphs is not None:
            self.loaded_paragraphs = new_paragraphs
            self.script_view.clear()
            self._apply_page_margins()
            _render_paragraphs_into_document(self.script_view.document(), new_paragraphs, PX_PER_INCH)
        else:
            self.loaded_paragraphs = None
            self._apply_page_margins()
            self.script_view.setPlainText(new_text)
        self.script_view.apply_page_gaps()
        self._recompute_scene_numbers()

        self.shots = []
        self.shot_lanes = {}
        self.pinned_lanes = set()
        self.manual_lanes = {}
        for shot, lane in shot_lane_pairs:
            self.shots.append(shot)
            self._set_manual_lane(shot.shot_id, lane)

        self._rebuild_shot_list()  # also rebuilds shots_by_id and calls update_highlights()
        self._dirty = True
        self.statusBar().showMessage("Script updated.", 3000)

    def start_merge_projects(self):
        """"Merge Projects..." entry point: pick another .slproj file and
        review its shots side by side with this one - shots whose
        surrounding text matches this project's script transfer over
        automatically, anything else has to be placed by hand, before
        the two shot lists are combined (see MergeProjectsDialog).
        Unlike Update Script, THIS project's text/shots are never
        replaced - only the other project's shots get merged in."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Merge Projects - Select Project to Merge In", self._default_dialog_dir(), PROJECT_FILE_FILTER
        )
        if not path:
            return
        try:
            data = load_project_data(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not open project", str(exc))
            return

        other_paragraphs = data.get("loaded_paragraphs")
        if other_paragraphs:
            temp_doc = QTextDocument()
            _render_paragraphs_into_document(temp_doc, other_paragraphs, PX_PER_INCH)
            other_text = temp_doc.toPlainText()
        else:
            other_text = data.get("script_text") or ""
        other_shots = dict_to_shots(data)
        other_shot_lanes = data.get("shot_lanes", {})

        from app.merge_projects_dialog import MergeProjectsDialog

        dialog = MergeProjectsDialog(
            self, other_text, other_paragraphs, other_shots, other_shot_lanes,
            Path(path).name, self,
        )
        if dialog.exec() == QDialog.Accepted:
            self._apply_project_merge(dialog.result_shots())

    def _apply_project_merge(self, merged_shot_lane_pairs):
        """Adds the reviewed shots from MergeProjectsDialog onto this
        project. Unlike _apply_script_update, this project's own
        document text/paragraphs and its existing shots are left
        completely untouched - only the incoming shots (already given
        unique ids by the dialog, to avoid colliding with this
        project's own) are appended."""
        for shot, lane in merged_shot_lane_pairs:
            self.shots.append(shot)
            self._set_manual_lane(shot.shot_id, lane)
            self.scene_used_letters.setdefault(shot.scene_number, set()).add(shot.camera_letter)

        self._rebuild_shot_list()  # also rebuilds shots_by_id and calls update_highlights()
        self._dirty = True
        self.statusBar().showMessage("Projects merged.", 3000)

    def open_unsupported_format(self):
        QMessageBox.information(
            self,
            "Not available yet",
            "Screenplay-formatted PDF and Movie Magic (.mmx) import haven't been built yet "
            "(a PDF or Word file that isn't a screenplay - a beat sheet, say - can be "
            "brought in with File > Import Document). Movie "
            "Magic's file format isn't something I have reliable documented "
            "knowledge of, so it needs a real sample file to build against "
            "correctly (the same way the .fadein and .rtf importers were). "
            "Plain text, Fountain, Final Draft (.fdx), Fade In (.fadein), "
            "RTF, and Celtx all work today.",
        )

    def _reset_shots(self):
        self.shots = []
        self.shots_by_id = {}
        self.scene_used_letters = {}
        self.shot_lanes = {}
        self.camera_letter_colors = {}
        self.pinned_lanes = set()
        self.manual_lanes = {}
        self.script_view.base_lane_width = LANE_WIDTH  # a new script starts at the default line density
        self.script_view._apply_scale_to_lane_geometry()
        self.undo_stack = []
        self.redo_stack = []
        self.shot_list.clear()
        self.selected_shot = None
        self.selected_shot_item = None
        self.inspector.set_shot(None)

    def _mark_fresh_unsaved_project(self):
        self.current_project_path = None
        self._dirty = True

    def _recompute_scene_numbers(self):
        self.script_view.scene_numbers = _compute_scene_numbers(
            self.script_view.document(), self.loaded_paragraphs
        )

    def _load_script_text(self, text: str):
        self.loaded_paragraphs = None
        self._apply_page_margins()
        self.script_view.setPlainText(text)
        self.script_view.apply_page_gaps()
        self._recompute_scene_numbers()
        self._reset_shots()
        self.update_highlights()
        self._mark_fresh_unsaved_project()

    def _load_structured_script(self, paragraphs):
        self.loaded_paragraphs = paragraphs
        self.script_view.clear()
        self._apply_page_margins()
        _render_paragraphs_into_document(self.script_view.document(), paragraphs, PX_PER_INCH)
        self.script_view.apply_page_gaps()
        self._recompute_scene_numbers()
        self._reset_shots()
        self.update_highlights()
        self._mark_fresh_unsaved_project()

    def show_script_context_menu(self, pos):
        cursor = self.script_view.textCursor()
        menu = QMenu(self)
        new_shot_action = None
        if cursor.hasSelection():
            new_shot_action = menu.addAction("New Shot")

        action = menu.exec(self.script_view.mapToGlobal(pos))
        if new_shot_action is not None and action == new_shot_action:
            self.create_shot()

    def _show_shot_list_context_menu(self, pos):
        item = self.shot_list.itemAt(pos)
        if item is None:
            return
        shot = self.shots_by_id.get(item.text())
        if shot is None:
            return
        menu = QMenu(self)
        duplicate_action = menu.addAction("Duplicate Shot")
        action = menu.exec(self.shot_list.mapToGlobal(pos))
        if action == duplicate_action:
            self._open_duplicate_dialog(shot)

    def _duplicate_selected_shot(self):
        if self.selected_shot is not None:
            self._open_duplicate_dialog(self.selected_shot)

    def _open_duplicate_dialog(self, preselected_shot=None):
        """Entry point for both the inspector's Duplicate Shot button and
        the shot_list right-click menu: lets the user pick which shot to
        copy and which of its fields to carry over, then hands off to
        the placement flow (_begin_duplicate)."""
        if not self.shots:
            self.statusBar().showMessage("No shots to duplicate.", 3000)
            return
        from app.duplicate_dialog import DuplicateShotDialog

        dialog = DuplicateShotDialog(self.shots, preselected_shot, self)
        if dialog.exec() == QDialog.Accepted:
            shot = dialog.selected_shot()
            if shot is not None:
                self._begin_duplicate(shot, dialog.selected_fields())

    def get_scene_number_for_position(self, clean_position: int) -> str:
        """Which scene a shot starting at `clean_position` (a "clean" -
        stable, saved/diffed - position, same space shots are always
        stored in; see SplitMap) belongs to, so new shots are
        auto-numbered off the actual scene structure instead of the
        user tracking it by hand.
        When the script came from a structured import (FDX, Fade In,
        Fountain, RTF, Celtx), self.loaded_paragraphs already tags each
        paragraph's real type - counting "Scene Heading" entries there is
        more reliable than regex-guessing from raw text, since a scene
        heading doesn't have to start with INT/EXT to be one (custom
        sluglines, forced headings, etc.). Paragraph i corresponds 1:1 to
        the document's NON-synthetic blocks (real (MORE)/(CONT'D) splits
        insert extra blocks with no paragraph of their own - see
        _SYNTHETIC_BLOCK_PROPERTY), so converting to this document's
        current rendered position and walking QTextCursor.blockNumber()
        maps straight to a paragraph index. If a paragraph carries an
        explicit numeric scene number (imported from the source file),
        that's honored instead of the auto-incremented count -
        non-numeric revision marks like "12A" fall back to the count,
        since shot IDs assume a purely-numeric scene number."""
        if self.loaded_paragraphs:
            cursor = self.script_view.textCursor()
            cursor.setPosition(self.script_view.split_map.clean_to_rendered(clean_position))
            target_block_number = cursor.blockNumber()
            # A real (MORE)/(CONT'D) split (see _try_split_dialogue_block)
            # inserts 3 extra blocks with no counterpart in
            # loaded_paragraphs - translate Qt's raw block index into
            # the equivalent PARAGRAPH index by not counting them.
            block = self.script_view.document().begin()
            block_number = -1
            paragraph_index = -1
            while block.isValid() and block_number < target_block_number:
                block_number += 1
                if not block.blockFormat().property(_SYNTHETIC_BLOCK_PROPERTY):
                    paragraph_index += 1
                block = block.next()

            scene_count = 0
            current_scene_number = None
            for i, para in enumerate(self.loaded_paragraphs):
                if i > paragraph_index:
                    break
                p_type = para[0]
                if p_type not in SECTION_HEADING_TYPES:
                    continue
                scene_count += 1
                explicit_number = para[2] if len(para) > 2 else None
                if explicit_number and str(explicit_number).isdigit():
                    current_scene_number = str(explicit_number)
                else:
                    current_scene_number = str(scene_count)
            return current_scene_number or str(max(scene_count, 1))

        # Plain-text (unstructured) scripts never get a real split (no
        # element-type tags to trigger one - see _PARA_TYPE_PROPERTY),
        # so clean and rendered positions are always identical here.
        text = self.script_view.toPlainText()
        upto = text[:clean_position]
        scene_count = 0
        for line in upto.split("\n"):
            if SCENE_HEADING_RE.match(line):
                scene_count += 1
        return str(max(scene_count, 1))

    def _next_shot_letter(self, scene_number) -> str:
        """Next unused per-scene shot-number suffix, e.g. the "A" in "12A".
        This is purely about making shot_id unique within a scene - it has
        no relationship to which physical camera a shot was filmed on."""
        scene_key = str(scene_number)
        used = self.scene_used_letters.setdefault(scene_key, set())
        index = 1
        while chr(64 + index) in used:
            index += 1
        letter = chr(64 + index)
        used.add(letter)
        return letter

    def _effective_line_style_labels(self) -> dict:
        """Line Style -> label used by the Line Style dropdown: joins the
        name of every shot type mapped to that style in Settings (e.g.
        "Wide/Medium" if both map to "solid"), honoring a per-shot-type
        override from self.line_style_labels. Unlike the Key's rows, this
        is purely mapping-based - it doesn't require a shot to already
        exist, since it's what makes a brand-new shot's dropdown show
        meaningful names in the first place."""
        grouped = {}
        for shot_type, style in self.shot_type_style_map.items():
            if not shot_type:
                continue
            label = self.line_style_labels.get(shot_type, shot_type)
            grouped[style] = f"{grouped[style]}/{label}" if style in grouped else label
        return grouped

    def _color_for_camera_letter(self, letter: str) -> str:
        """Shots sharing a camera letter (e.g. "A" for camera A in a
        multi-camera setup) always share the same color."""
        if letter not in self.camera_letter_colors:
            if self.unique_shot_colors:
                self.camera_letter_colors[letter] = self._next_shot_color(
                    extra_used=self.camera_letter_colors.values()
                )
            else:
                self.camera_letter_colors[letter] = self.colors[
                    len(self.camera_letter_colors) % len(self.colors)
                ]
        return self.camera_letter_colors[letter]

    def _next_shot_color(self, extra_used=()):
        """The color for a newly created shot. Normally the palette cycles
        in creation order. With "every shot gets its own new color" on,
        it's the first color in SHOT_COLORS that nothing is using - so no
        two shots share one until all 30 are taken, after which the
        least-used color repeats (earliest first)."""
        if not self.unique_shot_colors:
            return self.colors[len(self.shots) % len(self.colors)]
        uses = {}
        for color in list(extra_used) + [s.color for s in self.shots]:
            uses[color.lower()] = uses.get(color.lower(), 0) + 1
        return min(SHOT_COLORS, key=lambda c: (uses.get(c, 0), SHOT_COLORS.index(c)))

    def _shape_for_color(self, color: str) -> str:
        """The tick-mark shape paired with `color` by shared index into
        self.colors/TICK_SHAPES (see TICK_SHAPES) - keeps a shot's shape
        following whichever color it was actually assigned (by creation
        order or by camera letter) without a second parallel counter.
        Falls back to the first shape for a color that isn't one of the
        current palette entries (e.g. a custom/edited color)."""
        if color in self.colors:
            index = self.colors.index(color)
        elif color.lower() in SHOT_COLORS:
            # One of the fixed "every shot its own color" colors - its
            # position in that list, so consecutive shots get different
            # shapes too.
            index = SHOT_COLORS.index(color.lower())
        else:
            try:
                index = int(color.lstrip("#"), 16)  # an old custom color - still deterministic
            except ValueError:
                index = 0
        return TICK_SHAPES[index % len(TICK_SHAPES)]

    def create_shot(self):
        cursor = self.script_view.textCursor()

        if not cursor.hasSelection():
            self.statusBar().showMessage("No text selected.", 3000)
            return

        self._push_undo()

        selected_text = cursor.selectedText().replace("\u2029", "\n")
        # A selection is made against the live RENDERED document, but
        # everything a shot stores (start_index/end_index) is always in
        # "clean" (stable, saved/diffed) coordinates - see SplitMap.
        start = self.script_view.split_map.rendered_to_clean(cursor.selectionStart())
        end = self.script_view.split_map.rendered_to_clean(cursor.selectionEnd())

        scene_number = self.get_scene_number_for_position(start)
        shot_letter = self._next_shot_letter(scene_number)
        shot_id = f"{scene_number}{shot_letter}"

        # Camera letter starts blank - it's assigned deliberately in the
        # inspector, independent of the shot's scene-sequential letter.
        color = self._next_shot_color()

        shot = Shot(
            shot_id=shot_id,
            scene_number=str(scene_number),
            camera_letter="",
            start_index=start,
            end_index=end,
            text=selected_text,
            color=color,
            tick_shape=self._shape_for_color(color),
        )

        self.shots.append(shot)
        self.shots_by_id[shot.shot_id] = shot
        self.update_highlights()

        item = QListWidgetItem(shot.shot_id)
        self.shot_list.addItem(item)
        self.shot_list.setCurrentItem(item)
        self.select_shot(shot, item)

        self._check_lane_overflow(shot)

    def _begin_duplicate(self, source_shot, fields_to_copy):
        self._begin_placement("duplicate", source_shot, fields_to_copy)

    def start_edit_placement(self):
        """"Edit Start/End" entry point: edits the currently selected
        shot's own start/end/lane in place, using the exact same
        drag-to-place UI as duplicating a shot (see _begin_placement)."""
        shot = self.selected_shot
        if shot is None:
            return
        self._begin_placement("edit", shot, None)

    def _begin_placement(self, mode, source_shot, fields_to_copy):
        """Starts placement mode - either "duplicate" (create a new copy
        of source_shot) or "edit" (move source_shot's own start/end/lane
        in place) - immediately showing the preview and Show-Lanes-style
        guide lines, defaulted to source_shot's exact current
        start/end/length, rather than waiting for the user to click
        somewhere first. From there the user drags the preview's
        start/end handles (up/down, resize that end) and its center
        handle (side to side, pick the lane) to move it before hitting
        Confirm."""
        start, end = self._shot_span(source_shot)
        length = max(1, end - start)
        self._placement_mode = mode
        self._placement_source = source_shot
        self._duplicate_fields = fields_to_copy
        # Editing in place starts from the shot's own current lane (and
        # stays excluded from the other-shots fade below) rather than
        # jumping to lane 0 and looking like a brand new shot.
        initial_lane = self.shot_lanes.get(source_shot.shot_id, 0) if mode == "edit" else 0
        self.script_view.editing_shot_id = source_shot.shot_id if mode == "edit" else None
        self._place_duplicate_preview(start, length, lane=initial_lane)

    def _place_duplicate_preview(self, clean_position, length, lane=0):
        # `clean_position` is a "clean" (stable, saved/diffed) position -
        # same space _shot_span/_begin_placement pass in - but
        # duplicate_preview itself lives in RENDERED coordinates (that's
        # what painting/hit-testing/mouse-dragging all use), so convert
        # once here at the seed point. See SplitMap.
        position = self.script_view.split_map.clean_to_rendered(clean_position)
        doc_length = len(self.script_view.toPlainText())
        end = min(doc_length, position + length)
        start = max(0, end - length)  # keeps the full length even when placed near the document's end
        self.script_view.duplicate_preview = {"start": start, "end": end, "lane": lane}
        self.script_view.viewport().update()
        self.statusBar().showMessage(
            "Drag the top/bottom handles to adjust start/end, the center "
            "handle to pick a lane, then Confirm."
        )
        self._show_placement_confirm_controls(True)

    def _show_placement_confirm_controls(self, visible: bool):
        self.confirm_duplicate_btn.setVisible(visible)
        self.cancel_duplicate_btn.setVisible(visible)
        for shortcut in self._duplicate_confirm_shortcuts:
            shortcut.setEnabled(visible)
        # Starting a second placement mid-placement would leave two
        # conflicting interactive modes fighting over
        # position_pick_callback - block that until this one resolves.
        self.create_shot_btn.setEnabled(not visible)
        self.inspector.duplicate_btn.setEnabled(not visible and self.selected_shot is not None)
        self._sync_button_shortcuts(None, QApplication.instance().focusWidget())

    def _confirm_placement(self):
        preview = self.script_view.duplicate_preview
        source = self._placement_source
        mode = self._placement_mode
        if preview is None or source is None:
            self._cancel_placement()
            return

        self._push_undo()

        # preview holds RENDERED positions (from mouse-dragging the live
        # document) - shots always store "clean" (stable, saved/diffed)
        # positions, so convert before writing anything back. See
        # SplitMap. rendered_start/rendered_end are kept too, for
        # slicing the live document's OWN text below.
        rendered_start, rendered_end = preview["start"], preview["end"]
        start = self.script_view.split_map.rendered_to_clean(rendered_start)
        end = self.script_view.split_map.rendered_to_clean(rendered_end)

        if mode == "edit":
            # Only the overall start/end move - an existing multi-segment
            # (Split Shots) shot keeps its internal segments untouched.
            source.segments[0].start_index = start
            source.start_index = start
            source.segments[-1].end_index = end
            source.end_index = end
            self._set_manual_lane(source.shot_id, preview.get("lane", 0))
            self.update_highlights()
            self._check_lane_overflow(source)
            message = f"Updated {source.shot_id}'s placement."
        else:
            fields = self._duplicate_fields or set()
            scene_number = self.get_scene_number_for_position(start)
            shot_letter = self._next_shot_letter(scene_number)
            shot_id = f"{scene_number}{shot_letter}"

            # Camera Letter and its color are only copied if the dialog's
            # checkbox for it was checked - otherwise a fresh id/color/blank
            # camera letter, the same convention as any new shot
            # (create_shot). Every other field below is copied only if its
            # own checkbox was checked.
            camera_letter = source.camera_letter if "camera_letter" in fields else ""
            if camera_letter:
                color = self._color_for_camera_letter(camera_letter)
            else:
                color = self._next_shot_color()

            new_shot = Shot(
                shot_id=shot_id,
                scene_number=str(scene_number),
                camera_letter=camera_letter,
                start_index=start,
                end_index=end,
                text=self.script_view.toPlainText()[rendered_start:rendered_end],
                color=color,
                tick_shape=self._shape_for_color(color),
                line_style=source.line_style if "line_style" in fields else "solid",
                shot_type=source.shot_type if "shot_type" in fields else "",
                lens=source.lens if "lens" in fields else "",
                camera_movement=source.camera_movement if "camera_movement" in fields else "",
                description=source.description if "description" in fields else "",
                notes=source.notes if "notes" in fields else "",
            )

            self.shots.append(new_shot)
            self.shots_by_id[new_shot.shot_id] = new_shot
            # The lane the user picked by dragging the preview's center
            # handle is pinned, same as manually dragging a shot in Show
            # Lanes - otherwise auto-packing could silently move it
            # elsewhere the moment another shot is added nearby.
            self._set_manual_lane(new_shot.shot_id, preview.get("lane", 0))
            self.update_highlights()

            item = QListWidgetItem(new_shot.shot_id)
            self.shot_list.addItem(item)
            self.shot_list.setCurrentItem(item)
            self.select_shot(new_shot, item)

            self._check_lane_overflow(new_shot)
            message = f"Duplicated as {new_shot.shot_id}."

        self._placement_mode = None
        self._placement_source = None
        self._duplicate_fields = None
        self.script_view.editing_shot_id = None
        self.script_view.duplicate_preview = None
        self.script_view.position_pick_callback = None
        self.script_view.viewport().unsetCursor()
        self.script_view.viewport().update()
        self._show_placement_confirm_controls(False)
        self.statusBar().showMessage(message, 3000)

    def _cancel_placement(self):
        self._placement_mode = None
        self._placement_source = None
        self._duplicate_fields = None
        self.script_view.editing_shot_id = None
        self.script_view.duplicate_preview = None
        self.script_view.position_pick_callback = None
        self.script_view.viewport().unsetCursor()
        self.script_view.viewport().update()
        self._show_placement_confirm_controls(False)
        self.statusBar().clearMessage()

    def on_shot_selected(self, item):
        shot = self.shots_by_id.get(item.text())
        self.select_shot(shot, item)

    def select_shot(self, shot, item):
        self.selected_shot = shot
        self.selected_shot_item = item
        self.inspector.set_shot(shot)
        self._sync_button_shortcuts(None, QApplication.instance().focusWidget())

    def delete_selected_shot(self):
        shot = self.selected_shot
        if shot is None:
            return

        self._push_undo()

        # Same teardown as the "rename onto an existing shot" merge path:
        # free the shot's per-scene letter slot before dropping it.
        scene_key = shot.scene_number
        match = SHOT_ID_RE.match(shot.shot_id)
        if match and match.group(2) and scene_key in self.scene_used_letters:
            self.scene_used_letters[scene_key].discard(match.group(2).upper())

        self.shots.remove(shot)
        self.shot_lanes.pop(shot.shot_id, None)
        self.pinned_lanes.discard(shot.shot_id)
        self.manual_lanes.pop(shot.shot_id, None)
        self._rebuild_shot_list()

        self.statusBar().showMessage(f"Deleted shot {shot.shot_id}.", 3000)

    def on_shot_field_changed(self, field_name, value):
        shot = self.selected_shot
        if shot is None:
            return

        if field_name == "shot_id":
            new_id = value.strip()
            if not new_id:
                QMessageBox.warning(self, "Can't rename shot", "Shot number can't be empty.")
                self.inspector.set_shot(shot)
                return
            if new_id == shot.shot_id:
                return

            existing = self.shots_by_id.get(new_id)
            if existing is not None and existing is not shot:
                # Renaming to an existing shot's number merges this shot's
                # range into it as an additional segment - i.e. that shot
                # now has a break/gap in it, rather than two shots sharing
                # one number.
                self._push_undo()

                old_scene_key = shot.scene_number
                old_match = SHOT_ID_RE.match(shot.shot_id)
                if old_match and old_match.group(2) and old_scene_key in self.scene_used_letters:
                    self.scene_used_letters[old_scene_key].discard(old_match.group(2).upper())

                for seg in shot.segments:
                    existing.add_segment(seg.start_index, seg.end_index)

                self.shots.remove(shot)
                self._rebuild_shot_list()

                merged_item = None
                for i in range(self.shot_list.count()):
                    if self.shot_list.item(i).text() == existing.shot_id:
                        merged_item = self.shot_list.item(i)
                        break
                if merged_item is not None:
                    self.shot_list.setCurrentItem(merged_item)
                self.select_shot(existing, merged_item)

                self.statusBar().showMessage(
                    f"Merged into shot {existing.shot_id} as an additional segment.", 4000
                )
                self.update_highlights()
                return

            self._push_undo()

            old_scene_key = shot.scene_number
            old_match = SHOT_ID_RE.match(shot.shot_id)
            if old_match and old_match.group(2) and old_scene_key in self.scene_used_letters:
                self.scene_used_letters[old_scene_key].discard(old_match.group(2).upper())

            match = SHOT_ID_RE.match(new_id)
            if match and match.group(2):
                new_scene_key, new_letter = match.group(1), match.group(2).upper()
                shot.scene_number = new_scene_key
                self.scene_used_letters.setdefault(new_scene_key, set()).add(new_letter)

            del self.shots_by_id[shot.shot_id]
            shot.shot_id = new_id
            self.shots_by_id[new_id] = shot
            if self.selected_shot_item is not None:
                self.selected_shot_item.setText(new_id)
            self.update_highlights()
            return

        if field_name == "camera_letter":
            new_letter = value.strip()
            if new_letter == shot.camera_letter:
                return
            self._push_undo()
            shot.camera_letter = new_letter
            if new_letter:
                shot.color = self._color_for_camera_letter(new_letter)
                shot.tick_shape = self._shape_for_color(shot.color)
            self.update_highlights()
            return

        if field_name == "shot_type":
            if getattr(shot, field_name, None) == value:
                return
            self._push_undo()
            shot.shot_type = value
            if self.shot_type_affects_style and value in self.shot_type_style_map:
                shot.line_style = self.shot_type_style_map[value]
                self.inspector.set_shot(shot)  # refresh so Line Style reflects the auto-applied value
            self.update_highlights()
            return

        if field_name == "overlay":
            value = bool(value)
            if shot.overlay == value:
                return
            self._push_undo()
            shot.overlay = value
            if value:
                shot.overlay_opacity = self.overlay_default_opacity
            # Moving between the margin and the text means any manual
            # margin lane no longer applies.
            self.pinned_lanes.discard(shot.shot_id)
            self.manual_lanes.pop(shot.shot_id, None)
            self.update_highlights()
            self.inspector.set_shot(shot)  # refresh the opacity box's enabled state/value
            return

        if getattr(shot, field_name, None) == value:
            return
        self._push_undo()
        setattr(shot, field_name, value)

        if field_name == "line_style":
            self.update_highlights()
        elif field_name in ("overlay_opacity", "page_note"):
            self.script_view.viewport().update()

    @staticmethod
    def _shot_span(shot):
        """(start, end) character positions spanning every segment of a
        shot - the shot's overall range, ignoring any mid-shot gaps from
        Split Shots."""
        starts = [seg.start_index for seg in shot.segments]
        ends = [seg.end_index for seg in shot.segments]
        return min(starts), max(ends)

    def _recompute_lanes(self) -> int:
        """Assigns every shot a lane (self.shot_lanes) and returns how many
        MARGIN lanes that takes. A shot drawn over the text (Shot.overlay)
        is packed into its own separate set of lanes inside the text area,
        so it never uses up margin space - its number is only meaningful
        together with shot.overlay (see _shot_line_x)."""
        if not self.shots:
            self.shot_lanes = {}
            return 0

        def natural_key(s):
            match = SHOT_ID_RE.match(s.shot_id)
            if match:
                return (int(match.group(1)), match.group(2) or "")
            return (float("inf"), s.shot_id)

        # Y positions (normalized to scale=1.0, so the same threshold
        # applies however zoomed-in the interactive view happens to be
        # right now) used only to enforce MIN_LANE_GAP_PX - lane
        # assignment itself still keys off text position, as before.
        doc = self.script_view.document()
        scale = max(self.script_view.scale, 1e-6)

        def y_of(clean_position):
            # _shot_span returns "clean" positions - see SplitMap -
            # convert to this document's current rendered position
            # before measuring an actual pixel Y from it.
            rendered_position = self.script_view.split_map.clean_to_rendered(clean_position)
            return _absolute_y_for_position(doc, rendered_position) / scale

        ordered = sorted(self.shots, key=natural_key)
        margin_shots = [s for s in ordered if not s.overlay]
        overlay_shots = [s for s in ordered if s.overlay]
        # An automatic lane order (alphabetical, or by length) ignores
        # manual pins entirely and re-packs every shot, so overlaps always
        # line up the same way regardless of how shots were dragged.
        forced = self.auto_sort_shots or self.length_sort_shots
        assignment = {}

        pinned_lane_of = {}
        if not forced:
            # Pinned shots go back to where the user dragged them - read
            # from manual_lanes, which an automatic order never touches,
            # not shot_lanes, which it overwrites. (A pin from before
            # manual_lanes existed has no entry yet, so its current lane
            # is taken as the manual one - only ever filling a gap.)
            for shot in margin_shots:
                if shot.shot_id not in self.pinned_lanes:
                    continue
                if shot.shot_id not in self.manual_lanes and shot.shot_id in self.shot_lanes:
                    self.manual_lanes[shot.shot_id] = self.shot_lanes[shot.shot_id]
                lane = self.manual_lanes.get(shot.shot_id)
                if lane is not None:
                    pinned_lane_of[shot.shot_id] = lane

        if self.length_sort_shots:
            margin_count = self._pack_lanes_by_length(margin_shots, assignment, y_of, honor_disabled=True)
            self._pack_lanes_by_length(overlay_shots, assignment, y_of, honor_disabled=False)
        else:
            margin_count = self._pack_lanes_in_order(
                margin_shots, assignment, y_of, pinned_lane_of, honor_disabled=True
            )
            self._pack_lanes_in_order(overlay_shots, assignment, y_of, {}, honor_disabled=False)

        self.shot_lanes = assignment
        return margin_count

    def _pack_lanes_in_order(self, shots, assignment, y_of, pinned_lane_of, honor_disabled):
        """First-fit packing in scene/camera-letter order: each shot takes
        the first lane that's free by the time it starts. Shots in
        `pinned_lane_of` keep their exact lane and everything else packs
        around them. Returns the number of lanes used."""
        shot_bounds = self._shot_span
        lane_end_positions = []
        lane_end_y = []

        for shot in shots:
            lane = pinned_lane_of.get(shot.shot_id)
            if lane is None:
                continue
            shot_start, shot_end = shot_bounds(shot)
            while len(lane_end_positions) <= lane:
                lane_end_positions.append(0)
                lane_end_y.append(float("-inf"))
            lane_end_positions[lane] = max(lane_end_positions[lane], shot_end)
            lane_end_y[lane] = max(lane_end_y[lane], y_of(shot_end))
            assignment[shot.shot_id] = lane

        for shot in shots:
            if shot.shot_id in assignment:
                continue
            shot_start, shot_end = shot_bounds(shot)
            shot_start_y = y_of(shot_start)
            placed_lane = None
            for lane_index, occupied_until in enumerate(lane_end_positions):
                if honor_disabled and _lane_disabled(lane_index, self.active_lanes, self.lane_count):
                    continue
                if (
                    shot_start >= occupied_until
                    and shot_start_y - lane_end_y[lane_index] >= MIN_LANE_GAP_PX
                ):
                    lane_end_positions[lane_index] = shot_end
                    lane_end_y[lane_index] = y_of(shot_end)
                    placed_lane = lane_index
                    break
            if placed_lane is None:
                # Extend to the next USABLE lane index, padding over any
                # disabled slots along the way with permanent sentinels
                # so they keep their fixed visual column but can never be
                # selected (here or by the "first fit" search above).
                placed_lane = len(lane_end_positions)
                while honor_disabled and _lane_disabled(placed_lane, self.active_lanes, self.lane_count):
                    lane_end_positions.append(float("inf"))
                    lane_end_y.append(float("inf"))
                    placed_lane += 1
                lane_end_positions.append(shot_end)
                lane_end_y.append(y_of(shot_end))
            assignment[shot.shot_id] = placed_lane

        return len(lane_end_positions)

    def _pack_lanes_by_length(self, shots, assignment, y_of, honor_disabled):
        """Longest-first packing: the shot spanning the most page goes in
        the outermost (leftmost) lane, and shorter ones fill in toward
        the text, so overlapping shots nest like brackets - long ones on
        the outside, short ones on the inside. A shot takes the first lane
        where nothing it would touch is already sitting. Returns the
        number of lanes used."""
        shot_bounds = self._shot_span
        info = {}
        for shot in shots:
            start, end = shot_bounds(shot)
            start_y, end_y = y_of(start), y_of(end)
            info[shot.shot_id] = (start, end, start_y, end_y)

        def sort_key(shot):
            start, end, start_y, end_y = info[shot.shot_id]
            match = SHOT_ID_RE.match(shot.shot_id)
            natural = (int(match.group(1)), match.group(2) or "") if match else (float("inf"), shot.shot_id)
            return (-(end_y - start_y), -(end - start), natural)

        def fits(lane_items, start, end, start_y, end_y):
            for p_start, p_end, p_start_y, p_end_y in lane_items:
                after = start >= p_end and start_y - p_end_y >= MIN_LANE_GAP_PX
                before = p_start >= end and p_start_y - end_y >= MIN_LANE_GAP_PX
                if not (after or before):
                    return False
            return True

        lanes = []  # lanes[i] = [(start, end, start_y, end_y), ...]
        highest = -1
        for shot in sorted(shots, key=sort_key):
            start, end, start_y, end_y = info[shot.shot_id]
            lane_index = 0
            while True:
                if honor_disabled and _lane_disabled(lane_index, self.active_lanes, self.lane_count):
                    lane_index += 1
                    continue
                while len(lanes) <= lane_index:
                    lanes.append([])
                if fits(lanes[lane_index], start, end, start_y, end_y):
                    break
                lane_index += 1
            lanes[lane_index].append(info[shot.shot_id])
            assignment[shot.shot_id] = lane_index
            highest = max(highest, lane_index)
        return highest + 1

    def _set_manual_lane(self, shot_id: str, lane: int):
        """Pins a shot to `lane` and remembers it as a manual choice, so
        it survives an automatic lane order being turned on and off."""
        self.shot_lanes[shot_id] = lane
        self.pinned_lanes.add(shot_id)
        self.manual_lanes[shot_id] = lane

    def _pin_shot_lane(self, shot_id: str, lane: int):
        """Pin a shot to a specific lane (from dragging it in Show Lanes
        mode) and keep it there on future lane recomputes."""
        if shot_id not in self.shots_by_id:
            return
        self._set_manual_lane(shot_id, lane)
        self._push_undo()
        self._recompute_lanes()
        self.update_highlights()

    def _on_key_overflow_changed(self, needs_two_columns: bool):
        """Called from the interactive view's paintEvent with whether the
        Key legend needed a second column to fit in its top-margin band.
        Only nags once per "episode" of overflowing, not on every repaint."""
        if needs_two_columns and not self._key_overflow_notified:
            self._key_overflow_notified = True
            self.statusBar().showMessage(
                'The Key legend needs 2 columns to fit - consider turning on "Merge '
                'Multiples" in Settings for line styles used by multiple shot types '
                "to make it more compact.",
                8000,
            )
        elif not needs_two_columns:
            self._key_overflow_notified = False

    def _toggle_show_lanes(self, checked: bool):
        self.show_lanes = checked
        self.script_view.show_lanes = checked
        self.script_view.viewport().update()

    def _update_move_shots_availability(self):
        """The Move Shots button (manual drag-to-reassign) is pointless
        while "Shots in Alphabetical Order" is on - auto-sort ignores
        manual pins outright (see _recompute_lanes), so any drag would
        just get overridden. Grey the button out and swap its shortcut
        hint for "[Unavailable]" instead of leaving a live control that
        does nothing."""
        available = not (self.auto_sort_shots or self.length_sort_shots)
        self.show_lanes_btn.setEnabled(available)
        if available:
            self.show_lanes_btn.update_shortcut_hint(self._load_shortcut("show_lanes").toString())
        else:
            if self.show_lanes_btn.isChecked():
                self.show_lanes_btn.setChecked(False)
            self.show_lanes_btn._shortcut_hint_label.setText("[Unavailable]")

    def _check_lane_overflow(self, shot):
        needed_lanes = self._recompute_lanes()
        if needed_lanes > self.script_view.max_lanes:
            self._handle_lane_overflow(needed_lanes)

    def _handle_lane_overflow(self, needed_lanes: int):
        box = QMessageBox(self)
        box.setWindowTitle("Too Many Overlapping Shots")
        text = (
            f"This section of the script needs {needed_lanes} lanes to show "
            "every overlapping shot as a separate line, more than fit at the "
            "current size. Do you want to continue with the lines overlapping, "
            "or make all the lines smaller so more can fit side by side?"
        )
        if needed_lanes > MAX_LANE_COUNT:
            text += (
                f"\n\nThe smallest size fits {MAX_LANE_COUNT} lanes, so the "
                f"other {needed_lanes - MAX_LANE_COUNT} would still overlap - "
                "drawing the extras over the text avoids that."
            )
        box.setText(text)
        continue_btn = box.addButton("Continue with Overlap", QMessageBox.RejectRole)
        smaller_btn = box.addButton("Make Lines Smaller", QMessageBox.AcceptRole)
        overlay_btn = box.addButton("Draw Extra Lines Over the Text", QMessageBox.ActionRole)
        box.setDefaultButton(smaller_btn)
        box.exec()
        if box.clickedButton() is smaller_btn:
            self.script_view.set_lane_capacity(min(needed_lanes, MAX_LANE_COUNT))
        elif box.clickedButton() is overlay_btn:
            self._move_overflow_shots_over_text()

    def _move_overflow_shots_over_text(self):
        """Switches every shot that didn't fit in the margin lanes over to
        being drawn on top of the text (at the default opacity from
        Settings), which frees their margin lanes up. Undoable."""
        max_lanes = self.script_view.max_lanes
        overflowing = [
            s for s in self.shots
            if not s.overlay and self.shot_lanes.get(s.shot_id, 0) >= max_lanes
        ]
        if not overflowing:
            return
        self._push_undo()
        for shot in overflowing:
            shot.overlay = True
            shot.overlay_opacity = self.overlay_default_opacity
            self.pinned_lanes.discard(shot.shot_id)
            self.manual_lanes.pop(shot.shot_id, None)
        self.update_highlights()
        if self.selected_shot is not None:
            self.inspector.set_shot(self.selected_shot)
        plural = "s" if len(overflowing) != 1 else ""
        self.statusBar().showMessage(
            f"{len(overflowing)} line{plural} moved over the text - adjust opacity in the Inspector.", 5000
        )

    def _snapshot(self):
        return (
            copy.deepcopy(self.shots),
            copy.deepcopy(self.scene_used_letters),
        )

    def _push_undo(self):
        self.undo_stack.append(self._snapshot())
        if len(self.undo_stack) > MAX_UNDO_STEPS:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self._dirty = True

    def undo(self):
        if not self.undo_stack:
            self.statusBar().showMessage("Nothing to undo.", 2000)
            return
        self.redo_stack.append(self._snapshot())
        self.shots, self.scene_used_letters = self.undo_stack.pop()
        self._rebuild_shot_list()
        self._dirty = True
        self.statusBar().showMessage("Undid last change.", 2000)

    def redo(self):
        if not self.redo_stack:
            self.statusBar().showMessage("Nothing to redo.", 2000)
            return
        self.undo_stack.append(self._snapshot())
        self.shots, self.scene_used_letters = self.redo_stack.pop()
        self._rebuild_shot_list()
        self._dirty = True
        self.statusBar().showMessage("Redid change.", 2000)

    def _rebuild_shot_list(self):
        self.shot_list.clear()
        self.shots_by_id = {}
        for shot in self.shots:
            self.shots_by_id[shot.shot_id] = shot
            item = QListWidgetItem(shot.shot_id)
            self.shot_list.addItem(item)
        self.selected_shot = None
        self.selected_shot_item = None
        self.inspector.set_shot(None)
        self.update_highlights()

    def export_lined_pdf(self):
        from app.export_settings_dialog import ExportSettingsDialog

        settings_dialog = ExportSettingsDialog(self)
        if settings_dialog.exec() != ExportSettingsDialog.DialogCode.Accepted:
            return
        export_settings = settings_dialog.result_settings()

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Lined Script as PDF", "", "PDF Files (*.pdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"

        try:
            key_overflowed_columns = self._write_lined_pdf(path, export_settings)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not export PDF:\n{exc}")
            return

        self.statusBar().showMessage(f"Exported lined script to {path}", 4000)

        if key_overflowed_columns:
            QMessageBox.information(
                self,
                "Key legend needed 2 columns",
                "The Key legend didn't fit in a single column within its top-margin "
                "band, so it spilled into a second column.\n\nConsider turning on "
                '"Merge Multiples" in Settings for line styles used by multiple shot '
                "types to make it more compact.",
            )

    def _write_lined_pdf(self, path: str, export_settings=None):
        """export_settings (all optional, see ExportSettingsDialog):
        - "scene_filter": a set of scene-number strings (see
          _parse_scene_selector) - only pages that contain at least one
          of these scenes are actually written to the PDF; None (the
          default) exports every page. Page numbers/scene numbers are
          NOT renumbered for a filtered export - each printed page keeps
          its true position in the full script, matching how a real
          revision-page reprint works.
        - "black_and_white": draws every shot's line/ticks/label in
          black instead of shot.color, and swaps the plain diagonal tick
          for shot.tick_shape (see TICK_SHAPES/_draw_tick_mark) so shots
          stay distinguishable without color.
        - "header_text": stamped at the top of every exported page (e.g.
          "Goldenrod - 7/28/26") - purely a label, this never tints the
          page background (that's what actually printing on colored
          paper stock is for).
        - "back_notes": after every script page, adds a page listing the
          shots drawn on it (type/lens/movement, description, notes) -
          meant for double-sided printing, so each page's shot notes sit
          on the back of that page. Always at least one notes page per
          script page, padded to an odd count when a page's notes spill
          onto several, so the NEXT script page still lands on a front."""
        export_settings = export_settings or {}
        scene_filter = export_settings.get("scene_filter")
        black_and_white = bool(export_settings.get("black_and_white"))
        header_text = export_settings.get("header_text") or None
        back_notes = bool(export_settings.get("back_notes"))

        self._recompute_lanes()

        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        printer.setPageSize(QPageSize(QPageSize.Letter))
        printer.setFullPage(True)

        painter = QPainter(printer)
        painter.setRenderHint(QPainter.Antialiasing)

        page_width_px = printer.width()
        px_per_inch = page_width_px / 8.5
        scale = px_per_inch / PX_PER_INCH

        # Deliberately NOT derived from self.script_view.font(): that font's
        # point size tracks whatever on-screen zoom PageAspectContainer last
        # fitted to (see _apply_interactive_scale), which is almost never
        # exactly 1.0. Export must always start from the fixed canonical
        # size regardless of the current interactive zoom level.
        base_font = QFont(BASE_FONT_FAMILY)
        base_font.setPointSizeF(BASE_FONT_POINT_SIZE)
        # scaled_font (point size multiplied by `scale`) is ONLY correct
        # for QTextDocument's own rendering (doc.setDefaultFont() below) -
        # QTextDocument does its own internal DPI-aware layout and expects
        # a pre-scaled font. A bare QPainter.setFont()+drawText() call
        # targeting a high-resolution QPrinter does NOT need (and is
        # badly broken by) that same multiplication: Qt already rasterizes
        # an ordinary, unscaled point size correctly for the printer's
        # actual DPI. Confirmed empirically - the same "150pt" scaled
        # font that renders correctly via drawContents() renders roughly
        # 10x too large via direct drawText(). Every direct drawText()
        # call below (shot labels, page numbers, the key legend) must use
        # base_font, never scaled_font.
        # Sized by pixels, not points - see _points_for_pixels. base_font
        # above stays in true points: direct painter.drawText onto the
        # printer already resolves those against the printer's own DPI.
        scaled_font = QFont(base_font)
        scaled_font.setPointSizeF(_points_for_pixels(BASE_FONT_PX * scale))
        # A bare QPainter on a QPrinter has no widget to inherit a font
        # from, so without this it would silently fall back to the
        # system default font.
        painter.setFont(base_font)

        # A bare QTextDocument (no QTextEdit/scrollbar involved) is used so
        # that page positioning below is driven purely by explicit document
        # coordinates, never by a widget's scroll state.
        doc = QTextDocument()
        doc.setDefaultFont(scaled_font)
        doc.setDocumentMargin(0)
        frame_fmt = doc.rootFrame().frameFormat()
        frame_fmt.setLeftMargin(round(1.5 * px_per_inch))
        frame_fmt.setRightMargin(round(1.0 * px_per_inch))
        frame_fmt.setTopMargin(round(1.0 * px_per_inch))
        frame_fmt.setBottomMargin(round(1.0 * px_per_inch))
        doc.rootFrame().setFrameFormat(frame_fmt)

        if self.loaded_paragraphs:
            _render_paragraphs_into_document(doc, self.loaded_paragraphs, px_per_inch)
        else:
            QTextCursor(doc).insertText(self.script_view.toPlainText())

        lane_start_x = round(LANE_START_X * scale)
        # The on-screen view's line size (which "Make Lines Smaller" and the
        # lane count both change), not the default - otherwise the PDF
        # would pack lanes differently from what was on screen.
        lane_width = max(MIN_LANE_WIDTH, round(self.script_view.base_lane_width * scale))
        cross_size = max(3, round(CROSS_SIZE * scale))
        line_width = max(1, round(2 * scale))
        left_margin_px = round(1.5 * px_per_inch)
        max_lanes = max(1, (left_margin_px - lane_start_x) // lane_width)
        cross = min(cross_size, max(3, lane_width // 2 - 1))

        page_width_at_dpi = round(8.5 * px_per_inch)
        page_height_at_dpi = round(11.0 * px_per_inch)
        content_height_at_dpi = round(9.0 * px_per_inch)
        top_margin_at_dpi = round(1.0 * px_per_inch)
        bottom_margin_at_dpi = round(1.0 * px_per_inch)
        gap_at_dpi = round(PAGE_GAP_PX * scale)
        total_gap_at_dpi = top_margin_at_dpi + gap_at_dpi + bottom_margin_at_dpi

        doc.setTextWidth(page_width_at_dpi)

        content_starts, more_speakers, split_map = _apply_page_gaps_to_document(
            doc, content_height_at_dpi, total_gap_at_dpi, px_per_inch
        )

        shots_with_lanes = [(s, self.shot_lanes.get(s.shot_id, 0)) for s in self.shots]
        scene_numbers = _compute_scene_numbers(doc, self.loaded_paragraphs)
        # Pre-converted once for the scene/date Selection filter below -
        # see _scene_numbers_on_page's docstring for why this conversion
        # (character offset -> absolute pixel Y) can't be skipped.
        scene_number_ys = [(_absolute_y_for_position(doc, position), number) for position, number in scene_numbers]

        def draw_header_and_watermark(avoid_bottom_right):
            if header_text:
                header_font = QFont(base_font)
                header_font.setBold(True)
                painter.setFont(header_font)
                painter.setPen(QColor("#000000"))
                header_width = painter.fontMetrics().horizontalAdvance(header_text)
                header_x = (page_width_at_dpi - header_width) / 2
                painter.drawText(round(header_x), round(0.5 * px_per_inch), header_text)
                painter.setFont(base_font)

            # "Made with ScriptLiner" watermark: small, light, bold,
            # bottom-right by default - moves to the bottom-left instead
            # whenever the Key legend is ALSO drawn in that same corner
            # on this page, so the two never overlap regardless of where
            # the user has positioned the Key.
            watermark_font = QFont(base_font)
            watermark_font.setPointSizeF(max(6.0, base_font.pointSizeF() * 0.6))
            watermark_font.setBold(True)
            painter.setFont(watermark_font)
            painter.setPen(QColor("#aaaaaa"))
            watermark_text = "Made with ScriptLiner"
            watermark_width = painter.fontMetrics().horizontalAdvance(watermark_text)
            watermark_y = page_height_at_dpi - round(0.35 * px_per_inch)
            if avoid_bottom_right:
                watermark_x = round(0.4 * px_per_inch)
            else:
                watermark_x = page_width_at_dpi - round(0.4 * px_per_inch) - watermark_width
            painter.drawText(round(watermark_x), round(watermark_y), watermark_text)
            painter.setFont(base_font)

        def draw_shot_notes_pages(page_number, page_shots):
            """The back-of-page notes for script page `page_number` (see
            "back_notes"). Returns how many printed pages it used."""
            margin = round(1.0 * px_per_inch)
            text_width = page_width_at_dpi - 2 * margin
            top_y = round(1.4 * px_per_inch)
            bottom_y = page_height_at_dpi - round(0.9 * px_per_inch)
            page_space = bottom_y - top_y

            def start_page():
                printer.newPage()
                title_font = QFont(base_font)
                title_font.setBold(True)
                painter.setFont(title_font)
                painter.setPen(QColor("#000000"))
                painter.drawText(margin, round(1.0 * px_per_inch), f"Page {page_number} \u2014 Shot Notes")
                painter.setFont(base_font)
                draw_header_and_watermark(False)
                return top_y

            def entry_document(shot):
                body_font = QFont(base_font)
                body_font.setPointSizeF(_points_for_pixels(BASE_FONT_PX * 0.85 * scale))
                entry_doc = QTextDocument()
                entry_doc.setDefaultFont(body_font)
                entry_doc.setDocumentMargin(0)
                entry_doc.setTextWidth(text_width)
                entry_doc.setHtml(_shot_notes_html(shot))
                return entry_doc

            used = 1
            y = start_page()
            if not page_shots:
                painter.setPen(QColor("#999999"))
                painter.drawText(margin, y + painter.fontMetrics().ascent(), "No shots on this page.")
                return used

            gap = round(0.18 * px_per_inch)
            for shot in page_shots:
                entry_doc = entry_document(shot)
                height = entry_doc.size().height()
                if height > bottom_y - y and height <= page_space and y > top_y:
                    y = start_page()
                    used += 1
                offset = 0.0
                while True:
                    chunk = min(height - offset, bottom_y - y)
                    painter.save()
                    painter.setClipRect(margin, round(y), text_width, round(chunk) + 1)
                    painter.translate(margin, y - offset)
                    _draw_document_in_ink(painter, entry_doc, QRectF(0, offset, text_width, chunk))
                    painter.restore()
                    painter.setFont(base_font)
                    offset += chunk
                    y += chunk
                    if offset >= height - 0.5:
                        break
                    y = start_page()
                    used += 1
                y += gap

            if used % 2 == 0:
                printer.newPage()
                used += 1
            return used

        key_overflowed_columns = False
        any_page_output = False
        for page_index, content_start in enumerate(content_starts):
            page_outer_top = content_start - top_margin_at_dpi

            if page_index + 1 < len(content_starts):
                next_outer_top = content_starts[page_index + 1] - top_margin_at_dpi
                capture_height = next_outer_top - gap_at_dpi - page_outer_top
                page_content_bottom = content_starts[page_index + 1] - total_gap_at_dpi
            else:
                capture_height = page_height_at_dpi
                page_content_bottom = content_start + content_height_at_dpi
            capture_height = max(1, min(round(capture_height), page_height_at_dpi))

            if scene_filter is not None:
                page_scenes = _scene_numbers_on_page(scene_number_ys, content_start, page_content_bottom)
                if not (page_scenes & scene_filter):
                    continue

            if any_page_output:
                printer.newPage()
            any_page_output = True

            painter.save()
            painter.setClipRect(0, 0, page_width_at_dpi, capture_height)
            painter.translate(0, -page_outer_top)

            _draw_document_in_ink(
                painter, doc, QRectF(0, page_outer_top, page_width_at_dpi, capture_height)
            )
            # QTextDocument's own rendering sets the painter's font per
            # text run as it draws and leaves it at whatever the LAST run
            # used (e.g. a heading's bigger/bold style) rather than
            # restoring it - reassert the intended base font before any
            # further custom drawing (shot labels, the Key legend) reads
            # painter.font(). Must be base_font (unscaled), never
            # scaled_font - see the comment above where scaled_font is
            # defined for why direct drawText() breaks with it.
            painter.setFont(base_font)

            if scene_numbers:
                painter.setPen(QColor("#000000"))
                scene_number_rows = []
                for position, number in scene_numbers:
                    y = _absolute_y_for_position(doc, position)
                    if content_start - 0.5 <= y <= page_content_bottom + 0.5:
                        scene_number_rows.append((y, number))
                _draw_scene_numbers(painter, page_width_at_dpi, scene_number_rows, scale=scale)

            # (MORE)/(CONT'D): visual-only, same as the interactive view
            # (see _apply_page_gaps_to_document) - the printed page ends
            # with "(MORE)" and the next one opens with the speaker's
            # name again whenever an already-in-progress speech
            # continues past this break.
            if page_index < len(more_speakers) and more_speakers[page_index]:
                painter.setPen(QColor("#000000"))
                more_label = "(MORE)"
                more_width = painter.fontMetrics().horizontalAdvance(more_label)
                more_x = (page_width_at_dpi - more_width) / 2
                more_y = page_content_bottom + MORE_CONTD_MARGIN_OFFSET_PX * scale
                painter.drawText(round(more_x), round(more_y), more_label)
            if page_index > 0 and more_speakers[page_index - 1]:
                painter.setPen(QColor("#000000"))
                contd_x = round((1.5 + 2.0) * px_per_inch)
                contd_y = content_start - MORE_CONTD_MARGIN_OFFSET_PX * scale
                painter.drawText(round(contd_x), round(contd_y), f"{more_speakers[page_index - 1]} (CONT'D)")

            shots_on_page = []
            for shot, lane in shots_with_lanes:
                segs = []
                for seg_start, seg_end in sorted(shot.all_ranges()):
                    # shot.all_ranges() is "clean" - see SplitMap - and
                    # this export document has its OWN independent
                    # split_map (built above), separate from the
                    # interactive script_view's.
                    top = _absolute_y_for_position(doc, split_map.clean_to_rendered(seg_start))
                    bottom = _absolute_y_for_position(doc, split_map.clean_to_rendered(seg_end))
                    segs.append((top, max(bottom, top + 2)))

                visible_indices = [
                    i for i, (top, bottom) in enumerate(segs)
                    if bottom >= content_start - 0.5 and top <= page_content_bottom + 0.5
                ]
                if not visible_indices:
                    # Nothing of this shot - not even a mid-shot gap between
                    # two segments - actually falls on this page.
                    continue
                shots_on_page.append((shot, max(segs[visible_indices[0]][0], content_start)))

                x = _shot_line_x(shot, lane, lane_start_x, lane_width, max_lanes, left_margin_px)

                shot_ink = QColor("#000000") if black_and_white else QColor(shot.color)
                overlay_alpha = _overlay_alpha(shot)
                if overlay_alpha is not None:
                    shot_ink.setAlpha(overlay_alpha)
                pen = QPen(shot_ink)
                pen.setWidth(line_width)
                pen.setStyle(LINE_STYLE_MAP.get(shot.line_style, Qt.SolidLine))
                painter.setPen(pen)

                mark_height = _break_mark_height(cross)
                n = len(segs)
                first_vis, last_vis = visible_indices[0], visible_indices[-1]

                # Draw every segment (the clip rect hides whatever isn't on
                # this page); only the shot's very first/last segment gets
                # shortened here, to make room for a page-edge break mark.
                for i, (top, seg_bottom) in enumerate(segs):
                    draw_top, draw_bottom = top, seg_bottom
                    if i == 0 and top < content_start - 0.5:
                        draw_top = min(seg_bottom, content_start + mark_height)
                    if i == n - 1 and seg_bottom > page_content_bottom + 0.5:
                        draw_bottom = max(draw_top, page_content_bottom - mark_height)
                    if draw_bottom > draw_top:
                        painter.drawLine(x, draw_top, x, draw_bottom)
                        if shot.line_style == "double":
                            painter.drawLine(x + 4, draw_top, x + 4, draw_bottom)

                # Marks between consecutive segments - an "end" mark and a
                # flipped "start" mark - drawn wherever their respective
                # edge actually falls on this page. This covers both a
                # same-page gap and a gap that itself straddles a page
                # boundary (each edge then lands on a different page).
                extra_label_ys = []  # the shot number above every later section - see label_every_segment
                for (_, bottom_a), (top_b, _) in zip(segs, segs[1:]):
                    fitted_size = _mark_size_for_gap(cross, top_b - bottom_a)
                    if content_start - 0.5 <= bottom_a <= page_content_bottom + 0.5:
                        _draw_break_zigzag(painter, x, bottom_a, fitted_size)
                    if content_start - 0.5 <= top_b <= page_content_bottom + 0.5:
                        _draw_break_zigzag(painter, x, top_b, fitted_size, flip=True)
                        if self.label_every_segment:
                            extra_label_ys.append(top_b - _break_mark_height(fitted_size) - 3)

                # The shot's true start: a normal tick only on the page
                # where it actually lives; a flipped "resume" mark on a
                # page that a segment merely continues into.
                tick_shape = shot.tick_shape if black_and_white else "diagonal"

                first_top = segs[0][0]
                draw_label = False
                if content_start - 0.5 <= first_top <= page_content_bottom + 0.5:
                    _draw_tick_mark(painter, x, first_top, cross, tick_shape)
                    draw_label = True
                elif first_vis == 0 and first_top < content_start - 0.5:
                    _draw_break_zigzag(painter, x, content_start + mark_height, cross, flip=True)

                # The shot's true end: a normal tick only on the page where
                # it actually lives; a normal "continues" mark on a page
                # it runs past the edge of.
                last_bottom = segs[-1][1]
                if content_start - 0.5 <= last_bottom <= page_content_bottom + 0.5:
                    _draw_tick_mark(painter, x, last_bottom, cross, tick_shape)
                elif last_vis == n - 1 and last_bottom > page_content_bottom + 0.5:
                    _draw_break_zigzag(painter, x, page_content_bottom - mark_height, cross)

                # The label goes last so that changing the pen color for it
                # (which also resets the pen width to the default cosmetic
                # hairline) can't thin out a tick or break mark drawn after.
                if draw_label or extra_label_ys:
                    painter.setPen(shot_ink)
                    metrics = painter.fontMetrics()
                    text_width = metrics.horizontalAdvance(shot.shot_id)
                    label_x = x - (text_width // 2)
                    if draw_label:
                        label_y = first_top - cross - 4
                        painter.drawText(label_x, label_y, shot.shot_id)
                        note = (shot.page_note or "").strip()
                        if note:
                            _draw_page_note(
                                painter, note, 3 * scale, label_y - metrics.ascent() - 2,
                                left_margin_px - 6 * scale, shot_ink, QColor(255, 255, 255, 215),
                            )
                    for label_y in extra_label_ys:
                        painter.drawText(label_x, round(label_y), shot.shot_id)

            key_on_this_page = self.key_enabled and (page_index == 0 or self.key_placement == "every_page")
            if key_on_this_page:
                key_rows = _compute_key_rows(self.shots, self.shot_type_style_map, self.multi_style_mode)
                # Anchored within a fixed offset of the page's top or
                # bottom edge - see _draw_key_legend. page_bottom_y must
                # be THIS page's actual bottom (page_content_bottom +
                # its own bottom margin), not a full nominal page height
                # - most pages don't fill the whole content_height_at_dpi,
                # so their painter clip rect (capture_height, computed
                # above) ends before a full page's worth; anchoring past
                # that would silently clip a bottom-anchored box out of
                # the printed page entirely.
                page_top = content_start - top_margin_at_dpi
                if _draw_key_legend(
                    painter, page_width_at_dpi, page_top,
                    key_rows, self.line_style_labels, scale=scale,
                    position=self.key_box_position,
                    page_bottom_y=page_content_bottom + bottom_margin_at_dpi,
                ):
                    key_overflowed_columns = True

            painter.restore()

            if page_index > 0:
                painter.setPen(QColor("#666666"))
                label = f"{page_index + 1}."
                label_width = painter.fontMetrics().horizontalAdvance(label)
                label_x = page_width_at_dpi - round(1.0 * px_per_inch) - label_width
                painter.drawText(round(label_x), round(0.5 * px_per_inch), label)

            draw_header_and_watermark(key_on_this_page and self.key_box_position == "bottom_right")

            if back_notes:
                shots_on_page.sort(key=lambda entry: entry[1])
                draw_shot_notes_pages(page_index + 1, [entry[0] for entry in shots_on_page])

        painter.end()

        if not any_page_output:
            # QPrinter always emits at least one (blank) page once a
            # QPainter has been opened on it, even if nothing was ever
            # drawn - there's no way to ask it for a zero-page PDF. A
            # scene filter matching no page would otherwise silently
            # produce a blank file instead of a clear error.
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
            raise ValueError("No pages match the selected scenes.")

        return key_overflowed_columns

    def update_highlights(self):
        self._recompute_lanes()
        self.script_view.viewport().update()