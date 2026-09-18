"""Importers that turn Final Draft (.fdx) and Fade In (.fadein) files
into a list of (element_type, text, scene_number, runs) paragraphs,
e.g.:

    [("Scene Heading", "INT. KITCHEN - DAY", "1", None),
     ("Action", "Sarah enters the room.", None, None),
     ("Character", "SARAH", None, None),
     ("Dialogue", "Hello?", None, [("Hello?", True, False, False, False)])]

element_type uses Final Draft's naming ("Scene Heading", "Action",
"Character", "Parenthetical", "Dialogue", "Transition", "Shot",
"General") since Fade In's Open Screenplay Format uses the same names.
This structured list is what lets main_window.py render real
screenplay formatting (margins/alignment/caps) instead of guessing
from plain text. The third element is a locked/manual scene number
read from the source file (Final Draft and Fade In both store one on
Scene Heading paragraphs) - None when the format doesn't carry one, in
which case ScriptLiner falls back to auto-numbering scenes in order.

The fourth element is optional inline-formatting runs: [(run_text,
bold, italic, underline, strikethrough), ...], whose run_text values
concatenate to EXACTLY `text` - so a caller that only cares about plain
text can keep reading para[1] unchanged. None means "no formatting
info available," in which case renderers fall back to plain text.
"""

import re
import zipfile
import xml.etree.ElementTree as ET


def _strip_runs(runs):
    """Trims leading whitespace from the first run and trailing
    whitespace from the last (dropping any run left empty), mirroring
    str.strip() but preserving each run's own formatting flags - so
    "".join(text for text, *_ in _strip_runs(runs)) always equals
    "".join(text for text, *_ in runs).strip() exactly."""
    runs = [r for r in runs if r[0]]
    if not runs:
        return runs
    runs[0] = (runs[0][0].lstrip(), *runs[0][1:])
    runs[-1] = (runs[-1][0].rstrip(), *runs[-1][1:])
    return [r for r in runs if r[0]]


def _runs_or_none(runs, text):
    """None (safe plain-text fallback) unless `runs` reconstructs
    `text` exactly - a cheap correctness guard so a formatting-parser
    edge case can never silently corrupt the text a shot's position
    would be measured against."""
    if runs and "".join(r[0] for r in runs) == text:
        return runs
    return None


def import_fdx(path: str):
    """Parse a Final Draft .fdx file (documented XML format)."""
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ValueError(f"This doesn't look like a valid .fdx file: {exc}")

    root = tree.getroot()
    content = root.find("Content")
    if content is None:
        raise ValueError(
            "No <Content> section found - this may not be a Final Draft document."
        )

    paragraphs = []
    for paragraph in content.findall("Paragraph"):
        p_type = paragraph.get("Type", "Action")
        # Each <Text Style="Bold+Underline">...</Text> run carries its
        # own formatting as a "+"-joined Style attribute - build inline
        # runs from these directly rather than just joining .text.
        raw_runs = [
            (t.text or "", *_fdx_run_flags(t.get("Style")))
            for t in paragraph.findall("Text")
        ]
        text = "".join(r[0] for r in raw_runs).strip()
        runs = _runs_or_none(_strip_runs(raw_runs), text)
        # Final Draft stores a locked/manual scene number as a "Number"
        # attribute on Scene Heading paragraphs (e.g. Number="12" or the
        # revision-mark form "12A") - carry it through so ScriptLiner can
        # honor the writer's own numbering instead of just counting
        # scene headings in order.
        scene_number = paragraph.get("Number") if p_type == "Scene Heading" else None
        paragraphs.append((p_type, text, scene_number, runs))

    if not paragraphs:
        raise ValueError("No paragraphs were found in this .fdx file.")

    return paragraphs


def _fdx_run_flags(style_attr):
    """(bold, italic, underline, strikethrough) from a Final Draft
    Text element's "+"-joined Style attribute (e.g. "Bold+Italic")."""
    parts = (style_attr or "").split("+")
    return (
        "Bold" in parts,
        "Italic" in parts,
        "Underline" in parts,
        "Strikethrough" in parts,
    )


