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

1. Run `boxen-carry list`. Each line is `N  id8  date  turns  carried|-  title`.
   - If it prints nothing: say there are no earlier conversations in this
     project and stop.
   - If `boxen-carry` isn't found: say this project's container predates
     /carry and that running `boxen self-update` on the VM adds it. Stop.
   - If a selection was passed with the command (above), use it: numbers,
     ranges (`2-4`), `all`, or `new` (= every session not yet marked
     carried). Otherwise show the list as a numbered table (date, turns,
     carried mark, title) and ask which to carry, in those same terms.
   - A session marked `carried` is skipped unless the user explicitly selects
     it by number.

2. Run `boxen-carry memdir` once; call its output `<memdir>`, then
   `mkdir -p "<memdir>/carried"`.

3. For each chosen session, oldest first:
   - Run `boxen-carry read <id8>`. Its header gives the full id, title, date
     and turn count; a `[… N characters of the middle omitted …]` line means
     the transcript was truncated; a `## Earlier context (compaction summary)`
     section is Claude's own recap of a part that was trimmed earlier.
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
     messages. Omit a section only if it's truly empty. If the transcript was
     truncated, say so in one line right under the title.

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
- Never delete a carried file; re-carrying a session overwrites its own file
  only.
- Quote the transcript, don't guess: if a session was all exploration with no
  outcome, say so in two bullets rather than inventing decisions.
