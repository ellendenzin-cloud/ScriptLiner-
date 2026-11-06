import json
from dataclasses import asdict

from app.shot import Shot, Segment

PROJECT_VERSION = 1


def project_to_dict(window) -> dict:
    """Snapshot everything needed to reconstruct the window's project state."""
    return {
        "version": PROJECT_VERSION,
        "loaded_paragraphs": window.loaded_paragraphs,
        # clean_script_text() (never window.script_view.toPlainText()
        # directly) - the live document can have real "(MORE)"/"CONT'D"
        # text spliced into it depending on the window's current size,
        # which must never leak into what gets saved. Only relevant
        # when loaded_paragraphs is None anyway, since plain-text
        # scripts never get spliced - kept for defensive consistency.
        "script_text": window.clean_script_text() if window.loaded_paragraphs is None else None,
        "shots": [asdict(shot) for shot in window.shots],
        "shot_lanes": window.shot_lanes,
        "scene_used_letters": {
            key: sorted(letters) for key, letters in window.scene_used_letters.items()
        },
        "camera_letter_colors": window.camera_letter_colors,
        "colors": window.colors,
        "pinned_lanes": sorted(window.pinned_lanes),
        # Where each pinned shot was dragged to - kept separately from
        # shot_lanes (which an automatic lane order overwrites) so turning
        # that order off again brings the manual layout back.
        "manual_lanes": window.manual_lanes,
        "base_lane_width": window.script_view.base_lane_width,
        "unique_shot_colors": window.unique_shot_colors,
        "length_sort_shots": window.length_sort_shots,
        "label_every_segment": window.label_every_segment,
        "overlay_default_opacity": window.overlay_default_opacity,
        "shot_type_style_map": window.shot_type_style_map,
        "shot_type_affects_style": window.shot_type_affects_style,
        "key_enabled": window.key_enabled,
        "key_placement": window.key_placement,
        "key_box_position": window.key_box_position,
        "line_style_labels": window.line_style_labels,
        "multi_style_mode": window.multi_style_mode,
        "page_gap_style": window.page_gap_style,
        "active_lanes": sorted(window.active_lanes),
        "auto_sort_shots": window.auto_sort_shots,
    }


def dict_to_shots(data: dict):
    shots = []
    for shot_data in data.get("shots", []):
        shot_data = dict(shot_data)
        segments = [Segment(**seg) for seg in shot_data.get("segments", [])]
        shot_data["segments"] = segments
        shots.append(Shot(**shot_data))
    return shots


def save_project(window, path: str):
    data = project_to_dict(window)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_project_data(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
