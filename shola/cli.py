"""Command line: import words, send the daily emails, export consensus.

Typical operation:

    flask --app wsgi shola import-words --jsonl ../GhanaNouns/data/.translations.jsonl
    flask --app wsgi shola send-daily --window morning     # from cron, hourly
    flask --app wsgi shola export --language twi > twi-agreed.csv
"""

import csv
import glob
import gzip
import json
import os
import sys
from datetime import date

import click
from flask import current_app
from flask.cli import AppGroup

from . import consensus
from .assignment import redistribute
from .models import Candidate, Volunteer, Word, db, site_stats
from .tiers import (active_tier, assign_tiers, refresh_word, release_expired,
                    tier_for, tier_progress, top_up)

shola_cli = AppGroup("shola", help="SHOLA operations.")


def open_maybe_gz(path):
    """Open a text file, transparently handling .gz."""
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, newline="", encoding="utf-8")


def load_frequencies(path):
    """phrase -> (percentage, raw occurrences).

    Occurrences drive the tiers: the percentage column is rounded to four
    decimals, so 91% of words tie at 0.0000 and it cannot order the long tail.
    """
    freqs = {}
    with open_maybe_gz(path) as fh:
        for rec in csv.DictReader(fh):
            try:
                pct = float(rec.get("average_percentage") or 0)
            except (TypeError, ValueError):
                pct = 0.0
            total = 0
            for col in ("news_count", "research_count", "speech_count"):
                try:
                    total += int(float(rec.get(col) or 0))
                except (TypeError, ValueError):
                    pass
            freqs[rec["phrase"]] = (pct, total)
    return freqs


def _upsert_word(phrase, per_language, seen, freq=(0.0, 0)):
    """Add a word and its candidate translations. Returns True if new."""
    if phrase in seen:
        return False
    word = Word.query.filter_by(phrase=phrase).first()
    if word:
        seen.add(phrase)
        return False
    pct, occurrences = freq
    word = Word(phrase=phrase, frequency=pct, occurrences=occurrences,
                tier=tier_for(occurrences))
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
@click.option("--freq-csv", type=click.Path(exists=True),
              help="ghana-nouns.csv, to carry over each word's corpus "
                   "frequency so common words are evaluated first.")
@click.option("--limit", type=int, default=0, help="stop after N words.")
def import_words(csv_path, jsonl_path, freq_csv, limit):
    """Load words and their candidate translations."""
    if not csv_path and not jsonl_path:
        raise click.UsageError("pass --csv or --jsonl")

    languages = list(current_app.config["LANGUAGES"])
    freqs = {}
    if freq_csv:
        freqs = load_frequencies(freq_csv)
        click.echo(f"loaded frequencies for {len(freqs):,} phrases")
    seen, added, batch = set(), 0, 0

    def flush():
        db.session.commit()

    if jsonl_path:
        with open_maybe_gz(jsonl_path) as fh:
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
                if _upsert_word(phrase, per_lang, seen, freqs.get(phrase, (0.0, 0))):
                    added += 1
                    batch += 1
                if batch >= 500:
                    flush()
                    batch = 0
                    click.echo(f"  {added:,} imported", err=True)
                if limit and added >= limit:
                    break
    else:
        with open_maybe_gz(csv_path) as fh:
            for rec in csv.DictReader(fh):
                phrase = (rec.get("phrase") or "").strip()
                if not phrase:
                    continue
                per_lang = {L: [rec.get(f"{L}_{i}", "") for i in (1, 2, 3)]
                            for L in languages}
                if _upsert_word(phrase, per_lang, seen, freqs.get(phrase, (0.0, 0))):
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

    open_langs = current_app.config["LANGUAGES"]
    sent = skipped = failed = 0
    for volunteer in query.all():
        # Nothing to send to someone whose language has not opened yet.
        if volunteer.is_waiting(open_langs):
            skipped += 1
            continue
        if not force and volunteer.last_emailed_on == today:
            skipped += 1
            continue
        if volunteer.day_numbers and today.weekday() not in volunteer.day_numbers:
            # Not one of their days; overdue work waits for the next one.
            skipped += 1
            continue

        # Lease today's words first: nothing is reserved in advance any more.
        top_up(volunteer, target=current_app.config["WORDS_PER_VOLUNTEER"],
               today=today)
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


@shola_cli.command("assign-tiers")
def assign_tiers_cmd():
    """Recompute every word's tier from its occurrence count."""
    n = assign_tiers()
    click.echo(f"tiered {n:,} words")
    for row in tier_progress():
        click.echo(f"  tier {row['tier']}  {row['total']:>8,} words")