def import_fadein(path: str):
    """Parse a Fade In .fadein file: a zip archive containing
    document.xml in Open Screenplay Format. Paragraph type comes from
    a nested <style basestyle="..."/> element, not an attribute on
    <para> itself - e.g.:

        <para>
          <style basestyle="Character"/>
          <text>SARAH</text>
        </para>
    """
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                xml_candidates = [n for n in zf.namelist() if n.lower().endswith(".xml")]
                if not xml_candidates:
                    raise ValueError("No XML document found inside this .fadein file.")
                xml_name = "document.xml" if "document.xml" in xml_candidates else xml_candidates[0]
                data = zf.read(xml_name)
            root = ET.fromstring(data)
        else:
            root = ET.parse(path).getroot()
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError(f"Couldn't read this .fadein file: {exc}")

    paragraphs_el = root.find("paragraphs")
    if paragraphs_el is None:
        raise ValueError("No <paragraphs> section found - this may not be a Fade In script.")

    paragraphs = []
    for para in paragraphs_el.findall("para"):
        style_el = para.find("style")
        p_type = style_el.get("basestyle", "Action") if style_el is not None else "Action"

        text_el = para.find("text")
        text = (text_el.text or "").strip() if text_el is not None else ""

        # Best-effort: Fade In's Open Screenplay Format isn't fully
        # documented here (unverified against a real scene-numbered
        # file), but a locked scene number - if present - would land on
        # the <para> or its <style> as a "number" attribute. Falls back
        # to auto-numbering if it's not there or named differently.
        scene_number = para.get("number") or (style_el.get("number") if style_el is not None else None)
        # Inline bold/italic/underline/strikethrough markup isn't
        # documented/verified for this format here (see the module
        # docstring) - runs=None falls back to plain text safely rather
        # than guessing at a structure that might be wrong.
        paragraphs.append((p_type, text, scene_number if p_type == "Scene Heading" else None, None))

    if not paragraphs:
        raise ValueError("No paragraphs were recognized in this .fadein file.")

    return paragraphs


# ----------------------------------------------------------------------
# Fountain (.fountain) - plain-text screenplay markup, so this is a real
# parser rather than an XML read. Handles: title page (Key: value block
# at the top, stripped), scene headings (INT./EXT./EST., or a line
# forced with a leading "."), character cues (ALL CAPS short lines, or
# forced with a leading "@"), parentheticals, dialogue, transitions
# (ALL CAPS ending "TO:", or forced with a leading ">"), and action
# (everything else, grouped into paragraphs by blank lines).
# ----------------------------------------------------------------------

SCENE_HEADING_PATTERN = re.compile(r'^(INT|EXT|EST|INT\.?/EXT|I/E)[.\s]', re.IGNORECASE)
TRANSITION_PATTERN = re.compile(r'^[A-Z0-9 .\']+TO:\s*$')
PAREN_PATTERN = re.compile(r'^\(.*\)$')
TITLE_PAGE_KEY_PATTERN = re.compile(r'^([A-Za-z][A-Za-z \t]*):\s*(.*)$')

TITLE_PAGE_KEYS = {
    "title", "credit", "author", "authors", "source", "notes",
    "draft date", "date", "contact", "copyright", "revision",
}

# Fountain's inline emphasis markup - checked longest-marker-first
# (***bold italic*** before **bold** before *italic*) so a run of three
# asterisks is never misread as italic-inside-bold.
FOUNTAIN_EMPHASIS_RE = re.compile(
    r'\*\*\*(?P<boldital>.+?)\*\*\*'
    r'|\*\*(?P<bold>.+?)\*\*'
    r'|\*(?P<italic>.+?)\*'
    r'|_(?P<underline>.+?)_'
)


def _parse_fountain_emphasis(text):
    """[(run_text, bold, italic, underline, strikethrough), ...] -
    splits Fountain's inline **bold**/*italic*/_underline_ markup into
    formatting runs, stripping the marker characters (rather than
    leaving the literal asterisks/underscores in the displayed text).
    Single-pass and non-overlapping, so it doesn't handle emphasis
    nested inside other emphasis (e.g. bold containing underline) -
    rare enough in practice to leave as literal, unstripped markup
    rather than add a full recursive parser for it."""
    runs = []
    pos = 0
    for m in FOUNTAIN_EMPHASIS_RE.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False, False, False, False))
        if m.group("boldital") is not None:
            runs.append((m.group("boldital"), True, True, False, False))
        elif m.group("bold") is not None:
            runs.append((m.group("bold"), True, False, False, False))
        elif m.group("italic") is not None:
            runs.append((m.group("italic"), False, True, False, False))
        elif m.group("underline") is not None:
            runs.append((m.group("underline"), False, False, True, False))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False, False, False, False))
    return runs


