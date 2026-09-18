import copy

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QSplitter,
    QTextEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QWidget,
    QMessageBox,
    QDialogButtonBox,
    QStyle,
)
from PySide6.QtGui import QFont, QColor, QTextCursor, QTextCharFormat, QTextFormat, QGuiApplication
from PySide6.QtCore import Qt

from app.script_diff import remap_shots, line_offsets
from app.shot import Segment
from app.window import (
    ScriptTextEdit,
    BASE_FONT_FAMILY,
    BASE_FONT_POINT_SIZE,
    PAGE_WIDTH_PX,
    PAGE_MARGIN_LEFT_PX,
    PAGE_MARGIN_RIGHT_PX,
    PAGE_MARGIN_TOP_PX,
    PAGE_MARGIN_BOTTOM_PX,
    PX_PER_INCH,
    _compute_scene_numbers,
    _render_paragraphs_into_document,
)


def _shot_span(shot):
    starts = [seg.start_index for seg in shot.segments]
    ends = [seg.end_index for seg in shot.segments]
    return min(starts), max(ends)


def _next_free_letter(scene_used_letters: dict, scene_number: str) -> str:
    """Same allocation rule as MainWindow._next_shot_letter, but against
    a caller-supplied {scene_number: {letters}} dict instead of the live
    window - lets the dialog reserve ids for itself without touching
    MainWindow.scene_used_letters until the merge is actually confirmed."""
    key = str(scene_number)
    used = scene_used_letters.setdefault(key, set())
    index = 1
    while chr(64 + index) in used:
        index += 1
    letter = chr(64 + index)
    used.add(letter)
    return letter


