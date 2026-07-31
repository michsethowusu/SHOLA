# SHOLA — Share Your Language

**Live: [shola.inkika.org](https://shola.inkika.org)** — [sign
up](https://shola.inkika.org/join) · [champions](https://shola.inkika.org/champions)
· [progress](https://shola.inkika.org/stats) · [API](https://shola.inkika.org/api)

A volunteer app for confirming translations of everyday words in **Twi, Ewe, Ga
and Dagbani**.

Machine translation produced three candidate translations for each word. Much of
it is good, some is word-for-word and no speaker would say it, and some is
wrong. Software cannot tell the difference; a speaker can, in a couple of
seconds. SHOLA sends each volunteer a handful of words a day by email and
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
4. When two speakers of that language choose the same wording, the translation
   is confirmed and the word leaves the queue.

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

Words that go unanswered stay pending and appear in the next email —
`Volunteer.pending_today()` selects everything due *on or before* today. To stop
a missed week turning into one crushing list, `shola redistribute-missed`
re-dates overdue words across the volunteer's remaining days. Work is only ever
moved, never dropped.

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
0 3  * * 1  cd /srv/shola && .venv/bin/flask --app wsgi shola redistribute-missed
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

Or over HTTP — see **[/api](https://shola.inkika.org/api)** for the documented
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
├── cli.py           # import-words, send-daily, redistribute-missed, export
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
./deploy.sh
```

Use the script rather than typing rsync by hand. It excludes `.venv`, `.env`,
`instance/` and `seed/`, all of which exist only on the server — an `rsync
--delete` without those exclusions removes the server's virtualenv and takes the
site down.

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
SHOLA_SITE_URL        https://shola.inkika.org
SHOLA_SMTP_HOST       smtp.gmail.com
SHOLA_SMTP_PORT       587
SHOLA_SMTP_USER       the sending Gmail address
SHOLA_SMTP_PASSWORD   a Gmail app password, set in the Coolify UI rather than over the API
SHOLA_MAIL_FROM_NAME  SHOLA
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
30 3 * * 1    flask --app wsgi shola redistribute-missed
15 1 * * *    flask --app wsgi shola backup --keep 14
```

### Backups

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

[shola.inkika.org](https://shola.inkika.org) runs on the Inkika H200 box:

| | |
|---|---|
| Code | `/mnt/volume_d2wey28/projects/shola` |
| Service | `shola.service` — gunicorn, 3 workers, `127.0.0.1:8110` |
| Public address | Cloudflare named tunnel `ghana-tts`, ingress `shola.inkika.org` → `localhost:8110` |
| Database | SQLite at `instance/shola.db`, all 478,822 words loaded |
| Email | Gmail SMTP as `michseth@ghananlp.org`, port 587 STARTTLS |
| Schedule | cron at 07:00 / 13:00 / 18:00 for the three time windows, plus Monday 03:30 redistribute |

```bash
sudo systemctl status shola          # is it up
sudo journalctl -u shola -f          # what it is doing
tail -f /mnt/volume_d2wey28/projects/shola/mail.log   # what cron sent
```

Adding a hostname to the tunnel means editing `~/.cloudflared/config.yml` (new
rules go **above** the `http_status:404` catch-all), running `cloudflared tunnel
route dns ghana-tts <hostname>`, then restarting the tunnel. Note that
cloudflared only reads its config at startup, and that box has had stray
hand-started `cloudflared` processes outside systemd — if a config change
appears to do nothing, check `pgrep -af cloudflared` for a process no unit owns.

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
[shola.inkika.org/brand](https://shola.inkika.org/brand), and are published as a
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
including commercially, as long as you credit SHOLA and Inkika.

Two licences because they cover different things: MIT is a software licence and
says nothing sensible about a word list, while CC BY is written for data and
asks for the attribution the volunteers deserve.
