from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QCheckBox,
    QRadioButton,
    QButtonGroup,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QDialogButtonBox,
    QColorDialog,
    QScrollArea,
    QWidget,
    QMessageBox,
    QKeySequenceEdit,
    QSpinBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QKeySequence, QGuiApplication

from app.inspector_panel import ShotInspectorPanel
from app.window import (
    _compute_settings_key_rows,
    _group_settings_key_matches,
    KEY_BOX_POSITIONS,
    KEY_BOX_POSITION_DEFAULT,
    SHORTCUT_ACTIONS,
    DEFAULT_LANE_COUNT,
    MAX_LANE_COUNT,
    MAX_PALETTE_COLORS,
    DEFAULT_OVERLAY_OPACITY,
    SHOT_COLORS,
)


class KeyBoxPositionPicker(QWidget):
    """A tiny page outline with 5 clickable squares at the spots the Key
    box can be placed - clicking one picks that position, instead of
    choosing a name from a dropdown."""

    _PAGE_W = 70
    _PAGE_H = 90
    _MARGIN = 12
    _BTN = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        total_w = self._PAGE_W + self._MARGIN * 2
        total_h = self._PAGE_H + self._MARGIN * 2
        self.setFixedSize(total_w, total_h)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        self._buttons = {}

        edge_pad = 3
        spots = {
            "top_left": (edge_pad, edge_pad),
            "top_center": ((self._PAGE_W - self._BTN) / 2, edge_pad),
            "bottom_left": (edge_pad, self._PAGE_H - self._BTN - edge_pad),
            "bottom_center": ((self._PAGE_W - self._BTN) / 2, self._PAGE_H - self._BTN - edge_pad),
            "bottom_right": (self._PAGE_W - self._BTN - edge_pad, self._PAGE_H - self._BTN - edge_pad),
        }
        for key, (x, y) in spots.items():
            btn = QPushButton(self)
            btn.setCheckable(True)
            btn.setToolTip(KEY_BOX_POSITIONS[key])
            btn.setGeometry(round(self._MARGIN + x), round(self._MARGIN + y), self._BTN, self._BTN)
            btn.setStyleSheet(
                "QPushButton { background: #e8e8e8; border: 1px solid #999; }"
                "QPushButton:checked { background: #4d90fe; border: 1px solid #2b6fd6; }"
            )
            self._button_group.addButton(btn)
            self._buttons[key] = btn

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#888888"))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(self._MARGIN, self._MARGIN, self._PAGE_W, self._PAGE_H)
        painter.end()
        super().paintEvent(event)

    def set_position(self, value):
        btn = self._buttons.get(value)
        if btn is not None:
            btn.setChecked(True)

    def current_position(self):
        for key, btn in self._buttons.items():
            if btn.isChecked():
                return key
        return KEY_BOX_POSITION_DEFAULT

MAX_COLORS = MAX_PALETTE_COLORS


