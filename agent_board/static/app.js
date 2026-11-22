/* agent-board board. Vanilla JS: fetch for commands, SSE for everything live. */

const state = {
  machines: [], agents: [], channels: [], settings: {}, defaults: {},
  channelId: null, messages: [], members: [], halted: false, autoApprove: '',
  typing: new Set(), approvals: new Map(), lastId: 0, needsAuth: false,
  contexts: {}, mcp: {}, mcpCatalog: [], mcpPresets: [], providers: { endpoints: {}, listable: [] },
  // Agent (qualified name) → channel ids it is taking a turn in, across all
  // channels — the sidebar shows activity everywhere, not just the open one.
  working: new Map(),
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};
/*
 * The server revision this board needs. Kept in step with API_REVISION in
 * agent_board/__init__.py.
 *
 * These can drift: this file is read from disk on every request, so a browser
 * always gets the current board, while the Python routes were loaded when the
 * process started. Edit the tree without restarting and the new board asks the
 * old server for routes it does not have — which it answers with a bare 404.
 */
const EXPECTS_API = 32;

/** An agent's address on the board. The bare name is unique only per machine. */
const fqn = (agent) => agent.qualified || `${agent.name}.${agent.machine_name}`;

const token = {
  get: () => localStorage.getItem('ac-token') || '',
  set: (value) => localStorage.setItem('ac-token', value),
};

/* ── transport ──────────────────────────────────────────────────────── */

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const current = token.get();
  if (current) headers.Authorization = `Bearer ${current}`;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    state.needsAuth = true;
    askForToken();
    throw new Error('unauthorized');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const described = [payload.error, payload.detail].filter(Boolean).join('\n');
    if (described) throw new Error(described);
    // No error body on a 404 means the router never reached a handler: this
    // process does not have that route. Almost always a server left running
    // from before the feature existed.
    if (response.status === 404) {
      checkServerVersion();
      throw new Error(
        `This control plane has no ${options.method || 'GET'} ${path} — it is ` +
        'running older code than this page. Restart the service ' +
        '(declarative-agent board) and reload.');
    }
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return payload;
}

/**
 * Compare the running process against the board it served.
 *
 * /healthz is public, so this works before a token is entered — which matters,
 * because an old server may 401 or 404 everything else.
 */
async function checkServerVersion() {
  let health;
  try {
    health = await (await fetch('/healthz')).json();
  } catch {
    return;                               // offline; the SSE dot already says so
  }
  const revision = Number(health.api_revision || 0);
  $('#side-foot').title =
    `agent-board ${health.version || '?'} · api ${revision} · up since ${health.started || '?'}`;

  if (revision > EXPECTS_API) {
    // The other way round: the server moved on while this tab kept the
    // app.js it loaded earlier. Opening a dialog never re-reads the script,
    // so new fields stay invisible until the page is reloaded.
    showStale(
      `The board was updated (server api ${revision}, this page api ${EXPECTS_API}). ` +
      'Reload the page to get the new version.');
    return;
  }
  if (revision === EXPECTS_API) {
    $('#stale-banner').classList.add('hidden');
    return;
  }
  showStale(
    `This page expects server api ${EXPECTS_API}, but the running process is ` +
    `${revision ? `api ${revision}` : 'older than api 1'}` +
    `${health.started ? ` (started ${new Date(health.started).toLocaleString()})` : ''}. ` +
    'Some screens will fail with 404 until you restart it: ' +
    'declarative-agent board');
}

function showStale(text) {
  const banner = $('#stale-banner');
  $('#stale-text').textContent = text;
  banner.classList.remove('hidden');
}

function toast(message, bad = false) {
  $('#toast')?.remove();
  const node = el('div', bad ? 'bad' : '', message);
  node.id = 'toast';
  document.body.appendChild(node);
  setTimeout(() => node.remove(), bad ? 9000 : 4000);
}

/* ── bootstrap ──────────────────────────────────────────────────────── */

async function load() {
  const data = await api('/api/state');
  state.machines = data.machines;
  state.agents = data.agents;
  state.channels = data.channels;
  state.settings = data.settings;
  state.defaults = data.defaults || {};
  state.providers = data.providers || { endpoints: {}, listable: [] };
  setWorking(data.working);
  state.needsAuth = data.auth;
  renderSidebar();
  renderAgents();
  renderMachines();
  renderSettings();
  if (state.channelId == null && state.channels.length) selectChannel(state.channels[0].id);
  else if (state.channelId != null) renderBoardHeader();
}

function connect() {
  const query = token.get() ? `?token=${encodeURIComponent(token.get())}` : '';
  const stream = new EventSource(`/api/events${query}`);
  stream.addEventListener('hello', () => {
    $('#conn-dot').classList.add('live');
    // A (re)connect may follow a server restart, which ended every turn
    // without a typing-off; ask who is really working rather than keep a
    // stuck spinner (or wipe what load() just learned).
    syncWorking();
    // Every (re)connect, not just page load: a hello after a drop usually
    // means the server restarted, possibly onto newer code than this tab.
    checkServerVersion();
  });
  stream.addEventListener('ping', () => {});
  stream.addEventListener('update', (event) => handleEvent(JSON.parse(event.data)));
  stream.onerror = () => {
    $('#conn-dot').classList.remove('live');
    stream.close();
    setTimeout(connect, 3000);           // the server may be restarting
  };
}

function handleEvent(event) {
  switch (event.type) {
    case 'mcp':
      if (!$('#view-mcp').classList.contains('hidden')) loadMcp();
      break;
    case 'cleared':
      // Emptied here rather than refetched: the board's "Conversation cleared"
      // line follows as an ordinary message event and lands in the empty view.
      if (event.channel_id === state.channelId) {
        state.messages = [];
        state.lastId = 0;
        state.typing.clear();
        renderMessages();
      }
      break;
    case 'message':
      if (event.channel_id === state.channelId) {
        state.messages.push(event.message);
        state.lastId = Math.max(state.lastId, event.message.id);
        appendMessage(event.message);
      }
      break;
    case 'status':
      if (event.channel_id === state.channelId) {
        $('#typing').textContent = `@${event.agent}: ${event.text}`;
      }
      break;
    case 'typing': {
      if (event.channel_id === state.channelId) {
        event.on ? state.typing.add(event.agent) : state.typing.delete(event.agent);
        renderTyping();
      }
      const busy = state.working.get(event.agent) || new Set();
      event.on ? busy.add(event.channel_id) : busy.delete(event.channel_id);
      if (busy.size) state.working.set(event.agent, busy);
      else state.working.delete(event.agent);
      renderSidebarAgents();
      break;
    }
    case 'approval':
      state.approvals.set(event.approval.id, event.approval);
      if (event.channel_id === state.channelId) appendApproval(event.approval);
      break;
    case 'approval_resolved':
      state.approvals.delete(event.id);
      document.getElementById(`approval-${event.id}`)?.remove();
      break;
    case 'halt':
      if (event.channel_id === state.channelId) {
        state.halted = event.halted;
        renderBoardHeader();
      }
      load();
      break;
    default:
      load();                            // machines / agents / channels / settings
  }
}

/* ── sidebar ────────────────────────────────────────────────────────── */

/*
 * Whether an agent is usable right now. `ready` is install.sh's verdict on
 * the config at install time and is never refreshed, so on its own it shows
 * "ready" for an agent that has since stopped or whose machine is gone. The
 * Agents-tab poll asks every agent for /context; a failed connection there
 * overrides the stored flag. Before the first poll, the stored flag is all
 * there is.
 */
function liveness(agent) {
  const report = state.contexts[String(agent.id)];
  if (report && report.reachable === false) {
    return { ok: false, label: 'unreachable', note: report.error || '' };
  }
  if (!agent.ready) return { ok: false, label: 'not ready', note: agent.problems || '' };
  if (report && report.reachable) return { ok: true, label: 'ready', note: '' };
  return { ok: true, label: 'ready', note: '', unchecked: true };
}

const pinClass = (agent) => `pin ${liveness(agent).ok ? 'ready' : 'down'}`;

/*
 * The Agents list on its own, so a typing event redraws a handful of rows
 * instead of the whole sidebar. Working comes from the board's typing events;
 * reachable/down from liveness(), as everywhere else.
 */
function renderSidebarAgents() {
  const agents = $('#sidebar-agents');
  if (!agents) return;
  agents.innerHTML = '';
  state.agents.forEach((agent) => {
    const name = fqn(agent);
    const busy = state.working.get(name);
    const live = liveness(agent);
    const item = el('li', busy ? 'working' : '');
    const icon = el('span', `activity ${busy ? 'working' : 'idle'}${live.ok ? '' : ' down'}`);
    icon.setAttribute('role', 'img');
    icon.setAttribute('aria-label', busy ? 'working' : (live.ok ? 'idle' : live.label));
    item.append(icon, tint(el('span', 'agent-name', name), agent));
    const where = busy
      ? `working in ${[...busy].map((id) => `#${state.channels.find((c) => c.id === id)?.name || id}`).join(', ')}`
      : (live.ok ? 'idle' : live.label);
    item.title = `${where} · ${agent.model || agent.framework} · ${agent.url || 'no url'}`;
    agents.append(item);
  });
  if (!state.agents.length) agents.append(el('li', '', 'no agents yet'));
}

function setWorking(working) {
  state.working = new Map(Object.entries(working || {})
    .map(([agent, channels]) => [agent, new Set(channels)]));
}

async function syncWorking() {
  try {
    setWorking((await api('/api/state')).working);
    renderSidebarAgents();
  } catch { /* offline: the connection dot already says so */ }
}

function renderSidebar() {
  const channels = $('#channel-list');
  channels.innerHTML = '';
  state.channels.forEach((channel) => {
    const item = el('li', channel.id === state.channelId ? 'active' : '');
    if (channel.halted) item.classList.add('halted');
    item.append(el('span', '', `#${channel.name}`));
    const count = el('span', 'tag', String((channel.members || []).length));
    item.append(count);
    item.onclick = () => selectChannel(channel.id);
    channels.append(item);
  });
  if (!state.channels.length) {
    channels.append(el('li', '', 'no channels yet'));
  }

  renderSidebarAgents();

  $('#side-foot').textContent =
    `${state.machines.length} machine(s) · ${state.agents.length} agent(s)`;
}

/* ── board ──────────────────────────────────────────────────────────── */

async function selectChannel(id) {
  state.channelId = id;
  state.typing.clear();
  state.lastId = 0;
  const data = await api(`/api/channels/${id}/messages`);
  state.messages = data.messages;
  state.members = data.members;
  state.halted = data.halted;
  state.autoApprove = data.auto_approve || '';
  state.lastId = state.messages.length ? state.messages[state.messages.length - 1].id : 0;
  renderSidebar();
  renderBoardHeader();
  renderMessages();
  renderMembers();
}

function renderBoardHeader() {
  const channel = state.channels.find((c) => c.id === state.channelId);
  $('#channel-title').textContent = channel ? `#${channel.name}` : 'no channel';
  $('#channel-topic').textContent = channel?.topic || '';
  $('#composer').hidden = !channel;
  $('#halt-btn').hidden = !channel;
  $('#delete-channel').hidden = !channel;
  $('#clear-channel').hidden = !channel;
  $('#input').placeholder = channel
    ? `Message #${channel.name} — use @agent to address one`
    : '';
  const halt = $('#halt-btn');
  halt.textContent = state.halted ? 'Resume' : 'Stop';
  halt.className = state.halted ? 'btn' : 'btn danger';

  // Auto-approval is a standing decision, so it is shown next to Stop rather
  // than buried in Settings — and coloured when it is on, because "nobody is
  // being asked any more" should be visible from across the room.
  $('#auto-approve-wrap').hidden = !channel;
  $('#auto-approve').value = state.autoApprove;
  $('#auto-approve-wrap').classList.toggle('armed', Boolean(state.autoApprove));
  renderMentionHint();
}

function renderMentionHint() {
  const names = state.members.map((m) => `@${fqn(m)}`).join('  ');
  $('#mention-hint').textContent = state.halted
    ? 'Channel halted — dispatches are refused until you resume.'
    : (names ? `in channel: ${names}` : 'no agents in this channel yet — add one on the right');
}

function renderMessages() {
  const list = $('#messages');
  list.innerHTML = '';
  if (!state.channelId) {
    list.append(buildEmpty());
    return;
  }
  if (!state.messages.length) {
    const empty = el('div', 'empty');
    empty.append(el('h2', '', 'Nothing here yet'));
    empty.append(el('p', '', 'Address an agent with @name to start.'));
    list.append(empty);
  }
  state.messages.forEach((message) => appendMessage(message, false));
  state.approvals.forEach((approval) => {
    if (approval.channel_id === state.channelId) appendApproval(approval, false);
  });
  scrollDown();
}

function buildEmpty() {
  const node = el('div', 'empty');
  node.append(el('h2', '', 'No channel selected'));
  node.append(el('p', '', 'Create a channel, add agents, then address them with @name.'));
  return node;
}

/*
 * A bright colour of its own for every agent, the same everywhere it appears
 * (board, sidebar, members, mentions, tables) — and never shared by two
 * current agents.
 *
 * Colours come from a fixed palette of hues picked to stay apart on this dark
 * background, not computed from the id: hashing ids onto a colour wheel lands
 * unrelated ids side by side (ids 25 and 38 were 12° apart — both cyan).
 *
 * Allocation runs over the current agents in id order. Each prefers slot
 * id % palette size and, when that is taken, takes the next free one, so an
 * agent usually keeps its colour as others come and go. The palette is ordered
 * so neighbouring slots are far apart in hue, since probing fills neighbours.
 * Past the palette, the remaining agents are spread evenly over the wheel at a
 * lighter shade, which keeps them distinct from the palette and each other.
 *
 * A name that is no current agent's (a deleted agent in old history, a
 * hostname) gets a muted colour, so it never passes for a live agent.
 */
