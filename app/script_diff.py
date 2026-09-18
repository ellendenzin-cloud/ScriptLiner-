import difflib


def compute_line_diff(old_text: str, new_text: str):
    """(opcodes, old_lines, new_lines) - a difflib line-level diff.
    `keepends=True` so each line's length includes its own newline,
    which keeps line-index math and character-offset math (see
    line_offsets) exactly in sync with the full text's own indexing."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    return matcher.get_opcodes(), old_lines, new_lines


def line_offsets(lines):
    """Cumulative character offset at the start of each line, plus one
    trailing entry for the end of the text - offsets[i] is where line i
    begins, so offsets[len(lines)] is the text's total length."""
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def remap_shots(old_text: str, new_text: str, shots, shot_span_fn):
    """Diffs old_text against new_text and tries to relocate every shot's
    span onto the new text.

    A shot is only auto-transferred if its ENTIRE old span sits inside a
    single unchanged ("equal") diff block - if even part of it touches a
    changed/deleted region, the new position can't be trusted, so it's
    left for manual placement instead of guessing.

    Returns (opcodes, old_lines, new_lines, transferred, unresolved):
      - opcodes/old_lines/new_lines: for diff highlighting in the UI.
      - transferred: {shot_id: (new_start, new_end)}.
      - unresolved: [shot_id, ...], in the same order as `shots`.
    """
    opcodes, old_lines, new_lines = compute_line_diff(old_text, new_text)
    old_offsets = line_offsets(old_lines)
    new_offsets = line_offsets(new_lines)

    equal_blocks = [
        (old_offsets[i1], old_offsets[i2], new_offsets[j1])
        for tag, i1, i2, j1, j2 in opcodes
        if tag == "equal"
    ]

    transferred = {}
    unresolved = []
    for shot in shots:
        start, end = shot_span_fn(shot)
        for old_lo, old_hi, new_lo in equal_blocks:
            if old_lo <= start and end <= old_hi:
                delta = new_lo - old_lo
                transferred[shot.shot_id] = (start + delta, end + delta)
                break
        else:
            unresolved.append(shot.shot_id)

    return opcodes, old_lines, new_lines, transferred, unresolved
