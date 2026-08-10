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
from datetime import date, datetime, time

import click
from flask import current_app
from flask.cli import AppGroup

from . import consensus
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
    word = Word(phrase=phrase, frequency=pct, occurrences=occurrences,
                tier=tier_for(occurrences), project_id=project_id)
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

    sent = skipped = failed = 0
    for volunteer in query.all():
        if not force and volunteer.last_emailed_on == today:
            skipped += 1
            continue
        if volunteer.day_numbers and today.weekday() not in volunteer.day_numbers:
            skipped += 1
            continue

        # Did the last send go unanswered? Worked out before leasing, since
        # leasing is what replaces the old list, but only written down once an
        # email actually goes out: a send we never made is not a send they
        # missed.
        #
        # Compared as a datetime on purpose: created_at is a timestamp, and
        # leaning on string comparison against a bare date would be an accident
        # waiting for a database that stores dates differently.
        since = (datetime.combine(volunteer.last_emailed_on, time.min)
                 if volunteer.last_emailed_on else None)
        missed_last_send = bool(
            since and not volunteer.evaluations.filter(
                Evaluation.created_at >= since).first())
        misses = volunteer.missed_in_a_row + 1 if missed_last_send else 0

        # A wrong schedule is worth one suggestion, not a weekly reminder that
        # they are behind.
        nudge = (misses >= cfg["MISSES_BEFORE_NUDGE"]
                 and len(volunteer.day_numbers or []) != 1
                 and volunteer.nudged_on is None)

        # A fresh list. Anything from an earlier day goes back to the queue, so
        # missing days never builds a backlog to work through.
        top_up(volunteer, today=today)
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
            if nudge:
                volunteer.nudged_on = today
            db.session.commit()
            sent += 1
        except Exception as exc:      # noqa: BLE001 - keep going, report at end
            failed += 1
            click.echo(f"  failed {volunteer.email}: {exc}", err=True)

    click.echo(f"sent {sent}, skipped {skipped}, failed {failed}")
    if failed:
        sys.exit(1)


def announce_project(project):
    """Email every volunteer who speaks a language this project collects.

    Returns how many were emailed. Failures are logged and skipped rather than
    aborting: one bad address must not stop the rest being told.
    """
    from .mailer import build_project_email, send
    from .models import ProjectLanguage
    from .projects import mark_announced

    codes = [pl.language for pl in project.languages]
    if not codes:
        return 0
    volunteers = (Volunteer.query
                  .filter(Volunteer.active.is_(True),
                          Volunteer.language.in_(codes))
                  .all())
    sent = 0
    for volunteer in volunteers:
        try:
            subject, text, html = build_project_email(volunteer, project)
            send(volunteer.email, subject, text, html)
            sent += 1
        except Exception as exc:      # noqa: BLE001
            current_app.logger.warning("announce failed for %s: %s",
                                       volunteer.email, exc)
    mark_announced(project)
    return sent


@shola_cli.command("announce-project")
@click.option("--slug", required=True)
def announce_project_cmd(slug):
    """Email volunteers about an approved project."""
    from .models import Project

    project = Project.query.filter_by(slug=slug).first()
    if not project:
        raise click.UsageError(f"no project {slug!r}")
    if project.status != "approved":
        raise click.UsageError(f"{slug} is {project.status}, not approved")
    click.echo(f"emailed {announce_project(project)} volunteers")


@shola_cli.command("projects")
def projects_cmd():
    """Every project, its state and its size."""
    from .models import Project
    from .projects import item_counts

    for project in Project.query.order_by(Project.sort_order,
                                          Project.id).all():
        counts = item_counts(project)
        click.echo(f"{project.status:9s} {project.slug:28s} "
                   f"{project.item_count():>8,} items  "
                   f"{'options' if project.has_options else 'typed  '}  "
                   + ", ".join(f"{k}:{v:,}" for k, v in counts.items()))


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


@shola_cli.command("backup")
@click.option("--out", default="instance/backups", show_default=True)
@click.option("--keep-db", default=3, show_default=True,
              help="full database copies to retain.")
@click.option("--keep-people", default=60, show_default=True,
              help="volunteer exports to retain. Cheap, so keep many.")
@click.option("--keep-dirs", default=7, show_default=True,
              help="archives of each backed-up directory to retain.")
@click.option("--keep-config", default=30, show_default=True,
              help="Coolify configuration exports to retain.")
def backup(out, keep_db, keep_people, keep_dirs, keep_config):
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
    for spec in filter(None, os.environ.get("SHOLA_BACKUP_DIRS", "").split(":")):
        label, _, path = spec.partition("=")
        if not path or not os.path.isdir(path):
            click.echo(f"skip      {label or spec}: not a directory", err=True)
            continue
        arc = os.path.join(out_dir, f"{label}-{stamp}.tar.gz")
        with tarfile.open(arc, "w:gz", compresslevel=6) as tar:
            tar.add(path, arcname=label)
        made.append((arc, f"{label}-", keep_dirs))
        click.echo(f"files     {os.path.getsize(arc)//1048576} MB  {label}")

    # --- deployment configuration ----------------------------------------
    # The environment variables, domains and schedules of every application.
    # They are inside Coolify's own database dump too, but restoring from a
    # readable file does not require standing Coolify up first.
    cfg = export_coolify_config()
    if cfg is not None:
        cfg_path = os.path.join(out_dir, f"coolify-config-{stamp}.json.gz")
        with _gzip.open(cfg_path, "wt", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
        made.append((cfg_path, "coolify-config-", keep_config))
        click.echo(f"config    {os.path.getsize(cfg_path)//1024} KB  "
                   f"{len(cfg.get('applications', []))} apps")

    prune_local(out_dir, made)

    if os.environ.get("SHOLA_S3_BUCKET"):
        try:
            for path, stem, keep in made:
                key = upload_to_s3([path], keep=keep, stem=stem)[0]
                click.echo(f"uploaded  {key}")
        except Exception as exc:      # noqa: BLE001 - report loudly, fail loudly
            click.echo(f"S3 upload FAILED: {exc}", err=True)
            raise SystemExit(1)
    else:
        click.echo("no SHOLA_S3_BUCKET set, so this backup stays on this host")


def prune_local(out_dir, made):
    """Keep only the newest N of each kind on local disk."""
    for _path, stem, keep in {(None, m[1], m[2]) for m in made}:
        files = sorted(glob.glob(os.path.join(out_dir, f"{stem}*")))
        for old in files[:-keep or None]:
            os.remove(old)


def export_coolify_config():
    """Every application's environment variables, domains and schedules.

    Returns None when no Coolify token is configured, so the backup still
    works without it.
    """
    url = (os.environ.get("SHOLA_COOLIFY_URL") or "").rstrip("/")
    token = os.environ.get("SHOLA_COOLIFY_TOKEN")
    if not (url and token):
        return None

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
