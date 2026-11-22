# agent-board UI — design spec

A single page, plain JavaScript, no build step. Served from
`agent_board/static/` by the server (AC-SERVER-11).

Code: `agent_board/static/`.

## `agent_board/static/index.html`

**Does:** the page skeleton: the sidebar (views, channels, agents), one pane
per view (Board, Agents, Machines, Logs, Settings), the modal, and form
templates.

**Rules:**

- AC-UI-HTML-1: Create-agent form: machine, name, framework, provider, model,
  endpoint, key, description, instructions, address (picked from the
  machine's addresses, `0.0.0.0`, or typed), "Extra tools" (optional — core
  tools are built in), SDK source, approval.
- AC-UI-HTML-2: Settings view: the defaults for new agents (framework,
  provider, model, approval, SDK source) and the relay and timing settings
  (AC-STORE-5).

## `agent_board/static/app.js`

**Does:** all UI behaviour: loads `/api/state`, listens to `/api/events`
(SSE), and renders each view.

**Rules — general:**

- AC-UI-1: Commands go through `api()`, which adds the bearer token, asks for
  one on 401, and shows the server's `error` and `detail`. A bare 404 means the
  server runs older code; the page says to restart it.
- AC-UI-2: `EXPECTS_API` must equal the server's `API_REVISION` (AC-API-1);
  on a mismatch a banner says which side is stale.
- AC-UI-3: Text from agents is rendered as DOM nodes (via `md.js`), never as
  HTML, because it comes from a model.

**Rules — Board:**

- AC-UI-BOARD-1: Messages, a typing/working indicator per agent, pending
  approvals with approve/deny, the member list, Stop/Resume and the
  auto-approve level.
- AC-UI-BOARD-2: `@` in the input opens a picker of agents; picking one not in
  the channel adds it.
- AC-UI-BOARD-3: An agent file link (`http(s)://<agent>/files/<path>`) on a
  registered agent's address shows as a 📎 chip with the file name and owner;
  clicking it previews the file through the relay (AC-SERVER-8), with a
  download button.

**Rules — agent colours:**

- AC-UI-COLOR-1: Every current agent's name is shown in its own bright colour
  wherever it appears (messages, mentions, sidebar, members, Agents table,
  picker). No two current agents share a colour.
- AC-UI-COLOR-2: Colours come from a fixed 12-colour palette chosen to be
  readable on the dark background. Agents are taken in id order; each prefers
  slot `id % 12` and takes the next free one when that is taken, so an agent
  usually keeps its colour as others come and go. Past 12, the extra agents
  get lighter colours spread evenly round the wheel.
- AC-UI-COLOR-3: A name that is no current agent's gets a muted colour, so it
  never passes for a live agent. People and system messages keep their own
  colours.

**Rules — Agents:**

- AC-UI-AGENTS-1: One row per agent with Settings, Logs, Clear context,
  Reinstall and Delete. Below the table, model-server cards (AC-METRICS-1),
  polled only while the tab is open.
- AC-UI-AGENTS-2: Reinstall reports when the address was repaired
  (AC-SERVER-7).

**Rules — Logs:**

- AC-UI-LOGS-1: Two views. Control plane: polls `/api/logs?after=` every 2 s
  for new records only, with a level filter (default warnings and errors), a
  text filter, live on/off and Clear view; keeps at most 3000 lines on screen.
  Agents: pick an agent and stream (stderr, stdout, supervisord) and line
  count; refresh by hand or every 5 s.
- AC-UI-LOGS-2: Polling runs only while the Logs tab is open. A reply that
  arrives after the view or filter changed is dropped.
- AC-UI-LOGS-3: Download saves the whole log as a file. It fetches with the
  token in a header (never in a URL), and says when the file was cut to its
  last 50 MB.
- AC-UI-LOGS-4: An agent row's Logs button opens this tab on that agent.

## `agent_board/static/app.css`

**Does:** the styles. Dark theme with colour tokens on `:root`; layout is a
sidebar plus one pane. Agent colours use the `--agent-color` variable set per
element (AC-UI-COLOR-1).

## `agent_board/static/md.js`

**Does:** `AgentMarkdown.render(text, document, opts)`, a minimal Markdown →
DOM renderer for messages: headings, fenced code, lists, quotes, tables,
emphasis, inline code, links and `@mentions`.

**Rules:**

- AC-UI-MD-1: Builds nodes with `createElement`/`textContent` only; never
  `innerHTML`. No HTML passthrough.
- AC-UI-MD-2: `opts.onMention` and `opts.onLink` may return a node to draw a
  mention or link their own way (the colour tint, the file chip).

## `agent_board/static/md.test.cjs`

**Does:** Node tests for `md.js` (`node --test`).