const AGENT_PALETTE = [
  '#ff6b6b', // red
  '#3bc9db', // cyan
  '#ffd43b', // yellow
  '#b197fc', // violet
  '#69db7c', // green
  '#f783ac', // pink
  '#4dabf7', // blue
  '#ffa94d', // orange
  '#38d9a9', // teal
  '#e599f7', // orchid
  '#c0eb75', // lime
  '#91a7ff', // periwinkle
];
let agentPaletteKey = '';
let agentPaletteMap = new Map();

function agentFor(name) {
  if (!name) return null;
  const bare = String(name).replace(/^@/, '');
  const exact = state.agents.find((a) => fqn(a) === bare);
  if (exact) return exact;
  if (bare.includes('.')) return null;
  const matches = state.agents.filter((a) => a.name === bare);
  return matches.length === 1 ? matches[0] : null;
}

/** id -> colour for every current agent; recomputed when the set changes. */
function agentPalette() {
  const ids = state.agents.map((a) => Number(a.id)).sort((x, y) => x - y);
  const key = ids.join(',');
  if (key === agentPaletteKey) return agentPaletteMap;
  const size = AGENT_PALETTE.length;
  const taken = new Array(size).fill(false);
  const map = new Map();
  const overflow = [];
  ids.forEach((id) => {
    const want = ((id % size) + size) % size;
    for (let step = 0; step < size; step += 1) {
      const slot = (want + step) % size;
      if (!taken[slot]) {
        taken[slot] = true;
        map.set(id, AGENT_PALETTE[slot]);
        return;
      }
    }
    overflow.push(id);
  });
  overflow.forEach((id, i) => {
    // Offset by half a step so these sit between palette hues, not on them.
    const hue = ((i + 0.5) * 360) / overflow.length;
    map.set(id, `hsl(${hue.toFixed(1)} 90% 80%)`);
  });
  agentPaletteKey = key;
  agentPaletteMap = map;
  return map;
}

function agentColor(nameOrAgent) {
  const agent = typeof nameOrAgent === 'object' && nameOrAgent
    ? nameOrAgent : agentFor(nameOrAgent);
  const color = agent && agentPalette().get(Number(agent.id));
  if (color) return color;
  let hash = 0;
  for (const ch of String(nameOrAgent)) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return `hsl(${hash % 360} 25% 68%)`;
}

/** Colour a node for an agent: sets --agent-color, which the CSS uses. */
function tint(node, nameOrAgent) {
  node.style.setProperty('--agent-color', agentColor(nameOrAgent));
  node.classList.add('tinted');
  return node;
}

/**
 * How a sender is shown. Messages written before an agent had a machine — or
 * by an agent whose .env predates AGENT_QUALIFIED_NAME — carry a bare name, so
 * resolve it here rather than leaving the board inconsistent.
 */
function displaySender(name) {
  if (!name || name.includes('.')) return name;
  const matches = state.agents.filter((a) => a.name === name);
  return matches.length === 1 ? fqn(matches[0]) : name;
}

function appendMessage(message, scroll = true) {
  const list = $('#messages');
  list.querySelector('.empty')?.remove();
  const row = el('div', `msg ${message.sender_kind}`);
  const who = el('span', 'who',
    message.sender_kind === 'system' ? '*' : displaySender(message.sender));
  who.title = message.sender;
  if (message.sender_kind === 'agent') tint(who, displaySender(message.sender));
  row.append(who);
  const body = el('span', 'text');
  if (message.target) {
    const to = el('span', 'to', '→ ');
    to.append(tint(el('span', 'to-name', `@${displaySender(message.target)}`),
      displaySender(message.target)), document.createTextNode(' '));
    body.append(to);
  }
  body.append(renderBody(message.text));
  row.append(body);
  row.append(el('time', '', fmtTime(message.created)));
  list.append(row);
  if (scroll) scrollDown();
}

/**
 * Agents answer in Markdown whether or not you ask them to, so render it.
 * AgentMarkdown builds nodes rather than HTML strings — the text came from a
 * model, which is to say from whatever that model just read.
 */
// An agent's file link: http(s)://<agent>/files/<path in its workspace>.
const SHARED_LINK = /^https?:\/\/[^/\s?#]+(?:\/[^\s?#]*)?\/files\/([^\s?#]+)$/;

/** An agent file link as a chip that previews through the board's relay. */
function sharedFileChip(href) {
  const match = SHARED_LINK.exec(href);
  if (!match) return null;
  // Only links on a registered agent's address: the relay refuses others,
  // and an unrelated site's /files/ URL should stay an ordinary link.
  let host = '';
  try { host = new URL(href).host; } catch { return null; }
  const owner = state.agents.find((a) => {
    try { return new URL(a.url).host === host; } catch { return false; }
  });
  if (!owner) return null;
  let name = match[1];
  try { name = decodeURIComponent(name); } catch { /* keep it encoded */ }
  const chip = el('button', 'file-chip');
  chip.type = 'button';
  chip.append(el('span', 'icon', '📎'), el('span', 'label', name.split('/').pop()));
  chip.append(el('span', 'meta', `@${fqn(owner)}`));
  chip.title = href;
  chip.onclick = () => previewShared(href);
  const wrap = el('span', 'file-link');
  const box = el('span', 'file-thumb');
  if (IMAGE_PATH.test(name)) {
    // An image gets a thumbnail under the chip, fetched once it scrolls into view.
    box.append(el('span', 'note', 'loading preview…'));
    whenVisible(box, () => showThumb(box, href));
  } else if (VIDEO_PATH.test(name)) {
    // A still from the video, with the player's own play button over it,
    // once it scrolls into view. Only enough to draw that frame is fetched
    // (byte ranges) until play is pressed.
    box.append(el('span', 'note', 'loading preview…'));
    whenVisible(box, () => showVideo(box, href));
  } else {
    return chip;
  }
  wrap.append(chip, box);
  return wrap;
}

// Raster images only: an SVG is a document, and nothing here needs one.
const IMAGE_PATH = /\.(png|jpe?g|gif|webp|bmp|avif)$/i;
const VIDEO_PATH = /\.(mp4|m4v|webm|ogv|mov)$/i;
const IMAGE_MAX_BYTES = 10 * 1024 * 1024;
// href → Promise<{url, size} | {skip: reason}>. Messages are re-rendered when
// the board reloads a channel; a file is fetched once per page.
const media = new Map();

function relayFetch(href, signal) {
  const headers = token.get() ? { Authorization: `Bearer ${token.get()}` } : {};
  return fetch(`/api/files/relay?url=${encodeURIComponent(href)}`, { headers, signal });
}

const isImage = (kind) => /^image\//i.test(kind) && !/svg/i.test(kind);

/**
 * Fetch a file through the relay as a blob URL, when it is the expected kind
 * (`accept(contentType)`) and no larger than `maxBytes`.
 */
function loadMedia(href, accept, maxBytes) {
  if (media.has(href)) return media.get(href);
  const job = (async () => {
    const stop = new AbortController();
    const response = await relayFetch(href, stop.signal);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      media.delete(href);              // the agent may be back later
      return { skip: body.error || `${response.status} ${response.statusText}` };
    }
    const kind = (response.headers.get('content-type') || '').split(';')[0];
    const length = Number(response.headers.get('content-length') || 0);
    if (!accept(kind)) {
      stop.abort();
      return { skip: `cannot show ${kind || 'an unknown type'} here` };
    }
    if (length > maxBytes) {
      stop.abort();
      return { skip: `${compact(length)} bytes — use the chip to download it` };
    }
    const blob = await response.blob();
    if (blob.size > maxBytes) return { skip: `${compact(blob.size)} bytes — use the chip to download it` };
    return { url: URL.createObjectURL(blob), size: blob.size };
  })().catch((error) => {
    media.delete(href);
    return { skip: `could not load: ${error.message}` };
  });
  media.set(href, job);
  return job;
}

async function showThumb(box, href) {
  const result = await loadMedia(href, isImage, IMAGE_MAX_BYTES);
  box.replaceChildren();
  if (result.skip) {
    box.append(el('span', 'note', `no preview: ${result.skip}`));
    return;
  }
  const img = el('img');
  img.src = result.url;
  img.alt = decodeURIComponent(href.split('/files/')[1] || 'image');
  img.loading = 'lazy';
  img.title = `${compact(result.size)} bytes — click to enlarge`;
  img.onclick = () => previewShared(href);
  box.append(img);
}

/*
 * A <video> fetches its own URL and cannot send the Authorization header, so
 * the board hands out a ticket for this one link and the player streams the
 * relay with it — byte ranges, so it starts at once and can seek. The ticket
 * opens this file for an hour and nothing else; the board token never goes
 * in a URL.
 */
// href → {src, expires}: a message is redrawn whenever its channel reloads,
// and one ticket serves every redraw until it nears its end.
const tickets = new Map();

async function streamSrc(href) {
  const held = tickets.get(href);
  if (held && held.expires - Date.now() / 1000 > 600) return held.src;
  const answer = await api('/api/files/ticket', {
    method: 'POST', body: JSON.stringify({ url: href }),
  });
  tickets.set(href, { src: answer.src, expires: answer.expires });
  return answer.src;
}

async function showVideo(box, href) {
  let src;
  try {
    src = await streamSrc(href);
  } catch (error) {
    box.replaceChildren(el('span', 'note', `no preview: ${error.message}`));
    return;
  }
  box.replaceChildren(videoPlayer(src));
}

function videoPlayer(url) {
  const video = el('video', 'file-video');
  // #t=0.1: show a frame from just after the start as the preview. Without
  // it Safari draws nothing until play, and the first frame is often black.
  video.src = `${url}#t=0.1`;
  video.controls = true;
  video.playsInline = true;
  video.preload = 'metadata';
  // A .mov in Chrome, say, or a ticket that ran out on a page left open.
  video.onerror = () => video.replaceWith(el('span', 'note',
    'could not play this video: this browser may not support its format ' +
    '(download it from the chip), or the page was open too long (reload it)'));
  return video;
}

async function previewVideo(href, path) {
  let src;
  try {
    src = await streamSrc(href);
  } catch (error) {
    toast(`Video: ${error.message}`, true);
    return;
  }
  const name = path.split('/').pop();
  const wrap = el('div', 'file-preview');
  const head = el('div', 'file-head');
  head.append(el('span', 'note', `${name} · streamed through the board`));
  const save = el('a', 'btn small', 'Download');
  save.href = src;                     // the ticket covers a download too
  save.download = name;
  head.append(save);
  wrap.append(head, videoPlayer(src));
  openModal(`📎 ${name}`, wrap);
}

function whenVisible(node, run) {
  if (!('IntersectionObserver' in window)) { run(); return; }
  const watch = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) {
      watch.disconnect();
      run();
    }
  }, { rootMargin: '200px' });
  watch.observe(node);
}

/*
 * Fetch a shared file through /api/files/relay and show it. The token goes in
 * a header, never in a URL: a link with the board token in it would be a key
 * to the whole board for whoever it is passed to.
 */
async function previewShared(href) {
  let path = href.split('/files/')[1] || '';
  try { path = decodeURIComponent(path); } catch { /* keep it encoded */ }
  if (VIDEO_PATH.test(path)) {
    // Streamed, not downloaded first: a video can be gigabytes.
    previewVideo(href, path);
    return;
  }
  const headers = token.get() ? { Authorization: `Bearer ${token.get()}` } : {};
  let response;
  try {
    response = await fetch(`/api/files/relay?url=${encodeURIComponent(href)}`, { headers });
  } catch (error) {
    toast(`Could not fetch the shared file: ${error.message}`, true);
    return;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    toast(`Shared file: ${body.error || `${response.status} ${response.statusText}`}`, true);
    return;
  }
  const kind = response.headers.get('content-type') || 'application/octet-stream';
  const by = response.headers.get('x-shared-by') || 'an agent';
  const disposition = response.headers.get('content-disposition') || '';
  const name = (/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition) || [])[1]
    || href.split('/files/')[1].split('?')[0];
  const blob = await response.blob();

  const wrap = el('div', 'file-preview');
  const head = el('div', 'file-head');
  head.append(el('span', 'note', `${decodeURIComponent(name)} · ${kind.split(';')[0]} · ` +
                 `${compact(blob.size)} bytes · shared by @${by}`));
  const save = el('a', 'btn small', 'Download');
  save.href = URL.createObjectURL(blob);
  save.download = decodeURIComponent(name);
  head.append(save);
  wrap.append(head);

  const textual = /^(text\/|application\/(json|xml|yaml|x-yaml|javascript|toml))/i.test(kind);
  const image = isImage(kind);
  if (image) {
    const img = el('img', 'file-image');
    img.src = save.href;
    img.alt = decodeURIComponent(name);
    wrap.append(img);
  } else if (!textual) {
    wrap.append(el('p', 'note', 'Not a text file — download it to open.'));
  } else if (blob.size > 2 * 1024 * 1024) {
    wrap.append(el('p', 'note', 'Too large to preview here — download it instead.'));
  } else {
    const text = await blob.text();
    const markdown = /markdown/i.test(kind) || /\.md$/i.test(name);
    if (markdown && window.AgentMarkdown) {
      const body = el('div', 'msg-like');
      body.append(window.AgentMarkdown.render(text, document));
      wrap.append(body);
    } else {
      const pre = el('pre', 'code');
      pre.textContent = text;
      wrap.append(pre);
    }
  }
  openModal(`📎 ${decodeURIComponent(name)}`, wrap);
}

function renderBody(text) {
  if (window.AgentMarkdown) {
    return window.AgentMarkdown.render(text, document, {
      onMention: (token, doc) => {
        const span = doc.createElement('span');
        span.className = 'mention';
        span.textContent = '@' + displaySender(token.name);
        if (agentFor(token.name)) tint(span, agentFor(token.name));
        return span;
      },
      onLink: (link) => sharedFileChip(link.href),
    });
  }
  return document.createTextNode(text);     // md.js failed to load
}

