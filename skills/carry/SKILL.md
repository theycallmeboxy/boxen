---
# boxen-managed: installed by boxen; replaced on boxen self-update (edit a copy under another name instead)
name: carry
description: "Carry what earlier conversations in this project learned into its memory. Use when the user says /carry, 'carry over', 'catch up on old sessions', or asks what earlier sessions did."
disable-model-invocation: true
---

# /carry: carry earlier conversations into this project's memory

Earlier sessions in this folder are on disk as transcripts. Read the ones
the user picks and write a handoff for each into the project's auto memory,
so every later session here knows what happened. Transcripts are never
changed; `boxen resume` still replays them exactly.

Selection passed with the command, if any: $ARGUMENTS

## Steps

1. Run `boxen-carry list`. Each line is
   `N  id8  date  turns  parts  carried|-  title`.
   - If it prints nothing: say there are no earlier conversations in this
     project and stop.
   - If `boxen-carry` isn't found: say this project's container predates
     /carry and that running `boxen self-update` on the VM adds it. Stop.
   - If a selection was passed with the command (above), use it: numbers,
     ranges (`2-4`), `all`, or `new` (= every session not yet marked
     carried). Otherwise show the list as a numbered table (date, turns,
     parts, carried mark, title) and ask which to carry, in those same terms.
     A session with several parts is read in full, part by part, so it costs
     more to carry; say so in one line if any selected session has more than
     one part.
   - A session marked `carried` is skipped unless the user explicitly selects
     it by number.

2. Run `boxen-carry memdir` once; call its output `<memdir>`, then
   `mkdir -p "<memdir>/carried"`.

3. For each chosen session, oldest first, run `boxen-carry info <id8>` to
   learn its `parts` count M (and id, title, date, turns). Let
   `<notes>` = `<memdir>/carried/.<id8>.notes.md`.
   - If `<notes>` exists, this session's carry was interrupted earlier: tell
     the user you're resuming, read the notes, and continue from the part
     after the `last part read: K` recorded at its top.
   - If M == 1: run `boxen-carry read <id8>` and write the handoff file
     (below) from it directly.
   - If M > 1: for K = 1..M (or from the resume point), run
     `boxen-carry read <id8> --part K`, then update `<notes>`: first line
     `last part read: K`, then bullets under the six headings Goal, What was
     done, Decisions and why, Open items, Gotchas and things learned, Key
     files. Carry earlier bullets forward and revise them (an open item that
     a later part closed moves to "What was done") rather than repeating;
     keep the notes under ~150 lines by condensing. After the last part,
     write the handoff file from the notes, then delete `<notes>`.
   - In what you read, a `## Earlier context (compaction summary)` section is
     Claude's own recap of a part that was trimmed earlier; `[tools: …]`
     lines name the tools a turn used.
   - Write `<memdir>/carried/<id8>-<slug>.md`, slug = the title in lowercase
     with runs of anything but letters and digits as `-`, at most 40 chars.
     Use exactly this structure:

     ```
     ---
     type: project
     description: <one-line gist>
     ---
     # <title>  (session <id8>, <date>)
     ## Goal
     ## What was done
     ## Decisions and why
     ## Open items
     ## Gotchas and things learned
     ## Key files
     ```

     Each section 1-8 bullets, concrete: file paths, commands, numbers, error
     messages. Omit a section only if it's truly empty.

4. After each file, add one line to `<memdir>/MEMORY.md` under a
   `## Carried over from earlier sessions` heading (create the heading at the
   end of the file if it's missing; create the file if it's missing):
   `- [<title>](carried/<file>) — <date>: <one-line gist>`.
   Keep MEMORY.md under 200 lines or 25KB, whichever comes first. If that
   section already has 30 entries, say so and stop adding lines to it (the
   carried files still exist and are still worth writing).

5. Finish with a short "Where things stand" in chat: 3-8 bullets merging the
   open items across everything carried this time, then ask what to pick up.
   Don't re-list every file.

## Rules

- Never modify or delete transcript files (`*.jsonl`).
- Never summarize a part you have not read in this run or in the notes, and
  never skip parts: every part of a chosen session is read, in order.
- Check for `<notes>` before reading part 1; a resumed carry continues from
  the notes, it doesn't start over.
- Never delete a carried file; re-carrying a session overwrites its own file
  only.
- Quote the transcript, don't guess: if a session was all exploration with no
  outcome, say so in two bullets rather than inventing decisions.
