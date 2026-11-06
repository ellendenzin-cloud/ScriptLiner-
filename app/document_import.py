"""Importers for NON-screenplay documents - beat sheets, treatments,
outlines, shot lists, notes - so they can be lined with shots just like a
script. Word (.docx), text-based PDF, and plain text are supported.

The result has the same shape script_import.py returns -
[(type, text, scene_number_or_None, runs_or_None), ...] - using these
"Doc ..." paragraph types (styled in window.py's SCREENPLAY_STYLE_MAP):

    "Doc Title"     the document's title
    "Doc Section"   a top-level heading; starts a new numbered section
                    (the document equivalent of a scene - shot ids like
                    "2B" come from it)
    "Doc Heading"   any deeper heading
    "Doc Text"      an ordinary paragraph
    "Doc List 1/2/3"  a bullet or numbered item, by nesting level; the
                    marker ("-", "1.") is already part of the text

Invariants shared with script_import.py: a paragraph's text never
contains a newline (it would split into a second Qt block and break the
paragraph-to-block correspondence scene numbering relies on), and runs
(when present) concatenate to exactly the paragraph's text.
"""

import re
import zipfile
import xml.etree.ElementTree as ET

DOC_TITLE = "Doc Title"
DOC_SECTION = "Doc Section"
DOC_HEADING = "Doc Heading"
DOC_TEXT = "Doc Text"
DOC_LIST_TYPES = ("Doc List 1", "Doc List 2", "Doc List 3")

_BULLETS_BY_LEVEL = ("•", "◦", "▪")
_BULLET_CHARS = (
    "•●▪◦‣⁃·"
    # Symbol/Wingdings private-use bullets Word PDFs commonly extract as.
    ""
)

_TEXT_CLEAN = {
    0x00A0: " ", 0x2028: " ", 0x2029: " ", 0x0B: " ", 0x0C: " ",
    0x0D: " ", 0x0A: " ", 0x09: "    ",
    0x200B: None, 0x200C: None, 0x200D: None, 0xFEFF: None, 0x00AD: None,
    0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl",
}


def _clean(text):
    return text.translate(_TEXT_CLEAN)


def _trim_runs(runs):
    runs = [r for r in runs if r[0]]
    if not runs:
        return runs
    runs[0] = (runs[0][0].lstrip(), *runs[0][1:])
    runs[-1] = (runs[-1][0].rstrip(), *runs[-1][1:])
    return [r for r in runs if r[0]]


def _merge_runs(runs):
    merged = []
    for run in runs:
        if merged and merged[-1][1:] == run[1:]:
            merged[-1] = (merged[-1][0] + run[0], *run[1:])
        else:
            merged.append(run)
    return merged


def _plain_run(text):
    return (text, False, False, False, False)


# --------------------------------------------------------------------- docx

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w(tag):
    return _W + tag


def _toggle_on(element):
    """A Word on/off property: present with no value means on."""
    if element is None:
        return False
    value = element.get(_w("val"))
    return value is None or value.lower() not in ("0", "false", "off", "none")


def _parse_num_pr(num_pr):
    if num_pr is None:
        return None
    num_id = num_pr.find(_w("numId"))
    ilvl = num_pr.find(_w("ilvl"))
    num_id_val = num_id.get(_w("val")) if num_id is not None else None
    ilvl_val = ilvl.get(_w("val")) if ilvl is not None else None
    return (num_id_val, int(ilvl_val) if ilvl_val and ilvl_val.isdigit() else 0)


def _load_styles(zf):
    styles = {}
    try:
        root = ET.fromstring(zf.read("word/styles.xml"))
    except (KeyError, ET.ParseError):
        return styles
    for style in root.iter(_w("style")):
        style_id = style.get(_w("styleId"))
        if not style_id:
            continue
        name = style.find(_w("name"))
        based = style.find(_w("basedOn"))
        p_pr = style.find(_w("pPr"))
        outline = p_pr.find(_w("outlineLvl")) if p_pr is not None else None
        outline_val = outline.get(_w("val")) if outline is not None else None
        styles[style_id] = {
            "name": (name.get(_w("val")) if name is not None else "").lower(),
            "based": based.get(_w("val")) if based is not None else None,
            "outline": int(outline_val) if outline_val and outline_val.isdigit() else None,
            "num": _parse_num_pr(p_pr.find(_w("numPr"))) if p_pr is not None else None,
        }
    return styles


