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
import re
import sys
from pathlib import Path
from datetime import date, datetime, time, timedelta

# A word shows at most this many options; more is a wall, not a choice.
MAX_OPTIONS = 5

import click
from flask import current_app
from flask.cli import AppGroup

from . import consensus
from .scoreboard import split_sources
from .models import Candidate, Evaluation, Volunteer, Word, db, site_stats
from .tiers import (active_tier, assign_tiers, daily_quota, refresh_word,
                    release_expired, tier_for, tier_progress, top_up)

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


def _upsert_word(phrase, per_language, seen, freq=(0.0, 0), project_id=None):
    """Add a word and its candidate translations. Returns True if new.

    Scoped to the translation project: item text is unique within a project
    now, not across the whole table, so a word here must not be confused with
    the same text uploaded to a different project.
    """
    if phrase in seen:
        return False
    word = Word.query.filter_by(phrase=phrase, project_id=project_id).first()
    if word:
        seen.add(phrase)
        return False
    pct, occurrences = freq
    tier = tier_for(occurrences)
    if tier is None:
        # Under MIN_OCCURRENCES: too rare in the corpus to be worth an answer.
        seen.add(phrase)
        return False
    word = Word(phrase=phrase, frequency=pct, occurrences=occurrences,
                tier=tier, project_id=project_id)
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

    # These words belong to the translation project. Set here rather than left
    # for the boot-time migration to sweep up, so the rows are right the moment
    # they are written.
    from .models import CORE_PROJECT, Project
    core = Project.query.filter_by(slug=CORE_PROJECT["slug"]).first()
    core_id = core.id if core else None

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
                if _upsert_word(phrase, per_lang, seen,
                                freqs.get(phrase, (0.0, 0)),
                                project_id=core_id):
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
                if _upsert_word(phrase, per_lang, seen,
                                freqs.get(phrase, (0.0, 0)),
                                project_id=core_id):
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
    """Email each volunteer a fresh list of words for today."""
    from .mailer import build_daily_email, build_weekly_offer_email, send

    today = date.today()
    cfg = current_app.config
    query = Volunteer.query.filter(Volunteer.active.is_(True))
    if window != "all":
        query = query.filter(Volunteer.time_window.in_([window, "anytime"]))
    # A pause has an end date and clears itself, so it is filtered here rather
    # than by flipping `active` and hoping someone remembers to flip it back.
    query = query.filter(db.or_(Volunteer.paused_until.is_(None),
                                Volunteer.paused_until <= today))

    sent = skipped = failed = backing_off = 0
    for volunteer in query.all():
        if not force and volunteer.last_emailed_on == today:
            skipped += 1
            continue
        if volunteer.day_numbers and today.weekday() not in volunteer.day_numbers:
            skipped += 1
            continue

        # --- backing off ----------------------------------------------------
        # Sending behaves like a client talking to a service that is not
        # answering: each unanswered send lengthens the wait before the next
        # attempt, and answering anything clears it. The interval stretches; it
        # never becomes silence, because an attempt is the only thing that gives
        # them something to answer.
        waiting = bool(volunteer.next_send_on
                       and today < volunteer.next_send_on)
        if waiting and not force:
            backing_off += 1
            continue

        # Did the last send go unanswered?
        #
        # Compared as a datetime on purpose: created_at is a timestamp, and
        # leaning on string comparison against a bare date would be an accident
        # waiting for a database that stores dates differently.
        since = (datetime.combine(volunteer.last_emailed_on, time.min)
                 if volunteer.last_emailed_on else None)
        missed_last_send = bool(
            since and not volunteer.evaluations.filter(
                Evaluation.created_at >= since).first())

        # A wait that has come due is the retry. It is not re-penalised: the
        # miss that caused it was charged when the wait was set, and charging it
        # again on every attempt would push the next send out for ever and turn
        # a back-off into an abandonment.
        retrying = bool(volunteer.next_send_on
                        and today >= volunteer.next_send_on)

        # A miss is charged once, at the moment it is noticed, and that is when
        # the wait is set. --force means "send now regardless", so it still
        # charges the miss but does not wait.
        if missed_last_send and not retrying:
            volunteer.missed_in_a_row += 1
            volunteer.backoff_days = min(volunteer.missed_in_a_row,
                                         cfg["MAX_BACKOFF_DAYS"])
            volunteer.next_send_on = today + timedelta(
                days=volunteer.backoff_days)
            db.session.commit()
            if not force:
                click.echo(f"back off  {volunteer.email}: "
                           f"{volunteer.backoff_days} day(s), next attempt "
                           f"{volunteer.next_send_on}")
                backing_off += 1
                continue

        misses = volunteer.missed_in_a_row if missed_last_send else 0

        # A wrong schedule is worth one suggestion, not a weekly reminder that
        # they are behind.
        nudge = (misses >= cfg["MISSES_BEFORE_NUDGE"]
                 and len(volunteer.day_numbers or []) != 1
                 and volunteer.nudged_on is None)

        # A fresh list. Anything from an earlier day goes back to the queue, so
        # missing days never builds a backlog to work through.
        # A send is where a stale list is handed back and a new one built.
        top_up(volunteer, today=today, new_list=True)
        due = volunteer.pending_today(today).limit(daily_quota(volunteer)).all()
        if not due and not nudge:
            skipped += 1
            continue
        words = [a.word for a in due]

        if nudge:
            subject, text, html = build_weekly_offer_email(volunteer, words)
        else:
            subject, text, html = build_daily_email(volunteer, words)
        if dry_run:
            click.echo(f"[dry-run] {volunteer.email}: {subject} "
                       f"({len(words)} words"
                       + (", offering weekly" if nudge else "") + ")")
            sent += 1
            continue
        try:
            send(volunteer.email, subject, text, html)
            volunteer.last_emailed_on = today
            volunteer.missed_in_a_row = misses
            if not missed_last_send:
                # They answered: the schedule they chose resumes.
                volunteer.backoff_days = 0
            volunteer.next_send_on = None
            if nudge:
                volunteer.nudged_on = today
            db.session.commit()
            sent += 1
        except Exception as exc:      # noqa: BLE001 - keep going, report at end
            failed += 1
            click.echo(f"  failed {volunteer.email}: {exc}", err=True)

    click.echo(f"sent {sent}, skipped {skipped}, "
               f"backing off {backing_off}, failed {failed}")

    # A dated marker, the same idea as the backup one, for the same reason.
    # Every scheduled task on this deployment failed silently for a week - the
    # send included - because each one crashes at boot if the app does, and
    # nothing anybody looks at said so. /healthz reads this.
    if not dry_run:
        try:
            marker = Path(current_app.config["UPLOAD_DIR"]).parent / "last-send.txt"
            marker.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        except OSError as exc:      # noqa: BLE001 - never fail a send over this
            click.echo(f"could not write the send marker: {exc}", err=True)

    if failed:
        sys.exit(1)