def import_fountain(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return parse_fountain(text)


def parse_fountain(text: str):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = _strip_fountain_title_page(lines)

    paragraphs = []
    action_buffer = []
    i = 0
    n = len(lines)

    def emit(p_type, raw_text, scene_number=None):
        raw_runs = _parse_fountain_emphasis(raw_text)
        plain_text = "".join(r[0] for r in raw_runs).strip()
        runs = _runs_or_none(_strip_runs(raw_runs), plain_text)
        paragraphs.append((p_type, plain_text, scene_number, runs))

    def flush_action():
        if action_buffer:
            emit("Action", "\n".join(action_buffer))
            action_buffer.clear()

    while i < n:
        stripped = lines[i].strip()

        if not stripped:
            flush_action()
            i += 1
            continue

        if stripped.startswith(".") and not stripped.startswith(".."):
            flush_action()
            emit("Scene Heading", stripped[1:].strip())
            i += 1
            continue

        if SCENE_HEADING_PATTERN.match(stripped):
            flush_action()
            emit("Scene Heading", stripped)
            i += 1
            continue

        if stripped.startswith(">"):
            flush_action()
            emit("Transition", stripped.lstrip(">").strip())
            i += 1
            continue

        if TRANSITION_PATTERN.match(stripped):
            flush_action()
            emit("Transition", stripped)
            i += 1
            continue

        if stripped.startswith("@") or _looks_like_character_cue(stripped):
            flush_action()
            emit("Character", stripped.lstrip("@").strip())
            i += 1
            while i < n and lines[i].strip():
                sub = lines[i].strip()
                if PAREN_PATTERN.match(sub):
                    emit("Parenthetical", sub[1:-1])
                else:
                    emit("Dialogue", sub)
                i += 1
            continue

        action_buffer.append(stripped)
        i += 1

    flush_action()

    if not paragraphs:
        raise ValueError("No content was recognized in this .fountain file.")

    return paragraphs


def _looks_like_character_cue(stripped: str) -> bool:
    if SCENE_HEADING_PATTERN.match(stripped) or TRANSITION_PATTERN.match(stripped):
        return False
    core = re.sub(r"\(.*?\)", "", stripped).strip()
    if not core or not re.search(r"[A-Za-z]", core):
        return False
    if core != core.upper():
        return False
    if len(stripped) > 40:
        return False
    return True


def _strip_fountain_title_page(lines):
    if not lines:
        return lines

    first = lines[0].strip()
    match = TITLE_PAGE_KEY_PATTERN.match(first)
    if not match or match.group(1).strip().lower() not in TITLE_PAGE_KEYS:
        return lines

    i = 0
    n = len(lines)
    while i < n and lines[i].strip():
        i += 1
    while i < n and not lines[i].strip():
        i += 1
    return lines[i:]


# ----------------------------------------------------------------------
# RTF (.rtf) - screenwriting software (Movie Magic, older Final Draft
# exports, etc.) exports RTF with a \stylesheet block naming each
# paragraph style (SCENE HEADING, ACTION, CHARACTER, ...), and each
# body paragraph starts with \sN referencing that style number. We
# read the stylesheet to map N -> element type, then read each
# paragraph's actual style rather than guessing from text patterns.
# Verified against a real exported .rtf screenplay.
# ----------------------------------------------------------------------

RTF_STYLE_NAME_TO_ELEMENT = {
    "SCENE HEADING": "Scene Heading",
    "ACTION": "Action",
    "CHARACTER": "Character",
    "PARENTHETICAL": "Parenthetical",
    "DIALOGUE": "Dialogue",
    "TRANSITION": "Transition",
    "SHOT": "Shot",
    "GENERAL": "General",
}

RTF_HEX_ESCAPE_RE = re.compile(r"\\'([0-9a-fA-F]{2})")
RTF_UNICODE_RE = re.compile(r"\\u(-?\d+)\??")
RTF_CONTROL_WORD_RE = re.compile(r"\\[a-zA-Z]+-?\d*[ ]?")
RTF_PAR_SPLIT_RE = re.compile(r"\\par(?![a-zA-Z])")  # \par but not \pard etc.
RTF_STYLESHEET_RE = re.compile(r"\{\\stylesheet(.*?)\n\}\n", re.DOTALL)
RTF_STYLE_DEF_RE = re.compile(r"\\s(\d+)[^}]*?\s([A-Za-z][A-Za-z0-9 ]*);")
RTF_STYLE_NUM_RE = re.compile(r"\\s(\d+)\b")

# Tokenizes a paragraph fragment into: hex/unicode escapes (decoded),
# the 4 formatting toggle words this app cares about (state-tracked,
# not stripped-and-forgotten like other control words), any other
# control word (stripped, no state change - same as before), an
# escaped brace/backslash, a literal brace (stripped), or plain text.
# Longest-prefix-first within the toggle alternation (b0 before b, etc)
# so e.g. "\b0" is never misread as "\b" followed by literal "0".
RTF_TOKEN_RE = re.compile(
    r"(?P<hex>\\'[0-9a-fA-F]{2})"
    r"|(?P<uni>\\u-?\d+\??)"
    r"|\\(?P<toggle>b0|b|i0|i|ulnone|ul0|ul|strike0|strike)[ ]?"
    r"|(?P<esc>\\[{}\\])"
    r"|(?P<other_ctrl>\\[a-zA-Z]+-?\d*[ ]?)"
    r"|(?P<brace>[{}])"
    r"|(?P<text>[^\\{}]+)"
)


def import_rtf(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    return parse_rtf(raw)


def parse_rtf(raw: str):
    style_map = {}
    stylesheet_match = RTF_STYLESHEET_RE.search(raw)
    stylesheet_text = stylesheet_match.group(1) if stylesheet_match else ""
    for num, name in RTF_STYLE_DEF_RE.findall(stylesheet_text):
        element = RTF_STYLE_NAME_TO_ELEMENT.get(name.strip().upper())
        if element:
            style_map[num] = element

    body = raw[stylesheet_match.end():] if stylesheet_match else raw
    fragments = RTF_PAR_SPLIT_RE.split(body)

    paragraphs = []
    started = False
    for fragment in fragments:
        style_search = RTF_STYLE_NUM_RE.search(fragment)
        style_num = style_search.group(1) if style_search else None
        element = style_map.get(style_num)

        raw_runs = _rtf_paragraph_runs(fragment)
        text = "".join(r[0] for r in raw_runs)
        if not text:
            continue

        if element is None:
            if not started:
                continue  # title page material before the story starts
            element = "Action"
        else:
            started = True

        runs = _runs_or_none(_strip_runs(raw_runs), text)
        paragraphs.append((element, text, None, runs))

    if not paragraphs:
        raise ValueError("No screenplay content was recognized in this .rtf file.")

    return paragraphs


def _rtf_paragraph_runs(fragment: str):
    """[(run_text, bold, italic, underline, strikethrough), ...] for
    one RTF paragraph fragment - tracks \\b/\\i/\\ul/\\strike (and
    their off forms) as they toggle through the fragment, decodes hex/
    unicode escapes, and collapses whitespace exactly like the old
    plain-text cleanup (" ".join(text.split())) did, just attributing
    each surviving character to the run/formatting it came from."""
    bold = italic = underline = strike = False
    buf = []
    raw_runs = []

    def flush():
        if buf:
            raw_runs.append(("".join(buf), bold, italic, underline, strike))
            buf.clear()

    for m in RTF_TOKEN_RE.finditer(fragment):
        if m.group("hex"):
            try:
                buf.append(bytes([int(m.group("hex")[2:], 16)]).decode("cp1252", errors="replace"))
            except ValueError:
                pass
        elif m.group("uni"):
            num = int(re.match(r"\\u(-?\d+)", m.group("uni")).group(1))
            buf.append(chr(num & 0xFFFF))
        elif m.group("toggle"):
            flush()
            tok = m.group("toggle")
            if tok == "b":
                bold = True
            elif tok == "b0":
                bold = False
            elif tok == "i":
                italic = True
            elif tok == "i0":
                italic = False
            elif tok == "ul":
                underline = True
            elif tok in ("ulnone", "ul0"):
                underline = False
            elif tok == "strike":
                strike = True
            elif tok == "strike0":
                strike = False
        elif m.group("esc"):
            buf.append(m.group("esc")[1:])
        elif m.group("other_ctrl") or m.group("brace"):
            pass  # stripped, no state change - same as the old cleanup
        elif m.group("text"):
            buf.append(m.group("text"))
    flush()

    return _collapse_runs_whitespace(raw_runs)


def _collapse_runs_whitespace(runs):
    """Equivalent to " ".join(full_text.split()) applied across the
    whole run sequence - collapses any run of whitespace (including
    whitespace that straddles a formatting boundary) to a single space,
    and drops leading/trailing whitespace - while keeping each
    surviving character attributed to the run/formatting it came from."""
    out = []
    pending_space = False
    started = False
    for text, bold, italic, underline, strike in runs:
        buf = []
        for ch in text:
            if ch.isspace():
                if started:
                    pending_space = True
                continue
            if pending_space:
                buf.append(" ")
                pending_space = False
            buf.append(ch)
            started = True
        if buf:
            out.append(("".join(buf), bold, italic, underline, strike))
    return out


# ----------------------------------------------------------------------
# Celtx (.celtx) - a zip archive containing HTML-based script content
# with CSS classes naming each element (sceneheading, action,
# character, dialog, parenthetical, transition). UNVERIFIED: built
# from documented/reverse-engineered structure, not tested against a
# real .celtx file. If it doesn't parse correctly, send a sample file
# the same way the .fadein and .rtf files were provided, and this will
# get fixed against real data.
# ----------------------------------------------------------------------

CELTX_CLASS_TO_ELEMENT = {
    "sceneheading": "Scene Heading",
    "scene-heading": "Scene Heading",
    "action": "Action",
    "character": "Character",
    "dialog": "Dialogue",
    "dialogue": "Dialogue",
    "parenthetical": "Parenthetical",
    "transition": "Transition",
    "shot": "Shot",
    "general": "General",
}

CELTX_TAG_RE = re.compile(
    r'<p[^>]*class="([^"]+)"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL
)
# Formatting tags this app tracks (state, toggled by open/close), or
# any other tag (silently dropped, same as the old blanket tag-strip -
# includes things like <br>/<span> that this importer already didn't
# handle specially).
CELTX_INLINE_TOKEN_RE = re.compile(
    r"</?(?P<tag>b|strong|i|em|u|s|strike|del)\b[^>]*>"
    r"|<[^>]+>"
    r"|(?P<text>[^<]+)",
    re.IGNORECASE,
)


def _celtx_unescape(text: str) -> str:
    return (
        text.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">")
    )


def _celtx_inline_runs(inner_html: str):
    """[(run_text, bold, italic, underline, strikethrough), ...] from a
    Celtx paragraph's inner HTML - tracks <b>/<strong>, <i>/<em>, <u>,
    <s>/<strike>/<del> as they open/close. Doesn't build a real tag
    stack (mismatched/overlapping tags just toggle a flag rather than
    erroring), which is fine for the simple, non-nested markup actual
    exports produce."""
    bold = italic = underline = strike = False
    buf = []
    raw_runs = []

    def flush():
        if buf:
            raw_runs.append((_celtx_unescape("".join(buf)), bold, italic, underline, strike))
            buf.clear()

    for m in CELTX_INLINE_TOKEN_RE.finditer(inner_html):
        if m.group("text") is not None:
            buf.append(m.group("text"))
            continue
        tag = m.group("tag")
        if tag is None:
            continue  # some other HTML tag - dropped, no state change
        flush()
        is_close = m.group(0).startswith("</")
        tag = tag.lower()
        if tag in ("b", "strong"):
            bold = not is_close
        elif tag in ("i", "em"):
            italic = not is_close
        elif tag == "u":
            underline = not is_close
        elif tag in ("s", "strike", "del"):
            strike = not is_close
    flush()
    return raw_runs


def import_celtx(path: str):
    import zipfile as _zipfile

    if not _zipfile.is_zipfile(path):
        raise ValueError("This doesn't look like a valid .celtx file (expected a zip archive).")

    with _zipfile.ZipFile(path) as zf:
        html_candidates = [
            n for n in zf.namelist() if n.lower().endswith((".html", ".xhtml"))
        ]
        if not html_candidates:
            raise ValueError("No script content (HTML) found inside this .celtx file.")

        paragraphs = []
        for name in html_candidates:
            html = zf.read(name).decode("utf-8", errors="replace")
            for class_attr, inner_html in CELTX_TAG_RE.findall(html):
                element = None
                for cls in class_attr.lower().split():
                    if cls in CELTX_CLASS_TO_ELEMENT:
                        element = CELTX_CLASS_TO_ELEMENT[cls]
                        break
                if element is None:
                    continue
                raw_runs = _celtx_inline_runs(inner_html)
                text = "".join(r[0] for r in raw_runs).strip()
                if text:
                    runs = _runs_or_none(_strip_runs(raw_runs), text)
                    paragraphs.append((element, text, None, runs))

    if not paragraphs:
        raise ValueError(
            "No recognizable screenplay elements were found in this .celtx file - "
            "the internal format may differ from what this importer expects."
        )

    return paragraphs