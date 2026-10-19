---
# boxen-managed: installed by boxen; replaced on boxen self-update (edit a copy under another name instead)
name: carry
description: "Carry what earlier conversations in this project learned into its memory. Use when the user says /carry, 'carry over', 'catch up on old sessions', or asks what earlier sessions did."
disable-model-invocation: true
---

# /carry: carry earlier conversations into this project's memory

Earlier sessions in this folder are on disk as transcripts. Read the ones the
user picks and write a handoff for each into the project's auto memory, so
later sessions here know what happened. Transcripts are never changed.

Selection passed with the command, if any: $ARGUMENTS
This conversation's own id: ${CLAUDE_SESSION_ID}

## Setup

1. `boxen-carry memdir` prints MEMDIR, this project's auto-memory folder. If
   `boxen-carry` isn't found, ask the user to run `boxen self-update` on the
   VM, then stop. Every file under MEMDIR is written with the Write and Edit
   tools (they create folders), never with shell commands: the sandbox
   doesn't let the shell write there.
2. `boxen-carry list`: tab-separated `N id8 date turns parts state title`,
   newest first (turns = messages the user typed; a part is up to 60k chars).
   The state is `live` (open in a running Claude right now, never carried;
   this includes the conversation you're in), `partial` (a carry of it was
   interrupted; resumable), `carried` (a `carried/<id8>-*.md` file exists)
   or `-`. Nothing printed: say there are no earlier conversations here and
   stop.
3. Skip `live` sessions: never carry one that is open, and leave them out of
   `all` and `new`. As a second guard, also drop the line whose id8 starts
   this conversation's id (above).
4. `partial` = an interrupted carry. Say so; `all` and `new` include them.
5. Selection: from $ARGUMENTS (numbers, ranges `2-4`, `all`, `new` = every
   session not marked `carried`), else show the list as a table (N, date,
   turns, parts, state, title) and ask which, in those terms. A `carried`
   session is skipped unless picked by number; if that leaves nothing, say so
   and offer re-carrying by number. If a pick has parts > 1, say in one line
   that it is read in full, part by part, and costs more.

## Per session, oldest first

1. `boxen-carry info <id8>`: `id`, `title`, `date`, `turns`, `chars`, `parts`
   (M). NOTES = `$MEMDIR/carried/.<id8>.notes.md`.
2. If NOTES exists and its first line isn't `done`: say you're resuming, read
   it, and continue from the part after its first line `last part read: K`.
   Otherwise start at part 1.
3. M == 1: `boxen-carry read <id8>`. M > 1: `boxen-carry read <id8> --part K`
   for each K in order (the footer names the next command). After every part
   rewrite NOTES: `last part read: K`, then bullets under the headings below,
   carried forward and revised (an open item a later part closed moves to
   Outcome), under 150 lines. Every part is read; never summarize one you
   haven't read in this run or in NOTES.
4. What you read: `## You` / `## Claude` turns, Claude's text, `[tools: …]`
   lines (one entry per tool use: `Bash: <command>` (first 120 chars),
   `Edit`/`Write`/`Read`/`NotebookEdit: <file path>`, other tools by name;
   outputs are not in the transcript, so results come from the prose), and
   `## Earlier context (compaction summary)` = Claude's own recap of a
   trimmed stretch. Commands and paths can be exact: take them from there.
5. Title: if `title` is a slash command (`/model`), empty or under three
   words, use one you derive from the content instead (what the session was
   about, 60 chars max), everywhere below.
6. Write `$MEMDIR/carried/<id8>-<slug>.md` (slug: title lowercased, runs of
   anything but letters and digits as `-`, trimmed of `-`, 40 chars max). If
   a `<id8>-*.md` exists already, overwrite that file under its existing name
   instead: one file per session. Exactly this structure:

   ```
   ---
   type: project
   description: <one line: the topics (words a later search would use) and the outcome>
   ---
   # <title>  (session <id8>, <date>, <turns> turns)
   ## Goal
   ## Outcome
   ## Decisions
   ## Tried and dropped
   ## Commands and paths that worked
   ## Open items and questions
   ## Where it stopped
   ```

   - Decisions: `- <what> — because <why>; instead of <alternative>`.
   - Tried and dropped: what failed or was rejected, with the error or reason.
   - Commands and paths: exact commands, flags, file paths, URLs, numbers and
     error strings as the transcript has them; nothing reconstructed.
   - Where it stopped: the last exchange in 1-3 bullets: what was asked or
     promised last and whether it was done.
   - 1-8 bullets per section; drop a section only if it is empty. A session
     that was all exploration gets two bullets saying so. Quote, don't guess.
   - Then overwrite NOTES with the single line `done` (it can't be deleted).
7. `$MEMDIR/MEMORY.md`: under `## Carried over from earlier sessions` (create
   it at the end; create the file if missing) one line
   `- [<title>](carried/<file>) — <date>: <description>`, replacing the line
   for the same `<id8>` if there is one. Claude Code loads only the first 200
   lines or 25KB of MEMORY.md, so keep it under that; when the section has 30
   lines, replace its 10 oldest with one line:
   `- <n> older sessions, <first date> to <last date>: ls carried/`.

## Finish

"Where things stand" in chat: 3-8 bullets merging the open items across what
was carried this time, then ask what to pick up. Don't re-list every file.

## Rules

- Never modify or delete transcript files (`*.jsonl`) or carried files.
- Check NOTES before part 1; a resumed carry continues, it doesn't start over.
- Sections hold what the transcript says, in its words: no invented
  decisions, no reconstructed commands.