@shola_cli.command("export-typed")
@click.option("--slug", required=True)
@click.option("--language", required=True)
@click.option("--out", type=click.Path(), default="-")
def export_typed(slug, language, out):
    """Write the answers volunteers typed, which are never verified."""
    from .consensus import typed_rows
    from .models import Project

    project = Project.query.filter_by(slug=slug).first()
    if not project:
        raise click.UsageError(f"no project {slug!r}")
    fh = sys.stdout if out == "-" else open(out, "w", newline="",
                                           encoding="utf-8")
    writer = csv.writer(fh, lineterminator="\n")
    writer.writerow(["item", "typed_answer", "answered_on"])
    n = 0
    for row in typed_rows(language, project_id=project.id):
        writer.writerow(row)
        n += 1
    if fh is not sys.stdout:
        fh.close()
        click.echo(f"{n} typed answers -> {out}")


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
    from sqlalchemy import func

    n = assign_tiers()
    click.echo(f"tiered {n:,} words")
    # Totals across the whole table. tier_progress() answers this per language,
    # which is not the question here: a tier holds the same words whoever is
    # working on it.
    rows = (db.session.query(Word.tier, func.count(Word.id))
            .group_by(Word.tier).order_by(Word.tier).all())
    for tier, total in rows:
        label = "withdrawn" if tier == 0 else f"tier {tier}   "
        click.echo(f"  {label}  {total:>8,} words")


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


@shola_cli.command("languages")
def languages_cmd():
    """Volunteers per language, and whether that language has options yet."""
    from collections import Counter

    from .models import Candidate

    all_langs = current_app.config["ALL_LANGUAGES"]
    signed = Counter(v.language for v in Volunteer.query.all())
    with_options = {r[0] for r in db.session.query(
        db.distinct(Candidate.language))}

    click.echo(f"{len(all_langs)} languages, "
               f"{len(with_options)} with options to vote on\n")
    for code, info in sorted(all_langs.items(), key=lambda kv: kv[1]["name"]):
        n = signed.get(code, 0)
        if not n and code not in with_options:
            continue
        mark = "has options" if code in with_options else "empty, awaiting a first speaker"
        click.echo(f"  {info['name']:24s} {n:>4} volunteers   {mark}")


@shola_cli.command("reset-backoff")
@click.option("--email", default=None,
              help="One volunteer. Everyone, if left out.")
@click.option("--yes", is_flag=True, help="Do it, rather than listing first.")
def reset_backoff_cmd(email, yes):
    """Clear the back-off state after an outage on our side.

    Back-off assumes a quiet volunteer chose to be quiet. When the sends
    themselves have been failing, that assumption is wrong and the people who
    were owed a list get their next one pushed further away for it. Missing
    days was never supposed to cost anybody anything; being charged for our
    downtime is worse than that.
    """
    q = Volunteer.query.filter(
        db.or_(Volunteer.missed_in_a_row > 0,
               Volunteer.backoff_days > 0,
               Volunteer.next_send_on.isnot(None),
               Volunteer.last_emailed_on.isnot(None)))
    if email:
        q = q.filter(Volunteer.email == email.strip().lower())

    affected = q.all()
    if not affected:
        click.echo("Nobody is backed off.")
        return
    for v in affected:
        click.echo(f"  {v.email:36} missed={v.missed_in_a_row} "
                   f"backoff={v.backoff_days}d "
                   f"next={v.next_send_on or '-'}")
    if not yes:
        click.echo(f"\n{len(affected)} volunteer(s). Re-run with --yes to "
                   "clear it and send them on their normal schedule.")
        return
    for v in affected:
        v.missed_in_a_row = 0
        v.backoff_days = 0
        v.next_send_on = None
        v.nudged_on = None
        # The one that actually matters. A miss is "we emailed you and you did
        # not answer", read off last_emailed_on, so clearing the counters alone
        # changes nothing: the next send looks at that same stale date, sees no
        # answer since, and backs the volunteer off again. Forgetting the send
        # is what makes it a clean slate.
        #
        # Only an older one, though. The same field stops a volunteer being
        # emailed twice in a day, and clearing it wholesale sent three people a
        # second list minutes after their first.
        if v.last_emailed_on and v.last_emailed_on < date.today():
            v.last_emailed_on = None
    db.session.commit()
    click.echo(f"\nCleared for {len(affected)} volunteer(s).")


@shola_cli.command("retract-options")
@click.option("--source", required=True,
              help="The system withdrawing its options, e.g. 'gemini-3.6-flash'.")
@click.option("--language", multiple=True,
              help="Limit to these languages. Default: all of them.")