def _style_chain(styles, style_id):
    seen = set()
    while style_id and style_id in styles and style_id not in seen:
        seen.add(style_id)
        yield style_id, styles[style_id]
        style_id = styles[style_id]["based"]


def _heading_kind(styles, style_id, p_pr):
    """("title", 0) / ("heading", level) / None for a paragraph's style."""
    for sid, style in _style_chain(styles, style_id):
        match = re.match(r"^heading ?(\d)$", style["name"]) or re.match(r"^heading ?(\d)$", sid.lower())
        if match:
            return ("heading", int(match.group(1)))
        if style["name"] == "title" or sid.lower() == "title":
            return ("title", 0)
        if style["outline"] is not None and style["outline"] < 9:
            return ("heading", style["outline"] + 1)
    outline = p_pr.find(_w("outlineLvl")) if p_pr is not None else None
    if outline is not None:
        value = outline.get(_w("val"))
        if value and value.isdigit() and int(value) < 9:
            return ("heading", int(value) + 1)
    return None


def _load_numbering(zf):
    """({numId: abstractNumId}, {abstractNumId: {ilvl: (fmt, lvlText, start)}})"""
    nums, abstracts = {}, {}
    try:
        root = ET.fromstring(zf.read("word/numbering.xml"))
    except (KeyError, ET.ParseError):
        return nums, abstracts
    for abstract in root.iter(_w("abstractNum")):
        abstract_id = abstract.get(_w("abstractNumId"))
        levels = {}
        for lvl in abstract.iter(_w("lvl")):
            ilvl = lvl.get(_w("ilvl"))
            if ilvl is None or not ilvl.isdigit():
                continue
            fmt = lvl.find(_w("numFmt"))
            text = lvl.find(_w("lvlText"))
            start = lvl.find(_w("start"))
            start_val = start.get(_w("val")) if start is not None else None
            levels[int(ilvl)] = (
                fmt.get(_w("val")) if fmt is not None else "decimal",
                text.get(_w("val")) if text is not None else None,
                int(start_val) if start_val and start_val.isdigit() else 1,
            )
        abstracts[abstract_id] = levels
    for num in root.iter(_w("num")):
        num_id = num.get(_w("numId"))
        abstract_ref = num.find(_w("abstractNumId"))
        if num_id and abstract_ref is not None:
            nums[num_id] = abstract_ref.get(_w("val"))
    return nums, abstracts


def _letters(n, upper):
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(97 + rem) + out
    return out.upper() if upper else out


def _roman(n, upper):
    out = ""
    for value, symbol in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                          (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out += symbol
            n -= value
    return out.upper() if upper else out


def _format_counter(n, fmt):
    if fmt == "lowerLetter":
        return _letters(n, False)
    if fmt == "upperLetter":
        return _letters(n, True)
    if fmt == "lowerRoman":
        return _roman(n, False)
    if fmt == "upperRoman":
        return _roman(n, True)
    if fmt == "none":
        return ""
    return str(n)


class _ListState:
    """Tracks Word list counters so numbered items get their real
    numbers ("1.", "b)", "2.1") rather than just a generic bullet."""

    def __init__(self, nums, abstracts):
        self.nums = nums
        self.abstracts = abstracts
        self.counters = {}

    def marker(self, num_id, ilvl):
        levels = self.abstracts.get(self.nums.get(num_id), {})
        lvl = levels.get(ilvl)
        if lvl is None:
            return _BULLETS_BY_LEVEL[min(ilvl, 2)]
        fmt, template, start = lvl
        if fmt == "bullet":
            return _BULLETS_BY_LEVEL[min(ilvl, 2)]
        counters = self.counters.setdefault(num_id, {})
        counters[ilvl] = counters.get(ilvl, start - 1) + 1
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]

        def fill(match):
            k = int(match.group(1)) - 1
            level = levels.get(k)
            if level is None:
                return ""
            return _format_counter(counters.get(k, level[2]), level[0])

        return re.sub(r"%(\d)", fill, template or f"%{ilvl + 1}.")


def _run_runs(run):
    props = run.find(_w("rPr"))
    bold = italic = underline = strike = False
    if props is not None:
        bold = _toggle_on(props.find(_w("b")))
        italic = _toggle_on(props.find(_w("i")))
        underline = _toggle_on(props.find(_w("u")))
        strike = _toggle_on(props.find(_w("strike"))) or _toggle_on(props.find(_w("dstrike")))
    pieces = []
    for child in run:
        if child.tag == _w("t") and child.text:
            pieces.append(child.text)
        elif child.tag == _w("tab"):
            pieces.append("\t")
        elif child.tag in (_w("br"), _w("cr")):
            pieces.append(" ")
        elif child.tag == _w("noBreakHyphen"):
            pieces.append("-")
    text = _clean("".join(pieces))
    return [(text, bold, italic, underline, strike)] if text else []



