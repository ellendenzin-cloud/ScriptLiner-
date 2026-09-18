from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QRadioButton,
    QButtonGroup,
    QLineEdit,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QLabel,
    QDialogButtonBox,
    QMessageBox,
)
from PySide6.QtCore import QDate

from app.window import _parse_scene_selector

# The traditional Hollywood screenplay revision-page color order (White
# is the unrevised original, so it's excluded here - this dropdown is
# specifically for a REVISED batch of pages).
REVISION_COLORS = ["Blue", "Pink", "Yellow", "Green", "Goldenrod", "Buff", "Salmon", "Cherry"]


class ExportSettingsDialog(QDialog):
    """"Create PDF" dialog, modeled on Fade In's own export dialog:
    Selection (all pages, or a Fade In-style "Scenes:" field) on the
    left, Options on the right. The header text/color/date here is
    purely a text label stamped on each exported page - it never tints
    the page background; the actual color comes from printing the
    result on real colored paper stock, same as any real production
    revision-page workflow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create PDF")
        self._result_settings = None
        self._header_text_is_auto = True

        self.all_pages_radio = QRadioButton("All pages")
        self.all_pages_radio.setChecked(True)
        self.scenes_radio = QRadioButton("Scenes:")
        self.scenes_field = QLineEdit()
        self.scenes_field.setPlaceholderText("e.g. 12, 14, 15-18")
        self.scenes_field.setEnabled(False)
        self.scenes_radio.toggled.connect(self.scenes_field.setEnabled)

        selection_group = QButtonGroup(self)
        selection_group.addButton(self.all_pages_radio)
        selection_group.addButton(self.scenes_radio)

        scenes_row = QHBoxLayout()
        scenes_row.addWidget(self.scenes_radio)
        scenes_row.addWidget(self.scenes_field)

        selection_layout = QVBoxLayout()
        selection_layout.addWidget(self.all_pages_radio)
        selection_layout.addLayout(scenes_row)
        selection_layout.addStretch()
        selection_box = QGroupBox("Selection")
        selection_box.setLayout(selection_layout)

        self.black_and_white_check = QCheckBox("Black and white")
        self.black_and_white_check.setToolTip(
            "Shots are told apart by tick-mark shape instead of color."
        )

        self.header_check = QCheckBox("Add a page header")
        self.color_combo = QComboBox()
        self.color_combo.addItems(REVISION_COLORS)
        self.color_combo.setCurrentText("Goldenrod")
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("M/d/yy")
        self.header_text_field = QLineEdit()
        self.header_text_field.setPlaceholderText("Goldenrod – 7/28/26")
        # Auto-fills from color+date until the user actually types their
        # own text - textEdited (user keystrokes only, unlike textChanged/
        # setText) is what flips it out of "auto" mode.
        self.header_text_field.textEdited.connect(self._on_header_text_edited)
        self.color_combo.currentTextChanged.connect(self._refresh_auto_header_text)
        self.date_edit.dateChanged.connect(self._refresh_auto_header_text)
        self.header_check.toggled.connect(self._on_header_toggled)

        header_fields_row = QHBoxLayout()
        header_fields_row.addWidget(QLabel("Revision color:"))
        header_fields_row.addWidget(self.color_combo)
        header_fields_row.addWidget(QLabel("Date:"))
        header_fields_row.addWidget(self.date_edit)

        options_layout = QVBoxLayout()
        options_layout.addWidget(self.black_and_white_check)
        options_layout.addSpacing(10)
        options_layout.addWidget(self.header_check)
        options_layout.addLayout(header_fields_row)
        options_layout.addWidget(self.header_text_field)
        options_layout.addStretch()
        options_box = QGroupBox("Options")
        options_box.setLayout(options_layout)

        self._on_header_toggled(False)
        self._refresh_auto_header_text()

        columns = QHBoxLayout()
        columns.addWidget(selection_box)
        columns.addWidget(options_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        main_layout = QVBoxLayout()
        main_layout.addLayout(columns)
        main_layout.addWidget(buttons)
        self.setLayout(main_layout)

    def _on_header_toggled(self, checked):
        self.color_combo.setEnabled(checked)
        self.date_edit.setEnabled(checked)
        self.header_text_field.setEnabled(checked)

    def _on_header_text_edited(self, _text):
        self._header_text_is_auto = False

    def _refresh_auto_header_text(self, *_args):
        if not self._header_text_is_auto:
            return
        date_str = self.date_edit.date().toString("M/d/yy")
        self.header_text_field.setText(f"{self.color_combo.currentText()} – {date_str}")

    def _on_accept(self):
        if self.scenes_radio.isChecked():
            scene_filter = _parse_scene_selector(self.scenes_field.text())
            if not scene_filter:
                QMessageBox.warning(
                    self, "No scenes entered",
                    'Enter at least one scene number, e.g. "12, 14, 15-18".',
                )
                return
        else:
            scene_filter = None

        header_text = self.header_text_field.text().strip() if self.header_check.isChecked() else None
        if self.header_check.isChecked() and not header_text:
            QMessageBox.warning(self, "Empty header", "Enter the page header text, or turn off \"Add a page header\".")
            return

        self._result_settings = {
            "scene_filter": scene_filter,
            "black_and_white": self.black_and_white_check.isChecked(),
            "header_text": header_text,
        }
        self.accept()

    def result_settings(self):
        return self._result_settings
