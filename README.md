# SHOLA — Share Your Language

A volunteer app for verifying machine translations of everyday words in **Twi,
Ewe, Ga and Dagbani**.

A language model has guessed three translations for each English noun. Some
guesses are good, some are calques no speaker would use, and some are wrong. A
machine cannot tell the difference; a speaker can, in about two seconds. SHOLA
sends each volunteer a handful of words a day by email and records which wording
they would actually use.

## How it works

1. A volunteer signs up: name, email, language, the weekdays they are free, and
   roughly what time of day.
2. They are assigned **1000 words**, dated across their chosen weekdays over a
   year — so a handful per day, no matter how many days they picked.
3. On each of their days they get an email that **lists the actual words** and a
   link straight into the flow. No password.
4. For each word they tap one of three translations, skip it, or type their own.
   Choosing an option and typing your own are mutually exclusive.
5. When two or more speakers land on the same wording, that becomes the recorded
   translation.

### Assignment is coverage-first

Words are handed out **least-assigned first**, so every word reaches one
volunteer before any word reaches a second. Once the list is covered, words come
round again and the repeats become the agreement signal. See
`shola/assignment.py`.

### Missing days costs nothing

A day's words that go unanswered stay pending and appear in the next email —
`Volunteer.pending_today()` selects everything due *on or before* today. To stop
a missed week turning into one crushing list, `shola redistribute-missed`
re-dates overdue words across the volunteer's remaining days. Work is only ever
moved, never dropped, so the 1000 still land inside the year.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # SHOLA_SECRET_KEY
```

Fill in `.env`. For email you need a **Gmail app password** (Google Account →
Security → 2-step verification → App passwords) — the account password will be
refused.

Load the words and their candidate translations, from either the translated CSV
or the raw JSONL that produced it:

```bash
flask --app wsgi shola import-words --csv ../GhanaNouns/data/ghana-nouns-translated.csv
flask --app wsgi shola import-words --jsonl ../GhanaNouns/data/.translations.jsonl
```

Run it:

```bash
flask --app wsgi run                      # development
gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app    # production
```

## Daily email from cron

`send-daily` only emails volunteers whose chosen weekday is today, skips anyone
already emailed today, and skips anyone with nothing pending — so it is safe to
run hourly.

```cron
0 7  * * *  cd /srv/shola && .venv/bin/flask --app wsgi shola send-daily --window morning
0 13 * * *  cd /srv/shola && .venv/bin/flask --app wsgi shola send-daily --window afternoon
0 18 * * *  cd /srv/shola && .venv/bin/flask --app wsgi shola send-daily --window evening
0 3  * * 1  cd /srv/shola && .venv/bin/flask --app wsgi shola redistribute-missed
```

Check what would go out without sending anything:

```bash
flask --app wsgi shola send-daily --dry-run
```

## Getting the results out

```bash
flask --app wsgi shola export --language twi --min-votes 2 --out twi-agreed.csv
flask --app wsgi shola stats
```

Or over HTTP: `/api/consensus/<language>?min_votes=2`, and
`/api/word/<id>/<language>` for one word's full vote breakdown.

Consensus is **always computed from current votes**, never stored as truth, so
it improves as volunteers arrive and can never go stale (`shola/consensus.py`).
Votes are compared case-folded and NFC-normalised, so `Ɔdɔ` and `ɔdɔ` are one
vote and a combining-mark `ɛ` from one phone matches a precomposed `ɛ` from
another.

## The special characters

These languages need characters a phone keyboard hides. When a volunteer types
their own translation, every one is a single tap, and **holding** a plain letter
reaches its variants the way a native keyboard behaves:

| Language | Characters | Hold |
|----------|-----------|------|
| Twi | ɛ Ɛ ɔ Ɔ ŋ Ŋ | e, o, n |
| Ewe | ɖ Ɖ ɛ Ɛ ƒ Ƒ ɣ Ɣ ŋ Ŋ ɔ Ɔ ʋ Ʋ | d, e, f, g, n, o, v |
| Ga | ɛ Ɛ ŋ Ŋ ɔ Ɔ | e, n, o |
| Dagbani | ɛ Ɛ ɣ Ɣ ŋ Ŋ ɔ Ɔ ʒ Ʒ | e, g, n, o, z |

Defined once in `shola/config.py` and used by the keyboard, the language cards
and the page copy.

## Layout

```
shola/
├── config.py        # settings + the four languages and their characters
├── models.py        # Volunteer, Word, Candidate, Assignment, Evaluation
├── assignment.py    # coverage-first assignment, day spreading, redistribution
├── consensus.py     # votes -> most likely translation
├── mailer.py        # Gmail SMTP + signed daily links
├── views.py         # routes
├── cli.py           # import-words, send-daily, redistribute-missed, export
├── templates/
└── static/
tests/test_flow.py   # end-to-end checks
```

## Tests

```bash
python3 tests/test_flow.py
```

Covers the parts most likely to break: coverage-before-duplication, day
scheduling, missed-day carry-forward and redistribution, one-verdict-per-word,
typed answers beating machine options, single votes *not* counting as consensus,
and daily-link tokens round-tripping and rejecting tampering.

## Notes for whoever runs this

- **Photos** are optional and used in exactly one place: the champions
  leaderboard. Volunteers who skip it get their initials. The consent tick is
  stored separately from the file, so unticking it hides the photo without
  deleting anything.
- **`/resend` never reveals whether an address is registered** — it gives the
  same answer either way.
- **SQLite is the default.** It is fine for a few hundred volunteers; point
  `SHOLA_DATABASE_URL` at Postgres before you go wider, since every verdict is
  a write.
- **`SHOLA_SITE_URL` must be the public address.** Daily links are built from
  it, not from the incoming request, because the emails are sent from cron where
  there is no request to read.
