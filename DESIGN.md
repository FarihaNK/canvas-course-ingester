# Design

**TL;DR:** Two scripts that you run by hand. They share one SQLite database.
`canvas_sync.py` pulls Canvas content into files and notes. `notifier.py`
reads the database and sends Telegram messages.

## 1. Goals

- Capture everything in a Canvas course, including files hidden in links.
- Follow external reading links and capture those too.
- Re-run cheaply: only touch what's new or changed.
- Optional Telegram reminders that never repeat and never flood.
- Keep the Canvas token away from third-party sites.

**Non-goals**

- No scheduler, cron job or background process.
- Nothing is ever generated for submission. Output is summaries and
  *schedules* (what to work on, when).

## 2. Architecture

```
Canvas ──> canvas_sync.py ──> canvas_data.db
                │                  │
                ├─> ingested/      └──> notifier.py ──> Telegram
                └─> notes/                  │
                                            └─> Claude
External links found in content ──> fetched, filed under "readings"
```

The scripts are coupled only by the database schema. Neither imports the other.

| Piece | Choice |
|---|---|
| Storage | SQLite file, plus `ingested/` and `notes/` folders |
| Secrets | `.env` via `python-dotenv`, never committed |
| Services | Canvas API, Anthropic API, Telegram Bot API |

## 3. Data

All four tables live in `schema.sql`. Both scripts apply it on startup.

| Table | Written by | Holds |
|---|---|---|
| `courses` | sync | One row per active course |
| `items` | sync | One row per piece of content, any type |
| `notifications` | notifier | Every message sent. Prevents repeats |
| `milestones` | notifier | Generated pacing plans for assignments |

`items.type` is one of: `syllabus`, `announcement`, `assignment`, `quiz`,
`discussion`, `discussion_entry`, `module_item`, `file`, `calendar_event`,
`web_link`.

`items.content_hash` is an md5 of title, body and extracted text. It's how a
re-run tells new from changed from unchanged.

## 4. canvas_sync.py

**A run, per course:**

1. Syllabus
2. Announcements
3. Assignments and quizzes
4. Discussions and top-level replies
5. Modules (files and page bodies)
6. Calendar events (database only)
7. Files tab (orphans only)
8. External links found in steps 1-5

For each item: save to the database, write a markdown copy to `ingested/`, and
write a Claude study note to `notes/` if there's enough text.

**Key behaviours**

| Behaviour | How it works |
|---|---|
| Files hidden in text | Scans the raw HTML for `/files/<id>` links, since stripping HTML loses them |
| Filing by context | A file lands in the category where it was found (`assignments/`, `modules/`...) |
| Files tab | Skips any file already seen, so it only catches true orphans |
| Quizzes | Graded quizzes come via assignments. The quizzes endpoint adds the rest, skipping any with an `assignment_id` to avoid duplicates |
| Discussion replies | Top-level only, to avoid pulling in long nested threads |
| External links | Fetched without Canvas credentials. Capped at 100 per course per run. Failures are recorded so they aren't retried every run |
| Backfill | Writes a copy or note if the content changed **or** the file is missing on disk |

**Where to look in the code**

| Job | Functions |
|---|---|
| Canvas API calls | `api_get_all()`, `SESSION` |
| Finding links | `find_linked_file_ids()`, `find_external_links()` |
| Files | `download_file()`, `extract_text_from_file()` |
| Database | `upsert_item()`, `get_db()` |
| Output files | `maybe_write_ingested_text()`, `maybe_generate_note()` |
| External pages | `fetch_external_page()` |
| Orchestration | `sync_*()`, `sync_course()`, `main()` |

## 5. notifier.py

| Pipeline | Trigger | Message |
|---|---|---|
| New content | Announcement, file, discussion or module item not yet sent | Claude summary |
| Countdown | Quiz or calendar event due in 10, 5, 2 or 1 days | Review checklist from the course's 12 most recent items |
| Milestones | Assignment with a future due date | A pacing plan once, then a reminder on each milestone day |

**Rules**

- **No repeats.** Every message is logged in `notifications`, keyed by
  `(item_id, kind)`. The log is checked *before* calling Claude, so no credit
  is wasted on something that won't be sent.
- **No first-run flood.** If the log is empty, existing content is marked seen
  and not sent. `--send-existing` overrides this.
- **Safe preview.** `--dry-run` lists what would be sent. It makes no sends, no
  Claude calls and writes no records.
- **Failed sends retry.** A failed send isn't logged, so the next run tries
  again. Three failures in a row stop the run, so a bad token doesn't burn a
  Claude call per remaining item.
- **Plain text only.** Titles and summaries can contain characters that break
  Telegram's Markdown parser.

## 6. Decisions and why

| Decision | Why |
|---|---|
| One `items` table with a `type` column | Adding a content type needs no schema change |
| Check the hash **and** the file on disk | Adding a feature backfills automatically, with no migration script |
| Unauthenticated requests for external links | The Canvas token can never reach a third-party host |
| Pick `extracted_text` vs `body_text` explicitly in the checklist query | Files store text in `extracted_text`. Everything else leaves it as `''`, so `COALESCE` would pick the empty string and drop every non-file item |
| Validate a milestone plan before saving | A malformed Claude reply can't leave half a plan behind |
| Pacing plans are schedules only | No code path turns assignment text into submittable output |
| SQLite over a server | One user, low volume, easy to inspect |

## 7. Failure and safety

- **One endpoint failing** logs a warning and the sync continues (`sync_course()`
  wraps each step).
- **Timeouts:** 30s on Canvas calls and downloads, 15s on external pages and
  Telegram.
- **Cost:** one Claude call per new or changed item with at least 40
  characters, capped at 800 output tokens. The first run is the expensive one.
- **Privacy:** discussions can contain other students' posts. They stay local,
  apart from the text sent to Anthropic for summaries.

## 8. Extending

- **New Canvas content type:** add one `sync_*()` function that calls
  `ingest_text_item()` or `ingest_file()`. No schema change.
- **Different notification channel:** replace `send_telegram()` in
  `notifier.py`.
- **Bigger scale:** swap SQLite for Postgres behind `upsert_item()` and
  `get_db()`.
