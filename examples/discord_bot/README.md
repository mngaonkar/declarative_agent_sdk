# Discord Bot Example

Test program for `DiscordAgentServer`. Three modes, in increasing order of what they need.

## 1. `selftest` — no token, no API key, no network

```bash
python run_discord_bot.py
```

Runs a scripted stub agent behind a fake Discord channel and asserts the server's behaviour: mention/DM/prefix routing, working-status updates, `<think>` stripping, 2000-character splitting, per-channel sessions, the ✅/❌ tool-approval round trip (approve, deny, timeout), and error reporting. Prints a pass/fail report and exits non-zero on failure, so it works as a smoke test in CI.

## 2. `local` — your real agent, fake Discord

```bash
export GOOGLE_API_KEY=...      # whatever your agent.yaml provider needs
export TAVILY_API_KEY=...      # only if the config keeps the tavily_search tool
python run_discord_bot.py --mode local --config agent.yaml
```

`local` and `live` check credentials before starting and tell you exactly what to export if something is missing, rather than failing on the first model call. Keys are read from the environment, or from a `.env` file next to this example or at the repo root. The shipped `agent.yaml` uses Gemini — switch the `model` / `provider` / `endpoint` block to OpenAI or a local vLLM server if that is what you have a key for.

Gives you a terminal REPL. Everything you type is wrapped as a Discord message mentioning the bot and pushed through `DiscordAgentServer.handle_message`, so you see exactly what the bot would post — including status updates and tool-approval prompts, which you answer with `y`/`n` at the prompt instead of reactions. Use this to check prompt and formatting behaviour before involving Discord.

## 3. `connect` — real Discord, no agent

Once you have a bot token (setup below), check it before involving the agent:

```bash
export DISCORD_BOT_TOKEN=...
python run_discord_bot.py --mode connect
```

It logs in, prints the bot's identity, every server it joined and which channels it can actually post in, then logs out. Failures are named rather than dumped as tracebacks — a rejected token, or the MESSAGE CONTENT intent still switched off in the developer portal.

```
connected as newsbot#4821  (id 1319…)

  server: My Test Server
    can post in #general
    can post in #bot-testing

  mention it as <@1319…> in any channel above, once you start live mode.
```

## 4. `live` — real Discord, real agent

```bash
export DISCORD_BOT_TOKEN=...
export GOOGLE_API_KEY=...
python run_discord_bot.py --mode live --config agent.yaml
```

Then, in a channel the bot can see: `@YourBot what is the weather in Pune?`, or DM it, or use the `!ask ` prefix this example configures. Ctrl-C stops the bot.

---

## Discord bot setup

One-time, in the [Discord developer portal](https://discord.com/developers/applications):

1. **New Application** → name it → **Bot** in the sidebar → **Add Bot**.
2. **Reset Token** → copy it → `export DISCORD_BOT_TOKEN=...`. The token is shown once; treat it like a password and keep it out of git (a `.env` beside this example is read automatically and `.env` is gitignored).
3. **Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT** → Save. Without this the gateway delivers empty `message.content` and the bot silently never replies. This is the single most common reason a bot connects but does not answer.
4. Invite it to a server you administer — open this URL with your application's Client ID (found under **General Information**):

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot&permissions=68672
   ```

   `68672` = View Channels + Send Messages + Read Message History + Add Reactions. Reactions are what the ✅/❌ tool approval flow uses; drop it only if you set `tools_approval_required: false`.
5. Install the dependency if you have not: `pip install "declarative-agent-sdk[discord]"`.

Then `--mode connect` to confirm, and `--mode live` to serve.

### If the bot connects but stays silent

| Symptom | Cause |
|---|---|
| No reply to a mention, no log line for the query | MESSAGE CONTENT intent not enabled (step 3) |
| Replies to DMs but not in a channel | Bot lacks *Send Messages* / *View Channel* on that channel, or `allowed_channels` excludes it |
| Answers, but tool approval never resolves | Someone other than the asker answered — only the person who asked counts. If the ✅/❌ reactions are missing entirely the bot lacks *Add Reactions*; reply `yes` / `no` in the channel instead |
| `⚠️ Agent error: …` in the channel | The agent itself failed (missing LLM key, tool error); the message carries the reason |

## Files

| File | Purpose |
|---|---|
| `run_discord_bot.py` | The test program (all four modes) |
| `agent.yaml` | Sample agent config — used by `local` and `live` only |
| `instructions.md` | System instructions for the sample agent |
