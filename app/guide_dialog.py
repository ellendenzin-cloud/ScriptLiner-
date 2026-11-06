from PySide6.QtGui import QFont, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from app.window import (
    BASE_FONT_FAMILY,
    PAGE_DARK_BG,
    PAGE_DARK_INK,
    PX_PER_INCH,
    _render_paragraphs_into_document,
)


class GuideViewerDialog(QDialog):
    """Read-only reading copy of the guide that opens on first launch
    (Help > View Guide), so it can be consulted at any time WITHOUT
    swapping it in for the project that's open. "Open as Practice
    Script" is for the other case - line shots on the guide itself - and
    leaves the actual replacing to the caller, since that has to confirm
    before discarding unsaved work (see MainWindow.show_guide)."""

    def __init__(self, paragraphs, dark_page=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ScriptLiner Guide")
        self.resize(720, 760)
        self.practice_requested = False

        browser = QTextBrowser()
        browser.setFont(QFont(BASE_FONT_FAMILY, 11))
        background, ink = (PAGE_DARK_BG, PAGE_DARK_INK) if dark_page else ("#ffffff", "#000000")
        browser.setStyleSheet(f"background-color: {background}; color: {ink};")
        document: QTextDocument = browser.document()
        document.setDocumentMargin(28)
        _render_paragraphs_into_document(document, paragraphs, PX_PER_INCH)

        practice_btn = QPushButton("Open as Practice Script")
        practice_btn.setObjectName("primaryButton")
        practice_btn.setToolTip(
            "Load the guide into the main window so you can line shots on it. "
            "This replaces the current script."
        )
        practice_btn.clicked.connect(self._on_practice)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(practice_btn)
        buttons.addWidget(close_btn)

        layout = QVBoxLayout()
        layout.addWidget(browser)
        layout.addLayout(buttons)
        self.setLayout(layout)

    def _on_practice(self):
        self.practice_requested = True
        self.accept()