@click.option("--yes", is_flag=True, help="Do it, rather than counting first.")
def retract_options_cmd(source, language, yes):
    """Take a system's name off every option it proposed.

    For replacing one system's output with a fresh run rather than adding to
    it. Gemini was asked for three wordings per word while Google, Gemma and
    NLLB each give one, so it had three chances to match what a speaker says -
    its pick rate measured the extra chances as much as the translation. This
    clears the old claim so a one-wording run can be imported over it.

    It does not delete options a speaker has already answered against. Deleting
    a Candidate a verdict points at would erase that answer's wording and take
    the other systems' scores down with it. Those rows stay, marked `retracted`
    so nothing is scored for them. An option nobody has touched and nobody else
    claims is deleted outright.

    Run `add-options` with the new file afterwards. A wording that comes back
    identical is credited again, so the system keeps that score; one that comes
    back different does not.
    """
    from .models import Candidate, Evaluation

    q = db.session.query(Candidate.id, Candidate.source, Candidate.language)
    if language:
        q = q.filter(Candidate.language.in_(language))
    mine, freed, kept = [], [], 0
    for cid, src, lang in q:
        names = [p.strip() for p in (src or "").split(";") if p.strip()]
        if source not in names:
            continue
        rest = [n for n in names if n != source]
        mine.append((cid, ";".join(rest)))
        if rest:
            kept += 1
        else:
            freed.append(cid)

    # Of the ones left with no owner, which has a verdict pointing at it.
    answered = set()
    for start in range(0, len(freed), 5000):
        chunk = freed[start:start + 5000]
        answered |= {row[0] for row in
                     db.session.query(Evaluation.candidate_id)
                     .filter(Evaluation.candidate_id.in_(chunk)).distinct()}

    click.echo(f"{source}:")
    click.echo(f"  options claimed      {len(mine):>9,}")
    click.echo(f"  shared with others   {kept:>9,}  (name removed, option stays)")
    click.echo(f"  sole, unanswered     {len(freed) - len(answered):>9,}  (deleted)")
    click.echo(f"  sole, answered       {len(answered):>9,}  (kept, marked retracted)")
    if not yes:
        click.echo("\nRe-run with --yes to do it, then add-options with the "
                   "new file.")
        return
    if not mine:
        return

    to_delete = [cid for cid in freed if cid not in answered]
    delete_set = set(to_delete)
    updates = [{"id": cid, "source": rest or "retracted"}
               for cid, rest in mine if cid not in delete_set]
    for start in range(0, len(updates), 5000):
        db.session.bulk_update_mappings(Candidate, updates[start:start + 5000])
        db.session.commit()
    for start in range(0, len(to_delete), 5000):
        (Candidate.query.filter(Candidate.id.in_(to_delete[start:start + 5000]))
         .delete(synchronize_session=False))
        db.session.commit()
    click.echo(f"Deleted {len(to_delete):,}, re-labelled "
               f"{len(updates):,}. {source} now claims nothing.")


@shola_cli.command("sources")
@click.option("--language", help="Break one language down by tier instead.")
def sources_cmd(language):
    """What each system has actually put in the database.

    For checking an import landed. `add-options` reports what it wrote, but
    only this says what is there afterwards - and the two differ whenever a
    wording was already present, which is most of them once four systems are
    translating the same word list.
    """
    from sqlalchemy import func

    from .models import Candidate, Word

    if language:
        rows = (db.session.query(Word.tier, Candidate.source,
                                 func.count(Candidate.id))
                .join(Word, Candidate.word_id == Word.id)
                .filter(Candidate.language == language)
                .group_by(Word.tier, Candidate.source).all())
        if not rows:
            click.echo(f"No options in {language!r}.")
            return
        per = {}
        for tier, src, n in rows:
            for name in split_sources(src):
                per.setdefault(name, {}).setdefault(tier, 0)
                per[name][tier] += n
        tiers = sorted({t for d in per.values() for t in d})
        click.echo(f"{language} — options per tier\n")
        click.echo("  " + f"{'system':22s}" +
                   "".join(f"{'tier ' + str(t):>12s}" for t in tiers) +
                   f"{'total':>12s}")
        for name in sorted(per, key=lambda k: -sum(per[k].values())):
            d = per[name]
            click.echo("  " + f"{name:22s}" +
                       "".join(f"{d.get(t, 0):>12,}" for t in tiers) +
                       f"{sum(d.values()):>12,}")
        return

    # Grouping by the raw source string keeps this to one aggregate over the
    # candidate table. There are a handful of distinct strings even with four
    # systems, because a shared wording stores both names in one row, so the
    # semicolons are split afterwards over those few rows rather than over
    # millions.
    rows = (db.session.query(Candidate.source, Candidate.language,
                             func.count(Candidate.id))
            .group_by(Candidate.source, Candidate.language).all())
    if not rows:
        click.echo("No options at all.")
        return
    totals, langs = {}, {}
    for src, lang, n in rows:
        for name in split_sources(src):
            totals[name] = totals.get(name, 0) + n
            langs.setdefault(name, set()).add(lang)
    click.echo(f"{sum(n for _, _, n in rows):,} options in the database\n")
    click.echo(f"  {'system':22s} {'options':>12s} {'languages':>10s}")
    for name in sorted(totals, key=lambda k: -totals[k]):
        click.echo(f"  {name:22s} {totals[name]:>12,} {len(langs[name]):>10,}")
    click.echo("\nA wording two systems both proposed is stored once with both "
               "names on it,\nso these add up to more than the row count.")


@shola_cli.command("add-options")
@click.option("--jsonl", "path", type=click.Path(exists=True), required=True,
              help='One object per line: {"phrase","language","text"}.')
@click.option("--source", required=True,
              help="What wrote them, e.g. 'google-translate'. The scoreboard "
                   "reads this.")
