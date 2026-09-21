---
name: wiki-query
description: Answer a question against the wiki with citations, and optionally file the answer back as a new page. Use for any question about what the wiki knows, what it contradicts, or what it says about a person, thread or concept.
---

# wiki-query

Answers are cheap; the wiki is what compounds. A good answer is worth filing.

## 1. Find the relevant pages

1. `read_file wiki/index.md`.
2. `search_wiki` for the question's key terms over `wiki`. Search before you
   read — the index tells you what exists, search tells you where a claim lives.
3. `read_file` the pages that matter. Read them fully; a summary of a summary
   is how errors enter.

## 2. Answer

- Cite the wiki pages you used, by name, inline.
- Separate **what the wiki says** from **what is true**. If three sources
  support a claim, say so; if one journal entry from eight months ago is the
  only support, say that too.
- If the pages disagree, lead with the disagreement rather than averaging it.
- If the wiki cannot answer, say so plainly and name the source that would.

## 3. File it back, when asked

Only when the human asked you to save it:

1. `write_file wiki/<slug>.md` with the standard frontmatter (`type: question`)
   and the answer, keeping the citations as `[[wikilinks]]`. The wiki is flat —
   no subdirectories.
2. `edit_page` each page it draws on to link back to the new question page.
3. `edit_page wiki/index.md` to add it under Questions.
4. `log_note` with one line saying what the answer settled. Never write
   `wiki/logs.md` yourself — it is written by code, and the pages you just
   touched are recorded without you.

If the human did not ask you to save it, write nothing.

## 4. Finish

End with `[[decision:done]]`.
