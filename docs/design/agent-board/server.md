# agent-board server — design spec

Code: `agent_board/server.py`.

## `agent_board/server.py`

**Does:** `ControlPlane` wires the store, machine driver, router, metrics
sampler and logbook into one Starlette app (`create_app`): a JSON API, an SSE
stream and the static UI.

**Rules — security:**

- AC-SERVER-1: Binding anything but loopback without a token is refused at
  start (AC-1). With a token, every route needs `Authorization: Bearer
  <token>` (or `?token=`), except `/`, `/index.html`, `/healthz` and
  `/static/…`. The one other way in: a file ticket (AC-SERVER-13) opens
  `GET /api/files/relay` for the single link it was made for.
- AC-SERVER-2: Every handler goes through `guard`: auth, then errors mapped
  to answers — `MachineError` → 502 with the command's stderr as `detail`,
  `LookupError` → 404, `ValueError` → 400, anything else → 500 with the error
  named and the traceback logged.
- AC-SERVER-3: `guard` logs every change (POST/PUT/DELETE) with its status and
  time taken, and every failed request with its reason. Successful reads are
  not logged, because the UI polls them.

**Rules — machines and agents:**

- AC-SERVER-4: Creating an agent checks the name
  (`^[a-z0-9][a-z0-9_-]*$`, unique on the machine) and that addresses look like
  an IP or hostname, before anything runs on the machine.
- AC-SERVER-5: An agent's tools default to none extra; core tools come from
  the runtime (TOOLS-REG-3).
- AC-SERVER-12: The SDK source for a create or reinstall is the request's,
  else the default setting. `local` on a control plane installed as a package
  becomes `git`, with a warning in the log, instead of failing.
- AC-SERVER-6: A bind address must belong to the machine. Wildcards
  (`0.0.0.0`, `::`), loopback and hostnames are allowed through; an IP the
  machine does not have is refused with the machine's real addresses. The
  recorded addresses are re-read from the machine before refusing, and the
  record is updated. Checked on create and when Settings changes the address.
- AC-SERVER-7: Reinstall re-runs `install.sh` keeping config, `.env`, port,
  workspace and address. It refreshes the board variables in `.env` one at a
  time. If the agent's current bind address is not one the machine has, it is
  replaced by the machine address on the same network (same first octet), or
  `0.0.0.0` when none matches; a published address equal to the bad bind
  address is replaced too. The answer says what was changed
  (`address_fixed`).
- AC-SERVER-8: `GET /api/files/relay?url=<link>` passes an agent's file
  through for readers that cannot reach it (the UI, agents on other
  networks). The link must be `/files/<path>` with no `..`, empty or hidden
  parts, on a registered agent's address; the request goes to that agent's
  recorded URL, never to the host the link names. Nothing is stored. The
  `Range` / `If-Range` headers go to the owner and `206`, `Content-Range` and
  `Accept-Ranges` come back, so a media element can stream and seek.
- AC-SERVER-13: `POST /api/files/ticket {url}` (needs the token) checks the
  link as the relay would and returns a relay URL carrying a ticket instead
  of the token: `<expiry>.<HMAC-SHA256(expiry, link)>`, valid for exactly
  that link for an hour. The key is random per process, so a restart ends
  every ticket. A `<video>` cannot send the Authorization header, and the
  board token never goes in a URL.

**Rules — MCP catalog:**

- AC-SERVER-14: Attaching, detaching, editing with sync and deleting all go
  through one step: read the agent's `agent.yaml`, set `board_mcp_servers`
  from its links (MCP-CAT-6), write it and restart the agent — skipped when
  nothing changed (MCP-CAT-10). Each agent's outcome is returned; one failing
  agent does not stop the others.
- AC-SERVER-15: A secret's value goes from the request straight to
  `set_env_var` on each attached agent's machine (AC-MACH-11); it is not
  stored, logged or returned (MCP-CAT-3).

**Rules — logs:**

