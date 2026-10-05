"""
notifier.py
===========
Module 2 of 2. Reads canvas_data.db (written by canvas_sync.py) and:

  1. Sends you jot-note summaries of any new/updated content
     (announcements, files, discussions, module items).
  2. Sends a countdown of review reminders leading up to quizzes/exams.
  3. For new assignments, generates a milestone plan (NOT the assignment
     itself) and messages you at each milestone with what to work on.

It never repeats a message: everything it sends is logged in the
`notifications` table.

This script knows nothing about Canvas's API directly -- it only reads
the shared SQLite DB. Run it by hand, any time after canvas_sync.py has run.

Setup
-----
1. pip install -r requirements.txt
2. In .env, set ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
   - Telegram: message @BotFather -> /newbot -> copy the token. Then message
     your new bot anything and open
     https://api.telegram.org/bot<TOKEN>/getUpdates to find "chat":{"id": ...}

Usage
-----
    python notifier.py --dry-run         # list what WOULD be sent; sends and records nothing
    python notifier.py                   # send. On a first run (empty notification log)
                                         # existing content is marked seen, not sent
    python notifier.py --send-existing   # first run only: send the existing backlog too
"""

import os
import re
import sys
import json
import sqlite3
import datetime
import argparse
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "canvas_data.db")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# How far ahead to look for upcoming quizzes/exams/deadlines, and which
# day-counts before the due date to send a review reminder.
EXAM_COUNTDOWN_DAYS = [10, 5, 2, 1]
LOOKAHEAD_DAYS = 21

# Stop the run after this many sends in a row fail (bad token/chat id), so a
# misconfiguration doesn't burn a Claude call per remaining item first.
MAX_CONSECUTIVE_SEND_FAILURES = 3

_client = None
_consecutive_failures = 0


# ---------- infra ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(Path(__file__).parent / "schema.sql") as f:
        conn.executescript(f.read())
    return conn


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


def parse_due(due_at):
    try:
        return datetime.datetime.fromisoformat(due_at.replace("Z", "+00:00")).date()
    except Exception:
        return None


def already_sent(conn, item_id, kind):
    row = conn.execute(
        "SELECT 1 FROM notifications WHERE item_id=? AND kind=?", (item_id, kind)
    ).fetchone()
    return row is not None


def mark_sent(conn, item_id, kind, message):
    conn.execute(
        "INSERT OR IGNORE INTO notifications (item_id, kind, sent_at, message) VALUES (?,?,?,?)",
        (item_id, kind, now_iso(), message),
    )
    conn.commit()


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4000],   # Telegram message length limit
        # No parse_mode: titles/summaries come from Canvas and Claude and
        # aren't guaranteed to be valid Markdown (a stray * or _ breaks
        # Telegram's entity parser and 400s the whole message). Plain text
        # is unbreakable, at the cost of losing bold/italic styling.
    }, timeout=15)
    if not resp.ok:
        raise requests.HTTPError(f"Telegram rejected message: {resp.status_code} {resp.text}")


def redact(text):
    """Telegram puts the bot token in the request URL, and requests echoes the
    URL in network errors -- keep it out of terminal output and log files."""
    return text.replace(TELEGRAM_BOT_TOKEN, "<token>") if TELEGRAM_BOT_TOKEN else text


