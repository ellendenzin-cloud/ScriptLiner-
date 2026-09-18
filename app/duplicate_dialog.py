from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QGroupBox,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
)


class DuplicateShotDialog(QDialog):
    """Lets the user pick which existing shot to duplicate and which of
    its inspector fields carry over to the copy - Shot Number is never
    copyable (it's always auto-assigned from wherever the copy ends up
    getting placed), and Camera Letter defaults off since it (and the
    color tied to it) is normally a fresh per-shot assignment too."""

    # (field name on Shot, display label, default checked)
    FIELDS = [
        ("camera_letter", "Camera Letter", False),
        ("shot_type", "Shot Type", True),
        ("lens", "Lens", True),
        ("camera_movement", "Camera Movement", True),
        ("line_style", "Line Style", True),
        ("description", "Description", True),
        ("notes", "Notes", True),
    ]

    def __init__(self, shots, preselected_shot=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Duplicate Shot")
        self._shots = shots

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.shot_combo = QComboBox()
        for shot in shots:
            self.shot_combo.addItem(shot.shot_id, shot.shot_id)
        if preselected_shot is not None:
            idx = self.shot_combo.findData(preselected_shot.shot_id)
            if idx >= 0:
                self.shot_combo.setCurrentIndex(idx)
        form.addRow("Shot to duplicate", self.shot_combo)
        layout.addLayout(form)

        group = QGroupBox("Copy these fields to the new shot")
        group_layout = QVBoxLayout()
        self._checkboxes = {}
        for field_name, label, default_checked in self.FIELDS:
            checkbox = QCheckBox(label)
            checkbox.setChecked(default_checked)
            self._checkboxes[field_name] = checkbox
            group_layout.addWidget(checkbox)
        group.setLayout(group_layout)
        layout.addWidget(group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_shot(self):
        shot_id = self.shot_combo.currentData()
        for shot in self._shots:
            if shot.shot_id == shot_id:
                return shot
        return None

    def selected_fields(self) -> set:
        return {name for name, checkbox in self._checkboxes.items() if checkbox.isChecked()}