function appendApproval(approval, scroll = true) {
  if (document.getElementById(`approval-${approval.id}`)) return;
  const box = el('div', 'approval');
  box.id = `approval-${approval.id}`;
  const line = el('div');
  line.append(el('strong', '', `@${approval.agent}`));
  line.append(document.createTextNode(' wants to run '));
  line.append(el('span', 'tool', `${approval.tool}(${fmtArgs(approval.args)})`));
  box.append(line);
  const row = el('div', 'row');
  const approve = el('button', 'btn primary small', 'Approve');
  const deny = el('button', 'btn small', 'Deny');
  approve.onclick = () => resolveApproval(approval.id, true, box);
  deny.onclick = () => resolveApproval(approval.id, false, box);
  row.append(approve, deny);
  box.append(row);
  $('#messages').append(box);
  if (scroll) scrollDown();
}

async function resolveApproval(id, approved, box) {
  box.querySelectorAll('button').forEach((b) => (b.disabled = true));
  try {
    await api(`/api/approvals/${id}`, {
      method: 'POST', body: JSON.stringify({ approved }),
    });
  } catch (error) {
    toast(error.message, true);
    box.querySelectorAll('button').forEach((b) => (b.disabled = false));
  }
}

function renderTyping() {
  const names = [...state.typing];
  $('#typing').textContent = names.length
    ? `${names.map((n) => '@' + n).join(', ')} ${names.length > 1 ? 'are' : 'is'} working…`
    : '';
}

function renderMembers() {
  const list = $('#member-list');
  list.innerHTML = '';
  state.members.forEach((member) => {
    const item = el('li');
    const left = el('span');
    left.append(el('span', pinClass(member)));
    left.append(document.createTextNode(' '),
      tint(el('span', 'agent-name', fqn(member)), member));
    left.title = `${member.machine_name} · ${member.url || 'no url'}`;
    item.append(left);
    const remove = el('span', 'x', '×');
    remove.title = 'Remove from channel';
    remove.onclick = async () => {
      await api(`/api/channels/${state.channelId}/members/${member.id}`, { method: 'DELETE' });
      selectChannel(state.channelId);
    };
    item.append(remove);
    list.append(item);
  });

  const select = $('#member-select');
  select.innerHTML = '';
  const inChannel = new Set(state.members.map((m) => m.id));
  const available = state.agents.filter((a) => !inChannel.has(a.id));
  if (!available.length) {
    select.append(new Option('no agents to add', ''));
  } else {
    available.forEach((agent) => select.append(new Option(fqn(agent), agent.id)));
  }
  $('#member-add-btn').disabled = !available.length || !state.channelId;
}

function scrollDown() {
  const list = $('#messages');
  list.scrollTop = list.scrollHeight;
}

/* ── agents view ────────────────────────────────────────────────────── */

function renderAgents() {
  const table = $('#agents-table');
  table.innerHTML = '';
  table.append(headRow(
    ['Agent (name.machine)', 'Machine', 'Runtime', 'Context', 'MCP', 'URL', 'State', '']));
  if (!state.agents.length) {
    table.append(emptyRow(8, 'No agents. Register a machine, then create one.'));
    return;
  }
  state.agents.forEach((agent) => {
    const row = el('tr');
    row.append(tint(cell(fqn(agent), 'mono agent-name'), agent), cell(agent.machine_name));
    row.append(cell(`${agent.framework} · ${agent.provider} · ${agent.model || '—'}`));
    row.append(contextCell(state.contexts[String(agent.id)]));
    row.append(mcpCell(state.mcp[String(agent.id)]));
    const url = el('td', 'mono');
    if (agent.url) {
      const link = el('a', '', agent.url);
      link.href = agent.agent_card || agent.url;
      link.target = '_blank';
      url.append(link);
    } else url.textContent = '—';
    row.append(url);

    const status = el('td');
    const live = liveness(agent);
    const tag = el('span', `tag ${live.ok ? 'ok' : 'bad'}`, live.label);
    if (live.unchecked) tag.title = 'Install-time check only; not contacted yet.';
    status.append(tag);
    if (live.note) status.append(el('div', 'note', live.note));
    row.append(status);

    const actions = el('td');
    const settings = el('button', 'btn small', '⚙ Settings');
    settings.title = 'Edit agent.yaml on the machine';
    settings.onclick = () => openAgentConfig(agent);
    const forget = el('button', 'btn small', 'Clear context');
    forget.title = 'Start this agent on a blank history in every channel';
    forget.onclick = async () => {
      if (!confirm(`Clear ${fqn(agent)}'s context?\n\nIt forgets every channel's ` +
                   'conversation. Channel transcripts stay, and are replayed into ' +
                   'its next prompt as usual — use Clear conversation for those.')) return;
      forget.disabled = true;
      try {
        const result = await api(`/api/agents/${agent.id}/clear-context`, { method: 'POST' });
        toast(`${fqn(agent)}: context cleared` +
              (result.history !== 'freed' ? ` — old history ${result.history}` : ''));
      } catch (error) { toast(error.message, true); }
      finally { forget.disabled = false; }
    };
    const reinstall = el('button', 'btn small', 'Reinstall');
    reinstall.title = 'Re-run install.sh: new SDK code, same config, key, workspace and address';
    reinstall.onclick = () => openReinstall(agent);
    const logs = el('button', 'btn small', 'Logs');
    logs.title = "Tail this agent's log on its machine";
    logs.onclick = () => openAgentLogs(agent.id);
    const remove = el('button', 'btn danger small', 'Delete');
    remove.onclick = () => deleteAgent(agent);
    actions.append(settings, logs, forget, reinstall, remove);
    row.append(actions);
    table.append(row);
  });
}

async function deleteAgent(agent) {
  const purge = confirm(
    `Delete agent '${fqn(agent)}'?\n\n` +
    `OK = also purge its directory (.env, workspace/).\n` +
    `Cancel = stop and unregister only, keeping its files.`
  );
  try {
    await api(`/api/agents/${agent.id}?purge=${purge ? 1 : 0}`, { method: 'DELETE' });
    toast(`Deleted ${agent.name}${purge ? ' (purged)' : ''}`);
    load();
  } catch (error) { toast(error.message, true); }
}

/* ── model servers ──────────────────────────────────────────────────────
 * The numbers that explain a slow turn, read from the model server rather
 * than guessed at from the board: how big a prompt each request carries, how
 * fast tokens come out, and whether two agents are queueing for one slot.
 * Polled only while the Agents tab is open and the page is visible — this
 * costs three HTTP calls per endpoint per tick, and nobody needs them while
 * they are reading the board.
 */

const compact = (value) => {
  const n = Number(value);
  if (value === null || value === undefined || !Number.isFinite(n)) return '—';
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n));
};

/**
 * What this agent prefixes to every model request, as the agent reports it.
 *
 * Not the same number as a server card's prompt tokens: this is the history
 * the agent is carrying right now, which is knowable between requests, while
 * the card shows what one server was last handed.
 */
/**
 * An agent's MCP servers: one line each, connected with its tool count, or
 * not with why. From the agent's /mcp route, read with the metrics poll.
 */
function mcpCell(report) {
  const td = el('td');
  const servers = report?.servers || [];
  if (!servers.length) {
    td.append(el('span', 'dim', '—'));
    td.title = report?.error || "No MCP servers in this agent's agent.yaml.";
    return td;
  }
  servers.forEach((server) => {
    const line = el('div', 'mcp-server');
    const ok = Boolean(server.connected);
    line.append(el('span', `tag ${ok ? 'ok' : 'bad'}`, server.name));
    const tools = server.tools || [];
    line.append(el('span', 'note', ok ? ` ${tools.length} tool${tools.length === 1 ? '' : 's'}`
                                      : ' not connected'));
    line.title = [server.where, ok ? tools.join(', ') : server.error,
                  `approval: ${server.approval}`].filter(Boolean).join('\n');
    td.append(line);
  });
  return td;
}

function contextCell(report) {
  const td = el('td');
  if (!report || report.supported === false) {
    td.append(el('span', 'dim', '—'));
    if (report?.error) td.title = report.error;
    return td;
  }
  const top = report.top;
  if (!top) {
    td.append(el('span', 'dim', 'no history yet'));
    td.title = 'This agent has not been addressed since it started.';
    return td;
  }
  const hot = report.history_limit && top.messages >= report.history_limit;
  const value = el('span', `mono${hot ? ' warn-text' : ''}`,
                   `~${compact(top.prompt_tokens)} tok`);
  td.append(value);
  const note = [`${top.messages} msgs`];
  if (report.history_limit) note.push(`limit ${report.history_limit}`);
  if (report.session_count > 1) note.push(`${report.session_count} sessions`);
  if (top.elided_messages) note.push(`${top.elided_messages} trimmed`);
  td.append(el('div', 'note', note.join(' · ')));
  td.title = `~${top.prompt_tokens} tokens estimated from ${top.prompt_chars} `
    + `characters: ${report.system_prompt_chars} of system prompt plus `
    + `${top.chars} of history. Sent on every request in this session.`;
  return td;
}

/** "12s ago", "4m ago", "2h ago" — coarse on purpose; it labels a held figure. */
function ago(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function stat(key, value, note, cls) {
  const box = el('div', `stat${cls ? ` ${cls}` : ''}`);
  box.append(el('span', 'k', key));
  box.append(el('span', `v${value.hot ? ' hot' : ''}`, value.text ?? String(value)));
  if (note) box.append(el('span', 'n', note));
  return box;
}

function bar(fraction, hot) {
  const outer = el('div', 'bar');
  const fill = el('i', hot ? 'hot' : '');
  fill.style.width = `${Math.min(100, Math.max(0, fraction * 100)).toFixed(1)}%`;
  outer.append(fill);
  return outer;
}

function serverCard(server) {
  const card = el('div', 'server-card');
  const head = el('div', 'card-head');
  head.append(el('strong', '', server.model || 'model unknown'));
  if (server.url) head.append(el('span', 'mono', server.url));
  if (server.shared) {
    const tag = el('span', 'tag warn', 'shared');
    tag.title = 'More than one agent uses this server. If it has one slot, '
      + 'their turns queue behind each other.';
    head.append(tag);
  }
  if (!server.reachable) head.append(el('span', 'tag bad', 'unreachable'));
  card.append(head);

  const served = el('div', 'served');
  (server.agents || []).forEach((agent) => {
    served.append(el('span', `tag ${liveness(agent).ok ? 'ok' : 'bad'}`, agent.qualified));
  });
  card.append(served);

  if (server.error) {
    card.append(el('p', 'note', server.error));
    return card;
  }

  const stats = el('div', 'stats');
  const ctx = server.context;
  if (ctx) {
    // The headline: what the agent actually sends up on each request. While a
    // request is in the slot this is that request; afterwards the slot still
    // holds the last one, which is said rather than implied.
    const window = ctx.n_ctx ? ` / ${compact(ctx.n_ctx)}` : '';
    const parts = [ctx.live ? 'in flight' : 'last request'];
    if (ctx.live && ctx.cached) {
      parts.push(`${compact(ctx.cached)} from cache, ${compact(ctx.processed)} computed`);
    }
    if (ctx.fill !== undefined) parts.push(`${(ctx.fill * 100).toFixed(0)}% of window`);
    const hot = (ctx.fill || 0) > 0.6;
    const box = stat('Prompt tokens', { text: `${compact(ctx.tokens)}${window}`, hot },
                     parts.join(' · '), 'wide');
    if (ctx.fill !== undefined) box.append(bar(ctx.fill, hot));
    stats.append(box);

    if (ctx.live && ctx.decoded !== undefined) {
      const cap = ctx.max_tokens === -1 ? 'no cap' : `cap ${compact(ctx.max_tokens)}`;
      stats.append(stat('Generated', compact(ctx.decoded), cap));
    } else if (ctx.max_tokens !== undefined) {
      stats.append(stat('Output cap',
        { text: ctx.max_tokens === -1 ? 'none' : compact(ctx.max_tokens),
          hot: ctx.max_tokens === -1 },
        ctx.max_tokens === -1 ? 'a model may run on until it chooses to stop' : ''));
    }
  }

  const m = server.metrics || {};
  const recent = server.recent || {};
  // The rate from the latest window that finished a request. Between turns
  // the server holds the last measured figure (recent.held) and says how old
  // it is, rather than dropping to 0. llama.cpp's own gauge is a last resort:
  // it covers only requests since the previous /metrics read, so it reads 0
  // whenever the model is idle.
  [['Decode', 'decode_rate', 'decode_rate', 'tok/s out'],
   ['Prefill', 'prefill_rate', 'prefill_rate', 'tok/s in']].forEach(
    ([label, recentKey, gaugeKey, unit]) => {
      const measured = recent[recentKey];
      const gauge = m[gaugeKey];
      const value = measured ?? (gauge > 0 ? gauge : undefined);
      if (value === undefined) {
        stats.append(stat(label, '—', `${unit} · no request measured yet`));
        return;
      }
      let note = `${unit} · since last poll`;
      if (measured !== undefined && recent.held) {
        note = `${unit} · last turn, ${ago(recent.age_seconds)}`;
      } else if (measured !== undefined) {
        note = `${unit} · last ${Math.round(recent.window_seconds)}s`;
      }
      stats.append(stat(label, value.toFixed(1), note, recent.held ? 'held' : ''));
    });

  if (server.cache_hit_ratio !== undefined) {
    stats.append(stat('Prefix cache', `${(server.cache_hit_ratio * 100).toFixed(0)}%`,
                      'of prompt tokens reused'));
  }
  if (ctx && ctx.slots_total) {
    stats.append(stat('Slots', `${ctx.slots_busy}/${ctx.slots_total}`,
                      ctx.slots_total === 1 ? 'one at a time' : 'in parallel'));
  }
  if (m.deferred !== undefined) {
    stats.append(stat('Queued', { text: compact(m.deferred), hot: m.deferred > 0 },
                      'requests waiting for a slot'));
  }
  card.append(stats);

  [server.metrics_note, server.slots_note].filter(Boolean).forEach((note) => {
    card.append(el('p', 'note card-note', note));
  });
  return card;
}

function renderServers(servers) {
  const host = $('#server-cards');
  host.innerHTML = '';
  if (!servers.length) {
    host.append(el('p', 'note', 'No agents yet, so no model servers to read.'));
    return;
  }
  servers.forEach((server) => host.append(serverCard(server)));
}

let serverTimer = null;
let serverFailures = 0;

async function pollServers() {
  if (document.hidden) return;
  try {
    const data = await api('/api/agents/metrics');
    serverFailures = 0;
    state.contexts = data.contexts || {};
    state.mcp = data.mcp || {};
    renderServers(data.servers || []);
    renderAgents();            // the Context and Status columns come from this poll
    if (!$('#view-mcp').classList.contains('hidden')) renderMcp();   // its status chips too
    renderSidebar();           // and so do the sidebar's liveness pins
  } catch (error) {
    serverFailures += 1;
    // Three in a row means the board itself is unhappy; stop asking rather
    // than hammering it, and say why on the card.
    if (serverFailures >= 3) {
      stopServerPolling();
      $('#server-cards').innerHTML = '';
      $('#server-cards').append(el('p', 'note', `Gave up reading endpoints: ${error.message}`));
    }
  }
}

function startServerPolling() {
  if (serverTimer) return;
  serverFailures = 0;
  pollServers();
  serverTimer = setInterval(pollServers, 5000);
}

function stopServerPolling() {
  if (serverTimer) clearInterval(serverTimer);
  serverTimer = null;
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && serverTimer) pollServers();
});

