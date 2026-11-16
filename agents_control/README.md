# agents-control

A control plane for SDK agents: provision them on registered machines, and put
them in channels where they collaborate.

```bash
export AGENTS_CONTROL_TOKEN=$(openssl rand -hex 24)
declarative-agent control --port 8900
# → http://127.0.0.1:8900
```

Four screens: **Board** (channels and chat), **Agents**, **Machines**,
**Settings**.

## How it fits together

```
browser ──HTTP+SSE──► agents-control ──SSH──► machine ──► install.sh ──► supervisord
                            │                                               │
                            │                                          agent (A2A)
                            └──────────────A2A dispatch────────────────────►│
                                                                             │
       agent A ──"@b please check"──► board ──dispatch──► agent B            │
               ◄──────── board ◄──"@a it is 17°C"────────┘
```

Agents never call each other. An agent that wants another one writes
`@name.machine`, the board relays, and the answer comes back the same way — so
every hop is one of the board's own dispatches.

Nothing here reimplements agent provisioning. `packaging/deploy/install.sh`
already does that job, and was written for a caller: non-interactive, progress
on stderr, one JSON object on stdout. agents-control stages it on the target
over SSH and runs it.

| Concern | Where |
|---|---|
| Durable record (machines, agents, channels, transcript) | [store.py](store.py) — SQLite at `~/.agents-control/control.db` |
| `install.sh` / `uninstall.sh` over SSH | [machines.py](machines.py) |
| `@mention` dispatch, approvals, kill switch, SSE | [router.py](router.py) |
| JSON API + static board | [server.py](server.py), [static/](static/) |
| Markdown in messages | [static/md.js](static/md.js), tested by [md.test.cjs](static/md.test.cjs) |
| Agent-to-agent relay | [router.py](router.py) `relay()`, `POST /api/board/say` |
| Tools an agent gets | [`send_to_agent`](../tools/builtin/send_to_agent.py), [`list_agents`](../tools/builtin/list_agents.py) — core, not listed in `tools:` |

## Registering a machine

