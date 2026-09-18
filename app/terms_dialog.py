from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTextBrowser,
    QCheckBox,
    QPushButton,
)


class TermsDialog(QDialog):
    """Gates first use of the beta on reading and accepting
    BETA_TERMS_PATH (see main.py) - modal, and "I Agree" stays disabled
    until the checkbox is checked. Declining (or closing the dialog,
    which QDialog already treats as Cancel) leaves accepted_terms False
    so the caller can quit instead of launching the main window."""

    def __init__(self, terms_path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ScriptLiner Beta — Terms & Conditions")
        self.resize(560, 640)
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        self.accepted_terms = False

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        try:
            text = terms_path.read_text(encoding="utf-8")
        except OSError:
            text = "# ScriptLiner Beta Terms & Conditions\n\n(Could not load the terms text.)"
        browser.setMarkdown(text)

        self.agree_check = QCheckBox("I have read and agree to these Terms and Conditions.")
        self.agree_check.toggled.connect(self._on_toggled)

        self.agree_btn = QPushButton("I Agree")
        self.agree_btn.setObjectName("primaryButton")
        self.agree_btn.setEnabled(False)
        self.agree_btn.clicked.connect(self._on_agree)

        decline_btn = QPushButton("Decline && Quit")
        decline_btn.clicked.connect(self.reject)

        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(decline_btn)
        button_row.addWidget(self.agree_btn)

        layout = QVBoxLayout()
        layout.addWidget(browser)
        layout.addWidget(self.agree_check)
        layout.addLayout(button_row)
        self.setLayout(layout)

    def _on_toggled(self, checked):
        self.agree_btn.setEnabled(checked)

    def _on_agree(self):
        self.accepted_terms = True
        self.accept()
