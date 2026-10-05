# Design Doc: Canvas Course Ingester

## 1. Purpose & scope

Two independent, manually-run scripts that share a SQLite file:

1. **`canvas_sync.py`** ingests everything reachable from a Canvas
   course — syllabus, announcements, assignments, quizzes, modules (and
   the files/pages inside them), the Files tab, discussions and their
   top-level replies, all attached files, and any external
   website/reading link found in any of that content — into local,
   inspectable files and a database, and writes a short jot-note summary
   for each piece of content.
2. **`notifier.py`** reads that data and sends Telegram messages: new
   content notes, exam/quiz countdowns with review checklists, and
   assignment work-pacing reminders.

**Explicit non-goals:** neither script runs on a schedule — only when
invoked by hand. Generated text is always a digest or a *schedule*
(what to work on and when); the system never drafts or generates content
for submission.

## 2. Requirements

| # | Requirement | Notes |
|---|---|---|
| R1 | Pull syllabus, announcements, assignments, quizzes, modules, Files tab, discussions, and discussion replies from every active course | Files include PDFs, DOCX, PPTX |
| R2 | Follow every file attachment, wherever it's attached (announcement, assignment, module, discussion, discussion reply, or a bare link inside any of those) | |
| R3 | Follow every external (non-Canvas) link found in any synced content and ingest that page's content too | "website content" / "readings" |
| R4 | Run only when manually invoked | No scheduler, cron job, or background process of any kind |
| R5 | Detect *new* vs *changed* vs *unchanged* content, and re-run cheaply | Avoid redundant downloads/API calls/summaries on unchanged content |
| R6 | Generate a short jot-note summary for each new/changed piece of content with enough text to summarize | Written to disk by `canvas_sync.py`; only `notifier.py` ever sends anything |
| R7 | Never leak the Canvas API token to an external site | External link fetches must not reuse the authenticated Canvas session |
| R8 | Push notifications for new content, upcoming quizzes/exams, and assignment milestones | Telegram; opt-in (needs a bot token), `--dry-run` previews |
| R9 | Never send a duplicate notification, and never flood on first run | Idempotent across repeated runs; existing content is baselined, not blasted |
| R10 | The two scripts must be independently replaceable | Coupled only through the database schema; neither imports the other |

## 3. High-level architecture

Two processes, each run by hand, connected only through a shared
database file. Neither imports the other.

```
Canvas --> [canvas_sync.py] --> canvas_data.db (SQLite)
       |                    |
       |                    +--> ingested/<course_id>/<category>/   (raw + converted text)
       |                    +--> notes/<course_id>/<category>/      (Claude jot-note summaries)
       v
External links found in synced content --> fetched directly (unauthenticated) --> same two trees, category=readings

canvas_data.db --> [notifier.py] --> Telegram
                        |
                        +--> Claude (summaries, review checklists, milestone plans)
```

- **Compute:** run manually from the command line. No cron, no CI
  workflow, no always-on process.
- **Data store:** SQLite, single file, tracks what's been ingested and
  its content hash. Single writer, zero ops overhead, trivially
  portable/inspectable (`sqlite3 canvas_data.db "select ..."`).
- **Secrets:** `.env` file, loaded via `python-dotenv`, holding the
  Canvas API token, an Anthropic API key, and (for the notifier) a
  Telegram bot token and chat ID. Never in source control;
  `.env.example` documents the required keys without values.
- **File storage:** `ingested/<course_id>/<category>/` for raw +
  converted content, `notes/<course_id>/<category>/` for summaries.
  `<category>` ∈ `syllabus, announcements, assignments, quizzes,
  modules, files, discussions, readings, manual`.
- **External services:** Canvas REST API (personal access token),
  Anthropic API (summaries, review checklists, milestone plans),
  Telegram Bot API (notifications), and arbitrary external URLs found in
  course content (unauthenticated fetches only — see R7 below).

## 4. Data model

Single SQLite file, four tables, all defined in `schema.sql`. Both
scripts apply it on startup (`CREATE TABLE IF NOT EXISTS`), so either can
run first. `canvas_sync.py` writes `courses` and `items`; `notifier.py`
only reads those and writes `notifications` and `milestones`.

```
courses ----1:N---- items ----1:N---- notifications
                       |
                       1:N
                       |
                    milestones
```

- **`notifications`** — append-only log of every message sent (or
  baselined), keyed by `(item_id, kind)` with a `UNIQUE` constraint.
  This is the R9 idempotency mechanism: "have I sent this kind of
  message for this item?" is one indexed lookup.
- **`milestones`** — generated pacing plans for assignments. Kept
  separate from `items` because they're notifier-generated, not Canvas
  data, so `items` stays a faithful mirror of what Canvas returned.

