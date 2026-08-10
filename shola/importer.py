"""Turning an uploaded CSV into items a volunteer can answer.

A project is a file per language. Each file is either

    text
    Where is the market?

for a project where volunteers type the answer themselves, or

    text,option1,option2,option3
    Where is the market?,Ɛhe na dwaso wɔ?,Dwaso wɔ he?,...

for one where they choose. Two to five options are accepted; the number does not
have to match between rows, because a real dataset rarely has the same number of
guesses for every line.

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
MAX_ROWS = 200_000
MAX_TEXT = 600
MAX_OPTIONS = 5

TEXT_HEADERS = {"text", "item", "phrase", "word", "sentence", "paragraph",
                "source", "english"}


def _clean(value):
    return " ".join((value or "").split())


def parse(stream_or_bytes, want_options=None):
    """Read a CSV into rows of (text, [options]).

    Returns (rows, problems). `problems` is a list of human-readable strings
    naming line numbers; an empty list means the file is usable.

    `want_options` forces a shape: True to insist every row carries at least
    two options, False to insist the file is a single column. None accepts
    either and reports which it found.
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
                        "upload it again - otherwise ɛ, ɔ and ŋ arrive broken."]
    else:
        text = raw

    reader = csv.reader(io.StringIO(text))
    rows, problems = [], []
    seen = set()
    header_skipped = False

    for line_no, raw_row in enumerate(reader, start=1):
        cells = [_clean(c) for c in raw_row]
        while cells and not cells[-1]:
            cells.pop()
        if not cells or not any(cells):
            continue

        if (not header_skipped and line_no == 1
                and cells[0].lower() in TEXT_HEADERS):
            header_skipped = True
            continue

        item, options = cells[0], [c for c in cells[1:] if c]

        if not item:
            problems.append(f"Line {line_no}: no text in the first column.")
            continue
        if len(item) > MAX_TEXT:
            problems.append(f"Line {line_no}: text is longer than {MAX_TEXT} "
                            "characters.")
            continue
        if len(options) > MAX_OPTIONS:
            problems.append(f"Line {line_no}: {len(options)} options, "
                            f"more than the {MAX_OPTIONS} allowed.")
            continue
        if len(options) == 1:
            problems.append(f"Line {line_no}: one option on its own. Give at "
                            "least two to choose between, or none at all so "
                            "volunteers type the answer.")
            continue
        if any(len(o) > MAX_TEXT for o in options):
            problems.append(f"Line {line_no}: an option is longer than "
                            f"{MAX_TEXT} characters.")
            continue

        key = item.casefold()
        if key in seen:
            problems.append(f"Line {line_no}: repeats an earlier line.")
            continue
        seen.add(key)

        rows.append((item, options))
        if len(rows) > MAX_ROWS:
            problems.append(f"More than {MAX_ROWS:,} rows. Split the file.")
            break

    if not rows and not problems:
        problems.append("The file has no rows in it.")

    with_options = sum(1 for _t, o in rows if o)
    if rows and 0 < with_options < len(rows):
        problems.append(
            f"{with_options} of {len(rows)} lines have options and the rest do "
            "not. Either every line offers a choice or none of them do - a mix "
            "would mean some volunteers choose and others type, for the same "
            "job.")
    if want_options is True and not with_options:
        problems.append("This project was set up for volunteers to choose "
                        "between options, but the file has none.")
    if want_options is False and with_options:
        problems.append("This project was set up for volunteers to type the "
                        "answer, but the file carries options.")

    return rows, problems[:40]


def import_rows(project, language, rows, source="upload"):
    """Create items and their options for one language of one project.

    `language` may be None for a project whose items read the same whatever
    language the answer is in.
    """
    start = (db.session.query(db.func.coalesce(db.func.max(Word.position), 0))
             .filter(Word.project_id == project.id).scalar() or 0)
    made = 0
    for offset, (text, options) in enumerate(rows, start=1):
        item = Word(phrase=text, project_id=project.id, language=language,
                    position=start + offset, occurrences=0, frequency=0.0,
                    tier=5)
        db.session.add(item)
        db.session.flush()
        for slot, option in enumerate(options, start=1):
            db.session.add(Candidate(word_id=item.id,
                                     language=language or project.language_codes[0],
                                     position=slot, text=option, source=source))
        made += 1
    db.session.commit()
    return made


def existing_texts(project):
    """Item text already in this project, to catch a file uploaded twice."""
    return {row[0].casefold() for row in
            db.session.query(Word.phrase).filter(Word.project_id == project.id)}