class MergeProjectsDialog(QDialog):
    """Side-by-side review for merging another project's shots into this
    one, reusing Update Script's diff/transfer/placement machinery.

    Unlike Update Script, THIS project's script text never changes - the
    other project's shots are diffed against it, auto-transferred where
    their surrounding text matches, and otherwise left for manual
    placement (same drag-handle preview as Duplicate Shot) before
    "Confirm Merge" is enabled. This project's own existing shots are
    shown for reference but are never moved by the merge.
    """

    VIEW_SCALE = 0.75

    def __init__(self, main_window, other_text: str, other_paragraphs, other_shots,
                 other_shot_lanes, other_project_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge Projects")
        # See UpdateScriptDialog for why this is sized off the actual
        # screen instead of a flat pixel count.
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(1500, avail.width() - 40)
            target_h = max(700, avail.height() - 60)
        else:
            target_w, target_h = 1500, 1250
        self.resize(target_w, target_h)

        self.main_window = main_window
        self.other_project_name = other_project_name

        self.current_shots = main_window.shots
        # clean_script_text(), NOT script_view.toPlainText() directly -
        # the live document can have real "(MORE)"/"CONT'D" text spliced
        # in (see SplitMap), which must never leak into a diff.
        self.current_text = main_window.clean_script_text()
        self.current_paragraphs = main_window.loaded_paragraphs
        self.current_shot_lanes = dict(main_window.shot_lanes)

        self.other_text = other_text
        self.other_paragraphs = other_paragraphs
        self.other_shots = other_shots
        self.other_shot_lanes = other_shot_lanes

        # "old" = the other project (its shots are what may move), "new"
        # = this project's script, which stays fixed as the final canvas -
        # exactly the same roles as UpdateScriptDialog's old/new text.
        (
            self._opcodes,
            self._old_lines,
            self._new_lines,
            self._transferred,
            self._unresolved_ids,
        ) = remap_shots(self.other_text, self.current_text, self.other_shots, _shot_span)
        self._manual_placements = {}  # shot_id -> {"start":, "end":, "lane":}
        self._active_unresolved_shot = None

        self._build_ui()
        self._apply_diff_highlights()
        self._refresh_needs_placement_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            f"Merging shots in from “{self.other_project_name}”. This project's own "
            "script and shots (right) never change. Shots from the other project "
            "whose own text matches carry over automatically. Anything listed under "
            "“Needs Placement” can be positioned by hand - drag its handles into "
            "place, then click “Place This Shot”. You can also confirm with shots "
            "still unplaced; they'll simply be left out of the merge."
        )
        info.setWordWrap(True)
        layout.addWidget(info, 0)

        splitter = QSplitter(Qt.Horizontal)

        other_pane = QWidget()
        other_layout = QVBoxLayout(other_pane)
        other_layout.addWidget(QLabel(f"Other Project: {self.other_project_name}"))
        self.other_view = self._build_readonly_view(
            self.other_text, self.other_paragraphs, self._other_shots_with_lanes
        )
        other_layout.addWidget(self.other_view, 1)
        splitter.addWidget(other_pane)

        current_pane = QWidget()
        current_layout = QVBoxLayout(current_pane)
        current_layout.addWidget(QLabel("Current Project (After Merge)"))
        self.current_view = self._build_readonly_view(
            self.current_text, self.current_paragraphs, self._display_current_and_incoming
        )
        current_layout.addWidget(self.current_view, 1)
        splitter.addWidget(current_pane)

        side_pane = QWidget()
        side_layout = QVBoxLayout(side_pane)
        side_layout.addWidget(QLabel("Needs Placement"))
        self.needs_placement_list = QListWidget()
        self.needs_placement_list.itemClicked.connect(self._on_needs_placement_clicked)
        side_layout.addWidget(self.needs_placement_list)
        self.place_shot_btn = QPushButton("Place This Shot")
        self.place_shot_btn.setEnabled(False)
        self.place_shot_btn.clicked.connect(self._commit_active_placement)
        side_layout.addWidget(self.place_shot_btn)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        side_layout.addWidget(self.status_label)
        side_layout.addStretch()
        splitter.addWidget(side_pane)

        splitter.setSizes([450, 750, 260])
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Confirm Merge")
        buttons.accepted.connect(self._on_confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok_button = buttons.button(QDialogButtonBox.Ok)

    def _build_readonly_view(self, text, paragraphs, shots_provider):
        """A read-only ScriptTextEdit configured to match the main app's
        page look (margins, scene numbers, shot-line rendering), fixed to
        the scaled page width so it renders as a proper page rather than
        stretching to fill the splitter pane."""
        view = ScriptTextEdit()
        scale = self.VIEW_SCALE
        view.scale = scale
        scaled_font = QFont(BASE_FONT_FAMILY)
        scaled_font.setPointSizeF(BASE_FONT_POINT_SIZE * scale)
        view.setFont(scaled_font)
        view._apply_scale_to_lane_geometry()

        doc = view.document()
        doc.setDocumentMargin(0)

        frame_fmt = doc.rootFrame().frameFormat()
        frame_fmt.setLeftMargin(PAGE_MARGIN_LEFT_PX * scale)
        frame_fmt.setRightMargin(PAGE_MARGIN_RIGHT_PX * scale)
        frame_fmt.setTopMargin(PAGE_MARGIN_TOP_PX * scale)
        frame_fmt.setBottomMargin(PAGE_MARGIN_BOTTOM_PX * scale)

        if paragraphs:
            # Renders with each paragraph's own screenplay-element
            # formatting (margins/alignment/case/rich-text runs), same
            # as the main script view and export - NOT setPlainText(),
            # which would flatten everything to one uniform block with
            # no Character/Dialogue/Transition indentation or styling.
            doc.rootFrame().setFrameFormat(frame_fmt)
            _render_paragraphs_into_document(doc, paragraphs, PX_PER_INCH * scale)
        else:
            # setPlainText() resets the document, which resets the root
            # frame's format back to its default (zero) margins - so for
            # this plain-text fallback (no structured element types to
            # format from) the margins have to be applied AFTER the
            # text is set, not before.
            view.setPlainText(text)
            doc.rootFrame().setFrameFormat(frame_fmt)

        view.apply_page_gaps()
        view.scene_numbers = _compute_scene_numbers(view.document(), paragraphs)
        view.setReadOnly(True)
        view.shots_provider = shots_provider

        scrollbar_extent = view.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        view.setFixedWidth(round(PAGE_WIDTH_PX * scale) + scrollbar_extent)
        return view

    def _other_shots_with_lanes(self):
        """The left pane always shows the other project's shots exactly
        where they were in ITS OWN script, for reference - never affected
        by transfer/placement state, and never renamed (id reassignment
        to avoid collisions only happens in result_shots(), at confirm)."""
        return [(shot, self.other_shot_lanes.get(shot.shot_id, 0)) for shot in self.other_shots]

    # --- diff highlighting -------------------------------------------------

    def _apply_diff_highlights(self):
        old_offsets = line_offsets(self._old_lines)
        new_offsets = line_offsets(self._new_lines)

        # old_offsets/new_offsets are "clean" - other_view/current_view's
        # OWN documents may each have independent real (MORE)/CONT'D
        # splices (see SplitMap) - convert through each view's own
        # split_map before placing a cursor selection.
        other_map = self.other_view.split_map
        current_map = self.current_view.split_map

        other_selections = []
        current_selections = []
        for tag, i1, i2, j1, j2 in self._opcodes:
            if tag in ("replace", "delete"):
                other_selections.append(
                    self._make_selection(
                        self.other_view.document(),
                        other_map.clean_to_rendered(old_offsets[i1]),
                        other_map.clean_to_rendered(old_offsets[i2]),
                        QColor(255, 205, 205),
                    )
                )
            if tag in ("replace", "insert"):
                current_selections.append(
                    self._make_selection(
                        self.current_view.document(),
                        current_map.clean_to_rendered(new_offsets[j1]),
                        current_map.clean_to_rendered(new_offsets[j2]),
                        QColor(205, 240, 205),
                    )
                )
        self.other_view.setExtraSelections(other_selections)
        self.current_view.setExtraSelections(current_selections)

    @staticmethod
    def _make_selection(doc, start, end, color):
        cursor = QTextCursor(doc)
        cursor.setPosition(min(start, doc.characterCount() - 1))
        cursor.setPosition(min(end, doc.characterCount() - 1), QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setBackground(color)
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format = fmt
        return selection

    # --- lane packing for the preview render --------------------------------

    @staticmethod
    def _pack_lanes(entries):
        """entries: [(shot, start, end, pinned_lane_or_None), ...], keyed
        internally by id(shot) rather than shot_id - the other project's
        shots keep their ORIGINAL (possibly colliding-with-current) ids
        until result_shots() resolves them, so shot_id alone isn't a safe
        key here. Manually-placed/current shots keep their lane;
        everything else auto-packs around them - mirrors
        MainWindow._recompute_lanes."""
        lane_end_positions = []
        assignment = {}
        for shot, _start, end, pinned_lane in entries:
            if pinned_lane is None:
                continue
            while len(lane_end_positions) <= pinned_lane:
                lane_end_positions.append(0)
            lane_end_positions[pinned_lane] = max(lane_end_positions[pinned_lane], end)
            assignment[id(shot)] = pinned_lane
        for shot, start, end, pinned_lane in entries:
            if id(shot) in assignment:
                continue
            placed = None
            for i, occupied_until in enumerate(lane_end_positions):
                if start >= occupied_until:
                    lane_end_positions[i] = end
                    placed = i
                    break
            if placed is None:
                lane_end_positions.append(end)
                placed = len(lane_end_positions) - 1
            assignment[id(shot)] = placed
        return assignment

    def _combined_entries(self):
        """[(shot, start, end, pinned_lane), ...] for BOTH this project's
        own (fixed, always-pinned) shots and the other project's shots
        that currently have a resolved position (transferred or manually
        placed) - a still-unresolved, not-yet-placed shot is omitted."""
        entries = []
        for shot in self.current_shots:
            start, end = _shot_span(shot)
            entries.append((shot, start, end, self.current_shot_lanes.get(shot.shot_id, 0)))
        for shot in self.other_shots:
            if shot.shot_id in self._transferred:
                start, end = self._transferred[shot.shot_id]
                entries.append((shot, start, end, None))
            elif shot.shot_id in self._manual_placements:
                p = self._manual_placements[shot.shot_id]
                entries.append((shot, p["start"], p["end"], p["lane"]))
        return entries

    def _display_current_and_incoming(self):
        entries = self._combined_entries()
        lanes = self._pack_lanes(entries)
        result = []
        for shot, start, end, _pinned in entries:
            preview_shot = copy.copy(shot)
            preview_shot.segments = [Segment(start, end)]
            preview_shot.start_index = start
            preview_shot.end_index = end
            result.append((preview_shot, lanes[id(shot)]))
        return result

    # --- manual placement of unresolved shots -------------------------------

    def _refresh_needs_placement_list(self):
        self.needs_placement_list.clear()
        for shot_id in self._unresolved_ids:
            if shot_id in self._manual_placements:
                continue
            self.needs_placement_list.addItem(QListWidgetItem(shot_id))
        remaining = self.needs_placement_list.count()
        self.status_label.setText(
            f"{remaining} shot(s) still need placement. You can confirm without "
            "placing them - unplaced shots will be left out of the merge."
            if remaining else "All shots placed - ready to confirm."
        )
        self.current_view.viewport().update()

    def _on_needs_placement_clicked(self, item):
        shot_id = item.text()
        shot = next(s for s in self.other_shots if s.shot_id == shot_id)
        self._active_unresolved_shot = shot
        start, end = _shot_span(shot)
        length = max(1, end - start)

        # Default to roughly the same relative position in the current
        # project's text (not always position 0) and the shot's previous
        # lane (not always lane 0) - avoids every unresolved shot's
        # preview piling up in the same spot at the top of lane 0.
        old_len = max(1, len(self.other_text))
        doc_length = len(self.current_text)
        proportional_start = round((start / old_len) * doc_length)
        default_start = max(0, min(doc_length - length, proportional_start))
        default_end = min(doc_length, default_start + length)
        default_start = max(0, default_end - length)
        default_lane = self.other_shot_lanes.get(shot_id, 0)

        # default_start/default_end are "clean" - duplicate_preview
        # lives in current_view's RENDERED coordinates - convert at the
        # seed point. See SplitMap.
        self.current_view.duplicate_preview = {
            "start": self.current_view.split_map.clean_to_rendered(default_start),
            "end": self.current_view.split_map.clean_to_rendered(default_end),
            "lane": default_lane,
        }
        self.current_view.viewport().update()
        self.place_shot_btn.setEnabled(True)
        self.status_label.setText(
            f"Drag the handles to place shot {shot_id}, then click “Place This Shot”."
        )

    def _commit_active_placement(self):
        shot = self._active_unresolved_shot
        preview = self.current_view.duplicate_preview
        if shot is None or preview is None:
            return
        # preview holds RENDERED positions (from dragging current_view's
        # handles) - _manual_placements (like _transferred) is always
        # "clean", so convert on the way in. See SplitMap.
        self._manual_placements[shot.shot_id] = {
            "start": self.current_view.split_map.rendered_to_clean(preview["start"]),
            "end": self.current_view.split_map.rendered_to_clean(preview["end"]),
            "lane": preview.get("lane", 0),
        }
        self.current_view.duplicate_preview = None
        self.current_view.viewport().update()
        self.place_shot_btn.setEnabled(False)
        self._active_unresolved_shot = None
        self._refresh_needs_placement_list()

    # --- confirm / result ----------------------------------------------------

    def _on_confirm(self):
        remaining = self.needs_placement_list.count()
        if remaining > 0:
            shot_ids = ", ".join(
                self.needs_placement_list.item(i).text() for i in range(remaining)
            )
            answer = QMessageBox.warning(
                self, "Shots not placed",
                f"{remaining} shot(s) haven't been placed: {shot_ids}.\n\n"
                "If you continue, they will be LEFT OUT of the merge. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.accept()

    def result_shots(self):
        """[(shot, lane), ...] - the other project's shot objects that got
        resolved (transferred or manually placed), mutated in place with
        their final start/end/segments AND, only if their original
        shot_id collides with one already in this project, a freshly
        allocated unique id (scene number derived from where the shot
        landed in THIS project's text). This project's own shots are
        NOT included here - they're already correct in MainWindow and
        are never touched by a merge."""
        entries = self._combined_entries()
        lanes = self._pack_lanes(entries)

        taken_ids = {s.shot_id for s in self.current_shots}
        local_scene_letters = {k: set(v) for k, v in self.main_window.scene_used_letters.items()}

        updated = []
        for shot in self.other_shots:
            if shot.shot_id in self._transferred:
                start, end = self._transferred[shot.shot_id]
            elif shot.shot_id in self._manual_placements:
                p = self._manual_placements[shot.shot_id]
                start, end = p["start"], p["end"]
            else:
                continue

            shot.segments = [Segment(start, end)]
            shot.start_index = start
            shot.end_index = end

            if shot.shot_id in taken_ids:
                scene_number = self.main_window.get_scene_number_for_position(start)
                letter = _next_free_letter(local_scene_letters, scene_number)
                shot.scene_number = scene_number
                shot.camera_letter = letter
                shot.shot_id = f"{scene_number}{letter}"
            else:
                local_scene_letters.setdefault(str(shot.scene_number), set()).add(shot.camera_letter)
            taken_ids.add(shot.shot_id)

            updated.append((shot, lanes[id(shot)]))
        return updated