/* ── agent config editor ────────────────────────────────────────────── */

async function openAgentConfig(agent) {
  const loading = el('p', 'note', 'Reading agent.yaml from the machine…');
  openModal(`${fqn(agent)} · settings`, loading);

  let data;
  try {
    data = await api(`/api/agents/${agent.id}/config`);
  } catch (error) {
    loading.className = 'warn-box';
    loading.textContent = `Could not read the config: ${error.message}`;
    return;
  }

  const wrap = el('div');

  // live supervisord state, with the obvious controls next to it
  const statusRow = el('div', 'status-row');
  const renderStatus = (status) => {
    statusRow.innerHTML = '';
    const ok = status?.ok;
    statusRow.append(el('span', `tag ${ok ? 'ok' : 'warn'}`, ok ? 'supervisord' : 'unmanaged'));
    statusRow.append(el('span', 'out',
      (status?.output || status?.hint || 'no status').slice(0, 200)));
    const btns = el('div', 'btns');
    ['restart', 'stop', 'start'].forEach((action) => {
      const button = el('button', 'btn small', action);
      button.onclick = async () => {
        button.disabled = true;
        try {
          const result = await api(`/api/agents/${agent.id}/action`, {
            method: 'POST', body: JSON.stringify({ action }),
          });
          renderStatus(result);
          toast(`${agent.name}: ${action} ${result.ok ? 'ok' : 'failed'}`, !result.ok);
          load();
        } catch (error) { toast(error.message, true); }
        finally { button.disabled = false; }
      };
      btns.append(button);
    });
    statusRow.append(btns);
  };
  renderStatus(data.status);
  wrap.append(statusRow);
  const prefixNote = el('p', 'note', data.prefix);
  wrap.append(prefixNote);

  // tabs
  const tabs = el('div', 'tabs');
  const bodies = el('div', 'tab-body');
  const panes = {};
  ['Properties', 'Raw YAML', 'Instructions', 'MCP'].forEach((label, i) => {
    const tab = el('button', i === 0 ? 'active' : '', label);
    const pane = el('div', i === 0 ? 'active' : '');
    panes[label] = pane;
    tab.onclick = () => {
      [...tabs.children].forEach((t) => t.classList.toggle('active', t === tab));
      [...bodies.children].forEach((b) => b.classList.toggle('active', b === pane));
      if (label === 'MCP') renderAgentMcp(agent, pane);
    };
    tabs.append(tab);
    bodies.append(pane);
  });
  wrap.append(tabs, bodies);

  // Properties: rendered from the schema the server sent.
  //
  // A row names the runtimes it reaches; the loop budgets are lean's own and
  // the truncation settings are adk/deepagent's. Showing all of them would
  // offer knobs that the YAML accepts and the agent never reads.
  const form = el('form', 'form');
  const rows = [];
  const appliesTo = (prop, framework) =>
    !prop.runtimes || prop.runtimes.includes(framework);
  const showFor = (framework) => {
    rows.forEach(({ prop, label }) => {
      const on = appliesTo(prop, framework);
      label.classList.toggle('hidden', !on);
    });
    const hidden = rows.filter(({ prop }) => !appliesTo(prop, framework)).length;
    frameworkNote.textContent = hidden
      ? `${hidden} setting(s) hidden — they belong to the other runtimes.`
      : '';
  };
  const frameworkNote = el('small', 'note');

  data.schema.forEach((prop) => {
    const value = data.properties[prop.key];
    const label = el('label', prop.type === 'bool' ? 'check' : '');
    let field;
    if (prop.type === 'choice') {
      field = el('select');
      if (!prop.choices.includes(String(value))) field.append(new Option(String(value || ''), value || ''));
      prop.choices.forEach((choice) => field.append(new Option(choice, choice)));
      field.value = value ?? '';
    } else if (prop.type === 'bool') {
      field = el('input');
      field.type = 'checkbox';
      field.checked = Boolean(value);
    } else {
      field = el('input');
      field.type = prop.type === 'int' || prop.type === 'number' ? 'number' : 'text';
      if (prop.step) field.step = prop.step;
      field.value = value ?? '';
    }
    field.name = prop.key;
    if (prop.type === 'bool') {
      label.append(field, document.createTextNode(' ' + prop.label));
    } else {
      label.append(document.createTextNode(prop.label), field);
    }
    if (prop.help) label.append(el('small', '', prop.help));
    if (prop.runtimes) {
      label.append(el('small', 'drift', `${prop.runtimes.join(' / ')} only`));
    }
    form.append(label);
    rows.push({ prop, label, field });
  });

  // Re-decide as soon as the framework changes, before anything is saved.
  form.elements.agent_framework.addEventListener('change', (event) =>
    showFor(String(event.target.value || '').toLowerCase()));
  form.append(frameworkNote);
  showFor(data.framework || 'lean');
  panes['Properties'].append(form);

  // Model suggestions, read from the endpoint on the agent's own machine with
  // the key in its .env — the board never sees the key. Unsaved provider and
  // endpoint edits are passed along, so the list follows the form.
  const modelField = form.elements.model;
  if (modelField) {
    const listId = `agent-models-${agent.id}`;
    const options = el('datalist');
    options.id = listId;
    modelField.setAttribute('list', listId);
    modelField.autocomplete = 'off';
    const modelNote = el('small');
    modelField.after(options, modelNote);
    let asked = 0;
    let timer = null;
    const loadModels = async () => {
      const mine = ++asked;
      const query = new URLSearchParams({
        provider: form.elements.provider?.value || '',
        endpoint: (form.elements['endpoint.url']?.value || '').trim(),
      });
      modelNote.textContent = 'Reading models from the endpoint…';
      try {
        const result = await api(`/api/agents/${agent.id}/models?${query}`);
        if (mine !== asked) return;               // superseded by a newer edit
        options.innerHTML = '';
        result.models.forEach((id) => options.append(new Option(id, id)));
        modelNote.textContent = result.ok
          ? `${result.models.length} model(s) at ${result.endpoint} — clear the field to see them all`
          : result.error;
      } catch (error) {
        if (mine === asked) modelNote.textContent = `Could not list models: ${error.message}`;
      }
    };
    const soon = () => { clearTimeout(timer); timer = setTimeout(loadModels, 400); };
    form.elements.provider?.addEventListener('change', soon);
    form.elements['endpoint.url']?.addEventListener('change', soon);
    loadModels();
  }

  // API key. It lives in the agent's .env, not agent.yaml, so it sits outside
  // the tabs and is saved whichever editor is active. The board only ever
  // learns which variables are set, never their values.
  const keyBox = el('div', 'form');
  const keyLabel = el('label');
  const keyField = el('input');
  keyField.type = 'password';
  keyField.name = 'api_key';
  keyField.autocomplete = 'new-password';
  const keyNote = el('small');
  const describeKey = () => {
    const provider = form.elements.provider?.value || '';
    const name = (data.key_names || {})[provider] || data.key_name;
    const set = (data.env_names || []).includes(name);
    keyField.placeholder = set ? 'set — leave empty to keep it' : 'not set';
    keyNote.textContent = `Saved to the agent's .env as ${name} (0600, sent over SSH ` +
      'stdin, never on a command line). The current value is never read back.';
  };
  form.elements.provider?.addEventListener('change', describeKey);
  describeKey();
  keyLabel.append(document.createTextNode('API key'), keyField, keyNote);
  keyBox.append(keyLabel);
  // Shown at the top, not under the tabs: below the Properties list it sat
  // past the fold of a scrolling dialog, where nobody found it.
  prefixNote.after(keyBox);

  // Host interface and published address. Neither is in agent.yaml: HOST is
  // in the supervisord program and the URL is baked into run_server.py, so
  // they are read from the machine and saved by their own route on the server.
  const net = data.network || {};
  const netBox = el('div', 'form');
  const original = { bind: net.bind_host || '', advertise: net.advertise_host || '' };
  const bindSelect = el('select');
  const advertiseField = el('input');
  if (net.ok) {
    const machine = state.machines.find((m) => m.id === agent.machine_id);
    const interfaces = machine?.interfaces || [];
    bindSelect.append(new Option('all interfaces (0.0.0.0)', '0.0.0.0'));
    interfaces.forEach((iface) => bindSelect.append(new Option(
      `${iface.name} · ${iface.address}${iface.note ? ` — ${iface.note}` : ''}`, iface.address)));
    if (original.bind && ![...bindSelect.options].some((o) => o.value === original.bind)) {
      bindSelect.append(new Option(`${original.bind} (current)`, original.bind));
    }
    bindSelect.value = original.bind || '0.0.0.0';
    advertiseField.value = original.advertise;
    advertiseField.placeholder = 'address peers dial';
    // A specific interface is also what peers should dial; offer it, and let
    // the field be overridden for NAT or a proxy.
    bindSelect.onchange = () => {
      if (!['0.0.0.0', '::'].includes(bindSelect.value)) advertiseField.value = bindSelect.value;
    };

    const bindLabel = el('label');
    bindLabel.append(document.createTextNode('Host interface (HOST)'), bindSelect,
      el('small', '', 'The address the A2A server binds to.'));
    const advLabel = el('label');
    advLabel.append(document.createTextNode('Advertise as'), advertiseField,
      el('small', '', `Published in the agent card — what other agents connect to. ` +
        `Now ${net.url}. Changing either restarts the agent.`));
    netBox.append(bindLabel, advLabel);
    if (agent.url && net.url && agent.url !== net.url) {
      netBox.append(el('div', 'warn-box',
        `The board has ${agent.url} on record, but the card publishes ${net.url}. ` +
        'Messages go to the card\'s address. Saving either field also updates the board record.'));
    }
  } else {
    netBox.append(el('p', 'note', `Address settings unavailable: ${net.error || 'not reported'}`));
  }
  keyBox.after(netBox);
  const networkChange = () => {
    if (!net.ok) return null;
    const bind = bindSelect.value;
    const advertise = advertiseField.value.trim();
    if (bind === original.bind && advertise === original.advertise) return null;
    return { bind_host: bind, advertise_host: advertise };
  };
  panes['Properties'].append(el('p', 'note',
    'Saving this form rewrites agent.yaml, which keeps the header comment but ' +
    'drops any others. Use Raw YAML to control the file exactly.'));

  // Raw YAML + instructions
  const rawBox = el('textarea', 'code');
  rawBox.value = data.yaml;
  rawBox.spellcheck = false;
  panes['Raw YAML'].append(rawBox);
  panes['Raw YAML'].append(el('p', 'note',
    'Written verbatim. name cannot change — it keys the install directory and ' +
    'the supervisord program.'));

  const insBox = el('textarea', 'code');
  insBox.value = data.instructions;
  insBox.spellcheck = false;
  panes['Instructions'].append(insBox);
  panes['Instructions'].append(el('p', 'note',
    'instructions.md — the system prompt this agent starts from.'));


  // save
  const warnBox = el('div', 'warn-box');
  warnBox.hidden = true;
  wrap.append(warnBox);

  const actions = el('div', 'form-actions');
  const restartLabel = el('label', 'check');
  const restartBox = el('input');
  restartBox.type = 'checkbox';
  restartBox.checked = true;
  restartLabel.append(restartBox, document.createTextNode(' Restart after saving'));
  restartLabel.append(el('small', '', 'A running agent read its config once at startup.'));
  const save = el('button', 'btn primary', 'Save');
  actions.append(restartLabel, save);
  wrap.append(actions);

  const activeTab = () => tabs.querySelector('button.active').textContent;

  save.onclick = async () => {
    save.disabled = true;
    save.innerHTML = '<span class="spin">↻</span> saving…';
    warnBox.hidden = true;
    const payload = { restart: restartBox.checked, instructions: insBox.value };
    // Whichever editor you were last in wins, so the two cannot fight.
    if (activeTab() === 'Raw YAML') payload.yaml = rawBox.value;
    else payload.properties = values(form);
    if (keyField.value.trim()) payload.api_key = keyField.value.trim();
    const network = networkChange();
    if (network) payload.network = network;

    try {
      const result = await api(`/api/agents/${agent.id}/config`, {
        method: 'PUT', body: JSON.stringify(payload),
      });
      rawBox.value = result.yaml;
      if (result.warnings?.length) {
        warnBox.hidden = false;
        warnBox.innerHTML = '';
        warnBox.append(el('strong', '', 'Saved, with warnings:'));
        const list = el('ul');
        result.warnings.forEach((warning) => list.append(el('li', '', warning)));
        warnBox.append(list);
      } else {
        toast(result.network?.ok
          ? `${agent.name} saved, restarted on ${result.network.url}`
          : `${agent.name} saved${restartBox.checked ? ' and restarted' : ''}`);
        closeModal();
      }
      load();
    } catch (error) {
      warnBox.hidden = false;
      warnBox.textContent = error.message;
    } finally {
      save.disabled = false;
      save.textContent = 'Save';
    }
  };

  openModal(`${fqn(agent)} · settings`, wrap);
}

