---
name: wiki-lint
description: Health-check the wiki and report — contradictions, stale claims, orphan pages, missing cross-references, index drift, and gaps worth a new source. Use when asked to lint, audit, or check the health of the wiki.
---

# wiki-lint

Two halves. The structural half is arithmetic over the link graph and is done by
a script. The judgement half needs you to actually read pages.

Report findings. **Do not fix anything unless the human asks** — a lint pass
that silently rewrites pages is impossible to review.

## 1. Run the structural checks

`run_script` on `scripts/lint_report.py` (bundled with this skill; the absolute
path is listed when this skill loads). It reports orphans, wanted pages, index
drift, thin pages, missing frontmatter, and raw sources never ingested.

That is the cheap half. Do not do it by hand.

## 2. Read for the things a script cannot see

Take the script's output as a map of where to look, then read widely enough to
actually check the claims. A lint pass that only reads `index.md` is not a lint
pass.

Look for:

- **Contradictions** between pages that are not already recorded under
  `## Tensions`. `search_wiki` for the same claim stated two ways.
- **Stale claims** a newer source supersedes — compare each thread's dated
  claims against the newest `type: source` pages.
- **Concepts** mentioned across several pages with no page of their own.
- **Missing cross-references**: two pages about the same thing that never link
  to each other.
- **Gaps**: questions the wiki raises but has no source to answer.

## 3. Report

Group by severity, not by check. A contradiction between two thread pages
matters more than a thin entity page.

End with the three most useful things the human could do next: a source to add,
a question to sit with, a page to correct. Be specific — name the file.

Then end with `[[decision:done]]`.
