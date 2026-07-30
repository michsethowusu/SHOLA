"""Command line: import words, send the daily emails, export consensus.

Typical operation:

    flask --app wsgi shola import-words --jsonl ../GhanaNouns/data/.translations.jsonl
    flask --app wsgi shola send-daily --window morning     # from cron, hourly
    flask --app wsgi shola export --language twi > twi-agreed.csv
"""

import csv
import json
import sys
from datetime import date

import click
from flask import current_app
from flask.cli import AppGroup

from . import consensus
from .assignment import redistribute
from .models import Candidate, Volunteer, Word, db, site_stats

shola_cli = AppGroup("shola", help="SHOLA operations.")


def _upsert_word(phrase, per_language, seen):
    """Add a word and its candidate translations. Returns True if new."""
    if phrase in seen:
        return False
    word = Word.query.filter_by(phrase=phrase).first()
    if word:
        seen.add(phrase)
        return False
    word = Word(phrase=phrase)
    db.session.add(word)
    db.session.flush()
    for language, variants in per_language.items():
        for i, text in enumerate(variants[:3], start=1):
            text = (text or "").strip()
            if text:
                db.session.add(Candidate(word_id=word.id, language=language,
                                         position=i, text=text))
    seen.add(phrase)
    return True


@shola_cli.command("import-words")
@click.option("--csv", "csv_path", type=click.Path(exists=True),
              help="ghana-nouns-translated.csv with <lang>_1..3 columns.")
@click.option("--jsonl", "jsonl_path", type=click.Path(exists=True),
              help="translations.jsonl with one JSON object per noun.")
@click.option("--limit", type=int, default=0, help="stop after N words.")
def import_words(csv_path, jsonl_path, limit):
    """Load words and their candidate translations."""
    if not csv_path and not jsonl_path:
        raise click.UsageError("pass --csv or --jsonl")

    languages = list(current_app.config["LANGUAGES"])
    seen, added, batch = set(), 0, 0

    def flush():
        db.session.commit()

    if jsonl_path:
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                phrase = (rec.get("phrase") or "").strip()
                if not phrase:
                    continue
                per_lang = {L: [str(v) for v in (rec.get(L) or [])]
                            for L in languages}
                if _upsert_word(phrase, per_lang, seen):
                    added += 1
                    batch += 1
                if batch >= 500:
                    flush()
                    batch = 0
                    click.echo(f"  {added:,} imported", err=True)
                if limit and added >= limit:
                    break
    else:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                phrase = (rec.get("phrase") or "").strip()
                if not phrase:
                    continue
                per_lang = {L: [rec.get(f"{L}_{i}", "") for i in (1, 2, 3)]
                            for L in languages}
                if _upsert_word(phrase, per_lang, seen):
                    added += 1
                    batch += 1
                if batch >= 500:
                    flush()
                    batch = 0
                    click.echo(f"  {added:,} imported", err=True)
                if limit and added >= limit:
                    break
    flush()
    click.echo(f"imported {added:,} words; {Word.query.count():,} in total")


@shola_cli.command("send-daily")
@click.option("--window", default="all",
              help="only volunteers who chose this time window, or 'all'.")
@click.option("--dry-run", is_flag=True, help="print instead of sending.")
@click.option("--force", is_flag=True, help="ignore today's already-sent mark.")
def send_daily(window, dry_run, force):
    """Email each volunteer the words due today, plus anything they missed."""
    from .mailer import build_daily_email, send

    today = date.today()
    query = Volunteer.query.filter(Volunteer.active.is_(True))
    if window != "all":
        query = query.filter(Volunteer.time_window.in_([window, "anytime"]))

    sent = skipped = failed = 0
    for volunteer in query.all():
        if not force and volunteer.last_emailed_on == today:
            skipped += 1
            continue
        if volunteer.day_numbers and today.weekday() not in volunteer.day_numbers:
            # Not one of their days; overdue work waits for the next one.
            skipped += 1
            continue

        due = volunteer.pending_today(today).limit(400).all()
        if not due:
            skipped += 1
            continue
        words = [a.word for a in due]
        overdue = sum(1 for a in due if a.due_date < today)

        subject, text, html = build_daily_email(volunteer, words, overdue)
        if dry_run:
            click.echo(f"[dry-run] {volunteer.email}: {subject} "
                       f"({len(words)} words, {overdue} overdue)")
            sent += 1
            continue
        try:
            send(volunteer.email, subject, text, html)
            volunteer.last_emailed_on = today
            db.session.commit()
            sent += 1
        except Exception as exc:      # noqa: BLE001 - keep going, report at end
            failed += 1
            click.echo(f"  failed {volunteer.email}: {exc}", err=True)

    click.echo(f"sent {sent}, skipped {skipped}, failed {failed}")
    if failed:
        sys.exit(1)


@shola_cli.command("redistribute-missed")
@click.option("--dry-run", is_flag=True)
def redistribute_missed(dry_run):
    """Spread overdue words across each volunteer's remaining days."""
    total = 0
    for volunteer in Volunteer.query.filter(Volunteer.active.is_(True)).all():
        if dry_run:
            n = sum(1 for a in volunteer.pending_today()
                    if a.due_date < date.today())
            if n:
                click.echo(f"[dry-run] {volunteer.email}: {n} overdue")
            total += n
            continue
        moved = redistribute(volunteer)
        if moved:
            click.echo(f"  {volunteer.email}: moved {moved}")
        total += moved
    click.echo(f"{'would move' if dry_run else 'moved'} {total} assignments")


@shola_cli.command("export")
@click.option("--language", required=True)
@click.option("--min-votes", default=2, show_default=True)
@click.option("--out", type=click.Path(), default="-")
def export(language, min_votes, out):
    """Write the agreed translations for one language as CSV."""
    if language not in current_app.config["LANGUAGES"]:
        raise click.UsageError(f"unknown language {language!r}")
    fh = sys.stdout if out == "-" else open(out, "w", newline="",
                                            encoding="utf-8")
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(["phrase", language, "votes", "agreement", "total_votes"])
    n = 0
    for row in consensus.export_rows(language, min_votes=min_votes):
        w.writerow(row)
        n += 1
    if fh is not sys.stdout:
        fh.close()
        click.echo(f"wrote {n:,} agreed translations -> {out}")


@shola_cli.command("stats")
def stats_cmd():
    """Print a summary of where the project stands."""
    s = site_stats()
    click.echo(f"volunteers   {s['volunteers']:,}")
    click.echo(f"words        {s['words']:,}")
    click.echo(f"verdicts     {s['verdicts']:,}")
    click.echo(f"words seen   {s['covered']:,} ({s['coverage_pct']:.1f}%)")
    for language, d in sorted(consensus.language_progress().items()):
        click.echo(f"  {language:9s} {d['verdicts']:>7,} verdicts, "
                   f"{d['agreed']:>7,} with 2+ votes")