@click.option("--yes", is_flag=True, help="Do it, rather than counting first.")
def add_options_cmd(path, source, yes):
    """Attach machine translations to words that already exist.

    `import-words` builds the corpus and only knows the languages seeded at the
    start. This is for giving a language options later: a speaker of it then
    has something to agree with or correct, instead of typing every wording
    from nothing.

    Skips anything already there - same word, same language, same wording - so
    re-running after an interrupted pass costs nothing.

    Works a slice of the file at a time and asks the database only about the
    words in that slice. The first version read every existing option into a
    dictionary first, which is fine against a fresh database and was killed by
    the OOM reaper against a real one: six million candidates do not fit in a
    container's memory, and the failure came with no message beyond "Killed".
    """
    from .config import canonical_language
    from .models import CORE_PROJECT, Candidate, Project, Word

    core = Project.query.filter_by(slug=CORE_PROJECT["slug"]).first()
    if core is None:
        raise click.ClickException("No words project to attach to.")

    known = current_app.config["ALL_LANGUAGES"]
    # One phrase -> id map is unavoidable and affordable: 71,014 short strings.
    words = {phrase.casefold(): wid for wid, phrase in
             db.session.query(Word.id, Word.phrase)
             .filter(Word.project_id == core.id)}
    click.echo(f"{len(words):,} words in the project")

    # Gzip transparently: these files run to tens of millions of lines and
    # travel compressed, and making somebody gunzip them onto a container disk
    # first is a step with nothing in it.
    opener = gzip.open if path.endswith(".gz") else open

    def rows():
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    SLICE = 50_000
    added = skipped = shared = unknown_word = unknown_lang = 0

    def flush(batch):
        """Write one slice, skipping whatever is already there."""
        nonlocal added, skipped, shared
        wanted = {}
        for wid, lang, text in batch:
            wanted.setdefault(wid, []).append((lang, text))
        ids = list(wanted)

        # Only the rows this slice could collide with. The candidate id and
        # source come too: where this source has produced a wording somebody
        # else already proposed, the row is not written again but the name is
        # added to it.
        taken = {}
        for start in range(0, len(ids), 5000):
            chunk = ids[start:start + 5000]
            for cid, wid, lang, pos, text, src in db.session.query(
                    Candidate.id, Candidate.word_id, Candidate.language,
                    Candidate.position, Candidate.text,
                    Candidate.source).filter(
                    Candidate.word_id.in_(chunk)):
                slot = taken.setdefault((wid, lang), {"max": 0, "texts": {}})
                slot["max"] = max(slot["max"], pos or 0)
                slot["texts"][(text or "").strip().casefold()] = (cid, src or "")

        pending, credit = [], []
        for wid, lang, text in batch:
            slot = taken.setdefault((wid, lang), {"max": 0, "texts": {}})
            key = text.casefold()
            if key in slot["texts"]:
                # Somebody already proposed this wording. Two systems agreeing
                # is not a reason to show a speaker the same option twice, and
                # it is not a reason to credit only whichever was imported
                # first - the scoreboard reads a semicolon-separated list and
                # credits every name on it.
                cid, src = slot["texts"][key]
                if source not in {p.strip() for p in src.split(";")}:
                    credit.append({"id": cid,
                                   "source": f"{src};{source}" if src else source})
                    slot["texts"][key] = (cid, f"{src};{source}")
                    shared += 1
                else:
                    skipped += 1
                continue
            if slot["max"] >= MAX_OPTIONS:
                skipped += 1
                continue
            slot["max"] += 1
            slot["texts"][key] = (None, source)
            pending.append({"word_id": wid, "language": lang,
                            "position": slot["max"], "text": text[:400],
                            "source": source})
        if yes:
            if pending:
                db.session.bulk_insert_mappings(Candidate, pending)
            if credit:
                db.session.bulk_update_mappings(Candidate, credit)
            if pending or credit:
                db.session.commit()
        added += len(pending)

    batch = []
    seen_lines = 0
    for row in rows():
        seen_lines += 1
        text = (row.get("text") or "").strip()
        lang = canonical_language(row.get("language") or "")
        wid = words.get((row.get("phrase") or "").strip().casefold())
        if wid is None:
            unknown_word += 1
            continue
        if not text:
            continue
        if lang not in known:
            unknown_lang += 1
            continue
        batch.append((wid, lang, text))
        if len(batch) >= SLICE:
            flush(batch)
            batch = []
            click.echo(f"  {seen_lines:,} read | {added:,} "
                       f"{'written' if yes else 'to add'} | "
                       f"{shared:,} shared | {skipped:,} already there")
    if batch:
        flush(batch)

    click.echo(f"\n  read         {seen_lines:,}")
    click.echo(f"  {'written' if yes else 'to add':12} {added:,}")
    click.echo(f"  shared with another system {shared:>9,}")
    click.echo(f"  already there{skipped:>9,}")
    if unknown_word:
        click.echo(f"  no such word {unknown_word:,}")
    if unknown_lang:
        click.echo(f"  unknown lang {unknown_lang:,}")
    if not yes:
        click.echo("\nRe-run with --yes to write them.")
        return
    click.echo(f"Added {added:,} options as {source!r}.")


@shola_cli.command("drop-project")
@click.option("--slug", required=True)
@click.option("--yes", is_flag=True, help="Do it, rather than counting first.")
def drop_project_cmd(slug, yes):
    """Delete a project and everything hanging off it.

    For taking down work SHOLA no longer collects. It will not touch the words
    project: that is the one thing here, and deleting it by mistyping a slug
    is not a mistake worth leaving available.
    """
    from .models import (CORE_PROJECT, Assignment, Candidate, Evaluation,
                         Flag, Project, ProjectLanguage, Word, WordState)

    if slug == CORE_PROJECT["slug"]:
        raise click.ClickException(
            f"{slug} is the words project. It cannot be dropped here.")
    project = Project.query.filter_by(slug=slug).first()
    if project is None:
        raise click.ClickException(f"No project with slug {slug!r}.")

    ids = [row[0] for row in
           db.session.query(Word.id).filter(Word.project_id == project.id)]
    counts = {"items": len(ids)}
    for label, model in (("options", Candidate), ("answers", Evaluation),
                         ("assignments", Assignment), ("states", WordState),
                         ("reports", Flag)):
        counts[label] = (model.query.filter(model.word_id.in_(ids)).count()
                         if ids else 0)
    click.echo(f"{project.title}:")
    for label, n in counts.items():
        click.echo(f"  {label:12} {n:>8,}")
    if not yes:
        click.echo("\nRe-run with --yes to delete all of it.")
        return

    # Chunked, and children before parents: one statement over a hundred
    # thousand rows is what filled the disk the last time.
    for model in (Flag, WordState, Assignment, Evaluation, Candidate):
        done = 0
        for start in range(0, len(ids), 5000):
            chunk = ids[start:start + 5000]
            done += (model.query.filter(model.word_id.in_(chunk))
                     .delete(synchronize_session=False))
            db.session.commit()
        if done:
            click.echo(f"  deleted {done:,} {model.__name__.lower()} rows")
    for start in range(0, len(ids), 5000):
        chunk = ids[start:start + 5000]
        Word.query.filter(Word.id.in_(chunk)).delete(synchronize_session=False)
        db.session.commit()
    ProjectLanguage.query.filter_by(project_id=project.id).delete()
    db.session.delete(project)
    db.session.commit()
    click.echo(f"Dropped {slug!r}.")


