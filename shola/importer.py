"""Turning one uploaded CSV into items volunteers can answer.

One file covers any project, single language or many:

    text,language,priority,option1,option2,option3
    Where is the market?,twi,1,Ɛhe na dwaso wɔ?,Dwaso wɔ he?,
    Where is the market?,ewe,1,Afi ka asi le?,,
    Where is the market?,ga,1,,,
    How much is this?,,2,,,

Rows sharing the same `text` are the **same item**. An item exists once however
many languages it is answered in - a word does not become 88 words because 88
languages translate it - and the options attach per language.

The **language column is required** and holds a language *code*, even for a
single language, so nobody has to guess which speakers a row is for. The codes
are ISO 639-3 and the submit page lists every one of them next to its name.

Write `all` for a row that applies to every language - an English word awaiting
translation is one row, not eighty-eight. Blank is refused rather than assumed:
a project accidentally addressed to all 88 languages is not something to infer
from an empty cell.

A row with options gives that language a head start: volunteers choose between
them or edit one into what they would say. One option is fine - it reads as "is
this right, or correct it". A row with no options, or a language with no row at
all, simply starts empty: the first speaker types the wording and it becomes an
option for the next. That is how the translation project already works, where
four languages arrived with machine translations and the other eighty-four
started from nothing, and there is no reason a submitted project should have to
be tidier than that.

**priority** is optional. 1 is worked to completion before 2, so the part that
matters most is finished first even if the project never finishes. Left out,
everything is one band. It is the same mechanism the translation project uses to
settle the commonest words before the long tail.

Everything here reports rather than repairs. A file that is half wrong should
come back to the person who uploaded it with the line numbers, not be quietly
imported with the bad rows dropped - they are the only one who can tell whether
a stray blank column is a mistake or a legitimately empty option.
"""

import csv
import io

from .models import Candidate, Word, db

# A generous ceiling. It exists so a mis-selected file cannot fill the disk
# before anyone notices, not because a real project could not be larger.
MAX_ROWS = 400_000
MAX_ITEMS = 200_000
MAX_TEXT = 2000
MAX_OPTIONS = 5

TEXT_HEADERS = {"text", "item", "phrase", "word", "sentence", "paragraph",
                "source", "english", "prompt"}
LANG_HEADERS = {"language", "lang", "language_code", "code", "target"}
PRIORITY_HEADERS = {"priority", "tier", "band", "group", "rank"}
MAX_PRIORITY = 20


def _clean(value):
    return " ".join((value or "").split())


ANY_LANGUAGE = "all"


