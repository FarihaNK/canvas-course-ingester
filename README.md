# Canvas Course Ingester

Turns your Canvas courses into local markdown files with AI study notes, and
can message you on Telegram about what's new and what's due.

> Runs only when you run it. No scheduler, no background process.

## What it does

| Command | Result |
|---|---|
| `python canvas_sync.py` | Downloads your course content, converts it to markdown, writes a short study note for each item |
| `python notifier.py` *(optional)* | Sends Telegram messages: new content, quiz countdowns, assignment reminders |
| `python manual_summarize.py` | Summarizes one local file you point it at |

## Quick start

You need Python 3 (built on 3.13), a Canvas API token, and an Anthropic API key.

**1. Install**

```bash
git clone <this-repo> && cd <this-repo>
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

**2. Fill in `.env`**

| Variable | What to put |
|---|---|
| `CANVAS_BASE_URL` | Your school's Canvas address, e.g. `https://your-school.instructure.com` |
| `CANVAS_API_TOKEN` | Canvas > Account > Settings > Approved Integrations > **+ New Access Token** |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `COURSE_IDS` | *Optional.* Comma-separated course IDs. Blank = every active course |

**3. Sync**

```bash
python canvas_sync.py --list-courses   # find your course IDs
python canvas_sync.py
```

The first run on a course with lots of history makes one Claude call per item
worth summarizing, so it takes a while and uses some API credit. After that,
only new or changed content is processed.

## What gets ingested

- Syllabus, announcements, assignments, quizzes
- Modules, including page text and the files inside
- Discussions and their top-level replies
- The course Files tab
- Every attached file (PDF, DOCX, PPTX), including ones only linked inside text
- External links found in any of the above (articles, readings, PDFs)

## Where output goes

```
ingested/12345/                    <- one folder per course
├── syllabus/Syllabus_12345.md
├── modules/
│   ├── Week_1_Slides_5521.md      <- text extracted from the PDF
│   └── raw/Week 1 Slides.pdf      <- the original download
└── readings/Some_Article_ab12.md

notes/12345/
└── modules/Week_1_Slides_5521.md  <- the study note
```

Folders per course: `syllabus`, `announcements`, `assignments`, `quizzes`,
`modules`, `files`, `discussions`, `readings`.

## Telegram notifications (optional)

| You get | When |
|---|---|
| A short summary | New announcement, file, discussion or module item |
| A review checklist | 10, 5, 2 and 1 days before a quiz or exam |
| A pacing plan, then reminders | For each upcoming assignment. It's a schedule, never the assignment itself |

<details>
<summary><b>Set it up</b></summary>

1. In Telegram, message **@BotFather**, send `/newbot`, copy the token.
2. Message your new bot anything, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number in
   `"chat":{"id": ...}`.
3. Add both to `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Run the sync first, then:

```bash
python notifier.py --dry-run     # preview, sends nothing
python notifier.py               # send
```

</details>

Built-in safety:

- **Never repeats** a message.
- **First run doesn't flood you.** Existing content is marked as seen. Use
  `--send-existing` if you want the backlog.
- **Stops early on a bad token.** Three failed sends in a row end the run.

## One-off files

```bash
python manual_summarize.py --course 12345 path/to/file.pdf
```

Writes the text and a note to `ingested/12345/manual/` and `notes/12345/manual/`.
No database, no Canvas calls.

## Limitations

- Nested discussion replies are skipped (top-level only).
- Some courses block the Quizzes API or the Files tab (404/403). Those are
  logged and skipped; everything else still syncs.
- Links behind a login, paywall or JavaScript can't be fetched.
- Images and spreadsheets are saved but get no text or notes.
- The notifier is only as current as your last sync.

## Privacy

- Everything is stored on your machine.
- Discussions can contain other students' posts, so keep the database and
  folders private. `.gitignore` already excludes them, along with `.env`.
- Text is sent to Anthropic for summaries. Telegram messages go only to your
  own chat.

## More

[DESIGN.md](DESIGN.md) covers the architecture, the data, and how each script works.
