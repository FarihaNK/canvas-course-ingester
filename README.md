# Canvas Course Ingester

A command-line tool that mirrors everything in your Canvas LMS courses into
local, searchable files, writes a short study note for each piece of content
using Claude, and (optionally) messages you on Telegram about new material,
upcoming quizzes and exams, and assignment deadlines.

Point it at your courses, run one command, and get a folder per course with
the syllabus, announcements, assignments, module pages, discussions, every
attached document (PDF, DOCX, PPTX), and the external readings those pages
link to, all converted to plain markdown, plus a jot-note summary next to
each one. A second, optional command turns that data into Telegram reminders.

Both commands run only when you run them. There is no scheduler and no
background process. Course text is sent to the Anthropic API for
summarization, and the notifier (if you use it) sends messages to your own
Telegram chat; nothing else leaves your machine.

## Features

- **Broad ingestion.** Syllabus, announcements, assignments, quizzes, modules
  (including page bodies), discussions and their top-level replies, the course
  Files tab, and calendar events.
- **Follows attachments wherever they hide.** Files are picked up from
  structured attachment fields *and* from links buried in HTML bodies
  (assignment descriptions, module pages, announcements, discussion replies).
- **Follows external links.** Any non-Canvas link found in course content
  (articles, PDFs, readings) is fetched and converted too.
- **Text extraction.** PDF (`pdfplumber`), DOCX (`python-docx`), PPTX
  (`python-pptx`), and plain text/markdown.
- **AI study notes.** A concise bullet-point summary for each new or changed
  item with enough text to summarize.
- **Incremental and idempotent.** Content hashing detects new, changed, and
  unchanged items; re-runs only do work for what changed, and anything missing
  on disk is backfilled.
- **Fault tolerant.** Each content type syncs independently, so one failing
  endpoint (a disabled feature, a permissions error) logs a warning and the
  rest of the run continues.
- **Telegram notifications (optional).**
  - *New content:* the study note for each new announcement, file,
    discussion or module item.
  - *Quiz/exam countdowns:* a review checklist at 10, 5, 2 and 1 days out,
    built from that course's recent material.
  - *Assignment pacing:* a milestone plan for each upcoming assignment, then
    a reminder on each milestone day. It's a work *schedule* (what to do and
    when), never the assignment content itself.
- **Never repeats itself.** Every message is logged, and the first run marks
  existing content as already seen instead of flooding you. `--dry-run`
  previews everything without sending.

## How it works

```
Canvas API --> canvas_sync.py --> SQLite (canvas_data.db)    what was seen, content hashes
                    |
                    +--> ingested/<course_id>/<category>/    raw files + markdown copies
                    +--> notes/<course_id>/<category>/       Claude-generated jot notes

Links found in course content --> fetched without Canvas credentials --> category "readings"

canvas_data.db + notes/ --> notifier.py --> Telegram         (optional, separate command)
```

`<category>` is one of `syllabus`, `announcements`, `assignments`, `quizzes`,
`modules`, `files`, `discussions`, `readings`.

Example output for one course:

```
ingested/12345/
├── syllabus/Syllabus_12345.md
├── assignments/Essay_1_99001.md
├── modules/
│   ├── Week_1_Overview_88001.md
│   ├── Week_1_Slides_5521.md          <- text extracted from the PDF
│   └── raw/Week 1 Slides.pdf          <- the original download
└── readings/Some_Article_ab12cd34ef567890.md

notes/12345/
├── modules/Week_1_Slides_5521.md      <- bullet-point summary
└── readings/Some_Article_ab12cd34ef567890.md
```

### Design decisions

- **One polymorphic `items` table** keyed by `type`, rather than a table per
  Canvas resource. Adding a new content type needs no schema change. See
  [DESIGN.md](DESIGN.md) for the full rationale.
