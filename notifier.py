"""
notifier.py
===========
Reads canvas_data.db and the notes/ folder written by canvas_sync.py and
sends you Telegram messages:

  1. new/updated content   -- the jot-note canvas_sync already wrote for it
  2. exam/quiz countdowns  -- review checklists at 10/5/2/1 days out
  3. assignment milestones -- a work-pacing plan (NOT the assignment itself),
                              then a reminder on each milestone day

Nothing is sent twice: every message is logged in the `notifications` table.
It runs only when you run it -- no scheduler.

Setup: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env
  1. Telegram -> @BotFather -> /newbot -> copy the token
  2. Message your new bot anything, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id": ...}

Usage:
    python notifier.py --dry-run         # show what WOULD be sent; sends and records nothing
    python notifier.py                   # send. First run marks existing content as
                                         # already seen instead of flooding you
    python notifier.py --send-existing   # first run: really send everything already synced
"""

import os
import re
import sys
import json
import sqlite3
import datetime
import argparse

import requests

from canvas_sync import ask_claude, slug_for, now_iso, NOTES_DIR, DB_PATH

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

EXAM_COUNTDOWN_DAYS = [10, 5, 2, 1]
LOOKAHEAD_DAYS = 21

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    message     TEXT,
    UNIQUE(item_id, kind),
    FOREIGN KEY (item_id) REFERENCES items(id)
);
CREATE TABLE IF NOT EXISTS milestones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id        TEXT NOT NULL,
    milestone_date TEXT NOT NULL,
    label          TEXT NOT NULL,
    detail         TEXT,
    FOREIGN KEY (item_id) REFERENCES items(id)
);
"""


# ---------- infra ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def already_sent(conn, item_id, kind):
    return conn.execute(
        "SELECT 1 FROM notifications WHERE item_id=? AND kind=?", (item_id, kind)
    ).fetchone() is not None


def mark_sent(conn, item_id, kind, message):
    conn.execute(
        "INSERT OR IGNORE INTO notifications (item_id, kind, sent_at, message) VALUES (?,?,?,?)",
        (item_id, kind, now_iso(), message),
    )
    conn.commit()


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # No parse_mode: titles and summaries come from Canvas/Claude and aren't
    # guaranteed to be valid Markdown, and a stray * or _ makes Telegram 400.
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=15)
    if not resp.ok:
        print(f"  [warn] Telegram rejected message: {resp.status_code} {resp.text}")
    resp.raise_for_status()


def course_label(conn, course_id):
    row = conn.execute("SELECT course_code, name FROM courses WHERE id=?", (course_id,)).fetchone()
    return (row["course_code"] or row["name"]) if row else str(course_id)


def parse_due(due_at):
    try:
        return datetime.datetime.fromisoformat(due_at.replace("Z", "+00:00")).date()
    except Exception:
        return None


def read_note(item):
    """The jot-note canvas_sync.py already wrote for this item, if any."""
    pattern = f"{slug_for(item['id'], item['title'])}.md"
    matches = list((NOTES_DIR / str(item["course_id"])).glob(f"*/{pattern}"))
    if not matches:
        return None
    text = matches[0].read_text(encoding="utf-8")
    return re.sub(r"\A<!--.*?-->\s*", "", text, flags=re.DOTALL).strip() or None


# ---------- 1. new content -> the existing jot-notes ----------

def notify_new_content(conn, dry_run, baseline):
    rows = conn.execute("""
        SELECT * FROM items
        WHERE type IN ('announcement','file','discussion','module_item')
    """).fetchall()

    count = 0
    for item in rows:
        if already_sent(conn, item["id"], "new_content"):
            continue
        note = read_note(item)
        if not note:
            continue
        count += 1
        if baseline:
            if not dry_run:
                mark_sent(conn, item["id"], "new_content", "(baseline: existing content, not sent)")
            continue
        msg = (f"{course_label(conn, item['course_id'])} — new {item['type']}\n"
               f"{item['title']}\n\n{note}\n\n{item['url'] or ''}")
        if dry_run:
            print(f"  [dry-run] new content: {item['title']}")
        else:
            send_telegram(msg)
            mark_sent(conn, item["id"], "new_content", msg)
            print(f"Sent new_content notice: {item['id']}")

    if baseline:
        verb = "would mark" if dry_run else "marked"
        print(f"First run: {verb} {count} existing item(s) as already seen "
              f"(use --send-existing to send them instead).")


# ---------- 2. quiz/exam countdown ----------

def build_review_notes(conn, exam_item):
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


def notify_exam_countdowns(conn, dry_run):
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=LOOKAHEAD_DAYS)
    rows = conn.execute("""
        SELECT * FROM items
        WHERE type IN ('quiz', 'calendar_event') AND due_at IS NOT NULL
    """).fetchall()

    for item in rows:
        due = parse_due(item["due_at"])
        if not due or not (today <= due <= horizon):
            continue
        days_out = (due - today).days
        for threshold in EXAM_COUNTDOWN_DAYS:
            kind = f"exam_countdown:{threshold}d"
            if days_out != threshold or already_sent(conn, item["id"], kind):
                continue
            if dry_run:
                print(f"  [dry-run] countdown ({threshold}d): {item['title']}")
                continue
            msg = (f"{course_label(conn, item['course_id'])} — {item['title']} in {threshold} day(s)\n\n"
                   f"{build_review_notes(conn, item)}\n\n{item['url'] or ''}")
            send_telegram(msg)
            mark_sent(conn, item["id"], kind, msg)
            print(f"Sent exam countdown ({threshold}d): {item['id']}")


# ---------- 3. assignment milestone plans ----------

def ensure_milestone_plan(conn, assignment, dry_run):
    if conn.execute("SELECT 1 FROM milestones WHERE item_id=?", (assignment["id"],)).fetchone():
        return
    due = parse_due(assignment["due_at"])
    if not due:
        return
    days_available = (due - datetime.date.today()).days
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
    plan_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", plan_json.strip())
    try:
        milestones = json.loads(plan_json)
    except json.JSONDecodeError:
        print(f"  [warn] could not parse milestone plan for {assignment['id']}")
        return

    for m in milestones:
        m_date = due - datetime.timedelta(days=int(m["days_before_due"]))
        conn.execute(
            "INSERT INTO milestones (item_id, milestone_date, label, detail) VALUES (?,?,?,?)",
            (assignment["id"], m_date.isoformat(), m["label"], m["detail"]),
        )
    conn.commit()

    plan_lines = "\n".join(f"- {m['days_before_due']}d before: {m['label']}" for m in milestones)
    msg = (f"{course_label(conn, assignment['course_id'])} — plan created for {assignment['title']}\n"
           f"Due {assignment['due_at']}\n\n{plan_lines}\n\n"
           f"I'll message you on each milestone day.")
    send_telegram(msg)
    mark_sent(conn, assignment["id"], "milestone_plan_created", msg)


def check_due_milestones(conn, assignment, dry_run):
    today = datetime.date.today().isoformat()
    for m in conn.execute(
        "SELECT * FROM milestones WHERE item_id=? AND milestone_date=?", (assignment["id"], today)
    ).fetchall():
        kind = f"assignment_milestone:{m['id']}"
        if already_sent(conn, assignment["id"], kind):
            continue
        if dry_run:
            print(f"  [dry-run] milestone today: {assignment['title']} / {m['label']}")
            continue
        msg = (f"{course_label(conn, assignment['course_id'])} — milestone today: {assignment['title']}\n\n"
               f"{m['label']}\n{m['detail']}\n\nDue {assignment['due_at']}")
        send_telegram(msg)
        mark_sent(conn, assignment["id"], kind, msg)
        print(f"Sent milestone reminder: {assignment['id']} / {m['label']}")


def notify_assignment_milestones(conn, dry_run):
    for a in conn.execute("SELECT * FROM items WHERE type = 'assignment' AND due_at IS NOT NULL").fetchall():
        ensure_milestone_plan(conn, a, dry_run)
        check_due_milestones(conn, a, dry_run)


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent; send and record nothing")
    parser.add_argument("--send-existing", action="store_true",
                        help="on a first run, send already-synced content instead of marking it seen")
    args = parser.parse_args()

    if not args.dry_run and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first (see the top of this file), "
                 "or use --dry-run.")

    conn = get_db()
    first_run = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    baseline = first_run and not args.send_existing

    notify_new_content(conn, args.dry_run, baseline)
    notify_exam_countdowns(conn, args.dry_run)
    notify_assignment_milestones(conn, args.dry_run)
    conn.close()


if __name__ == "__main__":
    main()
