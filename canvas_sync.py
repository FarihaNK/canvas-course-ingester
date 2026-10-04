"""
canvas_sync.py
==============
Pulls the syllabus, announcements, assignments, quizzes, discussions
(including top-level replies), modules (including page content), the
Files tab, and calendar events for all your active Canvas
courses. Any file attached anywhere, and any external (non-Canvas) link
found in any of that content -- readings, articles, etc. -- is also
fetched. Everything downloaded gets:

  1. upserted into canvas_data.db (change detection via content_hash)
  2. a plain-text/markdown copy under ingested/<course_id>/<category>/
  3. a short Claude-generated jot-note summary under
     notes/<course_id>/<category>/ (only when there's enough text to
     summarize, and only regenerated when the content actually changed)

This script does NOT send any message anywhere and is not run on any
schedule -- it only runs when you invoke it directly. All it does is
make the local `ingested/` and `notes/` folders (and canvas_data.db)
match what's currently on Canvas.

Setup
-----
1. Get a personal access token:
   Canvas -> Account -> Settings -> scroll to "Approved Integrations"
   -> "+ New Access Token"
2. Copy .env.example to .env and fill in CANVAS_BASE_URL, CANVAS_API_TOKEN,
   and ANTHROPIC_API_KEY (used for the jot-note summaries)
3. pip install -r requirements.txt
4. python canvas_sync.py
"""

import io
import os
import re
import json
import hashlib
import sqlite3
import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