- **Two kinds of "already done" check.** The content hash says whether the
  *content* changed; a file-existence check says whether the *output* is
  present. Either one failing triggers a (re)write, so adding a feature never
  needs a migration script.
- **Credential isolation.** Canvas calls go through an authenticated session;
  external reading links are fetched with a separate, unauthenticated request
  so the Canvas token can never be sent to a third-party host.
- **Files are filed by where they were found.** A file attached to an
  assignment lands under `assignments/`; the same file in a module also lands
  under `modules/`. The Files-tab walk only keeps genuine orphans.
- **Loosely coupled scripts.** The notifier shares only the database and the
  `notes/` folder with the sync, so either can be replaced (e.g. email instead
  of Telegram) without touching the other. It reuses the notes the sync
  already wrote instead of summarizing everything twice, and creates its own
  tables so the sync works without it.

## Quick start

Requires Python 3 (developed and run on 3.13), a Canvas account with an API
token, and an Anthropic API key.

```bash
git clone <this-repo> && cd <this-repo>
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

| Variable | What it is |
|---|---|
| `CANVAS_BASE_URL` | Your school's Canvas address, e.g. `https://your-school.instructure.com` |
| `CANVAS_API_TOKEN` | Canvas -> Account -> Settings -> Approved Integrations -> **+ New Access Token** |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `COURSE_IDS` | Optional, comma-separated course IDs to limit the sync. Blank syncs every active course. |
| `INGESTED_DIR`, `NOTES_DIR`, `DB_PATH` | Optional output locations |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Only for the notifier. See below. |

List your courses and their IDs, then run the sync:

```bash
python canvas_sync.py --list-courses
python canvas_sync.py
```

The first run on a course with a lot of history makes one Claude call per
piece of content worth summarizing, so it can take a while and use some API
credit. Later runs only process what's new or changed.

### Telegram notifications (optional)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Message your new bot anything, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number in
   `"chat":{"id": ...}`.
3. Put both in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Run the sync first, then preview and send:

```bash
python notifier.py --dry-run     # shows what would be sent; sends and records nothing
python notifier.py               # sends
```

On the first real run the notifier marks everything already synced as seen
rather than sending it all; pass `--send-existing` if you actually want that
backlog. After that, each run only sends what's new. Assignment milestone
plans and quiz review checklists are generated with Claude, so those runs use
some API credit.

### Summarize a one-off file

For a document that isn't reachable through the Canvas API:

```bash
python manual_summarize.py --course 12345 path/to/file.pdf
```

This extracts the text and writes a note under `ingested/12345/manual/` and
`notes/12345/manual/`. It doesn't touch the database or call Canvas.

## Project layout

```
canvas_sync.py        # the ingester: API client, extraction, sync logic, notes
notifier.py           # optional Telegram notifications, reads the sync's output
manual_summarize.py   # standalone summarizer for local files
schema.sql            # SQLite schema for the sync (courses, items)
DESIGN.md             # architecture and design rationale
.env.example          # configuration template
```

## Limitations

- Discussion replies are top-level only; nested reply threads are skipped.
- Some Canvas endpoints depend on instructor permissions. The classic Quizzes
  API and the Files-tab listing can return 404/403 on some courses; those are
  logged and skipped, and the rest of the sync continues.
- External pages that need a login, a paywall, or JavaScript rendering can't
  be fetched. Only HTML and PDF responses are handled.
- Images and spreadsheets are downloaded but produce no text or notes.
- Only course content you already have access to is fetched, using your own
  token.
- The notifier only reports what's in your local database, so it's only as
  current as your last sync. It sends plain text (no formatting), and
  messages are capped at Telegram's length limit.

## Privacy

Everything is stored locally. Discussion content can include other students'
posts, so treat the generated database and folders as private: don't commit or
share them. Text sent for summarization goes to the Anthropic API under your
own key, and notifier messages go only to your own Telegram chat. The
repository's `.gitignore` excludes `.env`, the database, and all
ingested/generated content for this reason.