- AC-SERVER-9: `GET /api/logs?after=&level=&q=&limit=` pages the control log
  (AC-LOG-1). `GET /api/logs/download` returns `control.log` as a file (the
  in-memory records when there is no file).
- AC-SERVER-10: `GET /api/agents/{id}/logs?stream=&lines=` tails an agent's
  log (AC-MACH-14); `/logs/download?stream=` returns it whole (AC-MACH-15),
  with `X-Log-Truncated: 1` when cut.

**Rules — static UI:**

- AC-SERVER-11: Static files are served with `Cache-Control: no-cache`, so an
  edited UI is picked up on reload. `/healthz` reports the version, the API
  revision and when the process started.

**Interface — routes:**

| Method | Path | What |
|---|---|---|
| GET | `/`, `/healthz`, `/api/state` | UI, health, full board state |
| POST | `/api/machines` | register a machine (probes it) |
| DELETE | `/api/machines/{id}` | forget a machine |
| POST | `/api/machines/{id}/test`, `/refresh`, `/interfaces` | probe; re-read its agents; re-read its addresses |
| POST | `/api/agents` | create (install) an agent |
| DELETE | `/api/agents/{id}` | uninstall (optionally purge) |
| POST | `/api/agents/{id}/reinstall` | AC-SERVER-7 |
| GET / PUT | `/api/agents/{id}/config` | read / write `agent.yaml`, instructions, key, address |
| GET | `/api/agents/{id}/models` | models the agent's endpoint serves |
| POST | `/api/agents/{id}/action` | `start` / `stop` / `restart` / `status` |
| POST | `/api/agents/{id}/clear-context` | ROUTER-CTX-2 |
| GET | `/api/agents/metrics` | model-server metrics, agent context sizes and MCP servers |
| GET | `/api/agents/{id}/logs`, `/logs/download` | AC-SERVER-10 |
| POST | `/api/models` | list a provider's models for the create form |
| GET | `/api/files/relay` | AC-SERVER-8 |
| GET / POST | `/api/mcp` | the MCP catalog and presets; add an entry (MCP-CAT-1) |
| GET | `/api/mcp/registry?search=&cursor=` | search the MCP Registry (MCP-CAT-13); 502 when it cannot be read |
| PUT / DELETE | `/api/mcp/{id}` | edit (optionally updating attached agents) or delete (MCP-CAT-9) |
| POST | `/api/mcp/{id}/secret` | write one `${NAME}` into attached agents' `.env` (MCP-CAT-3) |
| GET / POST | `/api/agents/{id}/mcp` | an agent's attached entries; attach or change overrides (MCP-CAT-5) |
| DELETE | `/api/agents/{id}/mcp/{server_id}` | detach |
| POST | `/api/files/ticket` | AC-SERVER-13 |
| POST | `/api/channels` | create a channel |
| DELETE | `/api/channels/{id}` | delete a channel |
| POST | `/api/channels/{id}/clear` | ROUTER-CTX-2 |
| POST / DELETE | `/api/channels/{id}/members[/{agent_id}]` | join / leave |
| GET / POST | `/api/channels/{id}/messages` | read / post (posting dispatches) |
| POST | `/api/channels/{id}/halt`, `/auto-approve` | ROUTER-HALT-1, ROUTER-APPR-2 |
| POST | `/api/approvals/{id}` | answer a pending tool approval |
| GET / PUT | `/api/settings` | AC-STORE-5 |
| GET | `/api/logs`, `/api/logs/download` | AC-SERVER-9 |
| POST | `/api/board/say` | an agent speaks; its mentions are relayed |
| GET | `/api/peer/roster`, `/api/peer/preflight` | agents an agent can see; may it send (not halted) |
| POST | `/api/peer/log` | record an agent-to-agent exchange |
| GET | `/api/events` | SSE stream of board events |

**Tests:** `tests/test_agent_board.py`, `tests/test_board_logs.py`,
`tests/test_bind_address.py`, `tests/test_file_tickets.py`.
