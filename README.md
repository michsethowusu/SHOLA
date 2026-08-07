# SHOLA — Share Your Language

**Live: [sholaproject.org](https://sholaproject.org)** — [sign
up](https://sholaproject.org/join) · [champions](https://sholaproject.org/champions)
· [progress](https://sholaproject.org/stats) · [API](https://sholaproject.org/api)

A volunteer app for confirming translations of everyday words in **88 Ghanaian
languages**.

Machine translation produced three candidate translations for each word. Much of
it is good, some is word-for-word and no speaker would say it, and some is
wrong. Software cannot tell the difference; a speaker can, in a couple of
seconds. SHOLA sends each volunteer five words a day by email and
records the wording they would actually use.

The words come from [GhanaNouns](https://github.com/GhanaNLP/GhanaNouns):
478,822 English nouns drawn from Ghanaian news, research and speech, with three
candidate translations per language. All of them are loaded into the live
deployment.

## How it works

1. A volunteer signs up: name, email, language, the weekdays they are free, and
   roughly what time of day. A six-digit code confirms the address before
   anything is created.
2. On each of their days they get an email that **lists the actual words** and a
   personalised link. No account, no password.
3. For each word they tap one of three translations, skip it, or type their own.
   Choosing an option and typing your own are mutually exclusive, and they can
   step back to change their last answer.
4. When five speakers of that language choose the same wording, the translation
   is confirmed and the word leaves the queue. A typed answer becomes an option
   the next speaker can agree with, so a language with no loaded translations
   still builds up choices.

### It is open-ended

Nobody signs up for a fixed number of words or a fixed length of time. Every
send is `SHOLA_WORDS_PER_DAY` words on the days they chose, and they keep going
until they say otherwise.

From `/w/<token>/settings`, reached from the footer of every email, a volunteer
can change their days and time of day, pause for a week, a month, three months
or until they say otherwise, stop entirely, or start again. A timed pause
clears itself, so a break does not depend on remembering to come back, and
stopping is reversible from the same link - which is why
`volunteer_from_token(require_active=False)` exists.

**Missing days costs nothing, and builds no backlog.** A send hands back
anything from an earlier day and leases a fresh list, so a missed day is not a
debt collected later - the words go straight to other speakers, and the next
email is whatever the project needs then. Stopping releases outstanding words
immediately.

**A wrong schedule gets a suggestion, not a nag.** After
`SHOLA_MISSES_BEFORE_NUDGE` sends go unanswered in a row, one email offers a
lighter schedule with a link that switches them to a single day a week. Sent
once, tracked by `nudged_on`. A miss only counts when an email actually went
out, and answering anything resets the count.

**The length of a send is the volunteer's choice.** `words_per_send` on the
volunteer, set from their settings page, bounded by `SHOLA_WORDS_PER_SEND_MIN`
and `SHOLA_WORDS_PER_SEND_MAX`. Until they choose, `SHOLA_WORDS_PER_DAY`
applies. How often they are sent words and how many they get are deliberately
independent: coupling them would mean guessing on someone's behalf.

### There is no login

The link in the email carries a signed token identifying the volunteer, and
every evaluation URL includes it, so the flow works from any device with no
account and no dependence on cookies. The session only remembers the token so
the nav bar has somewhere to point; authorisation always comes from the URL.

### Words are worked in groups, commonest first

Words are banded by how often they occur in the source corpus. Nothing in a
group is handed out until the group above it is closed, so the vocabulary people
actually use is settled first and a half-finished project is still usable.

| Group | Occurrences | Words |
|-------|-------------|-------|
| 1 | 50+ | 11,206 |
| 2 | 20–49 | 11,184 |
| 3 | 10–19 | 16,466 |
| 4 | 5–9 | 32,158 |
| 5 | 1–4 | 407,808 |

Bands come from raw occurrence counts, not the percentage column in the source
dataset: that is rounded to four decimals, so 91% of words tie at 0.0000 and it
cannot order the long tail.

### Work is leased, not allocated

Nothing is reserved at signup. A volunteer is handed as many words as their
daily quota allows, drawn from the group being worked on, closest-to-agreement
first so a group converges rather than accumulating half-voted entries. A lease
expires after `LEASE_DAYS` and the word returns to the queue, so a word parked
with someone who stopped coming is never lost. See `shola/tiers.py`.

The daily quota is still the brief's arithmetic — an annual commitment spread
over the days they chose — but it now sets the size of each day's lease instead
of carving up a fixed list a year in advance.

### Every language is settled separately

Vote counts live in `word_state`, keyed by **word and language**. Two Twi
speakers agreeing says nothing about Ga, so each language keeps its own counts,
its own confirmed/contested flags and its own position in the groups. Lease
counting is language-scoped too: a Twi speaker holding a word does nothing
towards settling it in Ga.

### Missing days costs nothing

A send calls `release_stale()` first, handing back anything from an earlier day,
then leases a fresh list. Nothing accumulates: a missed day is not a debt to
work through later, and the words go where they are useful — to another speaker
— rather than sitting with someone who is busy. `pending_today()` still selects
everything due on or before today, so a list opened just after midnight or a
link followed hours late still works.

There used to be a `redistribute-missed` command that re-dated overdue words
across the coming weeks. It is gone: with nothing carried over there is nothing
to re-date, and it had been scheduling words for days long after their lease
would have handed them to someone else.

### When agreement never comes

Voting between the three options always resolves: the worst case is one vote
each and the fourth verdict has to create a pair. Typed answers are free text
and unbounded, though, so five speakers can each write a different wording and
no pair ever forms. After `MAX_VERDICTS_BEFORE_CONTESTED` such answers the word
is closed as contested, every variant is kept, and the group can finish.

### Languages not open yet

Anyone can sign up for one of 84 other Ghanaian languages. They confirm their
email as usual, then land on a page telling them they are on the list. No words
are leased to them and the daily mail skips them, so nobody ever opens a link to
an empty queue. `shola waitlist` shows who is waiting and for what.

Opening a language takes three steps:

```bash
# 1. load three candidate translations per word for the new language
flask --app wsgi shola import-words --csv <translated>.csv --freq-csv <source>.csv

# 2. add it to LANGUAGES in shola/config.py: name, special characters, long-press map

# 3. lease everyone a first list and tell them it is open
flask --app wsgi shola announce-language --language <code>
```

Step 3 matters: without it, waiting volunteers would receive an ordinary daily
list on their next chosen day with no explanation, and the waiting page promises
otherwise.

### Knowing how many volunteers to recruit

`/stats` shows what it would take to finish the current group in a year. It
counts what each open word still lacks, then divides by what one recruit can be
expected to deliver: the annual commitment times `SHOLA_COMPLETION_RATE`,
default 0.30. Volunteers who do not finish are counted as contributing nothing,
which is harsher than reality on purpose — hitting the number should put the
project ahead of the year, not behind it.

At the time of writing that is 75 volunteers per language for group 1, 300
across all four.

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
0 4  * * *  cd /srv/shola && .venv/bin/flask --app wsgi shola release-leases
```

`release-leases` returns words nobody answered to the queue. Without it a word
leased to someone who stopped coming would sit unavailable for good.

Check what would go out without sending anything:

```bash
flask --app wsgi shola send-daily --dry-run
```

## Getting the results out

```bash
flask --app wsgi shola export --language twi --min-votes 2 --out twi-agreed.csv
flask --app wsgi shola stats
```

Or over HTTP — see **[/api](https://sholaproject.org/api)** for the documented
endpoint:

```
GET /api/words/<language>?min_votes=2&limit=1000&offset=0&format=csv
GET /api/entry/<id>/<language>      # every wording given for one word
```

`/api/vocabulary/<language>` and `/api/consensus/<language>` still resolve to the
same handler, so older links keep working.

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
├── config.py        # settings + the four open languages and their characters
├── languages.py     # 84 Ghanaian languages people can join the list for
├── models.py        # Volunteer, Word, Candidate, WordState, Assignment, Evaluation
├── tiers.py         # groups, per-language vote state, the work queue
├── assignment.py    # verdicts, day spreading, redistribution, leaderboard
├── consensus.py     # votes -> confirmed translation
├── mailer.py        # Gmail SMTP + signed daily links
├── views.py         # routes
├── cli.py           # import-words, send-daily, release-leases, backup, export
├── templates/
└── static/
tests/test_flow.py   # end-to-end checks
```

## Tests

```bash
python3 tests/test_flow.py
```

Covers the parts most likely to break: group ordering and commonest-first
leasing, a word leaving the queue once two agree, **a word settled in one
language still being asked in the others**, contested words closing so a group
can finish, lease expiry, day scheduling, missed-day carry-forward and
redistribution, one-verdict-per-word and revising it, typed answers counting
equal to the options, single votes *not* counting as agreement, the recruitment
arithmetic, signup by one-time code creating nothing until confirmed, the
waiting list, evaluation working with no cookies at all, tampered links being
refused, and the closed sheet staying pointer-transparent.

## Deploying

```bash
COOLIFY_TOKEN=... ./deploy.sh
```

The app builds from GitHub, so deploying is a push plus a Coolify build. The
script refuses to run with a dirty working tree, because pushing is the deploy —
uncommitted changes simply would not ship.

## Running on Coolify

The repository builds as a container, matching the other apps on that server.

```
build pack   dockerfile
port         8000
volume       /app/instance      <-- required
```

**Mount the volume.** The SQLite database lives in `/app/instance`; without a
persistent mount every redeploy erases the volunteers and every answer given.

Environment:

```
SHOLA_SECRET_KEY      generate: python3 -c "import secrets;print(secrets.token_urlsafe(48))"
SHOLA_SITE_URL        https://sholaproject.org
SHOLA_SMTP_HOST       smtp.gmail.com
SHOLA_SMTP_PORT       587
SHOLA_SMTP_USER       the sending address the app password belongs to (michseth@ghananlp.org)
SHOLA_SMTP_PASSWORD   a Gmail app password, not the account password
SHOLA_MAIL_FROM_NAME  SHOLA
SHOLA_OLD_HOSTS       hostnames to 301 to SHOLA_SITE_URL (default shola.inkika.org)
SHOLA_WORDS_PER_DAY   words in one send until a volunteer chooses (default 5)
SHOLA_WORDS_PER_SEND_MIN     least a volunteer may choose (1)
SHOLA_WORDS_PER_SEND_MAX     most a volunteer may choose (100)
SHOLA_MISSES_BEFORE_NUDGE    unanswered sends before offering weekly (3)
```

On first boot the container fetches the published dataset and imports it —
38 MB down, a few minutes to load 478,822 words. It is skipped on later boots,
so a redeploy is quick. `SHOLA_SKIP_SEED=1` disables it entirely.

Scheduled tasks, as Coolify scheduled tasks on the same container:

```
0 7 * * *     flask --app wsgi shola send-daily --window morning
0 13 * * *    flask --app wsgi shola send-daily --window afternoon
0 18 * * *    flask --app wsgi shola send-daily --window evening
0 4 * * *     flask --app wsgi shola release-leases
15 1 * * *    flask --app wsgi shola backup --keep 14
```

### Backups

Off-site upload is done by this command rather than Coolify's S3 feature.
Coolify performs the upload in a helper container, and when that image is
missing it records the backup as **success** while nothing leaves the machine —
which is how four databases here went five days with no off-site copy while the
dashboard stayed green. `shola backup` uploads directly, verifies the stored
object's size, and exits non-zero if anything failed.

```
SHOLA_S3_BUCKET       eced-fln-platform
SHOLA_S3_ENDPOINT     https://<account>.r2.cloudflarestorage.com
SHOLA_S3_ACCESS_KEY   R2 access key
SHOLA_S3_SECRET_KEY   R2 secret key
SHOLA_S3_PREFIX       shola          (default)
```

Without `SHOLA_S3_BUCKET` the backup stays local and says so.

Coolify's scheduled backups only cover the databases it manages, so nothing
backs up a SQLite file inside an application volume. `shola backup` writes two
things into `instance/backups`:

- a consistent compressed copy of the database, made through SQLite's backup
  API rather than by copying a file that may be mid-write
- `volunteers-*.json.gz`: every volunteer and every answer they have given

The second is the one that matters. Words and translations can be re-imported
from the published dataset in minutes; a volunteer's email, the days they chose
and the answers they gave exist nowhere else, and the file is a few hundred
kilobytes. Both land inside the mounted volume, so copy them off the host as
well if the data matters.

## The live deployment

[sholaproject.org](https://sholaproject.org) runs on the Coolify VPS
(`82.29.179.121`), built from this repository:

| | |
|---|---|
| App | Coolify application `fj2jijjl9gavv683vcfuhuep`, dockerfile build pack |
| Server | gunicorn, 3 gthread workers, port 8000 in the container |
| Public address | Cloudflare proxied A record → the VPS; Coolify terminates and routes by hostname |
| Database | SQLite on a persistent volume at `instance/shola.db`, all 478,822 words loaded |
| Email | Gmail SMTP as `michsethowusu@gmail.com`, port 587 STARTTLS |
| Schedule | Coolify scheduled tasks: `send-daily` at 07:00 / 13:00 / 18:00, `release-leases` 04:00, `backup` 01:15 |

Deploy with `COOLIFY_TOKEN=... ./deploy.sh`: it pushes to GitHub, triggers the
build, and waits for the new commit to appear in `/healthz`. Waiting for a 200
is not enough — the old container answers 200 throughout a rolling update, which
reported success early twice before the commit check was added.

`/healthz` reports the serving commit and whether SMTP is configured:

```bash
curl -s https://sholaproject.org/healthz
# {"build":"34c3bef","email":true,"ok":true,"today":"2026-08-03"}
```

### Moving to another hostname

1. Point the new name at the VPS with a proxied Cloudflare A record.
2. Add it to the application's domains in Coolify, keeping the old one so it
   still resolves.
3. Set `SHOLA_SITE_URL` to the new address, and put the old hostname in
   `SHOLA_OLD_HOSTS` (comma-separated).

The app 301s any request arriving on a hostname in `SHOLA_OLD_HOSTS`, so old
links and anything already printed keep working. Only those hostnames are
redirected, not everything that is not canonical: Coolify's health check
arrives with a container hostname, and redirecting that would fail every
deploy.

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

## Media kit

Artwork and captions for recruiting volunteers live in `brand/`, are served at
[sholaproject.org/brand](https://sholaproject.org/brand), and are published as a
[release asset](https://github.com/michsethowusu/SHOLA/releases/latest/download/shola-brand-kit.zip).

```bash
python3 brand/build.py     # regenerate the artwork after a brand change
```

Assets are HTML templates rendered by headless Chrome at exact pixel sizes, so
they use the site's own typeface and palette rather than drifting from it.

## Licence

The **code** in this repository is MIT licensed; see `LICENSE`.

The **words** collected through SHOLA — the translations volunteers confirm —
are released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), the same licence as
[GhanaNouns](https://github.com/GhanaNLP/GhanaNouns). Use them for anything,
including commercially, as long as you credit SHOLA and Ghana Open Data.

Two licences because they cover different things: MIT is a software licence and
says nothing sensible about a word list, while CC BY is written for data and
asks for the attribution the volunteers deserve.