/*
 * Reinstall: the same install.sh run as Create, minus everything that would
 * reset the agent. The server reads the current config and address back from
 * the machine and passes them in; this form only picks the code to install.
 */
function openReinstall(agent) {
  const form = el('form', 'form');
  const source = el('select');
  source.name = 'sdk_source';
  source.append(new Option('this working tree (build & send a wheel)', 'local'),
                new Option('git (repository branch)', 'git'));
  source.value = state.settings.default_sdk_source || 'local';
  const sourceLabel = el('label');
  sourceLabel.append(document.createTextNode('SDK source'), source);
  const ref = el('input');
  ref.name = 'sdk_ref';
  ref.placeholder = 'master (only used with git)';
  const refLabel = el('label');
  refLabel.append(document.createTextNode('Git ref'), ref);
  const refresh = el('label', 'check');
  const refreshBox = el('input');
  refreshBox.type = 'checkbox';
  refreshBox.checked = true;
  refresh.append(refreshBox, document.createTextNode(' Refresh the board address and token in .env'));
  refresh.append(el('small', '', 'So send_to_agent, list_agents and the file relay reach this ' +
                    'board as it is configured now. The API key and other variables are kept.'));
  form.append(sourceLabel, refLabel, refresh);
  form.append(el('p', 'note',
    'Kept: agent.yaml, instructions.md, the workspace, the port, the API key, and the ' +
    'host / advertised address shown in ⚙ Settings. The agent restarts at the end, ' +
    'and building a wheel can take a few minutes.'));
  const actions = el('div', 'form-actions');
  const go = el('button', 'btn primary', 'Reinstall');
  go.type = 'submit';
  actions.append(go);
  form.append(actions);

  form.onsubmit = async (event) => {
    event.preventDefault();
    go.disabled = true;
    go.innerHTML = '<span class="spin">↻</span> reinstalling…';
    try {
      const result = await api(`/api/agents/${agent.id}/reinstall`, {
        method: 'POST',
        body: JSON.stringify({
          sdk_source: source.value, sdk_ref: ref.value.trim(),
          refresh_board_env: refreshBox.checked,
        }),
      });
      closeModal();
      const problems = result.result?.problems;
      const list = Array.isArray(problems) ? problems.join('; ') : (problems || '');
      const address = result.address_fixed
        ? ` — ${result.address_fixed}`
        : (result.kept_address ? '' : ' — address re-detected by install.sh');
      toast(result.agent?.ready
        ? `${fqn(agent)} reinstalled${address}`
        : `${fqn(agent)} reinstalled but not ready: ${list || 'see its log'}${address}`,
        !result.agent?.ready);
      load();
    } catch (error) {
      toast(`Reinstall failed: ${error.message}`, true);
      go.disabled = false;
      go.textContent = 'Reinstall';
    }
  };
  openModal(`Reinstall ${fqn(agent)}`, form);
}

/* ── machines view ──────────────────────────────────────────────────── */

function renderMachines() {
  const table = $('#machines-table');
  table.innerHTML = '';
  table.append(headRow(['Machine', 'Target', 'OS', 'Addresses', 'MCP runners', 'Status', '']));
  if (!state.machines.length) {
    table.append(emptyRow(7, 'No machines. Register this one, or an SSH host.'));
    return;
  }
  state.machines.forEach((machine) => {
    const row = el('tr');
    row.append(cell(machine.name, 'mono'));
    row.append(cell(machine.is_local ? 'local' :
      `${machine.ssh_user ? machine.ssh_user + '@' : ''}${machine.host}` +
      (machine.ssh_port !== 22 ? `:${machine.ssh_port}` : ''), 'mono'));
    row.append(cell(machine.os_kind || '—'));
    const addresses = el('td', 'mono');
    const list = machine.interfaces || [];
    if (!list.length) {
      addresses.textContent = '—';
    } else {
      list.forEach((iface) => {
        const note = iface.note ? ` (${iface.note})` : '';
        addresses.append(el('div', '', `${iface.name} ${iface.address}${note}`));
      });
    }
    row.append(addresses);

    // What stdio MCP servers usually run through; a probe finds them.
    const runners = el('td', 'mono');
    const found = new Set(machine.runners || []);
    ['npx', 'uvx'].forEach((name) => runners.append(
      el('div', found.has(name) ? '' : 'dim', `${found.has(name) ? '✓' : '✗'} ${name}`)));
    runners.title = 'npx and uvx run most stdio MCP servers. Test the machine to re-check.';
    row.append(runners);

    const status = el('td');
    const kind = machine.status === 'ready' ? 'ok' : machine.status === 'unknown' ? '' : 'warn';
    status.append(el('span', `tag ${kind}`, machine.status));
    if (machine.last_error) status.append(el('div', 'note', machine.last_error));
    row.append(status);

    const actions = el('td');
    const test = el('button', 'btn small', 'Test');
    test.onclick = async () => {
      test.innerHTML = '<span class="spin">↻</span>';
      try { await api(`/api/machines/${machine.id}/test`, { method: 'POST' }); load(); }
      catch (error) { toast(error.message, true); }
      finally { test.textContent = 'Test'; }
    };
    const refresh = el('button', 'btn small', 'Refresh agents');
    refresh.onclick = async () => {
      try {
        const result = await api(`/api/machines/${machine.id}/refresh`, { method: 'POST' });
        toast(`${result.found} agent(s) found, ${result.removed} stale record(s) dropped`);
        load();
      } catch (error) { toast(error.message, true); }
    };
    const remove = el('button', 'btn danger small', 'Forget');
    remove.onclick = async () => {
      if (!confirm(`Forget '${machine.name}'?\n\nAgents keep running on the box — ` +
                   `delete them first if you want them gone.`)) return;
      try { await api(`/api/machines/${machine.id}`, { method: 'DELETE' }); load(); }
      catch (error) { toast(error.message, true); }
    };
    actions.append(test, refresh, remove);
    row.append(actions);
    table.append(row);
  });
}

/* ── settings view ──────────────────────────────────────────────────── */

function renderSettings() {
  const form = $('#settings-form');
  Object.entries(state.settings).forEach(([key, value]) => {
    const field = form.elements[key];
    if (!field) return;
    if (field.type === 'checkbox') field.checked = Boolean(value);
    else field.value = value;
    markDrift(field, key, value);
  });
}

/**
 * Point out a stored value that no longer matches the shipped default.
 *
 * A store keeps whatever it was created with — seeding only fills missing keys
 * — so a default raised in a later version never reaches an existing store.
 * That is how a board carried on waiting only 300s for a reply after the
 * default became 900. Shown, not overwritten: the value may be deliberate.
 */
function markDrift(field, key, value) {
  field.parentElement.querySelector('.drift')?.remove();
  const shipped = state.defaults[key];
  if (shipped === undefined || String(shipped) === String(value)) return;

  const note = el('small', 'drift');
  note.append(document.createTextNode(`this build ships ${JSON.stringify(shipped)} · `));
  const use = el('a', '', 'use it');
  use.href = '#';
  use.onclick = (event) => {
    event.preventDefault();
    if (field.type === 'checkbox') field.checked = Boolean(shipped);
    else field.value = shipped;
    note.remove();
  };
  note.append(use);
  field.parentElement.append(note);
}

/* ── modal ──────────────────────────────────────────────────────────── */

function openModal(title, node) {
  $('#modal-title').textContent = title;
  const body = $('#modal-body');
  body.innerHTML = '';
  body.append(node);
  $('#modal-back').classList.remove('hidden');
  body.querySelector('input, select, textarea')?.focus();
}
function closeModal() { $('#modal-back').classList.add('hidden'); }

function formFrom(id) {
  return document.getElementById(id).content.cloneNode(true).querySelector('form');
}
function values(form) {
  const out = {};
  new FormData(form).forEach((value, key) => (out[key] = value));
  form.querySelectorAll('input[type=checkbox]').forEach((box) => (out[box.name] = box.checked));
  return out;
}

function askForToken() {
  const form = formFrom('tpl-token-form');
  form.onsubmit = (event) => {
    event.preventDefault();
    token.set(values(form).token);
    closeModal();
    location.reload();
  };
  openModal('Token required', form);
}

/* ── wiring ─────────────────────────────────────────────────────────── */

function headRow(labels) {
  const row = el('tr');
  labels.forEach((label) => row.append(el('th', '', label)));
  return row;
}
function emptyRow(span, text) {
  const row = el('tr');
  const cellNode = el('td', 'note', text);
  cellNode.colSpan = span;
  row.append(cellNode);
  return row;
}
function cell(text, cls) { return el('td', cls, text); }
function fmtTime(iso) {
  try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  catch { return ''; }
}
function fmtArgs(args) {
  const text = Object.entries(args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ');
  return text.length > 120 ? text.slice(0, 119) + '…' : text;
}

/* ── MCP view ───────────────────────────────────────────────────────── */

/*
 * The board's MCP catalog (docs/design/mcp.md, MCP-CAT-*): definitions here,
 * attached to agents in their Settings. Secret values are typed once and sent
 * to the agents' .env files; the board never shows or keeps them.
 */
async function loadMcp() {
  try {
    const data = await api('/api/mcp');
    state.mcpCatalog = data.servers || [];
    state.mcpPresets = data.presets || [];
    renderMcp();
  } catch (error) { toast(`MCP catalog: ${error.message}`, true); }
}

function mcpRuns(spec) {
  if (spec.transport === 'stdio') return [spec.command, ...(spec.args || [])].join(' ');
  return `${spec.transport}: ${spec.url || ''}`;
}

function renderMcp() {
  const table = $('#mcp-table');
  table.innerHTML = '';
  table.append(headRow(['Server', 'Runs', 'Approval', 'Secrets', 'Attached to', '']));
  if (!state.mcpCatalog.length) {
    table.append(emptyRow(6, 'No MCP servers yet. Add one — start from a preset such as ' +
                             'fetch — then attach it to agents in their ⚙ Settings.'));
    return;
  }
  state.mcpCatalog.forEach((server) => {
    const row = el('tr');
    row.append(cell(server.name, 'mono'));
    const runs = cell(mcpRuns(server.spec), 'mono');
    if (server.runner) runs.append(el('div', 'note', `needs ${server.runner} on each machine`));
    row.append(runs);
    row.append(cell({ false: 'no', true: 'always', destructive: 'destructive only' }[
      String(server.spec.approval ?? 'false')] || String(server.spec.approval)));

    const secrets = el('td');
    if (!server.vars.length) secrets.append(el('span', 'dim', '—'));
    server.vars.forEach((name) => {
      const button = el('button', 'btn small', `Set ${name}…`);
      button.title = server.agents.length
        ? `Write ${name} into the .env of the ${server.agents.length} attached agent(s)`
        : 'Attach the server to an agent first';
      button.disabled = !server.agents.length;
      button.onclick = () => setMcpSecret(server, name);
      secrets.append(button);
    });
    row.append(secrets);

    const attached = el('td');
    if (!server.agents.length) attached.append(el('span', 'dim', 'no agents'));
    server.agents.forEach((link) => {
      const agent = state.agents.find((a) => a.id === link.agent_id);
      if (!agent) return;
      const live = (state.mcp[String(agent.id)]?.servers || []).find((s) => s.name === server.name);
      const chip = el('div', 'mcp-server');
      chip.append(tint(el('span', 'mono agent-name', fqn(agent)), agent));
      const label = !live ? ' · not reported yet' : live.connected
        ? ` · ✓ ${(live.tools || []).length} tools` : ' · ✗ not connected';
      chip.append(el('span', live && !live.connected ? 'note warn-text' : 'note', label));
      chip.title = live ? (live.connected ? (live.tools || []).join(', ') : live.error || '')
        : 'The agent has not reported this server — it may still be starting, ' +
          'or need a Reinstall for MCP support.';
      attached.append(chip);
    });
    row.append(attached);

    const actions = el('td');
    const editBtn = el('button', 'btn small', 'Edit');
    editBtn.onclick = () => openMcpEditor(server);
    const remove = el('button', 'btn small danger', 'Delete');
    remove.onclick = async () => {
      const names = server.agents
        .map((l) => state.agents.find((a) => a.id === l.agent_id)).filter(Boolean).map(fqn);
      const note = names.length ? `\n\nIt is detached from ${names.join(', ')}, which restart.` : '';
      if (!confirm(`Delete the MCP server '${server.name}'?${note}`)) return;
      try {
        await api(`/api/mcp/${server.id}`, { method: 'DELETE' });
        toast(`Deleted ${server.name}`);
        loadMcp();
      } catch (error) { toast(error.message, true); }
    };
    actions.append(editBtn, remove);
    row.append(actions);
    table.append(row);
  });
}

function setMcpSecret(server, name) {
  const form = el('form', 'form');
  const label = el('label', '', `${name} `);
  const input = el('input');
  input.type = 'password';
  input.autocomplete = 'off';
  input.required = true;
  label.append(input);
  form.append(label, el('p', 'note',
    `Written into the .env of the agents ${server.name} is attached to, which then ` +
    'restart. The board does not keep it and cannot show it again.'));
  const actions = el('div', 'form-actions');
  const save = el('button', 'btn primary', 'Set');
  save.type = 'submit';
  actions.append(save);
  form.append(actions);
  form.onsubmit = async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      const result = await api(`/api/mcp/${server.id}/secret`, {
        method: 'POST', body: JSON.stringify({ name, value: input.value }),
      });
      input.value = '';
      const failed = result.agents.filter((a) => !a.ok);
      toast(failed.length
        ? `${name} not set on: ${failed.map((a) => `${a.agent} (${a.error})`).join('; ')}`
        : `${name} set on ${result.agents.length} agent(s)`, failed.length > 0);
      closeModal();
    } catch (error) { toast(error.message, true); save.disabled = false; }
  };
  openModal(`${server.name} · set ${name}`, form);
}

