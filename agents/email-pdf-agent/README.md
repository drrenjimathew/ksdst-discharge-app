# Sample Agent: Email → PDF Downloader

A small, self-contained example of an **agent**: a loop where an LLM is given a
set of tools, decides for itself which ones to call and in what order, and
keeps going until the task is done. This one searches Gmail for mail from a
given sender (e.g. "CMD office"), inspects any PDF attachments, and saves the
ones actually published in a target year.

It's meant to be read, not just run — see "How the agent works" below.

## What it does

1. Searches Gmail for messages matching a sender/query.
2. For each match, lists its attachments.
3. For each PDF attachment, opens it far enough to read its metadata
   (creation date, title) without saving it yet.
4. Only saves the PDF to disk if its publication year matches the year you
   asked for; skips everything else.
5. Prints a short report of what it downloaded and what it skipped, and why.

The model — not hard-coded control flow — decides the order of steps 1-4 and
when to stop. That's the difference between this and a plain script: give it
a different instruction ("find PDFs mentioning 'discharge policy'" or "only
look at the last 90 days") and the same tools support it without code changes.

## How the agent works

- **Tools** (`TOOLS` in `agent.py`): typed functions the model can call —
  `search_gmail`, `list_attachments`, `inspect_pdf`, `save_attachment`. The
  model never touches the filesystem or the Gmail API directly; it can only
  ask a tool to do it and read back the result.
- **System prompt**: tells the model its goal and the rule that matters
  ("only save PDFs whose publication year equals the target year") — the
  actual filtering judgment is the model's, using whatever `inspect_pdf`
  reports back.
- **Loop** (`run_agent` in `agent.py`): sends the conversation to Claude,
  and whenever the response contains `tool_use` blocks, executes the
  matching Python function and feeds the result back as a `tool_result`.
  This repeats until Claude replies with plain text and no further tool
  calls — that's the model deciding the task is finished.
- **Guardrails**: every tool call is logged; `save_attachment` is the only
  tool with a side effect (writing a file) and it always writes inside
  `--out`, never an arbitrary path the model invents.

## Setup

```bash
pip install -r requirements.txt
```

You need:

1. A Google Cloud project with the **Gmail API** enabled, and an OAuth
   client ID of type **Desktop app** — download it as `credentials.json`
   into this directory. (Google Cloud Console → APIs & Services →
   Credentials.) Scope used: `gmail.readonly`.
2. `ANTHROPIC_API_KEY` set in your environment.

The first run opens a browser for the Gmail OAuth consent screen and caches
the resulting token in `token.json` (already gitignored).

## Run

```bash
python agent.py --query 'from:"CMD office"' --year 2026 --out ./downloads
```

- `--query` accepts any Gmail search syntax (`from:`, `subject:`, etc.).
- `--year` is the publication year to keep; everything else is skipped.
- `--out` is the local folder PDFs get saved into.

## Notes / limitations of this sample

- "Published in `<year>`" is judged from PDF metadata first
  (`/CreationDate`), falling back to the email's own date if the PDF has no
  metadata — real-world PDFs are inconsistent here, so treat this as a
  reasonable heuristic, not a guarantee.
- This is a teaching sample, not a production pipeline: there's no retry
  logic, no incremental sync, and no dedup across runs.