@shola_cli.command("tier-status")
def tier_status():
    """Show how far each language has got through the tiers."""
    for code, lang in current_app.config["LANGUAGES"].items():
        current = active_tier(code)
        click.echo(f"\n{lang['name']} — " + (f"working on tier {current}"
                                             if current else "all tiers closed"))
        for row in tier_progress(code):
            mark = " <-- current" if row["tier"] == current else ""
            click.echo(f"  tier {row['tier']}  {row['done']:>8,} settled  "
                       f"{row['contested']:>6,} contested  "
                       f"{row['left']:>8,} to go  "
                       f"({row['pct']:.1f}% of {row['total']:,}){mark}")


@shola_cli.command("release-leases")
def release_leases_cmd():
    """Return unanswered leased words to the queue."""
    n = release_expired()
    click.echo(f"released {n:,} expired leases")


@shola_cli.command("refresh-words")
@click.option("--all", "do_all", is_flag=True,
              help="recompute every word, not just those with verdicts.")
def refresh_words_cmd(do_all):
    """Rebuild vote state from the verdicts on record, per language."""
    from .models import Evaluation
    pairs = db.session.query(Evaluation.word_id, Evaluation.language).distinct()
    n = 0
    for word_id, language in pairs:
        refresh_word(word_id, language, commit=False)
        n += 1
        if n % 2000 == 0:
            db.session.commit()
    db.session.commit()
    click.echo(f"refreshed {n:,} word/language pairs")


@shola_cli.command("waitlist")
def waitlist():
    """Who is waiting, and for which language.

    Use it to decide which language to open next: the number here is people
    ready to start the day it does.
    """
    from collections import Counter

    open_langs = current_app.config["LANGUAGES"]
    names = {c: n for c, n, _a in current_app.config["OTHER_LANGUAGES"]}
    waiting = Counter(v.language for v in Volunteer.query.all()
                      if v.is_waiting(open_langs))
    if not waiting:
        click.echo("nobody is waiting")
        return
    click.echo(f"{sum(waiting.values())} waiting across "
               f"{len(waiting)} languages:")
    for code, n in waiting.most_common():
        click.echo(f"  {names.get(code, code):24s} {n:>4}")


@shola_cli.command("announce-language")
@click.option("--language", required=True,
              help="the code as it now appears in LANGUAGES.")
@click.option("--dry-run", is_flag=True)
def announce_language(language, dry_run):
    """Tell everyone waiting for a language that it has opened.

    Run this once, after adding the language to LANGUAGES and importing its
    translations. The waiting page promises this email, so opening a language
    without sending it breaks that promise.
    """
    from .mailer import build_opened_email, daily_link, send

    open_langs = current_app.config["LANGUAGES"]
    if language not in open_langs:
        raise click.UsageError(
            f"{language!r} is not open yet. Add it to LANGUAGES and import its "
            "translations first, or nobody will have words to check.")

    people = Volunteer.query.filter_by(language=language, active=True).all()
    if not people:
        click.echo("nobody was waiting for that language")
        return

    name = open_langs[language]["name"]
    sent = failed = 0
    for volunteer in people:
        if not dry_run:
            top_up(volunteer, target=current_app.config["WORDS_PER_VOLUNTEER"])
        subject, text, html = build_opened_email(volunteer, name,
                                                 daily_link(volunteer))
        if dry_run:
            click.echo(f"[dry-run] {volunteer.email}: {subject}")
            sent += 1
            continue
        try:
            send(volunteer.email, subject, text, html)
            sent += 1
        except Exception as exc:      # noqa: BLE001
            failed += 1
            click.echo(f"  failed {volunteer.email}: {exc}", err=True)
    click.echo(f"told {sent} people {name} is open; {failed} failed")


@shola_cli.command("backup")
@click.option("--out", default="instance/backups", show_default=True)
@click.option("--keep", default=14, show_default=True,
              help="how many previous backups to retain.")
