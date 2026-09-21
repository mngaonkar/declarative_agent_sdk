# LLM-as-Wiki on the lean runtime

An agent that reads your sources and compiles them into a persistent,
interlinked markdown wiki — then keeps that wiki current as more sources
arrive. Tuned here for a personal knowledge base (journals, articles, podcast
notes), but the schema is the only domain-specific part.

The point is not retrieval. Each source is read **once** and integrated —
entity pages updated, threads revised, contradictions recorded — so the wiki is
a compounding artifact rather than something re-derived on every question.

## Run it

```bash
cd examples/llm_wiki
export OPENAI_API_KEY=...

python run_wiki.py ingest raw/journal-2026-08-14.md
python run_wiki.py ingest raw/journal-2026-09-03.md   # contradicts the first
python run_wiki.py ask "what actually improves their sleep?"
python run_wiki.py ask "where does the wiki contradict itself?" --save
python run_wiki.py lint
python run_wiki.py chat
python run_wiki.py log                # what has happened to this wiki
python run_wiki.py replay             # the plan to rebuild it from raw/
```

The two journal entries in `raw/` are fixtures. The second reverses the first
on caffeine and on running mileage — deliberately, so the first real ingest
shows whether tensions get recorded or whether August just gets overwritten by
September.

Point Obsidian at `wiki/` and watch the graph fill in while it works.

## Layout

```
agent.yaml          the agent: model, tools, skills, loop budgets, locations
instructions.md     the schema — conventions that hold for every operation
skills/
  wiki-ingest/      read a source, integrate it, update the index and log
  wiki-query/       answer a question with citations, optionally file it back
  wiki-lint/        health-check, with the structural half as a script
wiki_tools.py       edit_page and search_wiki (registered by run_wiki.py)
run_wiki.py         CLI: ingest / ask / lint / chat
raw/                immutable sources          (raw_directory)
wiki/               everything the agent writes (workspace_directory)
```

The wiki itself is flat — `wiki/<slug>.md`, no subdirectories. What kind of
page it is lives in its `type:` frontmatter (`source`, `entity`, `concept`,
`thread`, `question`), and `index.md` groups pages under those headings. That
is the same job a folder tree would do, minus a second place to keep in sync,
and it matches how `[[sleep-quality]]` already resolves — by name, wherever the
file sits. The only page shipped is `index.md`; `log.md` and `portrait.md` are
created on the first ingest, and everything else accumulates beside them.

Three layers, mapped onto lean:

| | |
|---|---|
| `instructions.md` | `instruction_file` — conventions, in every prompt |
| `skills/*/SKILL.md` | the workflows, loaded on demand |
| `wiki/` | `workspace_directory` — the writable layer |
| `raw/` | `raw_directory` — readable, never writable |

`raw/` immutability is not enforced by prompting. Lean's `_is_mutable()` only
permits writes under the workspace, the skills tree and `attachments/`, so any
directory outside it is read-only by construction; `edit_page` applies the same
rule itself, since it is a separate handler.

## Where the wiki lives

Both locations come from `agent.yaml` and default to `wiki/` and `raw/` beside
it:

```yaml
workspace_directory: wiki    # the wiki — the writable layer
raw_directory: raw           # the sources — read-only
```

A relative value resolves against the directory holding `agent.yaml`, so the
defaults mean "beside this file". An absolute one puts either somewhere else
entirely — a wiki in your Obsidian vault, sources dropped into a synced inbox,
one wiki shared between several agents:

```yaml
workspace_directory: ~/Documents/vault/wiki
raw_directory: ~/Dropbox/reading-inbox
```

The wiki has to be the workspace — that is what makes it the one writable tree,
and everything else read-only without asking the model nicely. `wiki_directory`
is accepted as an alias if you prefer to spell it that way; `run_wiki.py`
resolves it and pins the result back onto `workspace_directory` before building
the agent, so lean and the two tools can never end up pointed at different
trees.

`wiki_tools.load_layout()` is the single reader of those two keys. `run_wiki.py`
calls it at startup, and the lint script asks the already-configured
`wiki_tools` for the same paths rather than re-deriving them. The skills are
written in terms of `wiki/` and `raw/`, so when the config points elsewhere each
command names the real locations in its opening line.

## The transaction log

`wiki/logs.md` is the wiki's write-ahead log. One command is one transaction,
and each block records what it did to both trees:

```
## tx 0002 | 2026-09-07T14:32:11Z | ingest raw/journal-2026-09-03.md
note: Reverses the 2026-08 caffeine claim; sleep-quality now carries both.
raw.add       raw/journal-2026-09-03.md    sha=6f1c9a02b4d7e830 size=1204
page.create   wiki/journal-2026-09-03.md   sha=b28e40d1c6a7f915 size=1841
page.update   wiki/sleep-quality.md        sha=0d7a3e5182bc4f60 size=1203->1615
index.update  wiki/index.md                sha=94ab7c30fe215d88 size=980->1032
```

Operations are `raw.add` / `raw.update` / `raw.delete`, `page.create` /
`page.update` / `page.delete`, and `index.update` — the catalog gets its own
name so "when did it last move" is one grep. Paths use the logical `wiki/` and
`raw/` prefixes whatever the directories are called on disk, so a log stays
readable after you repoint `agent.yaml`.