class SettingsDialog(QDialog):
    """Modal dialog for all app-wide display/behavior settings. Reads
    initial values from `window` and only writes them back (via
    apply_to_window()) if the user accepts - Cancel discards everything."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.setWindowTitle("Settings")
        # A flat 480x620 was sized for a much shorter settings form -
        # long since outgrown (Lanes, Appearance, Keyboard Shortcuts...
        # were added later), forcing a manual resize almost every time
        # just to read a row's label without truncation or scroll the
        # long way down. Size off the actual screen instead, same as
        # the other big dialogs (Update Script, Merge Projects).
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(760, avail.width() - 40)
            target_h = max(600, avail.height() - 80)
        else:
            target_w, target_h = 760, 800
        self.resize(target_w, target_h)

        self._colors = list(window.colors)[:MAX_COLORS] or ["#ff4d4d"]

        outer_layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

        # --- Shot Type -> Line Style ---
        style_group = QGroupBox("Shot Type Affects Line Style")
        style_layout = QVBoxLayout(style_group)
        self.affects_style_check = QCheckBox(
            "Picking a Shot Type auto-sets the Line Style below\n"
            "(you can still override it afterward in the Line Style dropdown)"
        )
        self.affects_style_check.setChecked(window.shot_type_affects_style)
        style_layout.addWidget(self.affects_style_check)

        mapping_form = QFormLayout()
        self._type_combos = {}
        for shot_type in ShotInspectorPanel.SHOT_TYPES:
            if not shot_type:
                continue
            combo = QComboBox()
            for style in ShotInspectorPanel.LINE_STYLES:
                combo.addItem(style, style)
            current = window.shot_type_style_map.get(shot_type, "solid")
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.currentIndexChanged.connect(self._rebuild_key_rows)
            self._type_combos[shot_type] = combo
            mapping_form.addRow(shot_type, combo)
        style_layout.addLayout(mapping_form)
        layout.addWidget(style_group)

        # --- Key / Legend ---
        # The key's rows are always kept in sync with the Shot Type
        # mapping above (edit a mapping here and a "[style] means:" row
        # appears/disappears/updates below immediately) - "Enable Key"
        # only toggles whether the legend actually gets printed/drawn,
        # not whether the rows themselves exist. A row only shows up if
        # that style is mapped to a shot type in Settings above, or is
        # actually used by a shot already on the page. Leaving a row's
        # field blank means "use the shot type's name"; typing something
        # overrides it.
        key_group = QGroupBox("Key (Line Style Legend)")
        key_layout = QVBoxLayout(key_group)
        self.key_enabled_check = QCheckBox("Show the key legend on the page(s)")
        self.key_enabled_check.setChecked(window.key_enabled)
        key_layout.addWidget(self.key_enabled_check)

        placement_row = QHBoxLayout()
        self.placement_group = QButtonGroup(self)
        self.first_page_radio = QRadioButton("First page only")
        self.every_page_radio = QRadioButton("Every page")
        self.placement_group.addButton(self.first_page_radio)
        self.placement_group.addButton(self.every_page_radio)
        if window.key_placement == "every_page":
            self.every_page_radio.setChecked(True)
        else:
            self.first_page_radio.setChecked(True)
        placement_row.addWidget(self.first_page_radio)
        placement_row.addWidget(self.every_page_radio)
        placement_row.addStretch()
        key_layout.addLayout(placement_row)

        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("Box position:"))
        self.box_position_picker = KeyBoxPositionPicker()
        current_position = getattr(window, "key_box_position", KEY_BOX_POSITION_DEFAULT)
        self.box_position_picker.set_position(current_position)
        position_row.addWidget(self.box_position_picker)
        position_row.addStretch()
        key_layout.addLayout(position_row)

        self._multi_style_mode = dict(getattr(window, "multi_style_mode", {}))
        self._pending_label_text = dict(window.line_style_labels)

        self.label_form = QFormLayout()
        self._label_edits = {}
        key_layout.addLayout(self.label_form)
        layout.addWidget(key_group)

        self._rebuild_key_rows()

        # --- Display Style ---
        display_group = QGroupBox("Page Break Display")
        display_layout = QHBoxLayout(display_group)
        self.display_group_buttons = QButtonGroup(self)
        self.gap_radio = QRadioButton("Gap between pages")
        self.dotted_radio = QRadioButton("Dotted line (pages flow closer together)")
        self.display_group_buttons.addButton(self.gap_radio)
        self.display_group_buttons.addButton(self.dotted_radio)
        if window.page_gap_style == "dotted":
            self.dotted_radio.setChecked(True)
        else:
            self.gap_radio.setChecked(True)
        display_layout.addWidget(self.gap_radio)
        display_layout.addWidget(self.dotted_radio)
        layout.addWidget(display_group)

        # --- Appearance ---
        appearance_group = QGroupBox("Appearance")
        appearance_layout = QVBoxLayout(appearance_group)
        self.theme_group_buttons = QButtonGroup(self)
        self.theme_light_radio = QRadioButton("Light")
        self.theme_dark_ui_radio = QRadioButton("Dark Mode - keep the page light")
        self.theme_dark_full_radio = QRadioButton("Dark Mode - invert the page too")
        for btn in (self.theme_light_radio, self.theme_dark_ui_radio, self.theme_dark_full_radio):
            self.theme_group_buttons.addButton(btn)
            appearance_layout.addWidget(btn)
        current_theme = getattr(window, "theme", "light")
        if current_theme == "dark_full":
            self.theme_dark_full_radio.setChecked(True)
        elif current_theme == "dark_ui":
            self.theme_dark_ui_radio.setChecked(True)
        else:
            self.theme_light_radio.setChecked(True)
        appearance_note = QLabel(
            "Exported PDFs always print light/black-on-white, no matter which "
            "theme is active here."
        )
        appearance_note.setWordWrap(True)
        appearance_layout.addWidget(appearance_note)
        layout.addWidget(appearance_group)

        # --- Lanes ---
        lanes_group = QGroupBox("Lanes")
        lanes_layout = QVBoxLayout(lanes_group)

        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Number of lanes:"))
        self.lane_count_spin = QSpinBox()
        self.lane_count_spin.setRange(1, MAX_LANE_COUNT)
        self.lane_count_spin.setValue(getattr(window, "lane_count", DEFAULT_LANE_COUNT))
        self.lane_count_spin.valueChanged.connect(self._rebuild_lane_checks)
        count_row.addWidget(self.lane_count_spin)
        count_note = QLabel(
            f"({DEFAULT_LANE_COUNT} is the normal amount, up to {MAX_LANE_COUNT} - "
            "each extra lane makes every line narrower so they all fit)"
        )
        count_note.setWordWrap(True)
        count_row.addWidget(count_note, 1)
        lanes_layout.addLayout(count_row)

        lanes_layout.addWidget(QLabel("Active lanes:"))
        self._active_lanes_row = QHBoxLayout()
        self._lane_checks = []
        self._initial_active = set(getattr(window, "active_lanes", set(range(DEFAULT_LANE_COUNT))))
        self._rebuild_lane_checks(self.lane_count_spin.value())
        lanes_layout.addLayout(self._active_lanes_row)

        self.auto_sort_check = QCheckBox(
            "Shots in Alphabetical Order\n"
            "(overlapping shots auto-sort left to right across the active "
            "lanes by scene/camera letter - dragging a shot to a specific "
            "lane no longer has any effect while this is on)"
        )
        self.auto_sort_check.setChecked(getattr(window, "auto_sort_shots", False))
        lanes_layout.addWidget(self.auto_sort_check)

        self.length_sort_check = QCheckBox(
            "Shots by Length\n"
            "(overlapping shots nest by how much of the page they span - the "
            "longest shot goes in the outermost lane on the left, the shortest "
            "innermost, closest to the text)"
        )
        self.length_sort_check.setChecked(getattr(window, "length_sort_shots", False))
        lanes_layout.addWidget(self.length_sort_check)
        # Both are automatic orders that take over lane placement, so only
        # one can be on at a time.
        self.auto_sort_check.toggled.connect(
            lambda on: self.length_sort_check.setChecked(False) if on else None
        )
        self.length_sort_check.toggled.connect(
            lambda on: self.auto_sort_check.setChecked(False) if on else None
        )
        layout.addWidget(lanes_group)

        # --- Shot Lines ---
        lines_group = QGroupBox("Shot Lines")
        lines_layout = QVBoxLayout(lines_group)
        self.label_every_segment_check = QCheckBox(
            "Show the shot number above every section of a split shot\n"
            "(otherwise it's only printed above the first section)"
        )
        self.label_every_segment_check.setChecked(getattr(window, "label_every_segment", True))
        lines_layout.addWidget(self.label_every_segment_check)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("Opacity for new lines drawn over the text:"))
        self.overlay_opacity_spin = QSpinBox()
        self.overlay_opacity_spin.setRange(10, 100)
        self.overlay_opacity_spin.setSingleStep(5)
        self.overlay_opacity_spin.setSuffix(" %")
        self.overlay_opacity_spin.setValue(getattr(window, "overlay_default_opacity", DEFAULT_OVERLAY_OPACITY))
        opacity_row.addWidget(self.overlay_opacity_spin)
        opacity_row.addStretch()
        lines_layout.addLayout(opacity_row)
        opacity_note = QLabel(
            "Each shot can also be set to draw over the text in the Inspector, "
            "with its own opacity."
        )
        opacity_note.setWordWrap(True)
        lines_layout.addWidget(opacity_note)
        layout.addWidget(lines_group)

        # --- Colors ---
        color_group = QGroupBox("Shot Colors")
        color_outer = QVBoxLayout(color_group)

        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Number of colors:"))
        self.color_count_spin = QSpinBox()
        self.color_count_spin.setRange(1, MAX_COLORS)
        self.color_count_spin.setValue(len(self._colors))
        self.color_count_spin.valueChanged.connect(self._on_color_count_changed)
        count_row.addWidget(self.color_count_spin)
        count_row.addStretch()
        color_outer.addLayout(count_row)

        self._color_grid = QGridLayout()
        self._color_buttons = []
        color_outer.addLayout(self._color_grid)
        self._rebuild_color_buttons()

        self.unique_colors_check = QCheckBox(
            "Give every new shot its own color\n"
            "(uses 30 built-in colors picked to be very different from each other and "
            "to show up on a white page - after all 30 are in use, the least-used one repeats)"
        )
        self.unique_colors_check.setChecked(getattr(window, "unique_shot_colors", False))
        color_outer.addWidget(self.unique_colors_check)
        layout.addWidget(color_group)

        # --- Keyboard Shortcuts ---
        shortcuts_group = QGroupBox("Keyboard Shortcuts")
        shortcuts_group_layout = QVBoxLayout(shortcuts_group)
        shortcuts_form = QFormLayout()
        self._shortcut_edits = {}
        for action_id, label, _default in SHORTCUT_ACTIONS:
            edit = QKeySequenceEdit(window._load_shortcut(action_id))
            # A single key sequence is all any of these need - a second
            # (or third) recorded chord would just sit there unused and
            # be confusing to look at.
            edit.setMaximumSequenceLength(1)
            self._shortcut_edits[action_id] = edit
            shortcuts_form.addRow(label, edit)
        shortcuts_group_layout.addLayout(shortcuts_form)

        reset_row = QHBoxLayout()
        reset_row.addStretch()
        self.reset_shortcuts_btn = QPushButton("Reset All to Defaults")
        self.reset_shortcuts_btn.clicked.connect(self._reset_shortcuts_to_defaults)
        reset_row.addWidget(self.reset_shortcuts_btn)
        shortcuts_group_layout.addLayout(reset_row)
        layout.addWidget(shortcuts_group)

        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept_clicked)
        buttons.rejected.connect(self.reject)
        outer_layout.addWidget(buttons)

    def _rebuild_lane_checks(self, count):
        """One "active" checkbox per lane, keeping what's already ticked;
        a lane that's newly added starts active."""
        previous = {i: cb.isChecked() for i, cb in enumerate(self._lane_checks)}
        while self._active_lanes_row.count():
            item = self._active_lanes_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._lane_checks = []
        for i in range(count):
            cb = QCheckBox(str(i + 1))
            cb.setChecked(previous.get(i, i in self._initial_active or i >= DEFAULT_LANE_COUNT))
            self._lane_checks.append(cb)
            self._active_lanes_row.addWidget(cb)
        self._active_lanes_row.addStretch()

    def _reset_shortcuts_to_defaults(self):
        for action_id, _label, default in SHORTCUT_ACTIONS:
            self._shortcut_edits[action_id].setKeySequence(QKeySequence(default))

    def _on_accept_clicked(self):
        if not any(cb.isChecked() for cb in self._lane_checks):
            QMessageBox.warning(
                self, "No Active Lanes",
                "At least one lane must stay active - shots need somewhere to go.",
            )
            return
        conflicts = self._find_shortcut_conflicts()
        if conflicts:
            lines = "\n".join(f"  • {seq} → {', '.join(labels)}" for seq, labels in conflicts)
            QMessageBox.warning(
                self, "Duplicate Shortcuts",
                "The same key combination is assigned to more than one action:\n\n"
                f"{lines}\n\nGive each a different shortcut before saving.",
            )
            return
        self.accept()

    def _find_shortcut_conflicts(self):
        """[(key_sequence_str, [action_label, ...]), ...] for every key
        sequence assigned to more than one action - empty means no
        conflicts. Blank (unassigned) sequences don't count as a clash."""
        by_sequence = {}
        labels_by_id = {aid: label for aid, label, _default in SHORTCUT_ACTIONS}
        for action_id, edit in self._shortcut_edits.items():
            seq = edit.keySequence().toString()
            if not seq:
                continue
            by_sequence.setdefault(seq, []).append(labels_by_id[action_id])
        return [(seq, labels) for seq, labels in by_sequence.items() if len(labels) > 1]

    def _current_shot_type_style_map(self):
        return {
            shot_type: combo.currentData()
            for shot_type, combo in self._type_combos.items()
        }

    def _rebuild_key_rows(self, *_args):
        """Recomputes which "[style] means:" rows should exist from the
        CURRENT (possibly unsaved) Shot Type combo selections, and
        rebuilds the form to match - a row appears once per shot type
        mapped to a style here, so a style applied to two shot types gets
        two separate rows (unless that style is in "merge" mode, in which
        case it's one combined row instead). This is mapping-driven, not
        shot-driven, so the rows and the Merge/Overwrite controls are
        visible immediately while configuring the mapping, even before
        any shot uses it - a shot only adds an extra row if its own Line
        Style isn't already explained by the mapping. Typed text is kept
        in self._pending_label_text (not just read off the current
        widgets), so toggling Merge/Overwrite back and forth - which
        destroys and recreates the QLineEdits - never loses what the user
        already typed into a row, even while that row is temporarily
        hidden."""
        while self.label_form.rowCount():
            self.label_form.removeRow(0)
        self._label_edits = {}

        mapping = self._current_shot_type_style_map()
        _order, groups = _group_settings_key_matches(mapping, self.window.shots)
        multi_styles = {style for style, types in groups.items() if len([t for t in types if t]) > 1}

        rows = _compute_settings_key_rows(mapping, self.window.shots, self._multi_style_mode)
        shown_controls_for = set()
        for style, row_key, default_label in rows:
            if style in multi_styles and style not in shown_controls_for:
                shown_controls_for.add(style)
                self.label_form.addRow(self._make_multi_style_controls(style))
            if row_key in self._label_edits:
                continue  # e.g. two shots sharing a shot type
            edit = QLineEdit(self._pending_label_text.get(row_key, ""))
            edit.setPlaceholderText(default_label)
            edit.textChanged.connect(lambda text, k=row_key: self._pending_label_text.__setitem__(k, text))
            self._label_edits[row_key] = edit
            self.label_form.addRow(f"{style} means:", edit)

    def _make_multi_style_controls(self, style):
        """Row of mode buttons shown above a style's label field(s) once
        more than one shot type matches it - toggles between one row per
        shot type, each individually editable ("Overwrite Multiples",
        the default), and one combined row with an auto-joined label
        ("Merge Multiples")."""
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 4, 0, 0)
        note = QLabel(f'"{style}" matches multiple shot types:')
        row_layout.addWidget(note)

        mode = self._multi_style_mode.get(style, "overwrite")

        merge_btn = QPushButton("Merge Multiples")
        merge_btn.setCheckable(True)
        merge_btn.setChecked(mode == "merge")
        merge_btn.clicked.connect(lambda checked=False, s=style: self._set_multi_style_mode(s, "merge"))
        row_layout.addWidget(merge_btn)

        overwrite_btn = QPushButton("Overwrite Multiples")
        overwrite_btn.setCheckable(True)
        overwrite_btn.setChecked(mode == "overwrite")
        overwrite_btn.clicked.connect(lambda checked=False, s=style: self._set_multi_style_mode(s, "overwrite"))
        row_layout.addWidget(overwrite_btn)

        row_layout.addStretch()
        return row

    def _set_multi_style_mode(self, style, mode):
        self._multi_style_mode[style] = mode
        self._rebuild_key_rows()

    def _offer_to_update_existing_shots(self, old_map, new_map):
        """For each Shot Type whose mapped Line Style just changed, if
        existing shots of that type are still drawn in the old style,
        asks whether to update them to the new one now. Declining leaves
        those shots as they are - only future shots (or a manual edit)
        would pick up the new mapping."""
        for shot_type, new_style in new_map.items():
            old_style = old_map.get(shot_type)
            if old_style is None or old_style == new_style:
                continue
            matching = [
                s for s in self.window.shots
                if s.shot_type == shot_type and s.line_style == old_style
            ]
            if not matching:
                continue
            plural = "s" if len(matching) != 1 else ""
            reply = QMessageBox.question(
                self,
                "Update existing shots?",
                f'{len(matching)} existing "{shot_type}" shot{plural} '
                f'still use{"" if plural else "s"} Line Style "{old_style}".\n\n'
                f'Update {"them" if plural else "it"} to "{new_style}" now?',
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                for shot in matching:
                    shot.line_style = new_style

    def _rebuild_color_buttons(self):
        while self._color_grid.count():
            item = self._color_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._color_buttons = []
        for i, color in enumerate(self._colors):
            btn = QPushButton()
            btn.setFixedSize(56, 32)
            self._set_button_color(btn, color)
            btn.clicked.connect(lambda checked=False, idx=i: self._pick_color(idx))
            self._color_buttons.append(btn)
            self._color_grid.addWidget(btn, i // 6, i % 6)

    def _on_color_count_changed(self, count):
        """Grows or shrinks the palette. New slots start as the next of the
        built-in distinct colors not already in it, ready to be recolored."""
        taken = {c.lower() for c in self._colors}
        for candidate in SHOT_COLORS:
            if len(self._colors) >= count:
                break
            if candidate not in taken:
                taken.add(candidate)
                self._colors.append(candidate)
        del self._colors[count:]
        self._rebuild_color_buttons()

    def _set_button_color(self, btn, hex_color):
        btn.setStyleSheet(f"background-color: {hex_color}; border: 1px solid #888;")

    def _pick_color(self, index):
        current = QColor(self._colors[index])
        color = QColorDialog.getColor(current, self, "Choose Color")
        if color.isValid():
            self._colors[index] = color.name()
            self._set_button_color(self._color_buttons[index], color.name())

    def apply_to_window(self):
        """Call after exec() returns Accepted to write values back onto
        the MainWindow. The caller is responsible for propagating these
        onto the script view / inspector and marking the project dirty."""
        w = self.window
        old_map = dict(w.shot_type_style_map)
        w.shot_type_affects_style = self.affects_style_check.isChecked()
        w.shot_type_style_map = {
            shot_type: combo.currentData()
            for shot_type, combo in self._type_combos.items()
        }
        self._offer_to_update_existing_shots(old_map, w.shot_type_style_map)
        w.key_enabled = self.key_enabled_check.isChecked()
        w.key_placement = "every_page" if self.every_page_radio.isChecked() else "first_page"
        w.key_box_position = self.box_position_picker.current_position()
        # Only fields the user actually typed into become explicit
        # overrides; a blank field means "keep following the Shot Type
        # mapping" rather than freezing in whatever it happened to derive
        # to at save time. Read from _pending_label_text (not the current
        # widgets) so an override typed for a row that's since been
        # hidden by a mode toggle is still saved.
        w.line_style_labels = {
            row_key: text.strip()
            for row_key, text in self._pending_label_text.items()
            if text.strip()
        }
        w.multi_style_mode = dict(self._multi_style_mode)
        w.page_gap_style = "dotted" if self.dotted_radio.isChecked() else "gap"
        if self.theme_dark_full_radio.isChecked():
            w.theme = "dark_full"
        elif self.theme_dark_ui_radio.isChecked():
            w.theme = "dark_ui"
        else:
            w.theme = "light"
        w.lane_count = self.lane_count_spin.value()
        w.active_lanes = {i for i, cb in enumerate(self._lane_checks) if cb.isChecked()}
        w.auto_sort_shots = self.auto_sort_check.isChecked()
        w.length_sort_shots = self.length_sort_check.isChecked()
        w.label_every_segment = self.label_every_segment_check.isChecked()
        w.overlay_default_opacity = self.overlay_opacity_spin.value()
        w.unique_shot_colors = self.unique_colors_check.isChecked()
        w.colors = list(self._colors)

        for action_id, edit in self._shortcut_edits.items():
            w._set_shortcut(action_id, edit.keySequence())
