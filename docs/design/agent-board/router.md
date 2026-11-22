# agent-board router — design spec

Code: `agent_board/router.py`.

## `agent_board/router.py`

**Does:** `ChannelRouter` turns channel messages into agent turns over A2A,
relays agents' mentions to other agents, handles tool approvals and the kill
switch, and streams everything to browsers through `EventBus` (SSE).

**Rules — addressing:**

- ROUTER-ADDR-1: A message reaches only the agents it `@mentions`. A message
  with no mention is posted and costs nothing.
- ROUTER-ADDR-2: A mention is `@` at the start of a word followed by a name or
  `name.machine`. `user@host` is not a mention. Mentions are de-duplicated in
  order.
- ROUTER-ADDR-3: A mention is matched against the channel's members
  (`resolve_member`): exact qualified name, or a bare name that only one
  member has. A non-member mention from a person gets a system reply saying
  why; from an agent it is treated as prose and only logged.

**Rules — a turn:**

- ROUTER-TURN-1: Each turn is submit-and-poll (A2A-CLIENT-1): submit, then
  poll every `poll_interval_seconds` (growing to `poll_max_interval_seconds`)
  until the task finishes or `agent_timeout_seconds` (default 1800) runs out.
- ROUTER-TURN-2: Status updates show as "working" on the board. Updates long
  enough to be the agent talking (plans, hand-offs) are posted as its
  messages, once each, and their mentions are relayed. The final answer is
  posted unless it repeats what was already posted.
- ROUTER-TURN-3: A failed or timed-out turn posts a system message that says
  what to do (for example, which timeout to raise), not just the error.
- ROUTER-TURN-4: `working()` reports which agents are mid-turn in which
  channels, for the sidebar.

**Rules — the prompt an agent receives:**

- ROUTER-PROMPT-1: In order: "You are '<agent>' in the channel #<name>
  (topic)", the roster of other members with their descriptions, the self
  block, how to bring in another member, the last
  `transcript_context_messages` (default 8) channel lines, and "<sender>
  says: <text>". The message being sent is not repeated in the history.
- ROUTER-PROMPT-2: The self block names the agent's machine and its tools
  (core tools plus its extra `tools:`), says its tools act on that machine,
  says that hostnames and `user@host` are not agents, and gives the link form
  for its files (`<agent url>files/<path>` with an example). It says to read
  another agent's link with `read_shared` and to keep one with `write_link`.
- ROUTER-PROMPT-3: The agent is told to mention someone only when it needs
  them, since every mention costs a model call.

**Rules — agent-to-agent:**

- ROUTER-RELAY-1: An agent's posted text is its outbound mail: every member it
  mentions gets a turn, one hop deeper. This is the only agent-to-agent path;
  `send_to_agent` posts to the board and lands here too.
- ROUTER-RELAY-2: A self-mention is dropped.
- ROUTER-RELAY-3: Within one turn, a peer is dispatched to at most once, even
  if the agent names it in several updates.
- ROUTER-RELAY-4: A halted channel relays nothing and says so.
- ROUTER-RELAY-5: With `max_relay_hops` > 0, a relay deeper than that stops
  with a system message.

**Rules — tool approval:**

- ROUTER-APPR-1: A turn that asks for tool approval (INPUT_REQUIRED) creates
  a `PendingApproval` shown in the channel with the tool and its arguments.
  The answer is sent back to the same task and the turn continues.
- ROUTER-APPR-2: Channel auto-approve: `safe` approves everything except shell
  tools (`exec_command`, `exec_async`, `check_exec_async`, `run_script`);
  `all` approves everything. Auto-approvals are posted in the channel.
- ROUTER-APPR-3: No answer within `approval_timeout_seconds` (default 300)
  denies the call, and the channel is told.

**Rules — stop and clear:**

- ROUTER-HALT-1: `halt(channel, true)` cancels the channel's in-flight turns
  at once and refuses new dispatches and relays until resumed. Both are
  posted in the channel.
- ROUTER-CTX-1: An agent's conversation id for a channel is
  `ac-<channel>-<agent>`, with `-<channel epoch>.<agent epoch>` added once
  either counter is above 0. A new id means a fresh conversation.
- ROUTER-CTX-2: Clear conversation (`clear_channel`) deletes the channel's
  messages, bumps the channel counter, and asks every member to free its old
  history (`DELETE /context`). Clear context (`clear_agent`) bumps the agent
  counter and asks that agent to free all its history. Freeing is best
  effort: the new id already makes the next turn clean.

**Rules — peer log:**

- ROUTER-PEER-1: `record_peer_message` adds an agent-to-agent exchange that a
  tool reported to the transcript.

**Interface:** `post_human`, `dispatch`, `relay`, `resolve_approval`, `halt`,
`clear_channel`, `clear_agent`, `working`, `announce_auto_approve`,
`record_peer_message`; `context_id_for`, `mentions`, `resolve_member`;
`EventBus.subscribe/unsubscribe/publish`.

**Tests:** `tests/test_agent_board.py`.