def backup(out, keep):
    """Back up the database, and separately the part that cannot be rebuilt.

    Coolify's scheduled backups only cover the databases it manages, so a
    SQLite file inside an application volume is not backed up by anything. This
    writes two things:

      * a consistent copy of the whole database, compressed
      * volunteers and their answers as JSON

    The JSON matters more than its size suggests. Every word and translation
    can be re-imported from the published dataset in minutes, but a volunteer's
    email, their chosen days and the answers they gave exist nowhere else. That
    file is a few hundred kilobytes and is the only irreplaceable part.
    """
    import gzip as _gzip
    import shutil
    import sqlite3
    from datetime import datetime as _dt

    from .models import Evaluation, Volunteer

    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    if not uri.startswith("sqlite"):
        raise click.UsageError("this command backs up SQLite databases only")
    db_path = uri.split("sqlite:///")[-1]

    out_dir = os.path.abspath(out)
    os.makedirs(out_dir, exist_ok=True)
    stamp = _dt.utcnow().strftime("%Y%m%d-%H%M%S")

    # sqlite3's backup API copies a live database consistently; copying the
    # file by hand can catch it mid-write.
    tmp = os.path.join(out_dir, f".shola-{stamp}.db")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()

    db_gz = os.path.join(out_dir, f"shola-{stamp}.db.gz")
    with open(tmp, "rb") as f, _gzip.open(db_gz, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g, 1 << 20)
    os.remove(tmp)

    people = []
    for v in Volunteer.query.all():
        people.append({
            "name": v.name, "email": v.email, "language": v.language,
            "available_days": v.available_days, "time_window": v.time_window,
            "joined_at": v.joined_at.isoformat() if v.joined_at else None,
            "photo": v.photo, "photo_consent": v.photo_consent,
            "active": v.active,
            "answers": [
                {"phrase": e.word.phrase, "language": e.language,
                 "chose": e.chosen_text, "skipped": e.skipped,
                 "at": e.created_at.isoformat() if e.created_at else None}
                for e in v.evaluations
            ],
        })
    people_path = os.path.join(out_dir, f"volunteers-{stamp}.json.gz")
    with _gzip.open(people_path, "wt", encoding="utf-8") as fh:
        json.dump({"exported_at": stamp, "volunteers": people}, fh,
                  ensure_ascii=False, indent=1)

    for pattern in ("shola-*.db.gz", "volunteers-*.json.gz"):
        old = sorted(glob.glob(os.path.join(out_dir, pattern)))[:-keep or None]
        for f in old:
            os.remove(f)

    click.echo(f"database  {os.path.getsize(db_gz)//1048576} MB  {db_gz}")
    click.echo(f"people    {os.path.getsize(people_path)//1024} KB  "
               f"{len(people)} volunteers, "
               f"{sum(len(p['answers']) for p in people)} answers")
    click.echo(f"keeping the {keep} most recent of each")

    # Off-site, if credentials are present. Done here rather than through
    # Coolify's S3 feature so a backup does not depend on a helper container
    # that has been failing silently.
    if os.environ.get("SHOLA_S3_BUCKET"):
        try:
            uploaded = upload_to_s3([db_gz, people_path], keep=keep)
            for key in uploaded:
                click.echo(f"uploaded  {key}")
        except Exception as exc:      # noqa: BLE001 - never fail the local backup
            click.echo(f"S3 upload FAILED: {exc}", err=True)
            raise SystemExit(1)
    else:
        click.echo("no SHOLA_S3_BUCKET set, so this backup stays on this host")


def upload_to_s3(paths, keep=14, prefix=None):
    """Copy backups to S3-compatible storage and prune old remote copies.

    Raises on failure. A backup that silently fails to leave the machine is
    worse than no backup, because it looks like protection that is not there.
    """
    import boto3
    from botocore.config import Config

    bucket = os.environ["SHOLA_S3_BUCKET"]
    prefix = (prefix or os.environ.get("SHOLA_S3_PREFIX", "shola")).strip("/")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ.get("SHOLA_S3_ENDPOINT") or None,
        aws_access_key_id=os.environ["SHOLA_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["SHOLA_S3_SECRET_KEY"],
        region_name=os.environ.get("SHOLA_S3_REGION", "auto"),
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )

    keys = []
    for path in paths:
        key = f"{prefix}/{os.path.basename(path)}"
        client.upload_file(path, bucket, key)
        # Read it back: an upload that reports success but stores nothing is
        # exactly the failure mode this command exists to avoid.
        head = client.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] != os.path.getsize(path):
            raise RuntimeError(f"{key} uploaded at the wrong size")
        keys.append(key)

    for stem in ("shola-", "volunteers-"):
        listed = client.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{stem}")
        objs = sorted(listed.get("Contents", []), key=lambda o: o["Key"])
        for obj in objs[:-keep or None]:
            client.delete_object(Bucket=bucket, Key=obj["Key"])
    return keys