@shola_cli.command("drop-rare")
@click.option("--below", type=int, default=None,
              help="Occurrence floor. Defaults to MIN_OCCURRENCES.")
@click.option("--yes", is_flag=True, help="Do it, rather than counting first.")
def drop_rare_cmd(below, yes):
    """Delete items too rare in the corpus to be worth an answer.

    This is what took tier 5 out: 407,808 phrases seen fewer than five times,
    over 122,000 of them exactly once. Without it they sit in the queue for
    ever, because tier 4 never finishes and tier 5 never opens.

    It counts first and prints what it would delete. Answers already given on
    those items are part of that count - read it before passing --yes, because
    those are somebody's evenings and they do not come back.
    """
    from .models import (Assignment, Candidate, Evaluation, Flag, Word,
                         WordState)
    from .tiers import MIN_OCCURRENCES

    floor = MIN_OCCURRENCES if below is None else below
    ids = [row[0] for row in
           db.session.query(Word.id).filter(Word.occurrences < floor)]
    kept = Word.query.filter(Word.occurrences >= floor).count()

    counts = {"items": len(ids)}
    for label, model in (("options", Candidate), ("answers", Evaluation),
                         ("assignments", Assignment), ("states", WordState),
                         ("reports", Flag)):
        n = 0
        for start in range(0, len(ids), 5000):
            n += (model.query
                  .filter(model.word_id.in_(ids[start:start + 5000])).count())
        counts[label] = n

    click.echo(f"Items seen fewer than {floor} times:")
    for label, n in counts.items():
        click.echo(f"  {label:12} {n:>9,}")
    click.echo(f"\n  {kept:,} items stay.")
    if counts["answers"]:
        click.echo(f"  {counts['answers']:,} answers volunteers have already "
                   "given would go with them.")
    if not yes:
        click.echo("\nRe-run with --yes to delete all of it.")
        return
    if not ids:
        return

    # Chunked, children before parents: one statement over 400,000 rows is what
    # filled the disk the last time.
    for model in (Flag, WordState, Assignment, Evaluation, Candidate):
        done = 0
        for start in range(0, len(ids), 5000):
            done += (model.query
                     .filter(model.word_id.in_(ids[start:start + 5000]))
                     .delete(synchronize_session=False))
            db.session.commit()
        if done:
            click.echo(f"  deleted {done:,} {model.__name__.lower()} rows")
    for start in range(0, len(ids), 5000):
        (Word.query.filter(Word.id.in_(ids[start:start + 5000]))
         .delete(synchronize_session=False))
        db.session.commit()
    click.echo(f"Deleted {len(ids):,} items. {kept:,} left.")
    click.echo("Run `flask shola assign-tiers` to confirm the bands, then "
               "VACUUM to give the disk back.")


@shola_cli.command("name-model")
@click.option("--project", "slug", required=True,
              help="Project slug whose options are being attributed.")
@click.option("--model", "model", required=True,
              help='What wrote them, e.g. "gemini-3.6-flash".')
@click.option("--was", default=None,
              help="Only rename options currently recorded as this.")
@click.option("--language", default=None, help="One language only.")
@click.option("--yes", is_flag=True, help="Do it, rather than counting first.")
def name_model_cmd(slug, model, was, language, yes):
    """Say what wrote a project's existing options, for the scoreboard.

    Options imported before the model column existed carry a placeholder. This
    puts the real name on them so they can be scored. Wordings volunteers typed
    are never touched - they are not a model's work.
    """
    from .config import canonical_language
    from .models import Candidate, Project, Word

    project = Project.query.filter_by(slug=slug).first()
    if project is None:
        raise click.ClickException(f"No project with slug {slug!r}.")

    q = (Candidate.query.join(Word, Candidate.word_id == Word.id)
         .filter(Word.project_id == project.id)
         .filter(Candidate.source != "volunteer"))
    if was is not None:
        q = q.filter(Candidate.source == was)
    if language:
        q = q.filter(Candidate.language == canonical_language(language))

    n = q.count()
    if not n:
        click.echo("Nothing matches; nothing to do.")
        return
    if not yes:
        click.echo(f"{n:,} options in {project.title} would be recorded as "
                   f"{model!r}. Re-run with --yes to do it.")
        return

    # Chunked, and walked by id rather than by re-running the filter: one
    # statement over a million rows is what filled the disk the last time, and
    # a filter that still matches the rows it just updated never terminates.
    ids = [row[0] for row in q.with_entities(Candidate.id)
           .order_by(Candidate.id).all()]
    done = 0
    for start in range(0, len(ids), 5000):
        chunk = ids[start:start + 5000]
        (db.session.query(Candidate).filter(Candidate.id.in_(chunk))
         .update({Candidate.source: model}, synchronize_session=False))
        db.session.commit()
        done += len(chunk)
        click.echo(f"  {done:,} / {n:,}")
    click.echo(f"Recorded {done:,} options as {model!r}.")


