# Discord transport — design spec

Runs any `BaseAgent` as a Discord bot. Started with `declarative-agent
discord` ([cli.md](cli.md)).

Code: `transports/discord/`.

## `transports/discord/__init__.py`

**Does:** exports `DiscordAgentServer`.

## `transports/discord/server.py`

**Does:** `DiscordAgentServer` — receives Discord messages, runs the agent,
shows progress, asks for tool approval, and posts the answer with any files
the agent produced.

**Rules — when it answers:**

- DISCORD-1: It answers when @-mentioned in a server channel
  (`respond_to_mentions`), in direct messages (`respond_to_dms`), on messages
  starting with `command_prefix` (CLI default `!ask `), or to every message
  when `respond_to_all_messages` is on. `allowed_channels` limits it to those
  channel ids.
- DISCORD-2: The bot's mention and the prefix are removed before the text
  goes to the agent. An empty query is ignored.
- DISCORD-3: `session_scope` picks what shares a conversation: `channel`
  (default), `user`, or `global`. Any other value is an error at start.

**Rules — while it works:**

- DISCORD-4: With `show_working_updates`, one status message is posted and
  edited as the agent works. `show_thinking` adds the model's plan text.
  With `keep_thinking`, that message stays as a frozen record when the answer
  arrives; otherwise it is deleted.
- DISCORD-5: A tool-approval event posts a prompt naming the tool and its
  arguments. ✅ or a reply like `yes`/`ok`/`go` approves; ❌ or
  `no`/`cancel`/`stop` denies. No answer within `tool_confirmation_timeout`
  (default 120 s) denies.

**Rules — the answer:**

- DISCORD-6: With `format_markdown`, GitHub Markdown is adapted for Discord:
  tables become monospace blocks, images and HTML are cleaned up.
- DISCORD-7: Long answers are split into 2000-character messages at line
  breaks, then word breaks, never mid-word when avoidable.
- DISCORD-8: Files are attached when the answer refers to them:
  `[[attach:path]]` markers, Markdown images, file URLs or local paths, and
  image files newly written to the workspace during the turn. At most 10 files
  of up to 24 MB each; duplicates (same content) are sent once; a reference to
  an attached file is removed from the text.
- DISCORD-9: A file named like an image but not containing image bytes (an
  HTML error page saved as `.jpg`) is not attached as an image.
- DISCORD-10: `reply_as_embed` sends the answer as an embed instead of plain
  text.

**Interface:** `DiscordAgentServer(agent, token, …)`, `.run()`, `.start()`,
`.close()`; helpers `split_message`, `to_discord_markdown`,
`extract_file_refs`.

**Tests:** `tests/test_discord_agent_server.py`.
