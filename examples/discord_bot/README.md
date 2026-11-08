# Discord Bot Example

Serves an agent defined in `agent.yaml` as a Discord bot, via `DiscordAgentServer`.

```bash
export DISCORD_BOT_TOKEN=...   # see "Discord bot setup" below
export OPENAI_API_KEY=...      # whatever your agent.yaml provider needs
export TAVILY_API_KEY=...      # only if the config keeps the tavily_search tool

python run_bot.py --check      # verify the Discord side, then exit
python run_bot.py              # serve the agent
```

Then, in a channel the bot can see: `@YourBot what is the weather in Pune?`, or DM it, or use the `!ask ` prefix this example configures. Ctrl-C stops the bot; it also shuts down cleanly on `SIGTERM`.

| Flag | Purpose |
|---|---|
| `--check` | Log in, report servers and postable channels, log out. No agent, no LLM key. |
| `--config PATH` | A different agent YAML (default: `agent.yaml` beside the script) |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (default `INFO`) |
| `--env-file PATH` | Explicit `.env` to load |
| `--embed` | Post answers as an embed card instead of plain messages |

`run_bot.py` checks credentials before connecting and tells you exactly what to export if something is missing, rather than failing on the first model call. Keys are read from the environment, or from a `.env` beside the agent config, or `./.env`. Use `--env-file` to point elsewhere.

`run_bot.py` is a wrapper around the packaged CLI, so once the SDK is installed these are the same:

```bash
python run_bot.py --check
declarative-agent discord --config agent.yaml --check
```

A standalone binary with no Python dependency is also available — see
[packaging/README-dist.md](../../packaging/README-dist.md).

To test the transport's behaviour without a token, an API key, or a network, run its test suite instead — it covers mention/DM/prefix routing, working-status updates, `<think>` stripping, 2000-character splitting, per-channel sessions, the ✅/❌ tool-approval round trip (approve, deny, timeout), and error reporting:

```bash
pytest tests/test_discord_agent_server.py -q
```

## The agent config

The shipped `agent.yaml` uses **`agent_framework: lean`**. Pick **one** framework; each owns its loop end-to-end (no nesting):

```yaml
agent_framework: lean       # native ReAct + progressive skills (default)
# agent_framework: adk      # Google ADK Runner
# agent_framework: deepagent  # deepagents / LangGraph
provider: openai
model: gpt-5.4
skills_directory: skills
```

Switch `model` / `provider` / `endpoint` to match the API key you have (OpenAI, Anthropic, Google, or local vLLM).

## Checking the Discord side first

Once you have a bot token (setup below), check it before involving the agent:

```bash
export DISCORD_BOT_TOKEN=...
python run_bot.py --check
```

It logs in, prints the bot's identity, every server it joined and which channels it can actually post in, then logs out. Failures are named rather than dumped as tracebacks — a rejected token, or the MESSAGE CONTENT intent still switched off in the developer portal.

```
connected as newsbot#4821  (id 1319…)

  server: My Test Server
    can post in #general
    can post in #bot-testing

  mention it as <@1319…> in any channel above, or DM it, once you start the bot.
```

Channels where the bot cannot add reactions are flagged, since that is what the ✅/❌ tool-approval flow uses.

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

Then `python run_bot.py --check` to confirm, and `python run_bot.py` to serve.

The bot shows as **online in Discord only while `run_bot.py` is running** — it goes offline the moment you stop the process. For an always-on bot, run it under a supervisor (`systemd`, `docker compose` with `restart: unless-stopped`, etc.); see `examples/docker/`.

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
| `run_bot.py` | Entry point — a thin wrapper over `declarative-agent discord` |
| `agent.yaml` | Sample agent config (`agent_framework`, provider, tools) |
| `instructions.md` | System instructions for the sample agent |

### Choosing a runtime (peers — pick one)

| `agent_framework` | Class | Owns the loop |
|---|---|---|
| **`lean` (default)** | `LeanAIAgent` | Native ReAct + progressive skills |
| `adk` | `ADKAIAgent` | Google ADK Runner |
| `deepagent` | `LangChainAIAgent` | deepagents / LangGraph |

Shared across all three: YAML config shape, `AgentEvent` stream, Discord/A2A transports.  
**Do not mix frameworks** (no ADK/deepagent nested under lean).

Progressive skills (`skills/<name>/SKILL.md`) are a lean feature. Meta tools (`Skill`, `list_skills`, `read_file`) auto-approve on lean; other tools respect `tools_approval_required`.

### Markdown on Discord

Discord only supports a **subset** of Markdown. Agent replies are passed through
`to_discord_markdown()` so tables, HTML, images, and `---` rules become forms
Discord can show (e.g. tables → monospace code blocks). Bold, lists, and code
fences already render natively.

Pass `--embed` to put answers in an embed card instead (often cleaner for long
answers; ~4096 characters per embed chunk):

```bash
python run_bot.py --embed
```