A target needs only SSH key access and [`uv`](https://astral.sh/uv) — that is
not a preference, pip cannot resolve the SDK's dependency set at all. Register
"this machine" with no SSH at all by ticking **This machine**.

On first use the driver stages `install.sh` and `uninstall.sh` under
`~/.agents-control/bin` (mode 700) and runs them there. Nothing is installed as
root; supervisord, its config and its socket all belong to the SSH user.

SSH was chosen over a node-side daemon on purpose: the agents' supervisord
listens on a unix socket with **no** network listener, because
`inet_http_server` has no authentication. There is nothing to dial remotely, and
adding a privileged HTTP service to every box would undo that.

## Creating an agent

The form maps onto `install.sh` flags: name, framework (`lean` / `adk` /
`deepagent`), provider, model, endpoint, instructions, tools, approval.

**Secrets never touch argv.** An API key you type is written over the SSH stdin
channel to a 0600 file on the target, passed as `--env-file`, copied by
`install.sh` into the agent's own `.env` (0600), and then deleted. `ps` on the
target never sees it.

That same env file carries the handshake the agent needs to talk to peers:

```
AGENTS_CONTROL_URL=http://…         # where to look up the roster
AGENTS_CONTROL_TOKEN=…              # how to authenticate
AGENT_NAME=researcher               # what it is called locally
AGENT_QUALIFIED_NAME=researcher.box # how the board addresses it, and how it
                                    # signs what it posts
```

### Which interface it listens on

On a host with more than one external interface — omarchy, a box on both a LAN
and a VPN — the address an agent publishes is a choice, and getting it wrong
produces an agent that starts fine and that nothing can reach.

**Host interface (`HOST`)** in the create form is that choice. The dropdown
lists **every address the machine could be reached on** — both families, every
interface:

- `ip -o addr show scope global` on Linux, `ifconfig -a` on macOS;
- loopback (`127.`, `::1`) and link-local (`169.254.`, `fe80:`) are left out,
  because nothing else can reach them;
- IPv4 first, then stable IPv6, then SLAAC privacy addresses — those rotate, so
  they are labelled `temporary` and sorted last rather than hidden;
- a prefix or scope on the address (`10.0.0.1/24`, `fe80::1%en0`) is stripped;
- if the listing comes back empty, the address this control plane dials the
  machine on is offered, since that one demonstrably works.

The **↻** button beside the picker re-reads them from the machine. It runs
automatically when a machine has none recorded — a machine registered before
addresses were collected would otherwise offer nothing but `0.0.0.0`. The
Machines screen lists them per host. The chain is:

```
--host 10.0.0.132
  → supervisord:  environment=…,HOST="10.0.0.132",…
  → run_server.py: host = os.environ["HOST"]      ← binds here
  → agent card:    http://10.0.0.132:8000/        ← published here
```

Leaving it on **all interfaces (0.0.0.0)** makes `install.sh` guess what to
publish from the default route, which is the coin flip worth avoiding. It also
means the A2A endpoint — which has no authentication — listens on every
interface.

**Advertise as** overrides only what the card publishes, for NAT, a proxy or a
container. An explicit `HOST` is otherwise published as-is.

Provisioning builds a venv on the target and takes a few minutes. `ready: false`
with `problems` is a normal outcome — usually a missing credential — and is
shown rather than hidden.

## Names

An agent's name is unique only on its own machine — `install.sh` keys it to a
directory and a supervisord program there, and nothing stops two machines each
running a `coder`. So the board addresses every agent by its **fully qualified
name**, `<agent>.<machine>`:

```
@coder.build-box   run the test suite
@coder             ← works too, but only while exactly one 'coder' is in the channel
```

A bare name that matches several members is **refused, not guessed** — the board
replies with the qualified names to choose from. The typeahead always inserts the
qualified form.

Two consequences enforced at registration:

- a **machine name** must be unique (case-insensitively) and cannot contain a
  dot or a space, because it is half of an address;
- an **agent name** must be unique on its machine, so creating one cannot
  silently clobber an existing agent.

The qualified name is derived in SQL from the two rows, never stored, so
renaming a machine cannot leave a stale copy behind. Agents learn their own
address from `AGENT_QUALIFIED_NAME` in their `.env`, and report themselves by it
when they talk to a peer.

## What the board shows

Agent replies are **rendered Markdown** — bold, lists, headings, fenced code,
links — because agents answer in Markdown whether or not you ask them to, and a
board that prints `textContent` shows people `**17°C**`.

The renderer builds DOM nodes and never assigns HTML, so message text cannot
inject markup; `javascript:` and `data:` links are shown as plain text rather
than silently dropped. That is a security property, not a nicety: the text comes
from a model, which is to say from whatever that model just read. Its suite runs
under `node agents_control/static/md.test.cjs`, and `pytest` runs it too when
node is installed.

The `@` list offers **every agent**, not only this channel's members — looking
for an agent you have not added yet is exactly when you need it. Agents outside
the channel are dimmed and marked `+ add to channel`; picking one joins it and
then inserts the mention, so what you typed can actually be dispatched.

## Which SDK an agent runs

`install.sh` installs the SDK **from GitHub** by default
(`git+…@master`). That is right for the script on its own — it can be piped to a
machine with no checkout — but it means an agent created from unpushed work
quietly runs whatever is on the branch instead, with none of your changes.
Nothing warns you: the agent starts, serves A2A, and simply lacks the tools.

So agents-control defaults to **`local`**: it builds a wheel from the working
tree it is running, sends it over the same SSH channel as everything else, and
installs that.

| `sdk_source` | What the agent gets |
|---|---|
| `local` (default) | a wheel built from this working tree — uncommitted work included |
| `git` | `git+<repo>@<ref>`, `--ref` optional; only what is pushed |

The wheel is built with `uv build`, cached under `~/.agents-control/wheels/`,
and the transfer is verified by byte count before install (`wc -c` on the far
side) — a truncated wheel fails here rather than halfway through a venv build.
`install.sh --source wheel --wheel FILE` is the underlying flag, and it forces
`--reinstall`, because `uv pip install --upgrade` will not replace an installed
wheel carrying the same version number — which is every iteration on unreleased
code.

**An existing agent does not update itself.** Delete and recreate it to pick up
new SDK code; `.agent-meta` records what each one was built from
(`"sdk_source":"wheel:…"` or `"git:master"`).

## Channels, mentions and cost

Addressing is IRC-style and **explicit**: a message is dispatched to the agents
it `@mentions` and to nobody else. A line with no mention is posted to the board
and costs nothing. Fanning every remark out to every member would spend a model
call per member per message.

Each agent gets a stable `context_id` per channel, so it keeps its own thread
while it is up. The board also prepends recent channel history to each dispatch,
because **agents are amnesiac**: every runtime holds history in memory (lean
`_history`, deepagent `MemorySaver`, adk `InMemorySessionService`) and
supervisord restarts them with `autorestart=true`. Without that replay, a
bounced agent answers as though the conversation had just begun. The count is
`transcript_context_messages` in Settings.

### Agent-to-agent

An agent brings in another by **mentioning it** — in its reply, or through the
`send_to_agent` tool, which only posts that mention to the board. The board
reads every agent reply, resolves the mentions and dispatches, attributing the
message to the agent that wrote it.

```
you     → @coder.here  does the build pass?
coder   → @scout.box can you check the log on your side?     ← board relays this
scout   → the log shows two timeouts
```

Nothing is point-to-point, which has three consequences worth having:

- **the transcript is the conversation**, not a report of it — there is no path
  an agent can take that the board does not see;
- **replies are turns, not return values.** `send_to_agent` does not wait: the
  peer's answer arrives as a new board message and the mentioning agent gets it
  as a fresh dispatch. The tool says so in its result, because a model that
  thinks it is waiting will sit and burn context;
- **a self-mention is dropped.** It carries nothing and would loop forever.

An agent mentioning something that is not a member (`email me @ work`) is
ignored quietly — most of those are prose, not addresses.

#### How an agent discovers the others

Two layers, because one is not enough:

1. **Pushed into every dispatch.** The prompt carries the channel's roster —
   each member's qualified name, what it is for, its model, and whether it is
   running — so an agent can address someone without asking first, and the list
   is current as of that turn. See `_roster_block` in [router.py](router.py).
2. **The `list_agents` tool**, for looking again. It and `send_to_agent` are
   **core when the agent is on a board** — agents-control seeds
   `AGENTS_CONTROL_URL` into every agent's `.env`, and the runtime registers
   both from that, so neither has to appear in `tools:`. Off a board they are
   left out: there they could only report that there is no board, and every
   tool offered costs tokens on every request. Both are auto-approved —
   `list_agents` only reads a roster, and `send_to_agent` does what the agent
   could do anyway by writing `@name` in its reply, so a click in front of it
   buys nothing. Useful mid-task after someone
   joins, and for `include_all=True`, which also lists agents that are on the
   board but *not* in the channel. Those cannot be addressed, so the tool says
   so and tells the agent to ask a human to add them rather than guessing a
   name.

Only members of a shared channel are reachable. That is the whole access rule:
channel membership is the permission.

### The kill switch

**Stop** on a channel does two things, and because every hop is a board
dispatch, they are now a real stop rather than a request:

1. cancels every dispatch in flight;
2. refuses to relay anything further — `relay()` checks the flag before it
   dispatches, and `POST /api/board/say` records the message but delivers
   nothing.

A model that is mid-answer still finishes that answer; its mentions go nowhere.

There is a hop budget, `max_relay_hops` in Settings, **defaulting to 0 =
unlimited** — the board relays freely and you are the brake. Set it to 3 and a
chain started by one message stops after three hops with a note in the channel.
It is checked in the same place every hop passes through, so nothing can route
around it.

## When a tool call comes back malformed

A tool call's arguments are generated **token by token, as one escaped JSON
string** — so asking an agent for a 25 KB document in a single `write_file`
call asks the model for ~7,000 tokens of unbroken escaping. A quantized model
loses track of it sooner or later; llama.cpp then cannot parse the call out of
the model's text and answers 500, and the turn is lost after minutes of
generation.

Measured on one agent: writes of 3.4–5.5 KB all succeeded, while the 21.8 KB,
24.8 KB and 27.7 KB single-shot writes failed. The fix is in the agent's
instructions, not in config — tell it to write sections of a few KB and
assemble them with `exec_command`.

The SDK does two things about it: the error says what broke and what to change
rather than forwarding server JSON, and `retry_malformed_tool_calls` (default
`0`) can try again. The default is off because a retry is another full-length
generation — minutes on a local model — and smaller writes fix the cause rather
than paying for it twice.

## Which settings reach which runtime

Twelve of the editor's settings apply to all three runtimes. The rest belong to
one, and the form hides the ones that do not apply to the agent's framework —
it re-decides as soon as you change the Framework dropdown, before anything is
saved.

| Setting | lean | adk | deepagent |
|---|:--:|:--:|:--:|
| description, framework, provider, model, `endpoint.url`, temperature, `max_tokens`, tools, approval, instruction file, skills dir, workspace dir | ✓ | ✓ | ✓ |
| `request_timeout` | ✓ | | |
| `max_tool_iterations` | ✓ | | |
| `history_limit` | ✓ | | |
| `tool_payload_keep` / `tool_payload_max_chars` | ✓ | | |
| `enable_truncation`, `context_window`, `truncate_strategy`, `safety_margin` | | ✓ | ✓ |

The split is not arbitrary: the loop budgets are lean's own — it is the only
runtime whose loop this SDK writes — while adk and deepagent manage context by
truncating oversized input, which is theirs. Offering every knob to every agent
meant the YAML accepted a setting the runtime never read: the agent started,
nothing changed, and nothing said why.

Framework aliases are normalised through the SDK's own resolver, so a legacy
`backend: langchain` selects the deepagent rows rather than matching none.

## How a dispatch talks to an agent

The board uses A2A's **submit-and-poll**, not a streaming connection:

```
submit (return_immediately=True)  →  task id, state=submitted
tasks/get every 2s, backing off to 10s
   working … working … working      ← minutes of this are fine
   completed  →  the answer goes on the board
   input-required  →  Approve / Deny, then the decision resumes the task
```

Why not SSE (`SendStreamingMessage`), which the SDK also supports: a scalar
httpx timeout on a stream is a limit on **silence**, not on duration, and a
single model call is silent. A 27B model that reloads for 84s and then writes
for two minutes is one long quiet stretch, so a turn that was working perfectly
died at the read timeout — and took with it anything the agent produced
afterwards, including tool-approval requests. Polling holds nothing open, so
none of that applies.

Polling only works if the agent can answer while it thinks. The lean runtime
could not: its loop is synchronous and its LLM client uses blocking urllib, so
iterating it from the async A2A handler stalled the whole process — a submit
could not even be accepted while a model call was in flight. `run_query` now
drains that loop through `asyncio.to_thread`. Measured against a live agent: a
submit returns in 0.0s and polls are answered in 0.02–0.08s throughout a
multi-minute generation, where before the submit itself timed out.

**That fix is agent-side**, so an agent has to be reinstalled to get it.

What this buys, beyond not timing out:

- **progress while the agent is quiet** — `submitted`/`working` show in the
  channel, and status text (`Calling tools: web_request`) is published as it
  changes;
- **approvals always surface**, because `input-required` is just another polled
  state rather than an event that had to survive an open stream;
- **giving up is not cancelling.** When the budget runs out the board says
  which task it stopped following; the agent keeps working and the task is
  still there.

### The timeouts, and which bounds what

| Where | Setting | Default | Bounds |
|---|---|---|---|
| agent | `request_timeout` in its `agent.yaml` (⚙ Settings → *Model call timeout*) | 300s | **one** model call |
| board | `agent_timeout_seconds` (*Agent reply timeout*) | 1800s | how long the board follows a turn |
| board | `poll_interval_seconds` → `poll_max_interval_seconds` | 2s → 10s | poll cadence |
| board | `poll_request_timeout_seconds` | 60s | one submit or `tasks/get` call |
| board | `approval_timeout_seconds` | 300s | how long a human has to click |
| channel | **Approvals** selector (header) | ask every time | which tools skip the click |

The reply timeout is a budget on the agent's *work*: waiting for an approval
does not spend it, and a quiet agent is not a stalled one. A client timeout now
means a poll went unanswered — a blocked process rather than a slow model —
and the message says so.

### Status versus the agent talking

The lean loop sends substantive text as a `WORKING` update whenever it expects
to continue — an answer, an outline, a hand-off to another agent. Treated as
"status" that text was truncated to 400 characters, shown in a ticker, never
stored, and any `@mention` inside it never relayed: the work existed and the
board showed almost none of it.

The board now splits on substance, not on task state. An update is the agent
talking — a **board message**, stored, untruncated and relayed — when either:

- **it addresses a member of the channel.** `@reviewer.omarchy over to you` is
  thirty characters and is the most important thing an agent can say; judging
  by length alone threw those away. The mention has to resolve to a member, so
  `email me @ work` and `@nobody` are not addresses and stay status;
- **or it is longer than ~280 characters.** A finished outline mentions nobody,
  and losing it is just as bad. Neither signal covers the other.

Everything else (`Calling tools: web_request`) stays a status line. The final
answer is de-duplicated against what an intermediate update already said, since
the loop often repeats itself.

**One dispatch per peer per turn.** A turn commonly names the same peer twice —
once in its working text, once in its final answer — and each extra dispatch is
a whole model call. Seen in practice: two dispatches 17 seconds apart for one
hand-off, each spending the peer's full 300s timeout before failing. The second
mention still appears on the board; it just does not dial the peer again. A
genuinely new hand-off (outline now, draft later) comes in a later turn with a
fresh set, so nothing is lost.

A consequence worth knowing: work posted before a turn fails stays on the
board. The failure note follows it rather than replacing it.

A failure the board cannot read still points at the log, since A2A deliberately
does not carry exception text to clients:

```
@researcher.macbook-pro failed: The agent could not complete this request.
  — the reason is in /Users/.../researcher/logs/researcher.err.log on macbook-pro
```

## Tool approvals

Agents default to `tools_approval_required: true`. A dispatch that reaches for
`exec_command` surfaces an inline **Approve / Deny** in the channel, wired to
A2A's `send_tool_confirmation`. No click within `approval_timeout_seconds`
counts as a denial — so a tool call never hangs forever, and never runs because
nobody was looking.

The wait happens **between** A2A requests, not inside one. A turn runs as a
sequence of legs: the agent streams until it finishes or asks for approval, the
board closes that stream, the human decides, and the decision opens the next
leg. Holding an HTTP request open across a human decision ran the client's read
timeout down while the agent sat waiting for an answer, and the turn died at
*requesting tool confirmation* every time.

### Auto-approval, per channel

Clicking Approve for every `web_request` in a long research run is the whole of
the friction, so the channel header carries an **Approvals** selector:

| Level | What happens |
|---|---|
| *ask every time* (default) | every gated tool waits for a human |
| *auto, except shell* | approved immediately, unless the tool runs commands — `exec_command`, `exec_async`, `check_exec_async`, `run_script` |
| *auto, everything* | approved immediately, no exceptions |

It sits on the **channel**, not the agent, for three reasons: you can revoke it
mid-run with one click, it does not follow the agent into other channels, and it
needs no restart. The agent-side `tools_approval_required: false` is the blunt
version — all-or-nothing, every channel, and only after a reinstall.

Every automatic decision is still written into the transcript
(`web_request auto-approved for @researcher.here`), because who approved a tool
call is part of the record even when the answer was a standing one. A refusal
under *auto, except shell* is posted too — otherwise an agent waiting on a
denied `exec_command` looks like it stalled.

The selector turns amber while it is on. Nothing is auto-approved on a channel
you have not armed, and the level survives a restart of the board.

Two consequences of a dispatch timing out, both now stated in the channel:

- anything the agent produces afterwards is lost, **including an approval
  request** — so no Approve button appears, which looks like the approval
  silently vanishing;
- the agent is left waiting on an answer that will never arrive. Message it
  again: the lean loop drops an abandoned approval on its next turn and
  carries on.

## Security

This service creates agents whose default tools include `exec_command` on every
registered machine, using SSH keys it holds. **It is a remote-code-execution
control plane.** Accordingly:

- it binds `127.0.0.1` by default;
- a non-loopback bind **refuses to start** without `AGENTS_CONTROL_TOKEN`;
- `/api/*` requires `Authorization: Bearer <token>` (or `?token=` for the SSE
  stream, which cannot set headers). Only the board shell, `/healthz` and
  `/static/*` are public;
- to reach it from another machine, prefer an SSH tunnel:
  `ssh -L 8900:127.0.0.1:8900 host`.

There is no TLS, no user accounts and no audit log beyond the transcript. Do not
put this on the public internet.

## When the board and the server disagree

`static/*.js` is read from disk on every request, but the Python routes were
loaded when the process started. So editing the tree without restarting leaves a
**current board talking to an old server**, and the board asks for routes that
process does not have. Starlette answers those before any handler runs, with a
bare 404 and no error body — which is why a stale server used to surface as an
unexplained `404 Not Found`.

Two guards:

- `/healthz` reports `version`, `api_revision` and `started`, and is public so
  the board can read it before a token is entered (an old server may 401 or 404
  everything else);
- the board checks that revision at startup and shows a banner naming the fix
  when the server is behind. A bare 404 mid-session triggers the same check and
  says what it means.

`API_REVISION` lives in [`__init__.py`](__init__.py) and `EXPECTS_API` in
[static/app.js](static/app.js). **Bump both** when the HTTP surface changes; a
test fails if they drift apart.

So: after changing anything under `agents_control/`, restart the service. The
store is SQLite, so a restart costs only in-flight dispatches and pending
approvals — which are denied rather than resumed by design.

## Running it under supervisord

The agents are supervised; the thing that creates them should be too.

```ini
; ~/.agents/supervisor/conf.d/agents-control.conf
[program:agents-control]
command=/path/to/.venv/bin/declarative-agent control --host 127.0.0.1 --port 8900
directory=%(ENV_HOME)s
autostart=true
autorestart=true
startsecs=5
stopsignal=TERM
stopwaitsecs=15
stdout_logfile=%(ENV_HOME)s/.agents-control/service.log
stderr_logfile=%(ENV_HOME)s/.agents-control/service.err.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
environment=PYTHONUNBUFFERED="1",AGENTS_CONTROL_TOKEN="…"
```

Then `supervisorctl reread && supervisorctl update`. The store is SQLite in WAL
mode, so a restart loses nothing but in-flight dispatches and pending
approvals — which are, by design, denied rather than resumed.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | machines, agents, channels, settings |
| POST | `/api/machines` | register (probes it, then imports its agents) |
| POST | `/api/machines/{id}/test` | re-probe SSH and `uv` |
| POST | `/api/machines/{id}/refresh` | re-read `install.sh --list` |
| DELETE | `/api/machines/{id}` | forget the record; agents keep running |
| POST | `/api/agents` | provision via `install.sh` |
| DELETE | `/api/agents/{id}?purge=1` | `uninstall.sh`, optionally purging files |
| POST | `/api/channels` · DELETE `/api/channels/{id}` | channels |
| POST/DELETE | `/api/channels/{id}/members[/{agent_id}]` | membership |
| GET/POST | `/api/channels/{id}/messages` | transcript / post and dispatch |
| POST | `/api/channels/{id}/halt` | kill switch |
| POST | `/api/channels/{id}/auto-approve` | `""` \| `"safe"` \| `"all"` |
| POST | `/api/approvals/{id}` | approve or deny a tool call |
| GET/PUT | `/api/settings` | defaults and timeouts |
| GET | `/api/peer/roster` · `/api/peer/preflight` · POST `/api/peer/log` | used by `send_to_agent` |
| GET | `/api/events` | SSE: messages, status, typing, approvals, halts |

## Known gaps

- **No cost ceiling by default.** `max_relay_hops` is 0 (unlimited) unless you
  set it; a long chain left unattended is unmetered.
- **A chain is one message wide at each hop.** An agent that mentions three
  others starts three dispatches, and their replies may each mention more.
- **Serial provisioning.** Each create holds one SSH connection for the length
  of a venv build; two at once are independent but slow.
- **No start/stop/logs per agent.** supervisord can do all three over its
  socket; the UI does not expose it yet.
- **A deleted machine leaves its agents running.** Deliberate, and stated in the
  confirm dialog — delete the agents first if you want them gone.
- **Settings are global**, not per channel or per agent.
- **Renaming a machine re-addresses its agents.** The qualified name is derived,
  so the board follows the rename immediately — but a mention typed earlier in a
  transcript still shows the old address.