CANVAS_BASE_URL = os.environ["CANVAS_BASE_URL"].rstrip("/")   # e.g. https://your-school.instructure.com
CANVAS_API_TOKEN = os.environ["CANVAS_API_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_PATH = os.environ.get("DB_PATH", "canvas_data.db")
INGESTED_DIR = Path(os.environ.get("INGESTED_DIR", "ingested"))
NOTES_DIR = Path(os.environ.get("NOTES_DIR", "notes"))

# File types that are already plain text and don't need conversion.
PLAIN_TEXT_SUFFIXES = {".txt", ".md"}

# Safety cap on how many external links get fetched per course per run.
MAX_EXTERNAL_LINKS_PER_COURSE = 100

HEADERS = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

claude_client = Anthropic(api_key=ANTHROPIC_API_KEY)

SUMMARY_SYSTEM_PROMPT = (
    "You summarize university course material into concise jot notes "
    "for a student. Use short bullet points, no preamble, no restating "
    "the title. Focus only on the main concepts, definitions, and any "
    "explicit action items (e.g. 'read chapter 4', 'bring calculator'). "
    "Max 8 bullets."
)

# Optional allowlist: comma-separated Canvas course IDs, e.g. "12345,67890"
# in .env. If unset or blank, ALL active courses are synced (old behavior).
_raw_course_ids = os.environ.get("COURSE_IDS", "").strip()
COURSE_IDS = {int(x) for x in _raw_course_ids.split(",") if x.strip()} if _raw_course_ids else None


# ---------- low-level API helpers ----------

def api_get_all(path, params=None):
    """GET a Canvas endpoint, following pagination, return list of JSON objects."""
    url = f"{CANVAS_BASE_URL}/api/v1{path}"
    params = params or {}
    params.setdefault("per_page", 100)
    results = []
    while url:
        resp = SESSION.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
        # pagination via Link header
        url = None
        params = None
        link = resp.headers.get("Link", "")
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
    return results


def strip_html(html):
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()


def content_hash(*parts):
    m = hashlib.md5()
    for p in parts:
        m.update((p or "").encode("utf-8", errors="ignore"))
    return m.hexdigest()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


def safe_stem(name):
    """Filesystem-safe version of a filename, without extension."""
    stem = Path(name).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "file"


def slug_for(item_id, title):
    """Filesystem-safe, length-capped filename stem. `title` can be an
    arbitrarily long URL (e.g. a web_link whose fetch failed falls back to
    its raw URL as the title), so this must cap length, not just characters,
    or long query strings blow past the OS filename limit (~255 bytes)."""
    suffix = item_id.split(":")[-1] if ":" in item_id else item_id
    base = safe_stem(title) if title else "item"
    return f"{base[:100]}_{suffix}"


def ask_claude(system, user):
    resp = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# ---------- folder layout: ingested/<course_id>/<category>/ and notes/<course_id>/<category>/ ----------

def ingested_dir(cid, category):
    d = INGESTED_DIR / str(cid) / category
    d.mkdir(parents=True, exist_ok=True)
    return d


def notes_dir(cid, category):
    d = NOTES_DIR / str(cid) / category
    d.mkdir(parents=True, exist_ok=True)
    return d


def ingested_text_file_path(cid, category, item_id, title):
    return ingested_dir(cid, category) / f"{slug_for(item_id, title)}.md"


def note_file_path(cid, category, item_id, title):
    return notes_dir(cid, category) / f"{slug_for(item_id, title)}.md"


def maybe_write_ingested_text(cid, category, item_id, title, text, url, status):
    """Writes the plain-text/markdown mirror of an item's content, skipping
    the write when nothing changed and the file is already there."""
    path = ingested_text_file_path(cid, category, item_id, title)
    if status == "unchanged" and path.exists():
        return str(path)
    header = (f"<!-- Source: {title or item_id} | Canvas item: {item_id} | "
              f"URL: {url or ''} | Synced at: {now_iso()} UTC -->\n\n")
    path.write_text(header + (text or ""), encoding="utf-8")
    return str(path)


def summarize_text(type_, title, text):
    try:
        return ask_claude(
            system=SUMMARY_SYSTEM_PROMPT,
            user=f"Type: {type_}\nTitle: {title}\n\nContent:\n{text[:8000]}",
        )
    except Exception as e:
        print(f"  [warn] summarization failed for {title!r}: {e}")
        return None


def maybe_generate_note(cid, category, item_id, type_, title, text, url, status):
    """Generates and writes a jot-note summary, skipping anything too short
    to summarize and skipping regeneration when nothing changed."""
    if len((text or "").strip()) < 40:
        return None
    path = note_file_path(cid, category, item_id, title)
    if status == "unchanged" and path.exists():
        return str(path)
    summary = summarize_text(type_, title, text)
    if not summary:
        return None
    header = (f"<!-- Notes for: {title or item_id} | Type: {type_} | "
              f"URL: {url or ''} | Generated at: {now_iso()} UTC -->\n\n")
    path.write_text(header + summary, encoding="utf-8")
    return str(path)


# ---------- file text extraction ----------

def extract_text_from_file(local_path, content_type):
    try:
        suffix = local_path.suffix.lower()
        if suffix == ".pdf":
            import pdfplumber
            text = []
            with pdfplumber.open(local_path) as pdf:
                for page in pdf.pages:
                    text.append(page.extract_text() or "")
            return "\n".join(text)
        elif suffix == ".docx":
            import docx
            d = docx.Document(local_path)
            return "\n".join(p.text for p in d.paragraphs)
        elif suffix == ".pptx":
            from pptx import Presentation
            prs = Presentation(local_path)
            chunks = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        chunks.append(shape.text_frame.text)
            return "\n".join(chunks)
        elif suffix in (".txt", ".md", ".csv"):
            return local_path.read_text(errors="ignore")
        else:
            return ""  # unsupported type, skip silently
    except Exception as e:
        print(f"  [warn] could not extract text from {local_path.name}: {e}")
        return ""


def download_file(file_obj, course_id, category, course_name=""):
    """file_obj is a Canvas File API object with 'url' and 'filename'.
    Raw bytes go under ingested/<course>/<category>/raw/, the extracted
    text goes alongside as a sibling .md file."""
    url = file_obj.get("url")
    if not url:
        return None, "", None
    raw_dir = ingested_dir(course_id, category) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    filename = file_obj.get("display_name") or file_obj.get("filename") or f"file_{file_obj['id']}"
    local_path = raw_dir / filename
    freshly_downloaded = not local_path.exists()
    if freshly_downloaded:
        resp = SESSION.get(url, timeout=30)
        resp.raise_for_status()
        local_path.write_bytes(resp.content)

    if local_path.suffix.lower() in PLAIN_TEXT_SUFFIXES:
        text = local_path.read_text(errors="ignore")
        return str(local_path), text, None

    text = extract_text_from_file(local_path, file_obj.get("content-type"))
    file_item_id = f"file:{file_obj['id']}"
    converted_path = ingested_dir(course_id, category) / f"{slug_for(file_item_id, filename)}.md"
    if freshly_downloaded or not converted_path.exists():
        header = (f"<!-- Converted copy. Source: {filename} | Course: {course_name} (id {course_id}) "
                  f"| Canvas file id: {file_obj.get('id')} | Converted at: {now_iso()} UTC -->\n\n")
        converted_path.write_text(header + (text or ""), encoding="utf-8")
    return str(local_path), text, str(converted_path)


# ---------- DB helpers ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    with open(Path(__file__).parent / "schema.sql") as f:
        conn.executescript(f.read())
    return conn


def upsert_item(conn, item_id, course_id, type_, title, body_text,
                 extracted_text, url, posted_at, due_at, updated_at, raw,
                 converted_md_path=None):
    h = content_hash(title, body_text, extracted_text)
    row = conn.execute("SELECT content_hash, first_seen_at FROM items WHERE id=?", (item_id,)).fetchone()
    ts = now_iso()
    if row is None:
        conn.execute("""
            INSERT INTO items (id, course_id, type, title, body_text, extracted_text,
                                url, posted_at, due_at, updated_at, content_hash,
                                first_seen_at, last_synced_at, raw_json, converted_md_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (item_id, course_id, type_, title, body_text, extracted_text, url,
              posted_at, due_at, updated_at, h, ts, ts, json.dumps(raw), converted_md_path))
        return "new"
    else:
        old_hash, first_seen = row
        conn.execute("""
            UPDATE items SET title=?, body_text=?, extracted_text=?, url=?,
                              posted_at=?, due_at=?, updated_at=?, content_hash=?,
                              last_synced_at=?, raw_json=?, converted_md_path=COALESCE(?, converted_md_path)
            WHERE id=?
        """, (title, body_text, extracted_text, url, posted_at, due_at, updated_at,
              h, ts, json.dumps(raw), converted_md_path, item_id))
        return "updated" if old_hash != h else "unchanged"


def ingest_file(conn, cid, file_obj, category, course_name="", stats=None):
    """Shared path for turning a Canvas file object (however we found it --
    Modules, an announcement's attachments array, a discussion reply, or a
    link inside HTML) into a downloaded + extracted + converted + noted
    items row. Returns the item id."""
    local_path, text, converted_path = download_file(file_obj, cid, category, course_name)
    item_id = f"file:{file_obj['id']}"
    title = file_obj.get("display_name") or file_obj.get("filename")
    status = upsert_item(conn, item_id, cid, "file", title, "", text,
                          file_obj.get("url"), file_obj.get("created_at"),
                          None, file_obj.get("updated_at"), file_obj,
                          converted_md_path=converted_path)
    if stats is not None:
        stats[status] += 1
    maybe_generate_note(cid, category, item_id, "file", title, text, file_obj.get("url"), status)
    return item_id


def fetch_file_object(cid, file_id):
    """Resolves a bare file ID (found via an HTML link) into a full Canvas
    file object, the same shape Modules/attachments already give us."""
    try:
        resp = SESSION.get(f"{CANVAS_BASE_URL}/api/v1/courses/{cid}/files/{file_id}", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def fetch_module_page(cid, page_url):
    if not page_url:
        return None
    try:
        resp = SESSION.get(f"{CANVAS_BASE_URL}/api/v1/courses/{cid}/pages/{page_url}", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def find_linked_file_ids(html):
    """Canvas often attaches a file to an assignment/announcement not via a
    structured field but as a plain link inside the description/message HTML,
    e.g. '.../courses/123/files/456/download?...'. This pulls out every
    referenced file ID so we can fetch and ingest those too."""
    if not html:
        return set()
    return {int(m) for m in re.findall(r"/files/(\d+)", html)}


def ingest_linked_files(conn, cid, html, category, course_name, stats=None, seen_ids=None):
    """Finds and ingests any files linked directly inside an HTML body
    (assignment descriptions rarely use a structured attachments field --
    the file is just a link in the text)."""
    seen_ids = seen_ids if seen_ids is not None else set()
    for file_id in find_linked_file_ids(html):
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)
        file_obj = fetch_file_object(cid, file_id)
        if file_obj:
            ingest_file(conn, cid, file_obj, category, course_name, stats)


def find_external_links(html):
    """Pulls every non-Canvas http(s) link out of a raw HTML body -- these
    are candidate 'readings'/'website content' links (external articles,
    PDFs, textbook pages, etc). Must be called on raw HTML, not stripped
    text, since stripping loses href attributes."""
    if not html:
        return set()
    links = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if a["href"].startswith("http"):
                links.add(a["href"])
    except Exception:
        pass
    links |= set(re.findall(r'https?://[^\s"\'<>\)]+', html))

    base_host = urlparse(CANVAS_BASE_URL).netloc
    cleaned = set()
    for u in links:
        u = u.rstrip(").,;'\"")
        host = urlparse(u).netloc
        if host and host != base_host:
            cleaned.add(u)
    return cleaned


def fetch_external_page(url):
    """Fetches an external (non-Canvas) URL and extracts readable text.
    Deliberately uses a bare, unauthenticated request -- never SESSION --
    so the Canvas API bearer token is never sent to a third-party site."""
    try:
        resp = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; canvas-sync/1.0)"},
        )
        resp.raise_for_status()
        if len(resp.content) > 20_000_000:
            print(f"  [warn] skipping {url}: response too large")
            return None, None
        content_type = resp.headers.get("Content-Type", "").lower()
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            import pdfplumber
            text = []
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                for page in pdf.pages:
                    text.append(page.extract_text() or "")
            return (url.rsplit("/", 1)[-1] or url), "\n".join(text)
        if "html" in content_type or content_type.startswith("text/") or not content_type:
            soup = BeautifulSoup(resp.content, "html.parser")
            title = soup.title.get_text(strip=True) if soup.title else url
            return title, soup.get_text(separator="\n").strip()
        return None, None
    except Exception as e:
        print(f"  [warn] could not fetch {url}: {e}")
        return None, None


def ingest_text_item(conn, item_id, cid, type_, category, title, raw_html, url,
                      posted_at=None, due_at=None, updated_at=None, raw=None,
                      stats=None, external_links=None):
    """Shared path for any non-file, HTML-bearing item: syllabus,
    announcements, assignments, discussions/replies, quizzes, module pages."""
    body_text = strip_html(raw_html) if raw_html else ""
    status = upsert_item(conn, item_id, cid, type_, title, body_text, "", url,
                          posted_at, due_at, updated_at, raw or {})
    if stats is not None:
        stats[status] += 1
    maybe_write_ingested_text(cid, category, item_id, title, body_text, url, status)
    maybe_generate_note(cid, category, item_id, type_, title, body_text, url, status)
    if external_links is not None and raw_html:
        external_links.update(find_external_links(raw_html))
    return status, body_text


# ---------- per-type sync functions ----------

def sync_courses(conn):
    courses = api_get_all("/courses", {"enrollment_state": "active",
                                        "include[]": ["term", "syllabus_body"]})
    if COURSE_IDS is not None:
        courses = [c for c in courses if c["id"] in COURSE_IDS]
    for c in courses:
        conn.execute("""
            INSERT INTO courses (id, name, course_code, term, last_synced_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                course_code=excluded.course_code, term=excluded.term,
                last_synced_at=excluded.last_synced_at
        """, (c["id"], c.get("name"), c.get("course_code"),
              (c.get("term") or {}).get("name"), now_iso()))
    conn.commit()
    return courses


def list_courses():
    """Prints every active course with its Canvas ID, so you can pick which
    ones to put in COURSE_IDS in .env. Doesn't touch the database."""
    courses = api_get_all("/courses", {"enrollment_state": "active", "include[]": "term"})
    print(f"{'ID':<10} {'CODE':<16} NAME")
    for c in courses:
        print(f"{c['id']:<10} {(c.get('course_code') or ''):<16} {c.get('name')}")


def sync_syllabus(conn, course, external_links):
    cid = course["id"]
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    body = course.get("syllabus_body")
    if not body:
        return stats
    item_id = f"syllabus:{cid}"
    url = f"{CANVAS_BASE_URL}/courses/{cid}/assignments/syllabus"
    ingest_text_item(conn, item_id, cid, "syllabus", "syllabus", "Syllabus", body, url,
                      updated_at=course.get("updated_at"), raw={"course_id": cid},
                      stats=stats, external_links=external_links)
    return stats


def sync_announcements(conn, course, seen_file_ids, external_links):
    cid = course["id"]
    course_name = course.get("course_code") or course.get("name") or str(cid)
    anns = api_get_all(f"/announcements", {"context_codes[]": f"course_{cid}"})
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for a in anns:
        item_id = f"announcement:{a['id']}"
        ingest_text_item(conn, item_id, cid, "announcement", "announcements", a.get("title"),
                          a.get("message"), a.get("html_url"), posted_at=a.get("posted_at"),
                          updated_at=a.get("updated_at"), raw=a, stats=stats,
                          external_links=external_links)

        # Announcements can carry files two ways: a structured "attachments"
        # array, or a plain link to a file buried in the message HTML.
        for att in a.get("attachments") or []:
            ingest_file(conn, cid, att, "announcements", course_name, stats)
            seen_file_ids.add(att.get("id"))
        ingest_linked_files(conn, cid, a.get("message"), "announcements", course_name, stats, seen_file_ids)
    return stats


def sync_assignments(conn, course, seen_file_ids, external_links):
    cid = course["id"]
    course_name = course.get("course_code") or course.get("name") or str(cid)
    assignments = api_get_all(f"/courses/{cid}/assignments")
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for a in assignments:
        item_id = f"assignment:{a['id']}"
        is_quiz = bool(a.get("is_quiz_assignment") or a.get("quiz_id"))
        type_ = "quiz" if is_quiz else "assignment"
        category = "quizzes" if is_quiz else "assignments"
        ingest_text_item(conn, item_id, cid, type_, category, a.get("name"),
                          a.get("description"), a.get("html_url"),
                          posted_at=a.get("created_at"), due_at=a.get("due_at"),
                          updated_at=a.get("updated_at"), raw=a, stats=stats,
                          external_links=external_links)

        # Instructor-uploaded files (e.g. "A1 guidelines.docx") usually show
        # up as a plain link inside the description HTML, not a structured field.
        ingest_linked_files(conn, cid, a.get("description"), category, course_name, stats, seen_file_ids)
    return stats


def sync_quizzes(conn, course, seen_file_ids, external_links):
    """Catches quizzes that aren't tied to a graded assignment (practice
    quizzes, surveys). Assignment-linked quizzes are already handled by
    sync_assignments, so those are skipped here to avoid duplicates."""
    cid = course["id"]
    course_name = course.get("course_code") or course.get("name") or str(cid)
    quizzes = api_get_all(f"/courses/{cid}/quizzes")
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for q in quizzes:
        if q.get("assignment_id"):
            continue
        item_id = f"quiz:{q['id']}"
        ingest_text_item(conn, item_id, cid, "quiz", "quizzes", q.get("title"),
                          q.get("description"), q.get("html_url"),
                          posted_at=q.get("created_at"), due_at=q.get("due_at"),
                          updated_at=q.get("updated_at"), raw=q, stats=stats,
                          external_links=external_links)
        ingest_linked_files(conn, cid, q.get("description"), "quizzes", course_name, stats, seen_file_ids)
    return stats


def sync_discussions(conn, course, seen_file_ids, external_links):
    """Syncs discussion topics, their attachments/linked files, and their
    top-level replies (and any files on those replies)."""
    cid = course["id"]
    course_name = course.get("course_code") or course.get("name") or str(cid)
    discussions = api_get_all(f"/courses/{cid}/discussion_topics")
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for d in discussions:
        item_id = f"discussion:{d['id']}"
        ingest_text_item(conn, item_id, cid, "discussion", "discussions", d.get("title"),
                          d.get("message"), d.get("html_url"), posted_at=d.get("posted_at"),
                          updated_at=d.get("updated_at"), raw=d, stats=stats,
                          external_links=external_links)

        for att in d.get("attachments") or []:
            ingest_file(conn, cid, att, "discussions", course_name, stats)
            seen_file_ids.add(att.get("id"))
        ingest_linked_files(conn, cid, d.get("message"), "discussions", course_name, stats, seen_file_ids)

        try:
            entries = api_get_all(f"/courses/{cid}/discussion_topics/{d['id']}/entries")
        except requests.HTTPError as e:
            print(f"  [warn] could not fetch replies for discussion {d['id']}: {e}")
            entries = []
        for e in entries:
            entry_id = f"discussion_entry:{e['id']}"
            entry_title = f"Re: {d.get('title')}"
            entry_url = f"{d.get('html_url')}#entry-{e['id']}" if d.get("html_url") else None
            ingest_text_item(conn, entry_id, cid, "discussion_entry", "discussions", entry_title,
                              e.get("message"), entry_url, posted_at=e.get("created_at"),
                              updated_at=e.get("updated_at"), raw=e, stats=stats,
                              external_links=external_links)
            att = e.get("attachment")
            if att:
                ingest_file(conn, cid, att, "discussions", course_name, stats)
                seen_file_ids.add(att.get("id"))
            ingest_linked_files(conn, cid, e.get("message"), "discussions", course_name, stats, seen_file_ids)
    return stats


def sync_calendar_events(conn, course):
    """Catches exam/midterm calendar entries that aren't tied to an assignment."""
    cid = course["id"]
    events = api_get_all("/calendar_events", {
        "context_codes[]": f"course_{cid}",
        "type": "event",
        "start_date": (datetime.date.today()).isoformat(),
        "end_date": (datetime.date.today() + datetime.timedelta(days=180)).isoformat(),
    })
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for e in events:
        item_id = f"calendar_event:{e['id']}"
        status = upsert_item(conn, item_id, cid, "calendar_event", e.get("title"),
                              strip_html(e.get("description")), "", e.get("html_url"),
                              e.get("created_at"), e.get("start_at"), e.get("updated_at"), e)
        stats[status] += 1
    return stats


def sync_files_and_modules(conn, course, seen_file_ids, external_links):
    """Walks modules -> items. File items get downloaded + extracted; Page
    items get their actual body fetched and scanned for linked files/external
    links; ExternalUrl/ExternalTool items contribute their target as a
    candidate external link."""
    cid = course["id"]
    course_name = course.get("course_code") or course.get("name") or str(cid)
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    modules = api_get_all(f"/courses/{cid}/modules", {"include[]": "items"})
    for mod in modules:
        for mi in mod.get("items", []) or []:
            mi_type = mi.get("type")
            item_id = f"module_item:{mi['id']}"

            if mi_type == "File":
                file_obj = fetch_file_object(cid, mi["content_id"])
                if file_obj:
                    ingest_file(conn, cid, file_obj, "modules", course_name, stats)
                    seen_file_ids.add(file_obj.get("id"))

            elif mi_type == "Page":
                page = fetch_module_page(cid, mi.get("page_url")) or {}
                body = page.get("body")
                ingest_text_item(conn, item_id, cid, "module_item", "modules", mi.get("title"),
                                  body, mi.get("html_url"), updated_at=page.get("updated_at"),
                                  raw=mi, stats=stats, external_links=external_links)
                if body:
                    ingest_linked_files(conn, cid, body, "modules", course_name, stats, seen_file_ids)

            elif mi_type in ("ExternalUrl", "ExternalTool"):
                ingest_text_item(conn, item_id, cid, "module_item", "modules", mi.get("title"),
                                  None, mi.get("html_url"), raw=mi, stats=stats)
                if mi.get("external_url") and external_links is not None:
                    external_links.add(mi["external_url"])

            else:
                # SubHeader, Assignment/Quiz/Discussion pointers (already
                # synced via their own endpoints) -- just record the module entry.
                ingest_text_item(conn, item_id, cid, "module_item", "modules", mi.get("title"),
                                  None, mi.get("html_url"), raw=mi, stats=stats)
    return stats


def sync_all_files(conn, course, seen_file_ids):
    """Catches anything sitting in the course Files tab that wasn't already
    reached via modules/announcements/assignments/discussions."""
    cid = course["id"]
    course_name = course.get("course_code") or course.get("name") or str(cid)
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    files = api_get_all(f"/courses/{cid}/files")
    for f in files:
        if f.get("id") in seen_file_ids:
            continue
        ingest_file(conn, cid, f, "files", course_name, stats)
        seen_file_ids.add(f.get("id"))
    return stats


def sync_external_links(conn, course, links):
    """Fetches every external (non-Canvas) link collected while syncing this
    course's other content -- the 'website content' / 'readings' ingestion."""
    cid = course["id"]
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    links = sorted(links)
    if len(links) > MAX_EXTERNAL_LINKS_PER_COURSE:
        print(f"  [warn] {len(links)} external links found, only fetching first "
              f"{MAX_EXTERNAL_LINKS_PER_COURSE}")
        links = links[:MAX_EXTERNAL_LINKS_PER_COURSE]

    for url in links:
        item_id = f"web_link:{hashlib.md5(url.encode()).hexdigest()[:16]}"
        already_fetched = conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
        if already_fetched and ingested_text_file_path(cid, "readings", item_id, url).exists():
            continue  # don't re-fetch a URL we've already pulled in before

        title, text = fetch_external_page(url)
        if title is None:
            status = upsert_item(conn, item_id, cid, "web_link", url, "", "", url,
                                  None, None, now_iso(), {"url": url, "fetch_failed": True})
            stats[status] += 1
            continue

        status = upsert_item(conn, item_id, cid, "web_link", title, text, "", url,
                              None, None, now_iso(), {"url": url})
        stats[status] += 1
        maybe_write_ingested_text(cid, "readings", item_id, title, text, url, status)
        maybe_generate_note(cid, "readings", item_id, "web_link", title, text, url, status)
    return stats


def sync_course(conn, course):
    cid = course["id"]
    name = course.get("course_code") or course.get("name")
    print(f"Syncing {name} ...")
    totals = {"new": 0, "updated": 0, "unchanged": 0}
    seen_file_ids = set()
    external_links = set()

    def run(fn, *args):
        try:
            stats = fn(conn, course, *args)
            for k in totals:
                totals[k] += stats[k]
        except requests.HTTPError as e:
            print(f"  [warn] {fn.__name__} failed for {name}: {e}")

    run(sync_syllabus, external_links)
    run(sync_announcements, seen_file_ids, external_links)
    run(sync_assignments, seen_file_ids, external_links)
    run(sync_quizzes, seen_file_ids, external_links)
    run(sync_discussions, seen_file_ids, external_links)
    run(sync_files_and_modules, seen_file_ids, external_links)
    run(sync_calendar_events)
    run(sync_all_files, seen_file_ids)
    run(sync_external_links, external_links)

    conn.commit()
    print(f"  -> new: {totals['new']}, updated: {totals['updated']}, unchanged: {totals['unchanged']}")


def main():
    import sys
    if "--list-courses" in sys.argv:
        list_courses()
        return
    conn = get_db()
    courses = sync_courses(conn)
    print(f"Found {len(courses)} course(s) to sync (COURSE_IDS filter: {COURSE_IDS or 'none, syncing all'}).")
    for course in courses:
        sync_course(conn, course)
    conn.close()
    print("Sync complete.")


if __name__ == "__main__":
    main()