const lines = (text) => String(text || '').split('\n').map((l) => l.trim()).filter(Boolean);
function pairs(text, separator) {
  const out = {};
  lines(text).forEach((line) => {
    const at = line.indexOf(separator);
    if (at > 0) out[line.slice(0, at).trim()] = line.slice(at + separator.length).trim();
  });
  return out;
}

function fillMcpForm(form, name, spec) {
  const set = (key, value) => { if (form.elements[key]) form.elements[key].value = value ?? ''; };
  set('name', name);
  set('transport', spec.transport || 'stdio');
  set('command', spec.command);
  set('args', (spec.args || []).join('\n'));
  set('env', Object.entries(spec.env || {}).map(([k, v]) => `${k}=${v}`).join('\n'));
  set('cwd', spec.cwd);
  set('url', spec.url);
  set('headers', Object.entries(spec.headers || {}).map(([k, v]) => `${k}: ${v}`).join('\n'));
  set('tools', (spec.tools || []).join(', '));
  set('approval', String(spec.approval ?? 'false'));
  set('timeout', spec.timeout);
  set('startup_timeout', spec.startup_timeout);
}

function mcpSpecFrom(form) {
  const v = values(form);
  const spec = { transport: v.transport, approval: v.approval };
  if (v.transport === 'stdio') {
    Object.assign(spec, { command: v.command.trim(), args: lines(v.args),
                          env: pairs(v.env, '='), cwd: v.cwd.trim() });
  } else {
    Object.assign(spec, { url: v.url.trim(), headers: pairs(v.headers, ':') });
  }
  const tools = v.tools.split(',').map((t) => t.trim()).filter(Boolean);
  if (tools.length) spec.tools = tools;
  if (v.timeout) spec.timeout = Number(v.timeout);
  if (v.startup_timeout) spec.startup_timeout = Number(v.startup_timeout);
  return { name: v.name.trim(), spec };
}

