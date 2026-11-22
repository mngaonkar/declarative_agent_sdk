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
  download button. Text and Markdown show as text, raster images as an
  image; videos stream (AC-UI-BOARD-7).
- AC-UI-BOARD-6: A link to a raster image (png, jpg, gif, webp, bmp, avif;
  never SVG) also shows a thumbnail under its chip. It is fetched through the
  relay once it scrolls into view, with the token in a header, once per page;
  a file over 10 MB, or one that is not an image, shows a note instead.
  Clicking the thumbnail opens the full preview.
- AC-UI-BOARD-7: A link to a video (mp4, m4v, webm, ogv, mov) shows a player
  under its chip once it scrolls into view, with a still from the video
  (`#t=0.1`) under its play button; clicking the chip opens a larger player.
  Both stream the relay with a file ticket (AC-SERVER-13), so only enough to
  draw the still is fetched before play, play starts at once and seeks by
  byte range, and there is no size limit. A ticket is reused for its link
  until it has 10 minutes left. A video that cannot play says why it may be.

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
- AC-UI-AGENTS-3: An MCP column shows each agent's MCP servers (MCP-BOARD-1);
  `—` when it has none or could not be read, with the reason on hover.
- AC-UI-MCP-1: An MCP tab between Machines and Logs: the catalog (MCP-CAT-11),
  an editor with presets (MCP-CAT-4), Set… for each `${NAME}` (a password
  field, cleared after sending), and each attached agent's state from the
  agent poll, which runs while this tab or Agents is open.
- AC-UI-MCP-3: **Browse registry** opens a search over the MCP Registry
  (MCP-CAT-13), with More results for the next page. Each result shows its
  name, description and repository link, and a button per usable option;
  choosing one opens Add server filled in, with a note naming the source,
  the variables and that the entry is not reviewed.
- AC-UI-MCP-2: An agent's Settings has an MCP tab (MCP-CAT-12). Its changes
  apply at once and say the agent restarts; warnings from attaching
  (MCP-CAT-8) are shown.
- AC-UI-MACH-1: The Machines tab shows, per machine, whether `npx` and `uvx`
  were found by the last probe (MCP-BOARD-4).

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
