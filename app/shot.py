from dataclasses import dataclass, field
from typing import List


@dataclass
class Segment:
    start_index: int
    end_index: int


@dataclass
class Shot:
    shot_id: str            # e.g. "12A"
    scene_number: str       # e.g. "12"
    camera_letter: str      # e.g. "A"
    start_index: int
    end_index: int
    text: str = ""
    color: str = "#ff4d4d"
    tick_shape: str = "diagonal"  # see TICK_SHAPES in window.py - paired 1:1 with color by index, used instead of color to tell shots apart in a black-and-white export
    line_style: str = "solid"   # solid, dashed, dotted, dash-dot, double
    shot_type: str = ""
    lens: str = ""
    camera_movement: str = ""
    description: str = ""
    notes: str = ""
    references: List[str] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)
    suppress_start_tick: bool = False  # True when this shot's line continues from an earlier page
    suppress_end_tick: bool = False    # True when this shot's line continues onto a later page
    overlay: bool = False       # draw this shot's line over the text instead of in the side margin
    overlay_opacity: int = 40   # percent (10-100); only used while overlay is on
    page_note: str = ""         # free text printed on the page above the shot's start

    def __post_init__(self):
        if not self.segments:
            self.segments = [Segment(self.start_index, self.end_index)]

    def add_segment(self, start_index: int, end_index: int):
        """Used for the Split Shots feature - a shot with multiple
        line ranges that share the same id/color/label."""
        self.segments.append(Segment(start_index, end_index))

    def all_ranges(self):
        return [(seg.start_index, seg.end_index) for seg in self.segments]