def deliver(conn, item_id, kind, msg):
    """Sends one message and logs it. A failed send is reported and left
    unlogged, so the next run retries it; repeated failures stop the run."""
    global _consecutive_failures
    try:
        send_telegram(msg)
    except requests.RequestException as e:
        _consecutive_failures += 1
        print(f"  [warn] could not send {kind} for {item_id}: {redact(str(e))}")
        if _consecutive_failures >= MAX_CONSECUTIVE_SEND_FAILURES:
            sys.exit("Too many failed sends in a row -- check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        return False
    _consecutive_failures = 0
    mark_sent(conn, item_id, kind, msg)
    print(f"Sent {kind}: {item_id}")
    return True


def ask_claude(system, user):
    global _client
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def course_label(conn, course_id):
    row = conn.execute("SELECT course_code, name FROM courses WHERE id=?", (course_id,)).fetchone()
    return (row["course_code"] or row["name"]) if row else str(course_id)


# ---------- 1. new/updated content -> jot notes ----------

def notify_new_content(conn, dry_run, baseline):
    rows = conn.execute("""
        SELECT * FROM items
        WHERE type IN ('announcement','file','discussion','module_item')
        AND (body_text != '' OR extracted_text != '')
    """).fetchall()

    baselined = 0
    for item in rows:
        if already_sent(conn, item["id"], "new_content"):
            continue
        text = (item["extracted_text"] or item["body_text"] or "").strip()
        if len(text) < 40:
            continue  # too little content to bother summarizing

        if baseline:
            baselined += 1
            if not dry_run:
                mark_sent(conn, item["id"], "new_content", "(baseline: existing content, not sent)")
            continue
        if dry_run:
            print(f"  [dry-run] new content: {item['title']}")
            continue

        summary = ask_claude(
            system=(
                "You summarize university course material into concise jot notes "
                "for a student. Use short bullet points, no preamble, no restating "
                "the title. Focus only on the main concepts, definitions, and any "
                "explicit action items (e.g. 'read chapter 4', 'bring calculator'). "
                "Max 8 bullets."
            ),
            user=f"Title: {item['title']}\n\nContent:\n{text[:8000]}",
        )
        label = course_label(conn, item["course_id"])
        msg = f"{label} — new {item['type']}\n{item['title']}\n\n{summary}\n\n{item['url'] or ''}"
        deliver(conn, item["id"], "new_content", msg)

    if baseline:
        verb = "would mark" if dry_run else "marked"
        print(f"First run: {verb} {baselined} existing item(s) as already seen "
              f"(pass --send-existing to send them instead).")


# ---------- 2. quiz/exam countdown ----------

def notify_exam_countdowns(conn, dry_run):
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=LOOKAHEAD_DAYS)

    rows = conn.execute("""
        SELECT * FROM items
        WHERE type IN ('quiz', 'calendar_event')
        AND due_at IS NOT NULL
    """).fetchall()

    for item in rows:
        due = parse_due(item["due_at"])
        if due is None or not (today <= due <= horizon):
            continue
        days_out = (due - today).days

        for threshold in EXAM_COUNTDOWN_DAYS:
            kind = f"exam_countdown:{threshold}d"
            if days_out != threshold or already_sent(conn, item["id"], kind):
                continue
            if dry_run:
                print(f"  [dry-run] countdown ({threshold}d): {item['title']}")
                continue
            review_notes = build_review_notes(conn, item)
            label = course_label(conn, item["course_id"])
            msg = (f"{label} — {item['title']} in {threshold} day(s)\n\n"
                   f"{review_notes}\n\n{item['url'] or ''}")
            deliver(conn, item["id"], kind, msg)


def build_review_notes(conn, exam_item):
    """Pulls recent content from the same course to build a review checklist."""
    # Files carry their text in extracted_text; everything else (announcements,
    # module pages) carries it in body_text, with extracted_text left as ''.
    # COALESCE would pick that empty string, so choose explicitly.
    related = conn.execute("""
        SELECT title,
               CASE WHEN extracted_text != '' THEN extracted_text ELSE body_text END AS text
        FROM items
        WHERE course_id = ? AND type IN ('file','module_item','announcement')
        AND (extracted_text != '' OR body_text != '')
        ORDER BY last_synced_at DESC LIMIT 12
    """, (exam_item["course_id"],)).fetchall()

    corpus = "\n\n".join(f"## {r['title']}\n{(r['text'] or '')[:1500]}" for r in related)
    if not corpus.strip():
        return "No course material indexed yet to build a review list from — check Canvas directly."

    return ask_claude(
        system=(
            "You build a short, prioritized review checklist for a student preparing "
            "for an exam/quiz, based on recent course material. Bullet points only, "
            "grouped by topic, no filler."
        ),
        user=f"Upcoming assessment: {exam_item['title']}\n\nRecent course material:\n{corpus[:10000]}",
    )


# ---------- 3. assignment milestone plans ----------

def notify_assignment_milestones(conn, dry_run):
    assignments = conn.execute("""
        SELECT * FROM items WHERE type = 'assignment' AND due_at IS NOT NULL
    """).fetchall()

    for a in assignments:
        ensure_milestone_plan(conn, a, dry_run)
        check_due_milestones(conn, a, dry_run)