function openMcpEditor(server, prefill = null) {
  const form = formFrom('tpl-mcp-form');
  const preset = form.elements.preset;
  if (prefill) {
    // From the MCP Registry: say where it came from, and what to fill in.
    form.querySelector('.preset-row').replaceWith(registryNote(prefill));
  } else if (server) {
    form.querySelector('.preset-row').remove();
  } else {
    state.mcpPresets.forEach((p) => preset.append(new Option(`${p.name} — ${p.label}`, p.name)));
    preset.onchange = () => {
      const chosen = state.mcpPresets.find((p) => p.name === preset.value);
      if (chosen) { fillMcpForm(form, chosen.name, chosen.spec); showTransport(); }
    };
  }
  const showTransport = () => {
    const stdio = form.elements.transport.value === 'stdio';
    form.querySelector('.when-stdio').classList.toggle('hidden', !stdio);
    form.querySelector('.when-remote').classList.toggle('hidden', stdio);
  };
  form.elements.transport.onchange = showTransport;
  fillMcpForm(form, prefill?.name || server?.name || '',
              prefill?.spec || server?.spec || { transport: 'stdio' });
  showTransport();

  form.onsubmit = async (event) => {
    event.preventDefault();
    const payload = mcpSpecFrom(form);
    try {
      if (server) {
        const count = server.agents.length;
        payload.sync = count > 0 && confirm(
          `Update the ${count} agent(s) '${server.name}' is attached to now?\n\n` +
          'They restart with the new settings. Cancel saves the catalog only; agents ' +
          'pick it up the next time they are attached or edited.');
        const result = await api(`/api/mcp/${server.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        const failed = (result.synced || []).filter((s) => !s.ok);
        toast(failed.length
          ? `Saved, but not applied on: ${failed.map((s) => `${s.agent} (${s.error})`).join('; ')}`
          : `Saved ${payload.name}${payload.sync ? ` and updated ${result.synced.length} agent(s)` : ''}`,
          failed.length > 0);
      } else {
        await api('/api/mcp', { method: 'POST', body: JSON.stringify(payload) });
        toast(`Added ${payload.name} — attach it to agents in their ⚙ Settings`);
      }
      closeModal();
      loadMcp();
    } catch (error) { toast(error.message, true); }
  };
  openModal(server ? `MCP server · ${server.name}` : 'Add MCP server', form);
}

$('#new-mcp').onclick = () => openMcpEditor(null);
$('#browse-mcp').onclick = () => browseRegistry();

const httpLink = (url, text) => {
  const link = el('a', '', text || url);
  if (/^https?:\/\//i.test(url || '')) { link.href = url; link.target = '_blank'; link.rel = 'noopener'; }
  return link;
};

/** What the editor shows above a form filled from a registry entry. */
function registryNote(prefill) {
  const box = el('div', 'warn-box registry-note');
  const line = el('div');
  line.append(document.createTextNode(`From the MCP Registry: ${prefill.registryName} `));
  if (prefill.repository) line.append(httpLink(prefill.repository, 'repository'));
  box.append(line);
  box.append(el('div', '', 'Registry entries are published by their authors and not ' +
    'reviewed. A stdio server runs on the agent\'s machine with its access — read the ' +
    'repository before saving. Replace any <placeholder> in the arguments.'));
  if (prefill.vars.length) {
    const list = el('ul');
    prefill.vars.forEach((v) => {
      const item = el('li');
      item.append(el('code', '', v.name));
      const flags = [v.required ? 'required' : 'optional', v.secret ? 'secret' : '']
        .filter(Boolean).join(', ');
      item.append(document.createTextNode(` (${flags})${v.description ? ' — ' + v.description : ''}`
        + (v.required || v.secret ? '' : ' — not added; add it under Environment if needed')));
      list.append(item);
    });
    box.append(el('div', '', 'Variables (set secrets with Set… on the MCP tab once attached):'));
    box.append(list);
  }
  return box;
}

/** Search the official MCP Registry and pick a server (MCP-CAT-13). */
function browseRegistry() {
  const wrap = el('div', 'registry');
  const bar = el('form', 'registry-bar');
  const query = el('input');
  query.placeholder = 'search — e.g. github, postgres, filesystem, browser';
  const go = el('button', 'btn primary', 'Search');
  go.type = 'submit';
  // MCP-CAT-14: servers their own vendor published, unless asked otherwise.
  const officialBox = el('input');
  officialBox.type = 'checkbox';
  officialBox.checked = true;
  const officialLabel = el('label', 'check', ' Official publishers only');
  officialLabel.prepend(officialBox);
  officialLabel.title = 'Only servers published under the vendor\'s own namespace ' +
    '(for example io.github.github or com.stripe). Most services also have many ' +
    'third-party servers; untick to see those too.';
  bar.append(query, go, officialLabel);
  const results = el('div', 'registry-results');
  const more = el('button', 'btn small hidden', 'More results');
  wrap.append(bar, el('p', 'note', 'From registry.modelcontextprotocol.io — servers published ' +
    'from GitHub, npm, PyPI and Docker. Choosing one only fills in the Add server form.'),
    results, more);

  let cursor = '';
  const search = async (append) => {
    go.disabled = true;
    more.disabled = true;
    if (!append) { results.innerHTML = ''; cursor = ''; }
    const status = el('p', 'note', 'Searching…');
    results.append(status);
    try {
      const params = new URLSearchParams({ search: query.value.trim(), cursor,
                                           official: officialBox.checked ? '1' : '' });
      const data = await api(`/api/mcp/registry?${params}`);
      status.remove();
      if (!append && !data.servers.length) {
        results.append(el('p', 'note', officialBox.checked
          ? 'No official server matches. Untick "Official publishers only" to search ' +
            'third-party servers too. The presets in Add server cover fetch, time, git and files.'
          : 'Nothing found.'));
      }
      data.servers.forEach((entry) => results.append(registryResult(entry)));
      cursor = data.next || '';
      more.classList.toggle('hidden', !cursor);
    } catch (error) {
      status.className = 'warn-box';
      status.textContent = error.message;
    } finally { go.disabled = false; more.disabled = false; }
  };
  bar.onsubmit = (event) => { event.preventDefault(); search(false); };
  officialBox.onchange = () => search(false);
  more.onclick = () => search(true);
  openModal('MCP Registry', wrap);
  search(false);
}

function registryResult(entry) {
  const box = el('div', 'registry-item');
  const head = el('div', 'registry-head');
  head.append(el('strong', '', entry.title || entry.slug),
              el('span', 'note mono', ` ${entry.name} ${entry.version ? 'v' + entry.version : ''}`));
  head.append(entry.publisher
    ? el('span', 'tag ok', `✓ ${entry.publisher}`)
    : el('span', 'tag', 'community'));
  box.append(head);
  if (entry.description) box.append(el('div', '', entry.description));
  const links = el('div', 'note');
  if (entry.repository) links.append(httpLink(entry.repository, 'repository'));
  if (entry.website) { links.append(document.createTextNode(' · ')); links.append(httpLink(entry.website, 'website')); }
  if (links.childNodes.length) box.append(links);
  const options = el('div', 'btns');
  entry.options.forEach((option) => {
    if (!option.spec) {
      const off = el('span', 'note dim', `${option.label}: ${option.unsupported}`);
      options.append(off);
      return;
    }
    const use = el('button', 'btn small', `Use ${option.label}`);
    use.onclick = () => openMcpEditor(null, {
      name: entry.slug, spec: option.spec, vars: option.vars || [],
      registryName: entry.name, repository: entry.repository,
    });
    options.append(use);
  });
  if (!entry.options.length) options.append(el('span', 'note dim', 'no package or URL listed'));
  box.append(options);
  return box;
}

/** The MCP tab of an agent's Settings: attach, detach, override (MCP-CAT-12). */
async function renderAgentMcp(agent, pane) {
  pane.innerHTML = '';
  pane.append(el('p', 'note', 'Loading the MCP catalog…'));
  let data;
  try { data = await api(`/api/agents/${agent.id}/mcp`); }
  catch (error) { pane.innerHTML = ''; pane.append(el('p', 'warn-box', error.message)); return; }
  pane.innerHTML = '';
  pane.append(el('p', 'note', 'Changes here apply at once: the board writes them into this ' +
    "agent's agent.yaml (board_mcp_servers) and restarts it. Servers written into " +
    'agent.yaml by hand (mcp_servers) are not shown here and are left alone.'));
  if (!data.catalog.length) {
    pane.append(el('p', 'note', 'The catalog is empty — add servers on the MCP tab.'));
    return;
  }
  const attached = new Map(data.attached.map((a) => [a.server_id, a]));
  data.catalog.forEach((server) => {
    const link = attached.get(server.id);
    const box = el('div', 'mcp-attach');
    const head = el('div', 'mcp-attach-head');
    head.append(el('strong', 'mono', server.name), el('span', 'note', ` ${mcpRuns(server.spec)}`));
    box.append(head);

    const approval = el('select');
    [['', `default (${server.spec.approval ?? 'false'})`], ['false', 'no'],
     ['destructive', 'destructive only'], ['true', 'always']]
      .forEach(([value, label]) => approval.append(new Option(label, value)));
    approval.value = link?.overrides?.approval || '';
    const tools = el('input');
    tools.placeholder = server.spec.tools ? server.spec.tools.join(', ') : 'all tools';
    tools.value = (link?.overrides?.tools || []).join(', ');
    const options = el('div', 'row-2');
    const l1 = el('label', '', 'Approval '); l1.append(approval);
    const l2 = el('label', '', 'Tools '); l2.append(tools);
    options.append(l1, l2);
    box.append(options);

    const actions = el('div', 'btns');
    const apply = el('button', link ? 'btn small' : 'btn small primary', link ? 'Update' : 'Attach');
    apply.onclick = async () => {
      apply.disabled = true;
      try {
        const result = await api(`/api/agents/${agent.id}/mcp`, {
          method: 'POST',
          body: JSON.stringify({ server_id: server.id,
                                 overrides: { approval: approval.value, tools: tools.value } }),
        });
        const problems = [...(result.warnings || []),
                          ...(result.synced?.ok ? [] : [result.synced?.error || 'not applied'])];
        toast(problems.length ? `${server.name}: ${problems.join(' · ')}`
          : `${server.name} attached — ${agent.name} is restarting`, problems.length > 0);
        renderAgentMcp(agent, pane);
      } catch (error) { toast(error.message, true); apply.disabled = false; }
    };
    actions.append(apply);
    if (link) {
      const detach = el('button', 'btn small danger', 'Detach');
      detach.onclick = async () => {
        detach.disabled = true;
        try {
          await api(`/api/agents/${agent.id}/mcp/${server.id}`, { method: 'DELETE' });
          toast(`${server.name} detached — ${agent.name} is restarting`);
          renderAgentMcp(agent, pane);
        } catch (error) { toast(error.message, true); detach.disabled = false; }
      };
      actions.append(detach);
      if (server.vars.length) {
        actions.append(el('span', 'note',
          ` uses ${server.vars.map((v) => '${' + v + '}').join(', ')} — set on the MCP tab`));
      }
    }
    box.append(actions);
    pane.append(box);
  });
}

function showView(name) {
  document.querySelectorAll('.view-tab').forEach((t) =>
    t.classList.toggle('active', t.dataset.view === name));
  ['board', 'agents', 'machines', 'mcp', 'logs', 'settings'].forEach((view) =>
    $(`#view-${view}`).classList.toggle('hidden', view !== name));
  // The MCP tab shows each attached agent's connection state from the same poll.
  if (name === 'agents' || name === 'mcp') startServerPolling(); else stopServerPolling();
  if (name === 'mcp') loadMcp();
  if (name === 'logs') logsView.start(); else logsView.stop();
}

document.querySelectorAll('.view-tab').forEach((tab) => {
  tab.onclick = () => showView(tab.dataset.view);
});

/* ── logs view ──────────────────────────────────────────────────────── */

/*
 * Two sources in one pane:
 *  - control: this process's log ring buffer, polled with ?after=<seq> so each
 *    poll carries only new records. Filters are applied by the server and a
 *    change of filter re-reads from the start.
 *  - agent: the tail of an agent's stdout / stderr / supervisord log, read
 *    over SSH on demand (and optionally every 5s — each read is an SSH call).
 * Polling runs only while the tab is open.
 */
const logsView = (() => {
  const CONTROL_MS = 2000;
  const AGENT_MS = 5000;
  const MAX_LINES = 3000;  // keep the DOM bounded on a long-lived tab
  let mode = 'control';
  let after = 0;
  let timer = null;
  const busy = { control: 0, agent: false };  // control: requests in flight
  let open = false;
  let wantAgent = null;  // set by an agent row's Logs button
  let generation = 0;    // bumped by a reset; older control replies are dropped

  const view = () => $('#log-view');
  const atBottom = () => {
    const v = view();
    return v.scrollHeight - v.scrollTop - v.clientHeight < 40;
  };
  const stick = (was) => { if (was) view().scrollTop = view().scrollHeight; };

  function stamp(ts) {
    const d = new Date(ts * 1000);
    const pad = (n, w = 2) => String(n).padStart(w, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function appendRecords(records) {
    if (!records.length) return;
    const was = atBottom();
    const frag = document.createDocumentFragment();
    records.forEach((r) => {
      const line = el('div', `log-line lvl-${r.level.toLowerCase()}`);
      line.append(
        el('span', 'log-ts', stamp(r.ts)),
        el('span', 'log-lvl', r.level),
        el('span', 'log-src', r.logger.replace(/^declarative_agent_sdk\./, '')),
        el('span', 'log-msg', r.message),
      );
      frag.append(line);
    });
    view().append(frag);
    while (view().childElementCount > MAX_LINES) view().firstElementChild.remove();
    stick(was);
  }

  function placeholder(text) {
    view().replaceChildren(el('div', 'log-empty', text));
  }

  async function pollControl(reset = false) {
    if (busy.control && !reset) return;
    busy.control += 1;
    try {
      if (reset) { generation += 1; after = 0; view().replaceChildren(); }
      const mine = generation;
      const params = new URLSearchParams({
        after: String(after),
        level: $('#log-level').value,
        q: $('#log-filter').value.trim(),
        limit: '1000',
      });
      const result = await api(`/api/logs?${params}`);
      if (mode !== 'control') return;  // switched away while this was in flight
      if (mine !== generation) return;  // a filter change re-read from the start
      if (view().querySelector('.log-empty')) view().replaceChildren();
      appendRecords(result.records || []);
      if (reset && !(result.records || []).length) placeholder('Nothing logged at this level yet.');
      after = result.last || after;
      $('#log-path').textContent = result.file ? `also in ${result.file}` : '';
    } catch (error) {
      if (mode === 'control') placeholder(`Could not read the control log: ${error.message}`);
    } finally { busy.control -= 1; }
  }

  function fillAgents() {
    const select = $('#agent-log-agent');
    const keep = select.value;
    select.replaceChildren(...state.agents.map((agent) => {
      const option = el('option', '', fqn(agent));
      option.value = String(agent.id);
      return option;
    }));
    if (keep && state.agents.some((a) => String(a.id) === keep)) select.value = keep;
    if (wantAgent != null) { select.value = String(wantAgent); wantAgent = null; }
  }

  async function pollAgent() {
    const id = $('#agent-log-agent').value;
    if (!id) { placeholder('No agents yet. Install one from the Agents tab.'); return; }
    if (busy.agent) return;
    busy.agent = true;
    const button = $('#agent-log-refresh');
    button.disabled = true;
    try {
      const params = new URLSearchParams({
        stream: $('#agent-log-stream').value,
        lines: $('#agent-log-lines').value,
      });
      const result = await api(`/api/agents/${id}/logs?${params}`);
      if (mode !== 'agent' || $('#agent-log-agent').value !== id) return;
      $('#agent-log-path').textContent = `${result.machine}:${result.path}`;
      if (!result.exists) {
        placeholder(`${result.path} does not exist on ${result.machine} yet — ` +
          'the program has not written to it (never started, or not installed by supervisord).');
        return;
      }
      const was = atBottom() || !view().querySelector('.log-text');
      const pre = el('div', 'log-text', result.text || '(empty)');
      view().replaceChildren(pre);
      stick(was);
    } catch (error) {
      if (mode === 'agent') placeholder(`Could not read the log: ${error.message}`);
    } finally {
      busy.agent = false;
      button.disabled = false;
    }
  }

  function schedule() {
    clearInterval(timer);
    timer = null;
    if (!open) return;
    if (mode === 'control' && $('#log-follow').checked) {
      timer = setInterval(() => pollControl(), CONTROL_MS);
    } else if (mode === 'agent' && $('#agent-log-follow').checked) {
      timer = setInterval(() => pollAgent(), AGENT_MS);
    }
  }

  function setMode(next) {
    mode = next;
    document.querySelectorAll('.logs-tabs [data-logs]').forEach((b) =>
      b.classList.toggle('active', b.dataset.logs === mode));
    $('#logs-bar-control').classList.toggle('hidden', mode !== 'control');
    $('#logs-bar-agent').classList.toggle('hidden', mode !== 'agent');
    $('#logs-sub').textContent = mode === 'control'
      ? "This control plane's own log, live"
      : "An agent's log, read on its machine over SSH";
    view().replaceChildren();
    if (mode === 'control') pollControl(true);
    else { fillAgents(); pollAgent(); }
    schedule();
  }

  document.querySelectorAll('.logs-tabs [data-logs]').forEach((b) => {
    b.onclick = () => setMode(b.dataset.logs);
  });
  let filterTimer = null;
  $('#log-level').onchange = () => pollControl(true);
  $('#log-filter').oninput = () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => pollControl(true), 300);
  };
  $('#log-follow').onchange = schedule;
  $('#log-clear').onclick = () => view().replaceChildren();
  ['#agent-log-agent', '#agent-log-stream', '#agent-log-lines'].forEach((sel) => {
    $(sel).onchange = () => pollAgent();
  });
  $('#agent-log-follow').onchange = schedule;
  $('#agent-log-refresh').onclick = () => pollAgent();
  $('#log-download').onclick = (event) =>
    downloadFile('/api/logs/download', event.currentTarget);
  $('#agent-log-download').onclick = (event) => {
    const id = $('#agent-log-agent').value;
    if (!id) { toast('No agent selected', true); return; }
    const stream = $('#agent-log-stream').value;
    downloadFile(`/api/agents/${id}/logs/download?stream=${encodeURIComponent(stream)}`,
      event.currentTarget);
  };

  return {
    start() { open = true; setMode(mode); },
    stop() { open = false; schedule(); },
    // Takes effect when the tab opens, so only the agent's log is fetched.
    focusAgent(id) { mode = 'agent'; wantAgent = id; },
  };
})();

/*
 * Save a server response as a file. Fetched rather than linked so the bearer
 * token goes in a header: a plain <a href> cannot send one, and putting the
 * token in the URL would leave it in history and the server's access log.
 */
async function downloadFile(path, button) {
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = 'Downloading…'; }
  try {
    const headers = {};
    const current = token.get();
    if (current) headers.Authorization = `Bearer ${current}`;
    const response = await fetch(path, { headers });
    if (response.status === 401) { state.needsAuth = true; askForToken(); throw new Error('unauthorized'); }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error([payload.error, payload.detail].filter(Boolean).join('\n') ||
        `${response.status} ${response.statusText}`);
    }
    const disposition = response.headers.get('Content-Disposition') || '';
    const name = (disposition.match(/filename="([^"]+)"/) || [])[1] || 'log.txt';
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = el('a');
    link.href = url;
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    if (response.headers.get('X-Log-Truncated')) {
      toast(`${name}: the file was larger than 50 MB, so this is its last 50 MB`);
    }
  } catch (error) {
    toast(`Download failed: ${error.message}`, true);
  } finally {
    if (button) { button.disabled = false; button.textContent = label; }
  }
}

function openAgentLogs(agentId) {
  logsView.focusAgent(agentId);
  showView('logs');
}

$('#modal-close').onclick = closeModal;
$('#modal-back').onclick = (event) => { if (event.target.id === 'modal-back') closeModal(); };

$('#new-channel').onclick = () => {
  const form = formFrom('tpl-channel-form');
  form.onsubmit = async (event) => {
    event.preventDefault();
    try {
      const result = await api('/api/channels', {
        method: 'POST', body: JSON.stringify(values(form)),
      });
      closeModal();
      await load();
      selectChannel(result.channel.id);
    } catch (error) { toast(error.message, true); }
  };
  openModal('New channel', form);
};

$('#delete-channel').onclick = async () => {
  const channel = state.channels.find((c) => c.id === state.channelId);
  if (!channel || !confirm(`Delete #${channel.name} and its transcript?`)) return;
  await api(`/api/channels/${channel.id}`, { method: 'DELETE' });
  state.channelId = null;
  state.messages = [];
  state.members = [];
  await load();
  renderMessages();
  renderMembers();
  renderBoardHeader();
};

$('#clear-channel').onclick = async () => {
  const channel = state.channels.find((c) => c.id === state.channelId);
  if (!channel || !confirm(
    `Clear #${channel.name}?\n\nDeletes its messages and starts every member on a ` +
    'blank history. The channel and its members stay.')) return;
  try {
    const result = await api(`/api/channels/${channel.id}/clear`, { method: 'POST' });
    const notes = Object.entries(result.agents || {})
      .filter(([, how]) => how !== 'freed')
      .map(([name, how]) => `@${name}: ${how}`);
    toast(`#${channel.name} cleared (${result.messages_removed} messages)` +
          (notes.length ? ` — ${notes.join('; ')}` : ''));
  } catch (error) { toast(error.message, true); }
};

$('#halt-btn').onclick = async () => {
  try {
    await api(`/api/channels/${state.channelId}/halt`, {
      method: 'POST', body: JSON.stringify({ halted: !state.halted }),
    });
  } catch (error) { toast(error.message, true); }
};

$('#auto-approve').onchange = async (event) => {
  const level = event.target.value;
  try {
    await api(`/api/channels/${state.channelId}/auto-approve`, {
      method: 'POST', body: JSON.stringify({ level }),
    });
    state.autoApprove = level;
    renderBoardHeader();
  } catch (error) {
    toast(error.message, true);
    event.target.value = state.autoApprove;      // put it back
  }
};

$('#member-add-btn').onclick = async () => {
  const agentId = Number($('#member-select').value);
  if (!agentId) return;
  try {
    await api(`/api/channels/${state.channelId}/members`, {
      method: 'POST', body: JSON.stringify({ agent_id: agentId }),
    });
    await load();
    selectChannel(state.channelId);
  } catch (error) { toast(error.message, true); }
};

$('#new-machine').onclick = () => {
  const form = formFrom('tpl-machine-form');
  form.elements.name.oninput = (event) => event.target.setCustomValidity('');
  form.elements.is_local.onchange = (event) => {
    const local = event.target.checked;
    ['host', 'ssh_user', 'ssh_key', 'ssh_port'].forEach((name) => {
      form.elements[name].disabled = local;
    });
    if (local) form.elements.host.value = 'localhost';
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const field = form.elements.name;
    const wanted = field.value.trim();
    // Checked here for an instant answer; the server enforces it regardless.
    const clash = state.machines.find(
      (m) => m.name.toLowerCase() === wanted.toLowerCase());
    if (clash) {
      field.setCustomValidity(`'${clash.name}' is already registered`);
      field.reportValidity();
      toast(`A machine called '${clash.name}' is already registered ` +
            `(${clash.is_local ? 'this machine' : clash.host}). ` +
            `Machine names must be unique — they are half of an agent's address.`, true);
      return;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(wanted)) {
      field.setCustomValidity('letters, digits, - and _ only (no dots)');
      field.reportValidity();
      toast('A machine name becomes part of an agent address ' +
            '(<agent>.<machine>), so it cannot contain dots or spaces.', true);
      return;
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    button.innerHTML = '<span class="spin">↻</span> checking…';
    try {
      const result = await api('/api/machines', {
        method: 'POST', body: JSON.stringify(values(form)),
      });
      closeModal();
      toast(result.probe.ok
        ? `Registered ${result.machine.name} (${result.probe.os || 'unknown OS'})`
        : `Registered, but unreachable:\n${result.probe.error}`, !result.probe.ok);
      load();
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
      button.textContent = 'Register';
    }
  };
  openModal('Register machine', form);
};

$('#new-agent').onclick = () => {
  if (!state.machines.length) {
    toast('Register a machine first — agents are installed on a machine.', true);
    return;
  }
  const form = formFrom('tpl-agent-form');
  state.machines.forEach((machine) =>
    form.elements.machine_id.append(new Option(
      `${machine.name}${machine.status !== 'ready' ? ` (${machine.status})` : ''}`, machine.id)));
  // Scoped to the form, not the document: this runs before openModal() puts
  // the cloned template into the page, so a document query would find nothing
  // and throw — which killed the whole handler and left the button inert.
  const customRow = form.querySelector('#bind-host-custom-row');
  const rescan = form.querySelector('#bind-host-rescan');
  const hostNote = form.querySelector('#bind-host-note');

  const machineFor = () => state.machines.find(
    (m) => m.id === Number(form.elements.machine_id.value));

  const describe = (iface) => {
    const note = iface.note ? ` — ${iface.note}` : '';
    return `${iface.name} · ${iface.address}${note}`;
  };

  // The addresses come from the machine, so the picker is rebuilt whenever the
  // machine changes. They were collected when it was registered or last tested.
  const fillHosts = () => {
    const machine = machineFor();
    const list = machine?.interfaces || [];
    const select = form.elements.bind_host;
    const chosen = select.value;
    select.innerHTML = '';
    select.append(new Option('all interfaces (0.0.0.0)', '0.0.0.0'));
    list.forEach((iface) => select.append(new Option(describe(iface), iface.address)));
    select.append(new Option('other address…', '__custom__'));
    if (list.some((iface) => iface.address === chosen)) {
      select.value = chosen;               // keep the choice across a rescan
    } else if (list.length === 1) {
      // One obvious address and nothing else to weigh up: offer it rather than
      // making 0.0.0.0 the silent default on a single-homed box.
      select.value = list[0].address;
    }
    hostNote.textContent = list.length
      ? `${list.length} address(es) found on ${machine.name}`
      : `No addresses recorded for ${machine?.name || 'this machine'} — rescan to look.`;
    customRow.classList.toggle('hidden', select.value !== '__custom__');
  };

  /** Ask the machine what it has, now. */
  const rescanHosts = async () => {
    const machine = machineFor();
    if (!machine) return;
    rescan.disabled = true;
    hostNote.textContent = `Reading addresses from ${machine.name}…`;
    try {
      const result = await api(`/api/machines/${machine.id}/interfaces`,
                               { method: 'POST' });
      machine.interfaces = result.interfaces;      // local state, for this form
      fillHosts();
      if (!result.ok) hostNote.textContent = result.error || 'Machine unreachable.';
    } catch (error) {
      hostNote.textContent = `Could not read addresses: ${error.message}`;
    } finally {
      rescan.disabled = false;
    }
  };

  rescan.onclick = (event) => {
    event.preventDefault();
    rescanHosts();
  };
  form.elements.machine_id.onchange = () => {
    fillHosts();
    // A machine with nothing recorded predates address collection; go and look
    // rather than offering an empty picker.
    if (!(machineFor()?.interfaces || []).length) rescanHosts();
  };
  form.elements.bind_host.onchange = (event) => {
    customRow.classList.toggle('hidden', event.target.value !== '__custom__');
  };
  fillHosts();
  if (!(machineFor()?.interfaces || []).length) rescanHosts();

  form.elements.framework.value = state.settings.default_framework;
  form.elements.provider.value = state.settings.default_provider;
  form.elements.model.value = state.settings.default_model;

  // Provider → endpoint and model suggestions. The endpoint is only replaced
  // while it still holds what this form put there, so a typed one survives a
  // provider change; the model is suggested, never overwritten.
  const modelNote = form.querySelector('#model-note');
  const modelList = form.querySelector('#model-options');
  let filledEndpoint = '';
  let modelsTimer = null;
  let modelsAsked = 0;
  const fetchModels = async () => {
    const provider = form.elements.provider.value;
    modelList.innerHTML = '';
    if (!state.providers.listable.includes(provider)) {
      modelNote.textContent = '';
      return;
    }
    const asked = ++modelsAsked;
    modelNote.textContent = 'Reading models from the endpoint…';
    try {
      const result = await api('/api/models', {
        method: 'POST',
        body: JSON.stringify({
          provider,
          endpoint: form.elements.endpoint.value.trim(),
          api_key: form.elements.api_key.value,
        }),
      });
      if (asked !== modelsAsked) return;     // a newer request superseded this one
      result.models.forEach((id) => modelList.append(new Option(id, id)));
      modelNote.textContent = result.ok
        ? `${result.models.length} model(s) at ${result.endpoint} — type to filter`
        : result.error;
      if (result.ok && !form.elements.model.value.trim() && result.models.length === 1) {
        form.elements.model.value = result.models[0];   // a vLLM serving one model
      }
    } catch (error) {
      if (asked === modelsAsked) modelNote.textContent = `Could not list models: ${error.message}`;
    }
  };
  const loadModels = () => {
    clearTimeout(modelsTimer);
    modelsTimer = setTimeout(fetchModels, 400);
  };
  const applyProvider = () => {
    const field = form.elements.endpoint;
    const next = state.providers.endpoints[form.elements.provider.value] || '';
    if (!field.value.trim() || field.value.trim() === filledEndpoint) field.value = next;
    filledEndpoint = next;
    loadModels();
  };
  form.elements.provider.onchange = applyProvider;
  form.elements.endpoint.onchange = loadModels;
  form.elements.api_key.onchange = loadModels;
  applyProvider();
  form.elements.tools.value = '';
  form.elements.sdk_source.value = state.settings.default_sdk_source || 'local';
  form.elements.approval.checked = Boolean(state.settings.default_approval);

  const nameField = form.elements.name;
  nameField.oninput = () => nameField.setCustomValidity('');
  form.onsubmit = async (event) => {
    event.preventDefault();
    const wanted = nameField.value.trim();
    const machineId = Number(form.elements.machine_id.value);
    const machine = state.machines.find((m) => m.id === machineId);
    // Same rule as the server: an agent name is unique per machine, because
    // together they form the address the board dispatches to.
    const clash = state.agents.find(
      (a) => a.machine_id === machineId && a.name.toLowerCase() === wanted.toLowerCase());
    if (clash) {
      nameField.setCustomValidity(`'${fqn(clash)}' already exists`);
      nameField.reportValidity();
      toast(`'${fqn(clash)}' already exists. Agent names are unique per machine — ` +
            `pick another name, or delete that agent first.`, true);
      return;
    }
    const payload = values(form);
    if (payload.bind_host === '__custom__') {
      payload.bind_host = (payload.bind_host_custom || '').trim();
      if (!payload.bind_host) {
        toast('Pick an interface, or type the address to bind to.', true);
        return;
      }
    }
    delete payload.bind_host_custom;

    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    button.innerHTML = '<span class="spin">↻</span> provisioning…';
    try {
      const result = await api('/api/agents', {
        method: 'POST', body: JSON.stringify(payload),
      });
      closeModal();
      const problems = result.result.problems;
      toast(result.agent.ready
        ? `Created ${fqn(result.agent)} at ${result.agent.url}` +
          (result.result.sdk_source ? ` (${result.result.sdk_source})` : '')
        : `Installed ${result.agent.name}, but it cannot start:\n${
            Array.isArray(problems) ? problems.join('\n') : problems || 'unknown'}`,
        !result.agent.ready);
      load();
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
      button.textContent = 'Create';
    }
  };
  openModal('Create agent', form);
};

$('#settings-form').onsubmit = async (event) => {
  event.preventDefault();
  const payload = values($('#settings-form'));
  ['agent_timeout_seconds', 'approval_timeout_seconds', 'transcript_context_messages',
   'max_relay_hops', 'poll_interval_seconds', 'poll_max_interval_seconds',
   'poll_request_timeout_seconds'].forEach((key) => (payload[key] = Number(payload[key])));
  try {
    const result = await api('/api/settings', {
      method: 'PUT', body: JSON.stringify(payload),
    });
    state.settings = result.settings;
    toast('Settings saved');
  } catch (error) { toast(error.message, true); }
};

const input = $('#input');
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  mention.refresh();
});
input.addEventListener('keydown', (event) => {
  // While the @ list is open it owns Enter, Tab and the arrows; otherwise
  // Enter sends. Shift+Enter is always a newline.
  if (mention.open) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      mention.move(event.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault();
      mention.accept();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      mention.close();
      return;
    }
  }
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});
input.addEventListener('blur', () => setTimeout(() => mention.close(), 120));
input.addEventListener('click', () => mention.refresh());
$('#send').onclick = send;

