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


class UpdateScriptDialog(QDialog):
    """Side-by-side review for replacing the script with a new revision.

    Every existing shot whose own text is untouched by the revision is
    relocated onto the new script automatically (see app.script_diff);
    anything that overlaps a changed region is left for the user to
    place by hand, using the same drag-handle preview as Duplicate Shot,
    before "Confirm Update" is enabled.
    """

    VIEW_SCALE = 0.75

    def __init__(self, main_window, new_text: str, new_paragraphs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update Script")
        # A flat 1500x1250 request can exceed a real screen's work area
        # (taskbar-adjusted), and Qt/Windows silently clamps it back down
        # near the old size instead of erroring - so size height off the
        # actual screen instead of a hardcoded pixel count.
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(1500, avail.width() - 40)
            target_h = max(700, avail.height() - 60)
        else:
            target_w, target_h = 1500, 1250
        self.resize(target_w, target_h)

        self.old_shots = main_window.shots
        # clean_script_text(), NOT script_view.toPlainText() directly -
        # the live document can have real "(MORE)"/"CONT'D" text spliced
        # in (see SplitMap), which must never leak into a diff.
        self.old_text = main_window.clean_script_text()
        self.old_paragraphs = main_window.loaded_paragraphs
        self.old_shot_lanes = dict(main_window.shot_lanes)
        self.new_text = new_text
        self.new_paragraphs = new_paragraphs

        (
            self._opcodes,
            self._old_lines,
            self._new_lines,
            self._transferred,
            self._unresolved_ids,
        ) = remap_shots(self.old_text, self.new_text, self.old_shots, _shot_span)
        self._manual_placements = {}  # shot_id -> {"start":, "end":, "lane":}
        self._active_unresolved_shot = None

        self._build_ui()
        self._apply_diff_highlights()
        self._refresh_needs_placement_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            "Reviewing the new version below. Shots whose own text didn't change "
            "carry over automatically. Anything listed under “Needs Placement” "
            "can be positioned by hand - drag its handles into place on the New "
            "Version, then click “Place This Shot”. You can also confirm with "
            "shots still unplaced; they'll simply be removed from the updated script."
        )
        info.setWordWrap(True)
        # Explicit stretch factors: a word-wrapped QLabel with no fixed
        # height otherwise reports an Expanding vertical size policy and
        # ends up splitting the dialog's height with the splitter
        # roughly evenly, instead of taking just the couple of lines it
        # actually needs and giving the rest to the splitter.
        layout.addWidget(info, 0)

        splitter = QSplitter(Qt.Horizontal)

        old_pane = QWidget()
        old_layout = QVBoxLayout(old_pane)
        old_layout.addWidget(QLabel("Current Version"))
        self.old_view = self._build_readonly_view(
            self.old_text, self.old_paragraphs, self._original_shots_with_lanes
        )
        old_layout.addWidget(self.old_view, 1)
        splitter.addWidget(old_pane)

        new_pane = QWidget()
        new_layout = QVBoxLayout(new_pane)
        new_layout.addWidget(QLabel("New Version"))
        self.new_view = self._build_readonly_view(
            self.new_text, self.new_paragraphs, self._display_shots_with_lanes
        )
        new_layout.addWidget(self.new_view, 1)
        splitter.addWidget(new_pane)

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
        buttons.button(QDialogButtonBox.Ok).setText("Confirm Update")
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

    def _original_shots_with_lanes(self):
        """The old pane always shows the shots exactly where they were in
        the current (pre-update) script, for reference - never affected by
        diff transfer/manual-placement state."""
        return [(shot, self.old_shot_lanes.get(shot.shot_id, 0)) for shot in self.old_shots]

    # --- diff highlighting -------------------------------------------------

    def _apply_diff_highlights(self):
        old_offsets = line_offsets(self._old_lines)
        new_offsets = line_offsets(self._new_lines)

        # old_offsets/new_offsets are "clean" (computed from clean_text)
        # - old_view/new_view's OWN documents may have real (MORE)/
        # CONT'D text spliced in (see SplitMap), each independently -
        # convert through each view's own split_map before placing a
        # cursor selection, or the highlight lands on the wrong text.
        old_map = self.old_view.split_map
        new_map = self.new_view.split_map

        old_selections = []
        new_selections = []
        for tag, i1, i2, j1, j2 in self._opcodes:
            if tag in ("replace", "delete"):
                old_selections.append(
                    self._make_selection(
                        self.old_view.document(),
                        old_map.clean_to_rendered(old_offsets[i1]),
                        old_map.clean_to_rendered(old_offsets[i2]),
                        QColor(255, 205, 205),
                    )
                )
            if tag in ("replace", "insert"):
                new_selections.append(
                    self._make_selection(
                        self.new_view.document(),
                        new_map.clean_to_rendered(new_offsets[j1]),
                        new_map.clean_to_rendered(new_offsets[j2]),
                        QColor(205, 240, 205),
                    )
                )
        self.old_view.setExtraSelections(old_selections)
        self.new_view.setExtraSelections(new_selections)

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
        """entries: [(shot_id, start, end, pinned_lane_or_None), ...].
        Manually-placed shots keep their dragged-to lane; everything else
        auto-packs around them - mirrors MainWindow._recompute_lanes."""
        lane_end_positions = []
        assignment = {}
        for shot_id, _start, end, pinned_lane in entries:
            if pinned_lane is None:
                continue
            while len(lane_end_positions) <= pinned_lane:
                lane_end_positions.append(0)
            lane_end_positions[pinned_lane] = max(lane_end_positions[pinned_lane], end)
            assignment[shot_id] = pinned_lane
        for shot_id, start, end, pinned_lane in entries:
            if shot_id in assignment:
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
            assignment[shot_id] = placed
        return assignment

    def _current_entries(self):
        """[(shot, start, end, pinned_lane_or_None), ...] for every shot
        that currently has a resolved position (transferred or manually
        placed) - a still-unresolved, not-yet-placed shot is omitted."""
        entries = []
        for shot in self.old_shots:
            if shot.shot_id in self._transferred:
                start, end = self._transferred[shot.shot_id]
                entries.append((shot, start, end, None))
            elif shot.shot_id in self._manual_placements:
                p = self._manual_placements[shot.shot_id]
                entries.append((shot, p["start"], p["end"], p["lane"]))
        return entries

    def _display_shots_with_lanes(self):
        entries = self._current_entries()
        lanes = self._pack_lanes([(s.shot_id, start, end, pinned) for s, start, end, pinned in entries])
        result = []
        for shot, start, end, _pinned in entries:
            preview_shot = copy.copy(shot)
            preview_shot.segments = [Segment(start, end)]
            preview_shot.start_index = start
            preview_shot.end_index = end
            result.append((preview_shot, lanes[shot.shot_id]))
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
            "placing them - unplaced shots will be removed from the updated script."
            if remaining else "All shots placed - ready to confirm."
        )
        self.new_view.viewport().update()

    def _on_needs_placement_clicked(self, item):
        shot_id = item.text()
        shot = next(s for s in self.old_shots if s.shot_id == shot_id)
        self._active_unresolved_shot = shot
        start, end = _shot_span(shot)
        length = max(1, end - start)

        # Default to roughly the same relative position in the new
        # document (not always position 0) and the shot's previous lane
        # (not always lane 0) - otherwise every unresolved shot's preview
        # piles up in the same spot at the top of lane 0, making
        # individual handles impossible to grab.
        old_len = max(1, len(self.old_text))
        doc_length = len(self.new_text)
        proportional_start = round((start / old_len) * doc_length)
        default_start = max(0, min(doc_length - length, proportional_start))
        default_end = min(doc_length, default_start + length)
        default_start = max(0, default_end - length)
        default_lane = self.old_shot_lanes.get(shot_id, 0)

        # default_start/default_end are "clean" positions (proportional
        # math against the clean text lengths above) - duplicate_preview
        # itself lives in new_view's RENDERED coordinates (what
        # painting/hit-testing/dragging use), so convert at the seed
        # point. See SplitMap.
        self.new_view.duplicate_preview = {
            "start": self.new_view.split_map.clean_to_rendered(default_start),
            "end": self.new_view.split_map.clean_to_rendered(default_end),
            "lane": default_lane,
        }
        self.new_view.viewport().update()
        self.place_shot_btn.setEnabled(True)
        self.status_label.setText(
            f"Drag the handles to place shot {shot_id}, then click “Place This Shot”."
        )

    def _commit_active_placement(self):
        shot = self._active_unresolved_shot
        preview = self.new_view.duplicate_preview
        if shot is None or preview is None:
            return
        # preview holds RENDERED positions (from dragging new_view's
        # handles) - _manual_placements (like _transferred) is always
        # "clean", so convert on the way in. See SplitMap.
        self._manual_placements[shot.shot_id] = {
            "start": self.new_view.split_map.rendered_to_clean(preview["start"]),
            "end": self.new_view.split_map.rendered_to_clean(preview["end"]),
            "lane": preview.get("lane", 0),
        }
        self.new_view.duplicate_preview = None
        self.new_view.viewport().update()
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
                "If you continue, they will be REMOVED from the updated script "
                "instead of transferred. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.accept()

    def result_shots(self):
        """[(shot, lane), ...] - the ORIGINAL shot objects, mutated in
        place with their new start/end/segments, ready for MainWindow to
        adopt. Only valid once every shot has a resolved position (the
        Confirm button is disabled otherwise)."""
        entries = self._current_entries()
        lanes = self._pack_lanes([(s.shot_id, start, end, pinned) for s, start, end, pinned in entries])
        updated = []
        for shot, start, end, _pinned in entries:
            shot.segments = [Segment(start, end)]
            shot.start_index = start
            shot.end_index = end
            updated.append((shot, lanes[shot.shot_id]))
        return updated