**It is written by code, not by the model.** A log the model maintains by hand
drifts on the first long ingest, and a drifted log is worse than none — it
reads as authoritative and isn't. So rather than instrumenting each write path,
`wiki_log.py` content-hashes both trees before a command and again after, and
records the difference. That catches every write whichever tool made it, and it
catches changes made outside the agent: edit a page in Obsidian between two
runs and the next transaction records it. Committing happens in a `finally`, so
an interrupted ingest still logs the half that landed.

The model's only contribution is the `note:` line, through the `log_note` tool
— the *why*, which a diff cannot see. If it never calls it, the block just has
no note; the log's integrity never depends on it.

```bash
python run_wiki.py log --limit 5      # recent transactions
grep '^page.update' wiki/logs.md      # every edit any page ever received
```

### Replay

Only ingests build state, so replay is the recorded ingests re-run in order
against an empty wiki:

```bash
python run_wiki.py replay             # print the plan, check the sources
python run_wiki.py replay --execute   # actually re-run them
```

The dry run verifies each source is still present at the hash it was ingested
at, and warns about everything that would make the replay diverge: a source
deleted since, a source edited in place, a raw file never ingested, pages
changed outside an ingest.

Replay lands on a *similar* wiki, not a byte-identical one — the model is not a
deterministic function of its input, and a redo log that guaranteed otherwise
would have to store every page's full content, at which point it is a copy of
the wiki rather than a log of it. What it does guarantee is that the inputs and
their order are recoverable, and that any divergence has a named cause.

## Why the workflows are skills

`instructions.md` holds conventions (page format, wikilinks, how to record a
contradiction). The three workflows are skills, so the ingest playbook — nine
steps, the longest of the three — is not sitting in the prompt while you ask a
one-line question. The system prompt is ~8K chars; the ingest playbook loads
only when ingesting.

`skills/` is writable, so the agent can propose amendments to its own
playbooks as you settle on conventions. `_validate_manifest` keeps the
frontmatter honest.

## What this example adds to stock lean

**Two tools** (`wiki_tools.py`), registered with `ToolRegistry` in
`run_wiki.py` before the agent is built, then listed in `agent.yaml` like any
built-in:

- `edit_page` — lean only has whole-file `write_file`, which loses everything
  else on the page each time a long thread gains one line.
- `search_wiki` — lean has no search at all, so "which pages mention Marta?"
  otherwise means reading the tree.

**A lint script** (`skills/wiki-lint/scripts/lint_report.py`, run via
`run_script`). Orphans, dangling links, index drift and unfiled sources are
arithmetic over the link graph — computed, not inferred by reading every page.
The model reads the report and then goes looking for the things a script
cannot see: contradictions, stale claims, gaps.

**Four loop budgets in YAML.** `max_tool_iterations`, `max_step_retries`,
`max_no_tool_continues` and `history_limit` now come from the config
(`core/agent_config.py` → `core/agent_factory.py` → `LeanAIAgent`); omit them
and the runtime defaults are unchanged. They matter here: one ingest touches
10–15 pages, which is 30–60 tool rounds against a default of 32, and — worse —
a default `history_limit` of 48 messages would trim away the source read in
step 1 while it is still being filed.

## Model and provider

Defaults to `gpt-4o` on the OpenAI endpoint. `LeanLLMClient` speaks
OpenAI-compatible `/chat/completions`, so any compatible endpoint works:

```yaml
provider: vllm
model: Qwen/Qwen3-32B-Instruct
endpoint:
  url: http://localhost:8000/v1
```

**Anthropic models need a proxy.** `provider: anthropic` resolves to
`api.anthropic.com/v1` and posts `/chat/completions` with a Bearer token, which
is not Anthropic's native API (`/v1/messages`, `x-api-key`). Put litellm or an
equivalent OpenAI-compatible shim in front and point `endpoint.url` at it.

Wiki maintenance rewards a strong model — noticing that a new source
contradicts a claim made three pages away is the whole job — so `gpt-4o-mini`
will file sources but do it shallowly.

## Approval

`tools_approval_required: false` in `agent.yaml`, because one ingest makes a
lot of writes. Flip it to `true` and every write pauses for `y/N` at the
prompt, while reads and skill loading auto-approve. Over Discord the same pause
becomes a ✅/❌ reaction. Worth turning on once you have seen how many writes an
ingest makes.

## Other transports

Nothing here is CLI-specific — `agent.yaml` is a normal lean agent. Serve it
over A2A (see `../a2a_lean/run_server.py`) to let other agents query the wiki,
or over Discord (see `../discord_bot/`) to file a podcast note by dropping it
in a channel.

## Compare

There is a standalone implementation of the same pattern on the Anthropic
Messages API at `~/code/llm-wiki` — same schema, same three operations, ~730
lines with its own loop. Run both over these two journal entries to compare.
The short version: lean brings the deliberative loop (it will not let the model
stop after writing the source page while the index is stale), progressive
disclosure, approval, and transports; the standalone version brings prompt
caching, per-run cost accounting, streaming, and native Anthropic models.