/* ── @ typeahead ─────────────────────────────────────────────────────── */

const mention = {
  open: false,
  index: 0,
  matches: [],
  token: null,          // { start, end, query } of the @word at the caret

  /** The @word the caret sits in, or null. */
  tokenAt() {
    const caret = input.selectionStart;
    if (caret !== input.selectionEnd) return null;           // a selection, not a caret
    const before = input.value.slice(0, caret);
    // An @ only opens a mention at a word boundary, so an email address
    // or a mid-word @ does not trigger the list.
    const match = /(^|[\s(,:;!?])@([A-Za-z0-9_.-]*)$/.exec(before);
    if (!match) return null;
    const query = match[2];
    return { start: caret - query.length - 1, end: caret, query };
  },

  refresh() {
    const token = this.tokenAt();
    if (!token || !state.channelId) return this.close();
    const query = token.query.toLowerCase();
    const inChannel = new Set(state.members.map((m) => m.id));

    // Every agent is offered, not just this channel's members: looking for an
    // agent you have not added yet is exactly when you need the list. The ones
    // outside the channel are marked, and picking one joins it first.
    const hit = (agent) =>
      fqn(agent).toLowerCase().startsWith(query) ||
      agent.name.toLowerCase().startsWith(query);
    const loose = (agent) => fqn(agent).toLowerCase().includes(query);

    const members = state.agents.filter((a) => inChannel.has(a.id));
    const others = state.agents.filter((a) => !inChannel.has(a.id));
    let matches = [
      ...members.filter(hit).map((a) => ({ agent: a, member: true })),
      ...others.filter(hit).map((a) => ({ agent: a, member: false })),
    ];
    if (!matches.length) {
      matches = [
        ...members.filter(loose).map((a) => ({ agent: a, member: true })),
        ...others.filter(loose).map((a) => ({ agent: a, member: false })),
      ];
    }

    this.token = token;
    this.matches = matches.slice(0, 20);   // the list scrolls past a few
    this.index = 0;
    this.render();
  },

  render() {
    const pop = $('#mention-pop');
    pop.innerHTML = '';
    if (!this.matches.length) {
      // Say why there is nothing rather than showing an empty box.
      const note = el('li', 'none', state.agents.length
        ? 'no agent matches that'
        : 'no agents yet — create one on the Agents tab');
      pop.append(note);
      pop.classList.remove('hidden');
      this.open = false;                 // nothing to accept
      return;
    }
    this.matches.forEach((entry, i) => {
      const { agent, member } = entry;
      const item = el('li', i === this.index ? 'sel' : '');
      if (!member) item.classList.add('outside');
      item.setAttribute('role', 'option');
      item.append(el('span', pinClass(agent)));
      item.append(tint(el('span', 'name', '@' + fqn(agent)), agent));
      item.append(el('span', 'meta', member
        ? (agent.model || agent.framework || '')
        : '+ add to channel'));
      // mousedown, not click: blur would close the list first.
      item.onmousedown = (event) => {
        event.preventDefault();
        this.index = i;
        this.accept();
      };
      pop.append(item);
    });
    pop.classList.remove('hidden');
    this.open = true;
  },

  move(delta) {
    if (!this.matches.length) return;
    this.index = (this.index + delta + this.matches.length) % this.matches.length;
    this.render();
    $('#mention-pop').children[this.index]?.scrollIntoView({ block: 'nearest' });
  },

  accept() {
    const entry = this.matches[this.index];
    if (!entry || !this.token) return this.close();
    const { agent, member } = entry;
    const { start, end } = this.token;
    const value = input.value;
    // Always the fully qualified name: it is the unambiguous address, and two
    // machines may well run an agent with the same bare name.
    const insert = `@${fqn(agent)} `;
    input.value = value.slice(0, start) + insert + value.slice(end);
    const caret = start + insert.length;
    input.setSelectionRange(caret, caret);
    this.close();
    input.focus();

    // Picking an agent that is not in the channel adds it, so the mention you
    // just typed can actually be dispatched.
    if (!member) this.join(agent);
  },

  async join(agent) {
    const channelId = state.channelId;
    try {
      await api(`/api/channels/${channelId}/members`, {
        method: 'POST', body: JSON.stringify({ agent_id: agent.id }),
      });
      await load();
      if (state.channelId === channelId) await selectChannel(channelId);
      toast(`Added @${fqn(agent)} to this channel`);
    } catch (error) {
      toast(`Could not add @${fqn(agent)}: ${error.message}`, true);
    }
  },

  close() {
    this.open = false;
    this.matches = [];
    this.token = null;
    $('#mention-pop').classList.add('hidden');
  },
};

async function send() {
  const text = input.value.trim();
  if (!text || state.channelId == null) return;
  mention.close();
  input.value = '';
  input.style.height = 'auto';
  try {
    const result = await api(`/api/channels/${state.channelId}/messages`, {
      method: 'POST', body: JSON.stringify({ text }),
    });
    if (!result.dispatched.length && /@/.test(text) === false) {
      $('#mention-hint').textContent =
        'Posted to the board. Nothing was dispatched — address an agent with @name.';
      setTimeout(renderMentionHint, 4000);
    }
  } catch (error) {
    toast(error.message, true);
    input.value = text;
  }
}

/*
 * An exception inside a click handler is swallowed by the browser: the button
 * just does nothing, with no message anywhere a user would look. This surfaces
 * it instead — that is how "Create agent does nothing" presented, and it took a
 * code read rather than a glance to find.
 */
window.addEventListener('error', (event) => {
  toast(`Something broke: ${event.message}`, true);
});
window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  toast(`Something broke: ${reason?.message || reason}`, true);
});

$('#stale-close').onclick = () => $('#stale-banner').classList.add('hidden');

// Before anything else: an old process cannot serve this board correctly, and
// saying so once beats a 404 per screen.
checkServerVersion();

load().then(connect).catch((error) => {
  if (error.message !== 'unauthorized') toast(error.message, true);
});
