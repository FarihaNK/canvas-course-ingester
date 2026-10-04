# Design Doc: Canvas Course Ingester

## 1. Purpose & scope

On manual command, ingest everything reachable from a Canvas
course — syllabus, announcements, assignments, quizzes, modules (and the
files/pages inside them), the Files tab, discussions and their top-level
replies, all attached files, and any external website/reading link found
in any of that content — into local, inspectable files and a database.
For each piece of content, also generate a short jot-note summary.

**Explicit non-goal:** this system never sends anything anywhere (no
notifications, no messaging integration) and never runs on a schedule —
only when invoked by hand. Its summaries are digests of existing
material; it never drafts or generates content for submission.

## 2. Requirements

| # | Requirement | Notes |
|---|---|---|
| R1 | Pull syllabus, announcements, assignments, quizzes, modules, Files tab, discussions, and discussion replies from every active course | Files include PDFs, DOCX, PPTX |
| R2 | Follow every file attachment, wherever it's attached (announcement, assignment, module, discussion, discussion reply, or a bare link inside any of those) | |
| R3 | Follow every external (non-Canvas) link found in any synced content and ingest that page's content too | "website content" / "readings" |
| R4 | Run only when manually invoked | No scheduler, cron job, or background process of any kind |
| R5 | Detect *new* vs *changed* vs *unchanged* content, and re-run cheaply | Avoid redundant downloads/API calls/summaries on unchanged content |
| R6 | Generate a short jot-note summary for each new/changed piece of content with enough text to summarize | Written to disk only — never sent anywhere |
| R7 | Never leak the Canvas API token to an external site | External link fetches must not reuse the authenticated Canvas session |

## 3. High-level architecture

A single process, run by hand, that mirrors Canvas (plus anything linked
from it) into local files and a database.

```
Canvas --> [canvas_sync.py] --> canvas_data.db (SQLite)
       |                    |
       |                    +--> ingested/<course_id>/<category>/   (raw + converted text)
       |                    +--> notes/<course_id>/<category>/      (Claude jot-note summaries)
       v
External links found in synced content --> fetched directly (unauthenticated) --> same two trees, category=readings
```

- **Compute:** run manually from the command line. No cron, no CI
  workflow, no always-on process.
- **Data store:** SQLite, single file, tracks what's been ingested and
  its content hash. Single writer, zero ops overhead, trivially
  portable/inspectable (`sqlite3 canvas_data.db "select ..."`).
- **Secrets:** `.env` file, loaded via `python-dotenv`, holding the
  Canvas API token and an Anthropic API key (for summaries). Never in
  source control; `.env.example` documents the required keys without
  values.
- **File storage:** `ingested/<course_id>/<category>/` for raw +
  converted content, `notes/<course_id>/<category>/` for summaries.
  `<category>` ∈ `syllabus, announcements, assignments, quizzes,
  modules, files, discussions, readings, manual`.
- **External services:** Canvas REST API (personal access token),
  Anthropic API (summaries), and arbitrary external URLs found in
  course content (unauthenticated fetches only — see R7 below).

## 4. Data model

Single SQLite file, two tables. Full DDL in `schema.sql`.

```
courses ----1:N---- items
```

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
else.

**Security (R7):** `fetch_external_page()` uses a plain `requests.get()`,
never the Canvas-authenticated `SESSION` object, specifically so the
Canvas API bearer token in `SESSION`'s headers is never sent to a
third-party host reachable from course content.

## 6. Extension points

- **Query the data:** anything downstream can read `canvas_data.db`
  directly, or just walk `ingested/`/`notes/` on disk — the schema and
  folder layout are the only contracts.
- **Add a new Canvas resource:** add one `sync_*` function following the
  existing pattern (upsert via `ingest_text_item`/`ingest_file`, pick a
  category), no schema change needed.
- **Multi-user / bigger scale:** swap SQLite for Postgres behind the
  same repository functions; nothing in the orchestration logic assumes
  SQLite specifically except the connection string.
