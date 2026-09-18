from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QTextEdit,
    QComboBox,
    QPushButton,
    QLabel,
)
from PySide6.QtCore import Signal, Qt

from app.shortcut_button import make_shortcut_button


class ShotInspectorPanel(QWidget):
    """The right-hand properties panel for a single shot.

    Emits `field_changed(field_name, value)` whenever the user edits a
    field, `edit_placement_requested` when "Edit Start/End" is clicked,
    and `duplicate_requested` when "Duplicate Shot" is clicked. MainWindow
    listens to these and updates the underlying Shot + script highlighting.
    """

    field_changed = Signal(str, object)
    edit_placement_requested = Signal()
    duplicate_requested = Signal()

    SHOT_TYPES = [
        "", "Wide", "Medium", "Close-Up", "Extreme Close-Up",
        "Two-Shot", "OTS", "POV", "Insert",
    ]
    LINE_STYLES = ["solid", "dashed", "dotted", "dash-dot", "double"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shot = None
        self._loading = False  # guards against feedback loops while populating fields
        self._build_ui()
        self.set_shot(None)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.title_label = QLabel("Select a shot")
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(self.title_label)

        form = QFormLayout()

        self.shot_number_edit = QLineEdit()
        self.shot_number_edit.editingFinished.connect(
            lambda: self._emit_change("shot_id", self.shot_number_edit.text())
        )
        form.addRow("Shot Number", self.shot_number_edit)

        self.camera_letter_edit = QLineEdit()
        self.camera_letter_edit.editingFinished.connect(
            lambda: self._emit_change("camera_letter", self.camera_letter_edit.text())
        )
        form.addRow("Camera Letter", self.camera_letter_edit)

        self.shot_type_combo = QComboBox()
        self.shot_type_combo.addItems(self.SHOT_TYPES)
        self.shot_type_combo.currentTextChanged.connect(
            lambda val: self._emit_change("shot_type", val)
        )
        form.addRow("Shot Type", self.shot_type_combo)

        self.lens_edit = QLineEdit()
        self.lens_edit.editingFinished.connect(
            lambda: self._emit_change("lens", self.lens_edit.text())
        )
        form.addRow("Lens", self.lens_edit)

        self.camera_movement_edit = QLineEdit()
        self.camera_movement_edit.editingFinished.connect(
            lambda: self._emit_change("camera_movement", self.camera_movement_edit.text())
        )
        form.addRow("Camera Movement", self.camera_movement_edit)

        self.line_style_combo = QComboBox()
        for style in self.LINE_STYLES:
            self.line_style_combo.addItem(style, style)
        self.line_style_combo.currentIndexChanged.connect(
            lambda idx: self._emit_change("line_style", self.line_style_combo.itemData(idx))
        )
        form.addRow("Line Style", self.line_style_combo)

        layout.addLayout(form)

        layout.addWidget(QLabel("Description"))
        self.description_edit = QTextEdit()
        self.description_edit.setMaximumHeight(60)
        self.description_edit.textChanged.connect(
            lambda: self._emit_change("description", self.description_edit.toPlainText())
        )
        layout.addWidget(self.description_edit)

        layout.addWidget(QLabel("Notes"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setMaximumHeight(60)
        self.notes_edit.textChanged.connect(
            lambda: self._emit_change("notes", self.notes_edit.toPlainText())
        )
        layout.addWidget(self.notes_edit)

        self.edit_placement_btn = make_shortcut_button("Edit Start/End", "Q")
        self.edit_placement_btn.clicked.connect(self.edit_placement_requested.emit)
        layout.addWidget(self.edit_placement_btn)

        self.duplicate_btn = make_shortcut_button("Duplicate Shot", "D")
        self.duplicate_btn.clicked.connect(self.duplicate_requested.emit)
        layout.addWidget(self.duplicate_btn)

        layout.addStretch()

    def set_line_style_labels(self, labels: dict):
        """Update the displayed text for each Line Style option (from the
        Key feature) while keeping the underlying stored value unchanged.
        Pass an empty dict to restore the generic style names."""
        current_value = self.line_style_combo.currentData()
        self._loading = True
        self.line_style_combo.clear()
        for style in self.LINE_STYLES:
            self.line_style_combo.addItem(labels.get(style, style), style)
        if current_value is not None:
            idx = self.line_style_combo.findData(current_value)
            if idx >= 0:
                self.line_style_combo.setCurrentIndex(idx)
        self._loading = False

    def _emit_change(self, field_name, value):
        print(f'[inspector] _emit_change: {field_name}={value!r} (loading={self._loading}, shot={self._shot.shot_id if self._shot else None})')
        if self._loading or self._shot is None:
            return
        self.field_changed.emit(field_name, value)

    def set_shot(self, shot):
        """Populate the panel from a Shot object, or clear/disable it if None."""
        print(f'[inspector] set_shot called with: {shot.shot_id if shot else None}')
        self._shot = shot
        self._loading = True

        enabled = shot is not None
        for widget in [
            self.shot_number_edit, self.camera_letter_edit, self.shot_type_combo,
            self.lens_edit, self.camera_movement_edit, self.line_style_combo,
            self.description_edit, self.notes_edit, self.edit_placement_btn,
            self.duplicate_btn,
        ]:
            widget.setEnabled(enabled)
            widget.setFocusPolicy(Qt.StrongFocus)

        # Defensive: force these off explicitly in case something upstream
        # ever sets them (they should already default to False/editable).
        self.shot_number_edit.setReadOnly(False)
        self.camera_letter_edit.setReadOnly(False)
        self.lens_edit.setReadOnly(False)
        self.camera_movement_edit.setReadOnly(False)
        self.description_edit.setReadOnly(False)
        self.notes_edit.setReadOnly(False)

        if shot is None:
            self.title_label.setText("Select a shot")
            self.shot_number_edit.clear()
            self.camera_letter_edit.clear()
            self.shot_type_combo.setCurrentIndex(0)
            self.lens_edit.clear()
            self.camera_movement_edit.clear()
            self.line_style_combo.setCurrentIndex(0)
            self.description_edit.clear()
            self.notes_edit.clear()
        else:
            self.title_label.setText(f"Shot {shot.shot_id}")
            self.shot_number_edit.setText(shot.shot_id)
            self.camera_letter_edit.setText(shot.camera_letter)
            idx = self.shot_type_combo.findText(shot.shot_type)
            self.shot_type_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.lens_edit.setText(shot.lens)
            self.camera_movement_edit.setText(shot.camera_movement)
            idx = self.line_style_combo.findData(shot.line_style)
            self.line_style_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.description_edit.setPlainText(shot.description)
            self.notes_edit.setPlainText(shot.notes)

        self._loading = False