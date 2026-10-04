"""
manual_summarize.py
====================
One-off tool for files that were never pulled in by canvas_sync.py
(e.g. attachments on an announcement/assignment tab, which the sync
only reaches via course Files/Modules). Takes local file paths,
extracts their text, summarizes them into jot notes, and writes both
under ingested/<course_id>/manual/ and notes/<course_id>/manual/.

Does NOT touch canvas_data.db, does NOT send any notification, does
NOT call the Canvas API, and is not run on any schedule -- it only
runs when you invoke it directly.

Setup: requires ANTHROPIC_API_KEY in .env (same as canvas_sync.py).

Usage:
    python manual_summarize.py --course 12345 path/to/file1.pdf path/to/file2.docx
"""

import argparse
from pathlib import Path

from canvas_sync import (
    extract_text_from_file, safe_stem, now_iso, PLAIN_TEXT_SUFFIXES,
    ask_claude, ingested_dir, notes_dir, SUMMARY_SYSTEM_PROMPT,
)


def summarize_file(path, course_id):
    path = Path(path)
    if not path.exists():
        print(f"  [skip] {path} does not exist")
        return

    if path.suffix.lower() in PLAIN_TEXT_SUFFIXES:
        text = path.read_text(errors="ignore")
    else:
        text = extract_text_from_file(path, None)

    if not text.strip():
        print(f"  [skip] {path.name}: no extractable text")
        return

    summary = ask_claude(system=SUMMARY_SYSTEM_PROMPT, user=f"Title: {path.name}\n\nContent:\n{text[:8000]}")

    slug = safe_stem(path.name)

    ingested_path = ingested_dir(course_id, "manual") / f"{slug}.md"
    ingested_path.write_text(
        f"<!-- Source: {path.name} | Course id: {course_id} | Converted at: {now_iso()} UTC -->\n\n" + text,
        encoding="utf-8",
    )

    notes_path = notes_dir(course_id, "manual") / f"{slug}.md"
    notes_path.write_text(
        f"<!-- Notes for: {path.name} | Course id: {course_id} | Generated at: {now_iso()} UTC -->\n\n" + summary,
        encoding="utf-8",
    )
    print(f"  -> wrote {ingested_path} and {notes_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--course", required=True, help="Canvas course id, e.g. 12345")
    parser.add_argument("files", nargs="+", help="Local file paths to summarize")
    args = parser.parse_args()

    for f in args.files:
        summarize_file(f, args.course)


if __name__ == "__main__":
    main()