def parse(stream_or_bytes, known_languages=None):
    """Read a CSV into items.

    Returns (items, problems, meta).

        items: [{"text", "item_language", "options": {code: [...]}, "priority"}]
        meta:  {"languages": {code, ...}, "any_language": bool}

    `known_languages` is every code that exists; anything else in the file is
    reported rather than guessed at. `meta["any_language"]` is set when a row
    said `all`, which means the project collects every language.

    `item_language` is set only when the item itself belongs to one language - a
    sentence to be transcribed in Ewe is an Ewe item, and Twi speakers should
    never see it. An item marked `all`, or appearing under several languages, is
    shared: the text reads the same whoever answers it.
    """
    if isinstance(stream_or_bytes, bytes):
        raw = stream_or_bytes
    else:
        raw = stream_or_bytes.read()
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return [], ["The file is not UTF-8 text. Save it as CSV UTF-8 and "
                        "upload it again - otherwise ɛ, ɔ and ŋ arrive broken."], {}
    else:
        text = raw

    reader = csv.reader(io.StringIO(text))
    problems = []
    order = []
    items = {}
    seen_pairs = set()
    has_language_column = False
    priority_at = None
    rows_read = 0
    named = set()
    any_language = False

    for line_no, raw_row in enumerate(reader, start=1):
        cells = [_clean(c) for c in raw_row]
        while cells and not cells[-1]:
            cells.pop()
        if not cells or not any(cells):
            continue

        if line_no == 1 and cells[0].lower() in TEXT_HEADERS:
            headers = [c.lower() for c in cells]
            has_language_column = len(headers) > 1 and headers[1] in LANG_HEADERS
            for index, name in enumerate(headers):
                if name in PRIORITY_HEADERS:
                    priority_at = index
            if not has_language_column:
                problems.append(
                    "The second column must be the language. The format is "
                    "text,language,option1,option2,… - see the example above.")
            continue

        rows_read += 1
        if rows_read > MAX_ROWS:
            problems.append(f"More than {MAX_ROWS:,} rows. Split the file.")
            break

        from .config import canonical_language

        item_text = cells[0]
        # Whatever they wrote, stored as the code we publish - so a file using
        # the codes anybody would look up works, including the two we had wrong.
        language = canonical_language(cells[1] if len(cells) > 1 else "")
        rest = cells[2:]

        priority = None
        if priority_at is not None:
            raw_priority = cells[priority_at] if priority_at < len(cells) else ""
            if raw_priority:
                try:
                    priority = int(raw_priority)
                except ValueError:
                    problems.append(f"Line {line_no}: priority “{raw_priority}” "
                                    "is not a whole number.")
                    continue
                if not 1 <= priority <= MAX_PRIORITY:
                    problems.append(f"Line {line_no}: priority must be between "
                                    f"1 and {MAX_PRIORITY}.")
                    continue
            rest = [c for i, c in enumerate(cells)
                    if i > 1 and i != priority_at]
        options = [c for c in rest if c]

        if not item_text:
            problems.append(f"Line {line_no}: no text in the first column.")
            continue
        if len(item_text) > MAX_TEXT:
            problems.append(f"Line {line_no}: text is longer than "
                            f"{MAX_TEXT} characters.")
            continue
        if not language:
            problems.append(
                f"Line {line_no}: no language. Put the language code here, or "
                f"“{ANY_LANGUAGE}” if this line is for every language.")
            continue
        if language != ANY_LANGUAGE and known_languages is not None \
                and language not in known_languages:
            hint = _closest(language, known_languages)
            problems.append(
                f"Line {line_no}: “{language}” is not a language code we know."
                + (f" Did you mean {hint}?" if hint else
                   " The codes are listed on this page."))
            continue
        if len(options) > MAX_OPTIONS:
            problems.append(f"Line {line_no}: {len(options)} options, more "
                            f"than the {MAX_OPTIONS} allowed.")
            continue
        if options and language == ANY_LANGUAGE:
            problems.append(
                f"Line {line_no}: options on an “{ANY_LANGUAGE}” line. Options "
                "are in one language, so name it.")
            continue
        if any(len(o) > MAX_TEXT for o in options):
            problems.append(f"Line {line_no}: an option is longer than "
                            f"{MAX_TEXT} characters.")
            continue

        key = item_text.casefold()
        if key not in items:
            if len(items) >= MAX_ITEMS:
                problems.append(f"More than {MAX_ITEMS:,} separate items. "
                                "Split the file.")
                break
            order.append(key)
            items[key] = {"text": item_text, "options": {}, "langs": set(),
                          "priority": priority, "any": False}
        entry = items[key]
        if priority is not None and entry["priority"] is None:
            entry["priority"] = priority

        if language == ANY_LANGUAGE:
            entry["any"] = True
            any_language = True
            continue

        named.add(language)
        entry["langs"].add(language)
        if options:
            if (key, language) in seen_pairs:
                problems.append(
                    f"Line {line_no}: options for “{item_text}” in {language} "
                    "were already given earlier.")
                continue
            seen_pairs.add((key, language))
            # One option is allowed: with a text field always on the screen it
            # reads as "is this right, or correct it", which is a perfectly good
            # thing to ask.
            entry["options"][language] = options

    out = []
    for key in order:
        entry = items[key]
        # An item belongs to one language only when that is all it ever appears
        # as: one language, never marked `all`, and carrying no options, which is
        # what a transcription or read-aloud task looks like.
        item_language = None
        if (not entry["any"] and not entry["options"]
                and len(entry["langs"]) == 1):
            item_language = next(iter(entry["langs"]))
        out.append({"text": entry["text"], "item_language": item_language,
                    "options": entry["options"],
                    "priority": entry["priority"] or 1})

    if not out and not problems:
        problems.append("The file has no rows in it.")
    return out, problems[:40], {"languages": named, "any_language": any_language}


def _closest(code, known):
    """A gentle suggestion for a mistyped code, or None."""
    import difflib
    hits = difflib.get_close_matches(code, list(known), n=1, cutoff=0.7)
    return hits[0] if hits else None


def languages_in(items):
    """Every language the file mentions, whether as options or as an item."""
    found = set()
    for item in items:
        found.update(item["options"])
        if item["item_language"]:
            found.add(item["item_language"])
    return found


def import_items(project, items, source="upload"):
    """Create the items and their options.

    One row per item, not per language: the options hang off it. Returns
    (items created, options created).
    """
    start = (db.session.query(db.func.coalesce(db.func.max(Word.position), 0))
             .filter(Word.project_id == project.id).scalar() or 0)
    made = options_made = 0
    for offset, entry in enumerate(items, start=1):
        item = Word(phrase=entry["text"], project_id=project.id,
                    language=entry["item_language"],
                    position=start + offset, occurrences=0, frequency=0.0,
                    # Priority is the tier: band 1 is finished before band 2 is
                    # started, the same mechanism the translation project uses.
                    tier=entry.get("priority") or 1)
        db.session.add(item)
        db.session.flush()
        for language, options in entry["options"].items():
            for slot, option in enumerate(options, start=1):
                db.session.add(Candidate(word_id=item.id, language=language,
                                         position=slot, text=option,
                                         source=source))
                options_made += 1
        made += 1
        if made % 2000 == 0:
            db.session.commit()
    db.session.commit()
    return made, options_made


def existing_texts(project):
    """Item text already in this project, to catch a file uploaded twice."""
    return {row[0].casefold() for row in
            db.session.query(Word.phrase).filter(Word.project_id == project.id)}
