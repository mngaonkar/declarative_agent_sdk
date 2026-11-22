# agent-board store — design spec

Code: `agent_board/store.py`.

## `agent_board/store.py`

**Does:** `Store`, a thin synchronous SQLite wrapper (one connection behind a
lock) at `~/.agent-board/control.db`. It is the board's only durable
state.

**Rules — tables:**

- AC-STORE-1: `machines`: name (unique), host, SSH user/key/port, `is_local`,
  agents root, OS, status, last error, and `interfaces` (JSON list of
  `{name, address, family, note}`, returned decoded).
- AC-STORE-2: `agents`: machine, name (unique per machine), URL, agent card,
  port, framework, provider, model, tools, description, `ready`, problems,
  install `prefix`, model `endpoint`, `context_epoch`.
- AC-STORE-3: `channels` (name, topic, `halted`, auto-approve level,
  `context_epoch`), `channel_members`, `messages` (channel, sender kind
  `human`/`agent`/`system`, sender, target, text, time), `settings`
  (key → JSON value).
- AC-STORE-4: Columns added in later versions are added on open, so an old
  database keeps working.

**Rules — behaviour:**

- AC-STORE-11: With no path given, the database is `~/.agent-board/control.db`,
  or `~/.agents-control/control.db` (a board set up before the rename) when
  only that one exists (`board_home`, `default_db_path`). The choice is
  made by where `control.db` is, never by which folder exists: the board
  creates folders itself (wheels, staged `.env` files), which once switched a
  board onto a new, empty database. The machine driver stages its files in
  the same folder as the database (`board_home`).
- AC-STORE-5: `settings()` returns the shipped defaults overlaid with stored
  values. `update_settings` ignores unknown keys. Defaults:
  `default_framework` lean, `default_provider` openai, `default_model`
  gpt-4o-mini, `default_approval` true, `default_sdk_source` local in a
  source checkout and git when installed as a package (`running_from_checkout`),
  `agent_timeout_seconds` 1800, `poll_interval_seconds` 2,
  `poll_max_interval_seconds` 10, `poll_request_timeout_seconds` 60,
  `approval_timeout_seconds` 300, `transcript_context_messages` 8,
  `max_relay_hops` 0 (no limit).
- AC-STORE-6: `upsert_agent` records an install result or an `--list` entry,
  matched by machine and name. `prune_agents` drops records of agents no
  longer on the machine.
- AC-STORE-7: `agent_by_qualified` matches `<agent>.<machine>`
  case-insensitively.
- AC-STORE-8: Auto-approve levels per channel: `''` ask every time, `safe`
  approve everything except shell tools, `all` approve everything.
- AC-STORE-9: `bump_channel_context` / `bump_agent_context` add 1 to a
  counter that is part of the conversation id (ROUTER-CTX-1), which is how
  "clear" starts a fresh conversation.
- AC-STORE-10: `recent_transcript(channel, limit, exclude_id)` returns the
  last lines for a prompt, without the message being dispatched (it is added
  separately) and with runs of approval bookkeeping squashed into one line.

**Tests:** `tests/test_agent_board.py` (`TestStore` and others).