@shola_cli.command("check-databases")
def check_databases_cmd():
    """Say whether the backup could dump the other applications' databases.

    Reports the three things that actually stop it - no dump tool in the image,
    the database host not resolvable from this container, wrong credentials -
    separately, because they have different fixes and one message saying
    "failed" would not tell you which.
    """
    import shutil as _shutil
    import socket
    import subprocess

    for tool in ("pg_dump", "mysqldump"):
        where = _shutil.which(tool)
        click.echo(f"{tool:11s} {where or 'NOT INSTALLED - rebuild the image'}")
        if where:
            try:
                v = subprocess.run([tool, "--version"], capture_output=True,
                                   text=True, timeout=20).stdout.strip()
                click.echo(f"            {v}")
            except Exception as exc:          # noqa: BLE001
                click.echo(f"            could not run it: {exc}")

    try:
        found = coolify_databases()
    except Exception as exc:                  # noqa: BLE001
        raise click.ClickException(f"cannot reach the Coolify API: {exc}")
    if not found:
        click.echo("\nNo databases found. Is SHOLA_COOLIFY_TOKEN set?")
        return

    from urllib.parse import urlparse
    click.echo(f"\n{len(found)} managed databases:")
    for d in found:
        u = urlparse(d["dsn"])
        click.echo(f"\n  {d['name']}  ({d['image']}, {d['status']})")
        try:
            socket.gethostbyname(u.hostname)
        except OSError:
            if not str(d["status"]).startswith("running"):
                # Docker's DNS has no entry for a stopped container. Nothing is
                # wrong with the network, and the backup skips this one anyway.
                click.echo("    stopped, so no DNS entry - not dumped")
            else:
                click.echo(f"    host {u.hostname} does not resolve from this "
                           "container - attach it to the 'coolify' network")
            continue
        port = u.port or (5432 if "postgres" in d["kind"] else 3306)
        try:
            with socket.create_connection((u.hostname, port), timeout=10):
                click.echo(f"    reachable on {u.hostname}:{port}")
        except OSError as exc:
            click.echo(f"    cannot connect to {u.hostname}:{port}: {exc}")


@shola_cli.command("backup")
@click.option("--out", default="instance/backups", show_default=True)
@click.option("--keep-db", default=3, show_default=True,
              help="full database copies to retain.")
@click.option("--keep-people", default=60, show_default=True,
              help="volunteer exports to retain. Cheap, so keep many.")
@click.option("--keep-dirs", default=2, show_default=True,
              help="archives of each backed-up directory to retain. These are "
                   "the big ones - a nightly tar of a 7 GB upload directory - "
                   "so few, and only as a fallback for a failed upload.")
@click.option("--keep-config", default=30, show_default=True,
              help="Coolify configuration exports to retain.")
@click.option("--keep-external", default=14, show_default=True,
              help="dumps of each managed Postgres/MySQL database to retain.")