- **`courses`** — one row per active enrollment.
- **`items`** — one row per piece of content, whatever its origin:
  `syllabus`, `announcement`, `assignment`, `quiz`, `discussion`,
  `discussion_entry` (a reply), `module_item`, `file`, `calendar_event`,
  or `web_link` (a fetched external page). One shape, discriminated by
  `type`.
  - `content_hash` = md5(title + body + extracted_text). Comparing this
    against the stored value on each run is what drives R5: unchanged
    hash → skip re-writing the ingested copy and skip regenerating the
    note (an existence check on those files is also part of this — see
    §5, "backfill").

## 5. Module design — `canvas_sync.py`

**Responsibility:** after running, `canvas_data.db`, `ingested/`, and
`notes/` reflect what's currently on Canvas (plus whatever's linked from
it). No messaging, no scheduling.

| Component | Real functions | Responsibility |
|---|---|---|
| `CanvasClient` | `api_get_all()`, `SESSION` | Auth'd HTTP + pagination against the Canvas REST API. The only place that knows about Canvas's URL shapes or auth headers. |
| `HtmlNormalizer` | `strip_html()`, `find_linked_file_ids()`, `find_external_links()` | Canvas returns rich HTML in every text field. `strip_html` collapses it to plain text; the other two scan the *raw* HTML (before stripping) for Canvas file links and external links, respectively, since stripping loses `href` attributes. |
| `FileExtractor` | `download_file()`, `extract_text_from_file()` | Downloads a file once per category (idempotent — skips if already on disk), dispatches to `pdfplumber` / `python-docx` / `python-pptx` by extension. |
| `ExternalFetcher` | `fetch_external_page()` | Fetches a non-Canvas URL with a bare, unauthenticated `requests.get()` — deliberately never the Canvas-authenticated `SESSION` — so the Canvas bearer token can never leak to a third-party site (R7). Extracts text from HTML or PDF responses; anything else is skipped. |
| `ItemRepository` | `upsert_item()`, `get_db()` | The only code that touches the `items` table. Computes `content_hash`, decides new/updated/unchanged. |
| `IngestedWriter` / `NotesWriter` | `maybe_write_ingested_text()`, `maybe_generate_note()`, `ingested_dir()`, `notes_dir()` | Write the per-category markdown mirror and the Claude summary. Both are "backfill-aware": they write/regenerate whenever the DB status isn't `unchanged`, *or* when the expected file is missing on disk — so content already in the DB from before this feature existed still gets its folder entry and note on the next run, without needing a full re-sync. |
| `ClaudeSummarizer` | `ask_claude()`, `summarize_text()` | Thin wrapper around the Messages API, used for every jot-note summary. |
| `SyncEngine` | `sync_syllabus()`, `sync_announcements()`, `sync_assignments()`, `sync_quizzes()`, `sync_discussions()`, `sync_files_and_modules()`, `sync_calendar_events()`, `sync_all_files()`, `sync_external_links()`, `sync_course()`, `main()` | Orchestrates one sync function per content type, each independently error-handled so one endpoint failing doesn't abort the whole run. |

**Category routing:** every ingested/noted item is filed under a
category folder matching where it was found — a file attached to an
assignment lands in `assignments/`, the same file structurally attached
to a module also lands in `modules/` (deliberate: each context is a
legitimate view, not a duplicate to be deduped). The one place dedup
*does* happen is the generic Files-tab walk (`sync_all_files`), which
skips any file ID already seen via a more specific category this run, so
the Files tab only catches genuine orphans.

**Quiz handling:** a quiz that's a graded Canvas assignment is captured
by `sync_assignments` (type=`quiz`, keyed by assignment id). `sync_quizzes`
separately catches practice quizzes/surveys that have no assignment
(keyed by quiz id) — the `assignment_id` check prevents the same quiz
being ingested twice under two different item ids.

**Discussion replies:** `sync_discussions` calls the `/entries` endpoint,
which returns top-level replies only (not deeper nested threads) — a
deliberate scope decision to avoid pulling in every back-and-forth on an
active thread.

**External links (R3):** every `sync_*` function that touches raw HTML
passes it to `find_external_links()` and adds the results to a
per-course `external_links` set. After every other content type for a
course has been synced, `sync_external_links()` fetches each one and
files it under `readings/`. A safety cap
(`MAX_EXTERNAL_LINKS_PER_COURSE = 100`) prevents a course with an
unusually link-heavy syllabus from turning into an unbounded crawl.
Fetch failures (dead links, non-HTML/PDF content, timeouts) are recorded
in the DB with empty text so they don't get retried every single run,
but produce no ingested file or note.

**Idempotency & change detection (R5):** every sync run recomputes
hashes and upserts, same as before. What's new is that "already
ingested" is checked two ways: the DB hash (has the *content* changed?)
and file existence (does the *ingested copy / note* already exist on
disk?). Either one being "no" triggers a (re)write. This makes the
backfill on first run after a schema/feature change automatic, without
special-casing a migration script.

**Failure handling:** each `sync_*` function is wrapped in its own
`try/except requests.HTTPError` at the orchestration level (`sync_course`'s
`run()` helper), so one course's discussion-entries 404 or a dead
module page doesn't abort the whole run.

