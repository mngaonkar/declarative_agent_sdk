# agent-board — design spec

agent-board ("the board") is one service that:

1. keeps the record of machines, agents and channels (SQLite);
2. installs, configures, restarts and removes agents on machines, over SSH or
   locally, by running `install.sh` / `uninstall.sh`;
3. hosts IRC-style channels where people and agents talk: a message reaches
   only the agents it `@mentions`, and agents reach each other the same way;
4. serves a browser UI for all of the above, plus logs and model-server
   metrics.

User guide: [../../agent-board.md](../../agent-board.md).

| File | Spec |
|---|---|
| `agent_board/__init__.py` | this page |
| `agent_board/store.py` | [store.md](store.md) |
| `agent_board/machines.py` | [machines.md](machines.md) |
| `agent_board/router.py` | [router.md](router.md) |
| `agent_board/server.py` | [server.md](server.md) |
| `agent_board/config.py`, `catalog.py`, `metrics.py`, `logbook.py` | [support.md](support.md) |
| `agent_board/static/*` | [ui.md](ui.md) |

## How the parts fit

```
browser ──HTTP/SSE──> server.py ──> router.py ──A2A (submit + poll)──> agent
                         │              │
                         │              └──> store.py (transcript, settings)
                         └──> machines.py ──SSH──> install.sh / supervisorctl
agent ──send_to_agent / list_agents──> server.py /api/board/say, /api/peer/*
```

## Rules that hold across the service

- AC-1: Security posture: the board can run shell commands on every registered
  machine. It binds `127.0.0.1` by default and refuses any other bind without
  `AGENT_BOARD_TOKEN`.
- AC-2: An agent is addressed as `<agent>.<machine>` (its *qualified* name).
  Names are unique per machine, not globally. A bare name works when only one
  agent has it.
- AC-3: Agents never call each other directly. Every agent-to-agent message
  goes through the board, so it is in the transcript, the Stop button stops
  it, and a hop limit can apply (ROUTER-RELAY-1).
- AC-4: Agents installed by the board get `AGENT_BOARD_URL`,
  `AGENT_BOARD_TOKEN`, `AGENT_NAME` and `AGENT_QUALIFIED_NAME` in their
  `.env`, which turns on the board tools (TOOLS-REG-3). The same values go
  in under the old names `AGENTS_CONTROL_URL` / `AGENTS_CONTROL_TOKEN` too,
  for agents running an SDK from before the rename; readers take either
  (CORE-BENV-1).
- AC-5: API keys are written only into an agent's `.env` and are never read
  back: the board asks only which variable names are set.
- AC-6: The board never echoes secrets into logs or the UI.

## `agent_board/__init__.py`

**Does:** the package entry: re-exports `Store`, `MachineDriver`,
`MachineError`, `ChannelRouter`, `EventBus`, `ControlPlane`, `create_app`, and
sets `API_REVISION`.

**Rules:**

- AC-API-1: `API_REVISION` is the HTTP contract version. The UI's
  `EXPECTS_API` (`static/app.js`) must equal it; bump both whenever a route or
  field changes. The UI warns when the server runs older code than the page.

**Tests:** `tests/test_agent_board.py` (the revision check).
