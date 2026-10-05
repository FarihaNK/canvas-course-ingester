-- schema.sql
-- Shared database. canvas_sync.py writes `courses` and `items`. notifier.py
-- reads those and writes `notifications` and `milestones`.

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY,     -- Canvas course id
    name        TEXT NOT NULL,
    course_code TEXT,
    term        TEXT,
    last_synced_at TEXT
);

-- One row per "thing" pulled from Canvas or reached from it: syllabus,
-- announcement, assignment, quiz, discussion, discussion_entry (a reply),
-- module_item (page/link/subheader inside a module), file, calendar_event,
-- or web_link (an external page fetched via a link found in course content).
-- `content_hash` lets us detect edits to existing items, not just brand-new
-- ones -- that in turn drives whether the on-disk ingested copy / note gets
-- regenerated (see canvas_sync.py's maybe_write_ingested_text / maybe_generate_note).
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,      -- "{type}:{canvas_id}" e.g. "assignment:88213"
    course_id     INTEGER NOT NULL,
    type          TEXT NOT NULL,         -- syllabus | announcement | assignment | quiz | discussion | discussion_entry | module_item | file | calendar_event | web_link
    title         TEXT,
    body_text     TEXT,                  -- HTML stripped to plain text (or fetched webpage text, for web_link)
    extracted_text TEXT,                 -- text pulled from an attached file (pdf/docx/pptx), if any
    url           TEXT,                  -- html_url from Canvas, or the external URL for web_link
    posted_at     TEXT,                  -- created_at / posted_at from Canvas
    due_at        TEXT,                  -- for assignments/quizzes/calendar_events
    updated_at    TEXT,                  -- Canvas's updated_at
    content_hash  TEXT,                  -- md5 of title+body+extracted_text, to detect real changes
    first_seen_at TEXT,                  -- when *we* first saw it
    last_synced_at TEXT,
    raw_json      TEXT,                  -- full original API response, kept for debugging / re-processing
    converted_md_path TEXT,              -- for type='file' rows: path to the plain-text/markdown copy
    FOREIGN KEY (course_id) REFERENCES courses(id)
);

CREATE INDEX IF NOT EXISTS idx_items_course_type ON items(course_id, type);
CREATE INDEX IF NOT EXISTS idx_items_due_at ON items(due_at);

-- Every message the notifier has sent, so it never repeats itself.
-- kind examples: 'new_content', 'exam_countdown:10d', 'exam_countdown:1d',
-- 'milestone_plan_created', 'assignment_milestone:<milestone id>'
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    message     TEXT,
    UNIQUE(item_id, kind),
    FOREIGN KEY (item_id) REFERENCES items(id)
);

-- Generated milestone plans for assignments, created once per assignment.
-- Kept separate from `items` since these are notifier-generated, not
-- pulled from Canvas.
CREATE TABLE IF NOT EXISTS milestones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id        TEXT NOT NULL,        -- the assignment this belongs to
    milestone_date TEXT NOT NULL,
    label          TEXT NOT NULL,        -- e.g. "Outline + sources"
    detail         TEXT,                 -- what to actually do that day
    FOREIGN KEY (item_id) REFERENCES items(id)
);