def _paragraph_runs(paragraph):
    containers = {
        _w("hyperlink"), _w("ins"), _w("smartTag"), _w("fldSimple"),
        _w("sdt"), _w("sdtContent"), _w("customXml"), _w("moveTo"),
    }
    runs = []

    def walk(element):
        for child in element:
            if child.tag == _w("r"):
                runs.extend(_run_runs(child))
            elif child.tag in containers:
                walk(child)

    walk(paragraph)
    return _trim_runs(_merge_runs(runs))


def _docx_paragraph(paragraph, styles, list_state):
    runs = _paragraph_runs(paragraph)
    if not runs:
        return None
    p_pr = paragraph.find(_w("pPr"))
    style_el = p_pr.find(_w("pStyle")) if p_pr is not None else None
    style_id = style_el.get(_w("val")) if style_el is not None else None

    heading = _heading_kind(styles, style_id, p_pr)
    if heading is not None:
        kind, level = heading
        return {"kind": kind, "level": level, "runs": runs, "marker": None}

    num = _parse_num_pr(p_pr.find(_w("numPr"))) if p_pr is not None else None
    if num is None:
        for _sid, style in _style_chain(styles, style_id):
            if style["num"] is not None:
                num = style["num"]
                break
    if num is not None and num[0] not in (None, "0"):
        marker = list_state.marker(num[0], num[1])
        return {"kind": "list", "level": num[1], "runs": runs, "marker": marker}
    return {"kind": "text", "level": 0, "runs": runs, "marker": None}


def _docx_table_blocks(table, styles, list_state):
    blocks = []
    separator = _plain_run(" | ")
    for row in table.iter(_w("tr")):
        row_runs = []
        for cell in row.findall(_w("tc")):
            cell_runs = []
            for paragraph in cell.iter(_w("p")):
                runs = _paragraph_runs(paragraph)
                if runs:
                    if cell_runs:
                        cell_runs.append(_plain_run(" "))
                    cell_runs.extend(runs)
            if cell_runs:
                if row_runs:
                    row_runs.append(separator)
                row_runs.extend(cell_runs)
        row_runs = _trim_runs(_merge_runs(row_runs))
        if row_runs:
            blocks.append({"kind": "text", "level": 0, "runs": row_runs, "marker": None})
    return blocks


def import_docx(path):
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(
            "That doesn't look like a valid .docx file (it may be damaged, "
            f"password-protected, or an older .doc): {exc}"
        )
    with zf:
        try:
            document = ET.fromstring(zf.read("word/document.xml"))
        except KeyError:
            raise ValueError("That .docx file has no document body.")
        except ET.ParseError as exc:
            raise ValueError(f"Could not read that .docx file: {exc}")
        styles = _load_styles(zf)
        nums, abstracts = _load_numbering(zf)

    list_state = _ListState(nums, abstracts)
    body = document.find(_w("body"))
    blocks = []

    def walk(container):
        for child in container:
            if child.tag == _w("p"):
                block = _docx_paragraph(child, styles, list_state)
                if block is not None:
                    blocks.append(block)
            elif child.tag == _w("tbl"):
                blocks.extend(_docx_table_blocks(child, styles, list_state))
            elif child.tag == _w("sdt"):
                content = child.find(_w("sdtContent"))
                if content is not None:
                    walk(content)

    if body is not None:
        walk(body)
    return blocks


# ------------------------------------------------- text / PDF line pipeline

_MARKER_RE = re.compile(
    r"^(?P<marker>[%s]|[-*–—](?=\s)|\(?\d{1,3}[.)])(?:\s+|(?=[A-Za-z]))(?P<rest>\S.*)$"
    % re.escape(_BULLET_CHARS)
)
_MARKER_ONLY_RE = re.compile(r"^(?:[%s]|[-*–—]|\(?\d{1,3}[.)])$" % re.escape(_BULLET_CHARS))
_PAGE_NUMBER_RE = re.compile(r"^(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\.?$", re.IGNORECASE)
_WRAP_THRESHOLD = 0.85
_HEADING_END_PUNCTUATION = (".", "?", "!", ";", ",", "…")


