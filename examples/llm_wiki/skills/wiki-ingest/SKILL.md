---
name: wiki-ingest
description: Read a new source document from raw/ and integrate it into the wiki — write its source page, update every thread, entity and concept it touches, refresh the index and portrait, and append to the log. Use whenever a new source needs filing.
---

# wiki-ingest

Integrating the source is the entire point. A summary filed in isolation is
worth nothing — the value is in what it changes elsewhere.

Work the steps in order. A single source commonly touches 10–15 pages, so
expect this to run long; do not stop early because you have written something.

## 1. Read the source

`read_file` with `max_bytes: 200000`. The 8000-byte default will silently
truncate an article and you will file a summary of its first page.

Note its own date. Content is dated by the source; the log entry is dated
today.

## 2. Read what already exists

Do not skip this step.

1. `read_file wiki/index.md` — the catalog.
2. `search_wiki` for the proper nouns and recurring topics in the source, over
   `wiki`. Two or three searches, not twenty.
3. `read_file` every page those turn up that plausibly bears on the source.

You are looking for three things: pages this source **extends**, pages it
**contradicts**, and things it mentions that have **no page yet**.

## 3. Write the source page

`write_file wiki/<slug>.md` with `type: source`, named after the source. The
wiki is flat — no subdirectories, ever. It must stand on its own: what it is,
when it is from, what it says, what is new relative to what the wiki already
knew, and what it contradicts.

## 4. Update every page it touches

For each page identified in step 2, use `edit_page` — not `write_file`. A
thread page that has accumulated six months of history must not be rewritten to
add one line.

- Extends an existing claim → edit the claim, update `updated:` and `sources:`.
- Contradicts an existing claim → **keep both.** Add or extend `## Tensions`
  with the conflict, both sources, and both dates. Never pick a side silently.
- Superseded detail on a thread → move it down into `## History`.

## 5. Create pages for what has no home

Only where there is something to say. A page per proper noun is noise; create a
page when the wiki can now support a paragraph about it.

## 6. Update the portrait

`read_file wiki/portrait.md`, creating it with `type: portrait` if this is the
first ingest. Most sources will not move it — say so rather than padding it.
Edit it when the overall picture actually changed.

## 7. Update the index

`edit_page wiki/index.md`: add every page you created with its one-line
summary, revise the lines that changed, and add any `[[link]]` you wrote to a
page that does not exist yet under **Wanted**.

## 8. Note what this run was for

`log_note` with one line. **Do not write `wiki/logs.md` yourself** — it is a
transaction log, written by code from a before/after snapshot of the wiki, and
hand-edits corrupt it. Every page you created, changed or deleted is already
recorded without you.

What the snapshot cannot see is *why*, so that is the whole job of the note:

```
log_note("Filed journal-2026-09-01; it reverses the 2026-08 caffeine claim, so
sleep-quality now carries both under ## Tensions rather than the newer one.")
```

One sentence. What changed about the wiki's picture, not which files moved.

## 9. Report and finish

Tell the human, in a few lines: what you filed, what you changed, what surprised
you, and what you are unsure about. Do not re-narrate every tool call.

Then end with `[[decision:done]]`.
