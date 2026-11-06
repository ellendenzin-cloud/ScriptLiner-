import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.window import MainWindow, LOGO_SQUARE_PATH, LOGO_LONG_PATH, PROGRAM_FILES_DIR, BETA_TERMS_PATH
from app.studio_link import register_app
from app.terms_dialog import TermsDialog
from app.version import APP_VERSION
from app.theme import load_fonts, enable_dpi_awareness


# Must happen before QApplication is constructed - see theme.py docstring
# for why this can't just be left to Qt's own high-DPI scaling.
enable_dpi_awareness()


def _register_with_studio_hub():
    root_dir = Path(__file__).resolve().parent
    kwargs = dict(
        slug="scriptliner",
        display_name="ScriptLiner",
        version=APP_VERSION,
        root_dir=root_dir,
        project_extensions=(".slproj",),
        icon_path=LOGO_SQUARE_PATH if LOGO_SQUARE_PATH.exists() else None,
        logo_path=LOGO_LONG_PATH if LOGO_LONG_PATH.exists() else None,
        program_files_dir=PROGRAM_FILES_DIR,
    )
    if getattr(sys, "frozen", False):
        kwargs["exe_path"] = sys.executable
    else:
        kwargs["python_path"] = sys.executable
        kwargs["main_py_path"] = Path(__file__).resolve()
    register_app(**kwargs)


app = QApplication(sys.argv)
load_fonts()
if LOGO_SQUARE_PATH.exists():
    app.setWindowIcon(QIcon(str(LOGO_SQUARE_PATH)))

_register_with_studio_hub()

if len(sys.argv) > 3 and sys.argv[1] == "--export-pdf":
    # Headless export mode for Script Studio Hub's "Export Production
    # Docs" feature: `main.py --export-pdf <project.slproj> <output_dir>`.
    # No window shown, no event loop entered - this exits on its own.
    # Note: a corrupt/unreadable project file will surface as a blocking
    # QMessageBox from _open_project_path here since that's shared with
    # the interactive path; the Hub's subprocess timeout is the backstop
    # for that edge case rather than a special-cased headless load.
    project_path = Path(sys.argv[2])
    output_dir = Path(sys.argv[3])
    output_dir.mkdir(parents=True, exist_ok=True)
    window = MainWindow()
    if not window._open_project_path(str(project_path)):
        sys.exit(1)
    out_path = output_dir / f"{project_path.stem}.pdf"
    try:
        window._write_lined_pdf(str(out_path))
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

# Gates interactive use (never the headless --export-pdf path above,
# which must run unattended) behind reading and accepting the beta
# Terms & Conditions - shown once, persisted in QSettings.
settings = QSettings("ScriptLiner", "ScriptLiner")
if not settings.value("beta_terms_accepted", False, type=bool):
    terms_dialog = TermsDialog(BETA_TERMS_PATH)
    if terms_dialog.exec() != TermsDialog.DialogCode.Accepted or not terms_dialog.accepted_terms:
        sys.exit(0)
    settings.setValue("beta_terms_accepted", True)

window = MainWindow()
window.show()

if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
    # Launched with a project path (e.g. from Script Studio Hub) - open it
    # in place of whatever _restore_last_session already loaded.
    window._open_project_path(sys.argv[1])

sys.exit(app.exec())