def _looks_like_heading(text):
    return (
        len(text) <= 80
        and len(text.split()) <= 12
        and not text.endswith(_HEADING_END_PUNCTUATION)
        and any(ch.isalpha() for ch in text)
    )


def _join_wrapped(parts):
    out = parts[0]
    for part in parts[1:]:
        out += part if out.endswith("-") else " " + part
    return out


def _percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * fraction))]


def _blocks_from_lines(raw_lines):
    """Rebuilds paragraphs/headings/list items from a document's visual
    lines (a PDF page's layout-mode text, or a plain text file), where
    hard line wraps and blank lines are all the structure there is.
    Heuristic by nature: a paragraph's wrapped lines run close to the
    right edge, so a line that stops short ends its paragraph; a lone
    short line with no end punctuation between blank lines is a heading;
    lines opening with a bullet or number are list items."""
    entries = []
    for raw in raw_lines:
        if raw == "\f":
            entries.append(("page",))
            continue
        line = _clean(raw.expandtabs(4)).rstrip()
        if not line.strip():
            entries.append(("blank",))
            continue
        entries.append(("line", len(line) - len(line.lstrip(" ")), line.strip()))

    extents = [e[1] + len(e[2]) for e in entries if e[0] == "line" and len(e[2]) >= 25]
    if not extents:
        extents = [e[1] + len(e[2]) for e in entries if e[0] == "line"]
    if not extents:
        return []
    short_of_edge = _WRAP_THRESHOLD * _percentile(extents, 0.9)
    indent_counts = {}
    for e in entries:
        if e[0] == "line":
            indent_counts[e[1]] = indent_counts.get(e[1], 0) + 1
    base_indent = max(indent_counts, key=indent_counts.get)

    blocks, current = [], []

    def flush():
        nonlocal current
        if current:
            blocks.append(current)
            current = []

    for entry in entries:
        if entry[0] == "blank":
            flush()
        elif entry[0] == "page":
            # A paragraph can run straight across a page break; only end
            # it if the last line clearly finished.
            if current:
                indent, text = current[-1]
                if indent + len(text) < short_of_edge or text.endswith((".", "?", "!", ":")):
                    flush()
        else:
            current.append((entry[1], entry[2]))
    flush()

    items = []  # (kind, indent, text, marker)
    for block in blocks:
        merged = []
        carry = None
        for indent, text in block:
            if carry is not None:
                text = f"{carry[1]} {text}"
                indent = carry[0]
                carry = None
            if _MARKER_ONLY_RE.match(text):
                carry = (indent, text)
                continue
            merged.append((indent, text))
        if carry is not None:
            merged.append(carry)

        groups = []
        previous = None  # (indent, text) of the line just before this one
        for indent, text in merged:
            match = _MARKER_RE.match(text)
            # A paragraph's wrapped lines run to the right edge, so a line
            # that stops short ended its paragraph. An INDENTED line that
            # ends a sentence also does - that's how a bulleted list looks
            # once its bullet glyphs fail to extract, and a normal
            # paragraph at the base indent is unaffected.
            ended_previous = previous is not None and (
                previous[0] + len(previous[1]) < short_of_edge
                or (previous[0] > base_indent + 3 and previous[1].endswith((".", "?", "!")))
            )
            if not groups or match or ended_previous:
                groups.append({
                    "indent": indent,
                    "marker": match.group("marker") if match else None,
                    "parts": [match.group("rest") if match else text],
                })
            else:
                groups[-1]["parts"].append(text)
            previous = (indent, text)

        for group in groups:
            text = _join_wrapped(group["parts"])
            if group["marker"] is not None:
                kind = "list"
            elif len(groups) == 1 and len(group["parts"]) == 1 and _looks_like_heading(text):
                kind = "heading"
            else:
                kind = "text"
            items.append((kind, group["indent"], text, group["marker"]))

    list_indents = [indent for kind, indent, _t, _m in items if kind == "list"]
    base_indent = min(list_indents) if list_indents else 0
    result = []
    for position, (kind, indent, text, marker) in enumerate(items):
        if kind == "heading" and position == 0 and (text.isupper() or indent >= 10):
            result.append({"kind": "title", "level": 0, "runs": [_plain_run(text)], "marker": None})
        elif kind == "heading":
            result.append({"kind": "heading", "level": 1 if text.isupper() else 2,
                           "runs": [_plain_run(text)], "marker": None})
        elif kind == "list":
            level = min(2, max(0, round((indent - base_indent) / 6)))
            if marker is not None and (marker in _BULLET_CHARS or marker in ("-", "*", "–", "—")):
                marker = _BULLETS_BY_LEVEL[level]
            result.append({"kind": "list", "level": level, "runs": [_plain_run(text)], "marker": marker})
        else:
            result.append({"kind": "text", "level": 0, "runs": [_plain_run(text)], "marker": None})
    return result