def backup(out, keep_db, keep_people, keep_dirs, keep_config, keep_external):
    """Back up the database, the volunteers, any mounted directories and the
    deployment configuration.

    Retention differs by how replaceable each thing is. The word list can be
    re-imported from the published dataset in minutes, so only a few full
    database copies are kept. A volunteer's email, chosen days and answers
    exist nowhere else and weigh a few hundred kilobytes, so many are kept.
    """
    import gzip as _gzip
    import shutil
    import sqlite3
    import tarfile
    from datetime import datetime as _dt

    from .models import Evaluation, Volunteer

    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    if not uri.startswith("sqlite"):
        raise click.UsageError("this command backs up SQLite databases only")
    db_path = uri.split("sqlite:///")[-1]

    out_dir = os.path.abspath(out)
    os.makedirs(out_dir, exist_ok=True)
    stamp = _dt.utcnow().strftime("%Y%m%d-%H%M%S")
    made = []

    # --- the database -----------------------------------------------------
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
    made.append((db_gz, "shola-", keep_db))
    click.echo(f"database  {os.path.getsize(db_gz)//1048576} MB")

    # --- the part that cannot be rebuilt ----------------------------------
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
    made.append((people_path, "volunteers-", keep_people))
    click.echo(f"people    {os.path.getsize(people_path)//1024} KB  "
               f"{len(people)} volunteers, "
               f"{sum(len(p['answers']) for p in people)} answers")

    # --- directories from other applications ------------------------------
    # Set SHOLA_BACKUP_DIRS to "name=/path:name=/path". These are other apps'
    # upload directories, bind-mounted in read-only, because user-uploaded
    # files live in no database and nothing else was backing them up.
    # "label=/path" archives a whole directory. Appending "|name" excludes any
    # entry whose path contains it, which is how a site's code is kept without
    # its uploads: those are large, they change constantly, and a nightly full
    # snapshot of them is what filled the disk.
    #
    #   education-au-site=/sources/education-au-html|images|media|cache
    for spec in filter(None, os.environ.get("SHOLA_BACKUP_DIRS", "").split(":")):
        head, *excludes = spec.split("|")
        label, _, path = head.partition("=")
        if not path or not os.path.isdir(path):
            click.echo(f"skip      {label or spec}: not a directory", err=True)
            continue

        def _filter(info, _excludes=excludes):
            # Compared against the path inside the archive, so "images" matches
            # both "images" and "html/images/photo.jpg". Excluding a directory
            # prunes the whole subtree, so no count of what was left out is
            # reported - tar never walks it, and a number that undercounts by a
            # subtree would be worse than none.
            if any(x and x in info.name for x in _excludes):
                return None
            return info

        arc = os.path.join(out_dir, f"{label}-{stamp}.tar.gz")
        with tarfile.open(arc, "w:gz", compresslevel=6) as tar:
            tar.add(path, arcname=label, filter=_filter)
        made.append((arc, f"{label}-", keep_dirs))
        note = f"  (without {', '.join(excludes)})" if excludes else ""
        click.echo(f"files     {os.path.getsize(arc)//1048576} MB  {label}{note}")

    # --- other applications' databases ------------------------------------
    # Coolify's own scheduled backups record success when its helper container
    # is missing and nothing has left the machine, which is how four databases
    # went five days with no off-site copy while the dashboard stayed green.
    # These are dumped here instead: through the same path that verifies the
    # stored object's size afterwards, and loudly enough that a failure cannot
    # be mistaken for a backup.
    #
    # Nothing is configured. The connection strings come from the Coolify API,
    # which this command already talks to.
    failed_dbs = []
    try:
        discovered = coolify_databases()
    except Exception as exc:          # noqa: BLE001
        click.echo(f"databases skipped: cannot reach Coolify: {exc}", err=True)
        discovered = []
    for entry in discovered:
        if not str(entry["status"]).startswith("running"):
            click.echo(f"database  {entry['name']}: {entry['status']}, "
                       "not dumped")
            continue
        try:
            path = dump_database(entry, out_dir, stamp)
        except Exception as exc:      # noqa: BLE001
            failed_dbs.append(entry["name"])
            click.echo(f"database  {entry['name']} FAILED: {exc}", err=True)
            continue
        label = re.sub(r"[^A-Za-z0-9_.-]", "-", entry["name"])
        made.append((path, f"db-{label}-", keep_external))
        click.echo(f"database  {entry['name']}  "
                   f"{max(1, os.path.getsize(path)//1048576)} MB  "
                   f"{entry['image']}")

    # --- deployment configuration ----------------------------------------
    # The environment variables, domains and schedules of every application.
    # They are inside Coolify's own database dump too, but restoring from a
    # readable file does not require standing Coolify up first.
    # Never fatal. This is the least important thing the backup collects - the
    # deployment configuration can be read off the dashboard - and for ten days
    # a DNS failure here aborted the whole command before it pruned or uploaded
    # anything, which is how 63 GB of archives accumulated and filled the disk.
    try:
        cfg = export_coolify_config()
    except Exception as exc:      # noqa: BLE001
        click.echo(f"config    skipped: {exc.__class__.__name__}: {exc}",
                   err=True)
        cfg = None
    if cfg is not None:
        cfg_path = os.path.join(out_dir, f"coolify-config-{stamp}.json.gz")
        with _gzip.open(cfg_path, "wt", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
        made.append((cfg_path, "coolify-config-", keep_config))
        click.echo(f"config    {os.path.getsize(cfg_path)//1024} KB  "
                   f"{len(cfg.get('applications', []))} apps")

    if os.environ.get("SHOLA_S3_BUCKET"):
        try:
            for path, stem, keep in made:
                key = upload_to_s3([path], keep=keep, stem=stem)[0]
                click.echo(f"uploaded  {key}")
                # Gone from the disk the moment it is safely in the bucket, and
                # verified there by size. Keeping local copies as well filled a
                # 96 GB disk with 63 GB of archives and took the site down: the
                # nightly tar of a 6.9 GB upload directory is not the same kind
                # of object as a 200 KB volunteer export, and retaining seven of
                # each was never measured against the disk.
                if not os.environ.get("SHOLA_KEEP_LOCAL"):
                    os.remove(path)
        except Exception as exc:      # noqa: BLE001 - report loudly, fail loudly
            click.echo(f"S3 upload FAILED: {exc}", err=True)
            # Leave what is on disk: a failed upload is the one case where the
            # local copy is the only copy.
            prune_local(out_dir, made)
            raise SystemExit(1)
        # Anything left from an earlier failure, now that this run succeeded.
        prune_local(out_dir, made)
        # A dated marker, so staleness is visible without asking the bucket.
        # Off-site backups have now silently stopped twice - once because
        # Coolify's helper image was missing, once because this command died on
        # a full disk - and both times the dashboards stayed green for days.
        stamp_path = os.path.join(os.path.dirname(out_dir), "last-backup.txt")
        try:
            with open(stamp_path, "w", encoding="utf-8") as fh:
                fh.write(_dt.utcnow().isoformat())
        except OSError as exc:
            click.echo(f"could not record the backup time: {exc}", err=True)
    else:
        click.echo("no SHOLA_S3_BUCKET set, so this backup stays on this host")
        prune_local(out_dir, made)

    left = sum(os.path.getsize(f) for f in glob.glob(os.path.join(out_dir, "*")))
    free = shutil.disk_usage(out_dir).free
    click.echo(f"local     {left//1048576} MB kept, "
               f"{free//1073741824} GB free on disk")
    if free < 5 * 1073741824:
        click.echo("WARNING: under 5 GB free. SQLite needs room for a journal "
                   "as large as the rows a transaction touches.", err=True)

    # Last, so a database that would not dump does not cost the backup of
    # everything that would - but non-zero, so the run is not recorded as a
    # success. A green tick over a missing database is the whole problem.
    if failed_dbs:
        click.echo(f"FAILED to dump: {', '.join(failed_dbs)}", err=True)
        raise SystemExit(1)


ORPHAN_DAYS = 7


def prune_local(out_dir, made, orphan_days=ORPHAN_DAYS):
    """Keep only the newest N of each kind, and clear abandoned kinds.

    Pruning by stem only reaches the kinds this run produced. Retire a backup
    target - as education-au was - and its archives are left behind for ever,
    which is 6.3 GB nobody is coming back for. Anything older than
    `orphan_days` whose kind is no longer produced goes too; by then it has
    either reached the bucket or been reported as failed for a week.
    """
    import time

    stems = {m[1] for m in made}
    for _path, stem, keep in {(None, m[1], m[2]) for m in made}:
        files = sorted(glob.glob(os.path.join(out_dir, f"{stem}*")))
        for old in files[:-keep or None]:
            os.remove(old)

    cutoff = time.time() - orphan_days * 86400
    for path in glob.glob(os.path.join(out_dir, "*")):
        name = os.path.basename(path)
        if any(name.startswith(stem) for stem in stems):
            continue
        if os.path.getmtime(path) < cutoff:
            click.echo(f"removed   {name} "
                       f"({os.path.getsize(path)//1048576} MB, no longer "
                       "backed up)")
            os.remove(path)


def coolify_databases():
    """Every managed database Coolify knows about, with how to reach it.

    Discovered rather than configured: the connection strings are already in
    Coolify's API, and a second copy of four sets of database credentials in
    this app's environment is four more things to leak and to keep in step.
    Returns [] when no token is set.
    """
    url = (os.environ.get("SHOLA_COOLIFY_URL") or "").rstrip("/")
    token = os.environ.get("SHOLA_COOLIFY_TOKEN")
    if not (url and token):
        return []
    import httpx

    with httpx.Client(timeout=30,
                      headers={"Authorization": f"Bearer {token}"}) as http:
        rows = http.get(f"{url}/api/v1/databases").json()
    out = []
    for d in rows if isinstance(rows, list) else []:
        dsn = d.get("internal_db_url")
        if not dsn:
            continue
        out.append({"name": d.get("name") or d.get("uuid"),
                    "uuid": d.get("uuid"),
                    "kind": (d.get("database_type") or ""),
                    "image": d.get("image") or "",
                    "status": d.get("status") or "",
                    "dsn": dsn,
                    # A backup wants more rights than an application user has.
                    # Coolify's connection string is the one the app uses -
                    # for MySQL that is the site's own account, which is
                    # granted what the site needs and not what mysqldump
                    # --routines needs.
                    "root_password": d.get("mysql_root_password") or ""})
    return out


def dump_database(db, out_dir, stamp):
    """One managed database to a gzipped SQL file. Returns the path, or None.

    Raises on failure so the caller can report it and exit non-zero. A backup
    that skips a database quietly is the failure mode this whole command
    exists to avoid.
    """
    import gzip as _gzip
    import subprocess
    from urllib.parse import urlparse, unquote

    u = urlparse(db["dsn"])
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", db["name"])
    path = os.path.join(out_dir, f"db-{name}-{stamp}.sql.gz")
    env = dict(os.environ)

    if "postgres" in db["kind"] or u.scheme.startswith("postgres"):
        env["PGPASSWORD"] = unquote(u.password or "")
        cmd = ["pg_dump", "--no-owner", "--no-privileges", "--clean",
               "--if-exists", "-h", u.hostname, "-p", str(u.port or 5432),
               "-U", unquote(u.username or "postgres"),
               (u.path or "/").lstrip("/")]
    elif "mysql" in db["kind"] or "maria" in db["kind"] or \
            u.scheme.startswith(("mysql", "maria")):
        # root where Coolify has it, and the application's own account only as
        # a fallback. The app user is granted what the site needs; mysqldump
        # --routines reads stored programs, and on MySQL 8 that needs rights a
        # Joomla account is not given. Falling back rather than insisting means
        # a database Coolify has no root password for still gets dumped.
        user = db.get("root_password") and "root" or unquote(u.username or "root")
        password = db.get("root_password") or unquote(u.password or "")
        # Through the environment, not -p on the command line: an argument is
        # visible in `ps` to every user on the host, and this one is a root
        # database password.
        env["MYSQL_PWD"] = password
        cmd = ["mysqldump", "--single-transaction", "--routines", "--triggers",
               "--no-tablespaces", "-h", u.hostname, "-P", str(u.port or 3306),
               "-u", user, (u.path or "/").lstrip("/")]
    else:
        raise RuntimeError(f"no dumper for {db['kind'] or u.scheme!r}")

    with _gzip.open(path, "wb", compresslevel=6) as gz:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            gz.write(chunk)
        err = proc.stderr.read().decode(errors="replace").strip()
        code = proc.wait()
    if code != 0:
        os.path.exists(path) and os.remove(path)
        raise RuntimeError(err.splitlines()[-1] if err else f"exit {code}")
    # An empty dump is a failed dump that exited zero, which is exactly the
    # shape of the bug this replaces.
    if os.path.getsize(path) < 200:
        os.remove(path)
        raise RuntimeError("dump was empty")
    return path


def export_coolify_config():
    """Every application's environment variables, domains and schedules.

    Returns None when no Coolify token is configured, so the backup still
    works without it.
    """
    url = (os.environ.get("SHOLA_COOLIFY_URL") or "").rstrip("/")
    token = os.environ.get("SHOLA_COOLIFY_TOKEN")
    if not (url and token):
        return None
    # host.docker.internal is not resolvable from every container. Say so
    # usefully rather than through a DNS traceback.
    if "host.docker.internal" in url:
        import socket
        try:
            socket.gethostbyname("host.docker.internal")
        except OSError:
            raise RuntimeError(
                "SHOLA_COOLIFY_URL points at host.docker.internal, which this "
                "container cannot resolve. Use the host's address instead.")

    import httpx
    from datetime import datetime as _dt

    head = {"Authorization": f"Bearer {token}"}
    out = {"exported_at": _dt.utcnow().isoformat(), "applications": [],
           "databases": []}
    with httpx.Client(timeout=30, headers=head) as http:
        for app in http.get(f"{url}/api/v1/applications").json():
            uuid = app.get("uuid")
            envs = http.get(f"{url}/api/v1/applications/{uuid}/envs").json()
            tasks = http.get(
                f"{url}/api/v1/applications/{uuid}/scheduled-tasks").json()
            storages = http.get(
                f"{url}/api/v1/applications/{uuid}/storages").json()
            out["applications"].append({
                "name": app.get("name"), "uuid": uuid,
                "fqdn": app.get("fqdn"), "build_pack": app.get("build_pack"),
                "git_repository": app.get("git_repository"),
                "git_branch": app.get("git_branch"),
                "ports_exposes": app.get("ports_exposes"),
                "environment": {e.get("key"): e.get("value") for e in envs
                                if not e.get("is_preview")},
                "scheduled_tasks": [
                    {"name": t.get("name"), "command": t.get("command"),
                     "frequency": t.get("frequency")} for t in tasks],
                "storages": storages,
            })
        for db in http.get(f"{url}/api/v1/databases").json():
            out["databases"].append({
                "name": db.get("name"), "uuid": db.get("uuid"),
                "type": db.get("database_type"),
                "internal_url": db.get("internal_db_url"),
            })
    return out


def upload_to_s3(paths, keep=14, prefix=None, stem=None):
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

    if stem:
        listed = client.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{stem}")
        objs = sorted(listed.get("Contents", []), key=lambda o: o["Key"])
        for obj in objs[:-keep or None]:
            client.delete_object(Bucket=bucket, Key=obj["Key"])
    return keys