def parse_milestone_plan(raw):
    """Parses Claude's JSON plan into [(days_before_due, label, detail)], or
    None if it isn't the expected shape. Tolerates a markdown code fence."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        plan = json.loads(raw)
        return [(int(m["days_before_due"]), str(m["label"]), str(m.get("detail", ""))) for m in plan]
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError):
        return None


def ensure_milestone_plan(conn, assignment, dry_run):
    exists = conn.execute("SELECT 1 FROM milestones WHERE item_id=?", (assignment["id"],)).fetchone()
    if exists:
        return
    due = parse_due(assignment["due_at"])
    if due is None:
        return
    today = datetime.date.today()
    days_available = (due - today).days
    if days_available < 1:
        return
    if dry_run:
        print(f"  [dry-run] would create milestone plan: {assignment['title']} ({days_available}d left)")
        return

    plan_json = ask_claude(
        system=(
            "You create a study/work-pacing plan (NOT the actual assignment content) "
            "for a student, breaking an assignment into milestone checkpoints between "
            "today and the due date. Respond ONLY with a JSON array, no prose, no "
            "markdown fences. Each element: {\"days_before_due\": int, \"label\": str, "
            "\"detail\": str}. 'detail' should describe what the student should DO that "
            "day (e.g. 'outline your argument and list sources', 'write a rough draft "
            "of section 2') -- never actual answer content. 3-6 milestones depending on "
            "days_available."
        ),
        user=f"Assignment: {assignment['title']}\nDescription: {(assignment['body_text'] or '')[:3000]}\n"
             f"Days available: {days_available}",
    )
    milestones = parse_milestone_plan(plan_json)
    if not milestones:
        print(f"  [warn] could not parse milestone plan for {assignment['id']}")
        return

    # Insert only after the whole plan parsed, so a malformed plan never
    # leaves a half-written set of milestones behind.
    for days_before, label, detail in milestones:
        m_date = due - datetime.timedelta(days=days_before)
        conn.execute(
            "INSERT INTO milestones (item_id, milestone_date, label, detail) VALUES (?,?,?,?)",
            (assignment["id"], m_date.isoformat(), label, detail),
        )
    conn.commit()

    course = course_label(conn, assignment["course_id"])
    plan_lines = "\n".join(f"- {d}d before: {label}" for d, label, _ in milestones)
    msg = (f"{course} — plan created for {assignment['title']}\n"
           f"Due {assignment['due_at']}\n\n{plan_lines}\n\n"
           f"I'll ping you on each milestone day. Want help thinking through any part "
           f"of it now, just ask.")
    deliver(conn, assignment["id"], "milestone_plan_created", msg)


def check_due_milestones(conn, assignment, dry_run):
    today = datetime.date.today().isoformat()
    milestones = conn.execute(
        "SELECT * FROM milestones WHERE item_id=? AND milestone_date=?",
        (assignment["id"], today),
    ).fetchall()
    for m in milestones:
        kind = f"assignment_milestone:{m['id']}"
        if already_sent(conn, assignment["id"], kind):
            continue
        if dry_run:
            print(f"  [dry-run] milestone today: {assignment['title']} / {m['label']}")
            continue
        label = course_label(conn, assignment["course_id"])
        msg = (f"{label} — milestone today: {assignment['title']}\n\n"
               f"{m['label']}\n{m['detail']}\n\n"
               f"Due {assignment['due_at']}")
        deliver(conn, assignment["id"], kind, msg)


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Send Telegram notifications from the synced Canvas data.")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be sent; send and record nothing")
    parser.add_argument("--send-existing", action="store_true",
                        help="on a first run, send already-synced content instead of marking it seen")
    args = parser.parse_args()

    if not args.dry_run:
        missing = [name for name, value in (("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
                                            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
                                            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)) if not value]
        if missing:
            sys.exit(f"Set {', '.join(missing)} in .env first (see the top of this file), or use --dry-run.")

    conn = get_db()
    first_run = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    baseline = first_run and not args.send_existing

    notify_new_content(conn, args.dry_run, baseline)
    notify_exam_countdowns(conn, args.dry_run)
    notify_assignment_milestones(conn, args.dry_run)
    conn.close()


if __name__ == "__main__":
    main()