def _page_lines_without_furniture(pages):
    """Drops bare page numbers and running headers/footers (a first or
    last line repeated on most pages) from each page's line list."""
    stripped = []
    for lines in pages:
        while lines and not lines[-1].strip():
            lines = lines[:-1]
        while lines and not lines[0].strip():
            lines = lines[1:]
        stripped.append(list(lines))

    def normalised(line):
        return re.sub(r"\d+", "#", line.strip().lower())

    if len(stripped) >= 3:
        for end in (0, -1):
            tally = {}
            for lines in stripped:
                if lines:
                    key = normalised(lines[end])
                    tally[key] = tally.get(key, 0) + 1
            repeated = {key for key, count in tally.items() if key and count >= 0.6 * len(stripped)}
            for lines in stripped:
                if lines and normalised(lines[end]) in repeated:
                    del lines[end]

    for lines in stripped:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and _PAGE_NUMBER_RE.match(lines[-1].strip()):
            lines.pop()
    return stripped


def import_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("PDF import needs the 'pypdf' package, which isn't installed.")

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("That PDF is password-protected - remove the password and try again.")
        pages = []
        for page in reader.pages:
            try:
                text = page.extract_text(extraction_mode="layout")
            except Exception:
                text = page.extract_text()
            pages.append((text or "").split("\n"))
    except ValueError:
        raise
    except Exception as exc:  # pypdf raises a wide range of errors on damaged files
        raise ValueError(f"Could not read that PDF: {exc}")

    pages = _page_lines_without_furniture(pages)
    lines = []
    for index, page_lines in enumerate(pages):
        if index:
            lines.append("\f")
        lines.extend(page_lines)
    blocks = _blocks_from_lines(lines)
    if not blocks:
        raise ValueError(
            "That PDF has no selectable text - it may be a scan or a photo of "
            "a document. Run it through OCR (or export a text-based PDF) and try again."
        )
    return blocks


def import_text_document(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    return _blocks_from_lines(text.split("\n"))


# ------------------------------------------------------------------ output


def _finalize(blocks):
    heading_levels = [b["level"] for b in blocks if b["kind"] == "heading"]
    section_level = min(heading_levels) if heading_levels else None
    paragraphs = []
    for block in blocks:
        kind, runs = block["kind"], list(block["runs"])
        if kind in ("title", "heading"):
            runs = [(text, True, i, u, s) for text, _b, i, u, s in runs]
            if kind == "title":
                p_type = DOC_TITLE
            elif block["level"] == section_level:
                p_type = DOC_SECTION
            else:
                p_type = DOC_HEADING
        elif kind == "list":
            marker = block["marker"] or ""
            prefix = ""
            if marker:
                prefix = marker + ("  " if len(marker) == 1 else " ")
            if prefix:
                runs.insert(0, _plain_run(prefix))
            p_type = DOC_LIST_TYPES[min(block["level"], 2)]
        else:
            p_type = DOC_TEXT
        runs = _merge_runs(runs)
        text = "".join(r[0] for r in runs)
        if not text.strip():
            continue
        has_formatting = any(any(r[1:]) for r in runs)
        paragraphs.append((p_type, text, None, runs if has_formatting else None))
    return paragraphs


def import_document(path: str):
    """Structured paragraphs for a .docx, text-based .pdf, or .txt
    document. Raises ValueError with a user-readable message on anything
    unreadable or empty."""
    lower = path.lower()
    if lower.endswith(".docx"):
        blocks = import_docx(path)
    elif lower.endswith(".pdf"):
        blocks = import_pdf(path)
    elif lower.endswith(".txt"):
        blocks = import_text_document(path)
    elif lower.endswith(".doc"):
        raise ValueError(
            "Older .doc files aren't supported - open it in Word and use "
            "Save As > Word Document (.docx), then import that."
        )
    else:
        raise ValueError("Import Document supports Word (.docx), PDF, and plain text (.txt) files.")
    paragraphs = _finalize(blocks)
    if not paragraphs:
        raise ValueError("No text was found in that document.")
    return paragraphs