**Rate limits & cost:** Canvas calls are bounded by course size — a few
dozen to a few hundred requests per course per run, well under personal-
scale rate limits. Claude calls are one per new/changed piece of content
with ≥40 characters of text, capped at 800 output tokens each; a first
run against a course with a full semester of history can trigger dozens
of calls, but every subsequent run only summarizes what's actually new.

**Privacy/FERPA:** `discussion`/`discussion_entry` sync pulls other
students' posts. This stays local to the SQLite file and local folders,
and is sent externally only to Anthropic (for summarization) and to
whatever external site a reading link points to (for that link's own
public page content) — never re-published, aggregated, or sent anywhere
else. The notifier sends the text of announcements, files, discussion
topics and module items to Anthropic for summarization (not individual
discussion replies), and its messages go only to your own Telegram chat.

**Security (R7):** `fetch_external_page()` uses a plain `requests.get()`,
never the Canvas-authenticated `SESSION` object, specifically so the
Canvas API bearer token in `SESSION`'s headers is never sent to a
third-party host reachable from course content.

## 6. Module design — `notifier.py`

**Responsibility:** read the database, decide what's newly worth telling
the user, generate the message content, send it, and record that it was
sent. Never touches the Canvas API.

| Component | Real functions | Responsibility |
|---|---|---|
| `NotificationLog` | `already_sent()`, `mark_sent()` | Wraps the `notifications` table. Every send path checks `already_sent()` *before* generating content, so no Claude call is wasted on something that won't be sent. |
| `TelegramSender` | `send_telegram()`, `deliver()` | The only delivery mechanism, isolated so swapping the channel (email, Discord) touches one place. Sends plain text with no `parse_mode`, since titles and summaries come from Canvas/Claude and a stray `*` or `_` would make Telegram reject a Markdown message. `deliver()` sends and logs in that order, so a failed send is reported, left unlogged and retried next run. |
| `ClaudeSummarizer` | `ask_claude()` | Thin wrapper around the Messages API; every "generate text for the user" call goes through it, so model/params change in one place. The client is created lazily so `--dry-run` needs no API key. |
| `NewContentPipeline` | `notify_new_content()` | R8. Scans `items` for announcements/files/discussions/module items with real text, skips anything under a length floor, summarizes it into jot notes, sends, logs. |
| `ExamCountdownPipeline` | `notify_exam_countdowns()`, `build_review_notes()` | R8. For quizzes/calendar events due within the lookahead window, fires at 10/5/2/1 days out, each threshold logged as its own `kind` so all four reminders are independent. The review checklist is generated from the course's 12 most recently synced items, not generic advice. Files carry their text in `extracted_text` and everything else in `body_text` (with `extracted_text` left as an empty string), so the query selects between them explicitly rather than using `COALESCE`, which would pick the empty string and silently drop every non-file item. |
| `MilestonePipeline` | `notify_assignment_milestones()`, `ensure_milestone_plan()`, `parse_milestone_plan()`, `check_due_milestones()` | R8. Generate-once: if an assignment has no milestone rows, ask Claude for a JSON pacing plan, validate it, persist it, and announce it. Check-daily: if today matches a stored milestone date, send that reminder. The parser tolerates a markdown code fence and rejects malformed plans before anything is written, so a bad response never leaves half a plan behind. |

**First-run baseline (R9):** if the `notifications` table is empty, the
notifier marks everything already synced as seen instead of sending it —
otherwise the first run would send one message per item ever ingested.
`--send-existing` overrides this. `--dry-run` prints what *would* be sent
and makes no Claude calls and no sends.

**Content-safety boundary:** `ensure_milestone_plan()`'s prompt asks for
a pacing plan, not assignment content, and requests a JSON array of
`{days_before_due, label, detail}` where `detail` describes an
*activity* ("outline your argument"), never an answer. No code path takes
assignment text and asks an LLM to produce submittable output; adding
one would be new code, not a flag flip.

**Failure handling:** a failed send is logged as a warning and *not*
recorded, so nothing is marked sent that wasn't and the next run retries
it. After three failures in a row (almost always a bad token or chat ID)
the run stops, so a misconfiguration doesn't spend a Claude call per
remaining item first. An unparseable milestone plan is logged and
skipped.

## 7. Extension points

- **Query the data:** anything downstream can read `canvas_data.db`
  directly, or just walk `ingested/`/`notes/` on disk — the schema and
  folder layout are the only contracts.
- **Swap the delivery channel:** replace `send_telegram()` in
  `notifier.py`. Nothing else changes.
- **Add a new Canvas resource:** add one `sync_*` function following the
  existing pattern (upsert via `ingest_text_item`/`ingest_file`, pick a
  category), no schema change needed.
- **Multi-user / bigger scale:** swap SQLite for Postgres behind the
  same repository functions; nothing in the orchestration logic assumes
  SQLite specifically except the connection string.
