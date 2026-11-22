"""
Channel router — turns a board message into A2A calls, and streams what happens.

Addressing is IRC-style: a message is dispatched to the agents it ``@mentions``
and to nobody else. A line with no mention is posted to the board and costs
nothing, which is deliberate — fanning every idle remark out to every member
would spend tokens per member per message.

Agent-to-agent traffic goes through here too. An agent does not call its peers:
it writes ``@name.machine`` in its reply (or uses ``send_to_agent``, which only
posts to the board), and the board relays. Every hop is therefore one of this
router's own dispatches, which buys three things:

  - the transcript is the conversation, not a report of it;
  - the kill switch is authoritative — a halted channel refuses to relay, so
    nothing more is sent the moment you press Stop;
  - a hop budget is possible at all, because one place sees every hop.

Dispatch uses A2A's **submit-and-poll** (``return_immediately`` + ``tasks/get``)
rather than an SSE stream. A scalar httpx timeout on a stream limits *silence*,
not duration, and a single model call is silent — so a turn that was working
perfectly died at the read timeout, taking any tool-approval request with it.
Polling holds nothing open: the agent can think for twenty minutes, the board
sees ``working`` the whole time, and a restart loses no more than the poll loop.
"""

from __future__ import annotations

import asyncio
import re
import uuid

import httpx
from typing import Any, Dict, List, Optional, Set, Tuple

from google.protobuf.json_format import MessageToDict

from a2a.types import TaskState

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.tools.tool_registry import ToolRegistry
from declarative_agent_sdk.transports.a2a.client import AIAgentClient
from declarative_agent_sdk.agent_board.store import Store

logger = get_logger(__name__)

# An agent is addressed by its fully qualified name, "<agent>.<machine>", or by
# the bare agent name when that is unambiguous. The machine part is optional in
# the pattern, and the dot only counts when a name character follows it, so
# "ask @coder." keeps its full stop.
# Tools that run commands. Auto-approving these is a different decision from
# auto-approving a fetch or a sandboxed write, so 'safe' holds the line here.
SHELL_TOOLS = frozenset({
    "exec_command", "exec_async", "check_exec_async", "run_script",
})

# An @ that starts a word. `mahadev@10.0.0.147` is an SSH target, not a
# mention of "@10.0": answering it with "not in this channel" told the model
# the host did not exist, and it stopped reaching for its own tools.
MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_.@-])@([A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)?)")


def context_id_for(channel: Dict[str, Any], agent: Dict[str, Any]) -> str:
    """
    The A2A context id a channel's turns with one agent are filed under.

    The agent keeps one in-memory history per context id, so clearing is done
    by moving to a new id: "clear conversation" bumps the channel's epoch,
    "clear context" the agent's. That works on an agent of any age, with no
    agent-side support. At 0/0 the id is the original form, so a board that
    has never cleared anything keeps talking to the history it already has.
    """
    name = str(agent.get("qualified") or agent.get("name") or "")
    base = f"ac-{channel.get('name', channel.get('id', ''))}-{name}"
    c, a = int(channel.get("context_epoch") or 0), int(agent.get("context_epoch") or 0)
    return base if (c, a) == (0, 0) else f"{base}-{c}.{a}"


def mentions(text: str) -> List[str]:
    """Agent names addressed in a message, in order, de-duplicated."""
    seen: Set[str] = set()
    out: List[str] = []
    for name in MENTION_RE.findall(text or ""):
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def resolve_member(
    members: List[Dict[str, Any]], mention: str
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Match one @mention against a channel's members.

    Returns (member, error). A qualified name matches exactly. A bare name
    matches when exactly one member carries it; when several do, the message
    says which qualified names to pick from rather than guessing a machine.
    """
    wanted = (mention or "").lstrip("@").strip().lower()
    if not wanted:
        return None, "empty mention"

    for member in members:
        if str(member.get("qualified", "")).lower() == wanted:
            return member, ""

    by_bare = [m for m in members if str(m["name"]).lower() == wanted]
    if len(by_bare) == 1:
        return by_bare[0], ""
    if len(by_bare) > 1:
        options = ", ".join("@" + str(m["qualified"]) for m in by_bare)
        return None, (
            f"@{mention} is ambiguous in this channel — {len(by_bare)} agents are "
            f"called '{wanted}'. Address one of: {options}"
        )
    return None, f"@{mention} is not in this channel."


class EventBus:
    """Fan-out of control-plane events to connected browsers (SSE)."""

    def __init__(self, max_queue: int = 256) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: Dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A browser that cannot keep up loses events rather than
                # blocking a dispatch; it re-reads history on reconnect.
                logger.debug("dropping event for a slow subscriber")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class PendingApproval:
    """A tool call waiting on a human decision in the channel."""

    def __init__(
        self,
        channel_id: int,
        agent: str,
        tool: str,
        args: Dict[str, Any],
        task_id: str = "",
        context_id: str = "",
        function_id: str = "",
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.channel_id = channel_id
        self.agent = agent
        self.tool = tool
        self.args = args
        # What resuming the task needs. Held here because the decision is
        # awaited after the stream that carried the request has been closed.
        self.task_id = task_id
        self.context_id = context_id
        self.function_id = function_id
        self.decision: Optional[bool] = None
        self.event = asyncio.Event()

    def resolve(self, approved: bool) -> None:
        self.decision = approved
        self.event.set()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "agent": self.agent,
            "tool": self.tool,
            "args": self.args,
        }


class ChannelRouter:
    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus
        self.pending: Dict[str, PendingApproval] = {}
        self._tasks: Dict[int, Set[asyncio.Task]] = {}
        # (agent, channel) → turns in flight. Counted, because a relay can
        # start a second turn for the same pair before the first ends, and the
        # first one ending must not show the agent idle.
        self._turns: Dict[Tuple[str, int], int] = {}
        self._failure_hint = ""

    # ------------------------------------------------------------ messaging

    def _post(
        self,
        channel_id: int,
        sender_kind: str,
        sender: str,
        text: str,
        target: str = "",
    ) -> Dict[str, Any]:
        message = self.store.add_message(channel_id, sender_kind, sender, text, target)
        self.bus.publish({"type": "message", "channel_id": channel_id, "message": message})
        return message

    async def post_human(self, channel_id: int, text: str, sender: str = "you") -> Dict[str, Any]:
        """Record a human message and dispatch it to every agent it mentions."""
        message = self._post(channel_id, "human", sender, text)
        addressed = mentions(text)
        if not addressed:
            return {"message": message, "dispatched": []}

        if self.store.is_halted(channel_id):
            self._post(channel_id, "system", "system",
                       "Channel is halted — nothing was dispatched. Resume to continue.")
            return {"message": message, "dispatched": []}

        members = self.store.members(channel_id)
        dispatched = []
        for name in addressed:
            member, error = resolve_member(members, name)
            if member is None:
                self._post(channel_id, "system", "system", error)
                continue
            self._spawn(channel_id, self.dispatch(
                channel_id, member, text, sender,
                message_id=int(message["id"]),
            ))
            dispatched.append(member["qualified"])
        return {"message": message, "dispatched": dispatched}

    def working(self) -> Dict[str, List[int]]:
        """Agents taking a turn right now → the channels they are taking it in."""
        out: Dict[str, List[int]] = {}
        for (agent, channel_id) in self._turns:
            out.setdefault(agent, []).append(channel_id)
        return out

    # ------------------------------------------------------------- clearing

    async def _forget(self, agent: Dict[str, Any], session: str = "") -> str:
        """
        Ask an agent to drop a history it no longer needs. Best effort.

        The board has already moved to a new context id, so the next turn is
        clean either way; this frees the old one. An agent installed before
        the route existed answers 404/405 and keeps it until it restarts.
        """
        url = str(agent.get("url") or "").rstrip("/")
        if not url:
            return "no address"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.delete(
                    f"{url}/context", params={"session": session} if session else {},
                    timeout=5)
        except Exception as exc:  # noqa: BLE001 — a stopped agent has no history anyway
            return f"unreachable ({type(exc).__name__})"
        if response.status_code in (404, 405, 501):
            return "kept until it restarts (older agent)"
        return "freed" if response.status_code == 200 else f"answered {response.status_code}"

    async def clear_channel(self, channel_id: int) -> Dict[str, Any]:
        """
        Clear conversation: the transcript, and every member's history of it.

        Both halves matter. The board replays recent transcript into each
        prompt, and each agent remembers its own turns; clearing only one
        leaves the other to bring the old conversation back.
        """
        channel = self.store.channel(channel_id)
        if channel is None:
            raise LookupError("no such channel")
        members = self.store.members(channel_id)
        old = {int(m["id"]): context_id_for(channel, m) for m in members}
        removed = self.store.clear_messages(channel_id)
        self.store.bump_channel_context(channel_id)
        # Before the system line, so every open board empties its view and
        # then receives that line as the first message of the new transcript.
        self.bus.publish({"type": "cleared", "channel_id": channel_id})
        freed = await asyncio.gather(*(
            self._forget(m, old[int(m["id"])]) for m in members))
        self._post(channel_id, "system", "system",
                   "Conversation cleared. Members start from a blank history.")
        self.bus.publish({"type": "channels"})
        return {"channel": channel["name"], "messages_removed": removed,
                "agents": {str(m["qualified"]): f for m, f in zip(members, freed)}}

    async def clear_agent(self, agent_id: int) -> Dict[str, Any]:
        """Clear context: this agent forgets every channel's conversation."""
        agent = self.store.agent(agent_id)
        if agent is None:
            raise LookupError("no such agent")
        self.store.bump_agent_context(agent_id)
        freed = await self._forget(agent)
        self.bus.publish({"type": "agents"})
        return {"agent": agent["qualified"], "history": freed}

    def _spawn(self, channel_id: int, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.setdefault(channel_id, set()).add(task)
        task.add_done_callback(lambda t: self._tasks.get(channel_id, set()).discard(t))
        return task

    # ------------------------------------------------------------- dispatch

    def _prompt_for(
        self, channel_id: int, agent_name: str, text: str, sender: str,
        message_id: Optional[int] = None,
    ) -> str:
        """
        What the agent actually receives.

        The channel transcript is prepended because agents keep their history in
        memory and supervisord restarts them — without this, a bounced agent
        answers as if the conversation had just begun.
        """
        channel = self.store.channel(channel_id) or {}
        limit = int(self.store.setting("transcript_context_messages", 8) or 0)
        parts = [
            f"You are '{agent_name}' in the channel #{channel.get('name', '')}"
            + (f" (topic: {channel['topic']})" if channel.get("topic") else "")
            + ".",
            "",
            # Discovery, pushed: the roster costs nothing to include and is
            # current as of this turn, so an agent never has to ask who exists
            # before it can address anyone. The list_agents tool is for looking
            # again mid-task, or at agents outside this channel.
            self._roster_block(channel_id, agent_name),
            "",
            self._self_block(agent_name),
            "",
            "To bring in another member, write their @name in your reply — the "
            "board delivers it to them and their answer comes back here. You "
            "cannot call them directly, and you do not need to wait: finish "
            "your turn, and their reply will arrive as a new message. The "
            "send_to_agent tool does the same thing if you would rather make "
            "it an explicit call, and list_agents shows the current roster. "
            "Mention someone only when you actually need them; every mention "
            "costs a model call.",
        ]
        if limit:
            # The message being dispatched is already stored, and this prompt
            # appends it below as "<sender> says:". Replaying it in the history
            # too made every dispatch carry the question twice.
            transcript = self.store.recent_transcript(
                channel_id, limit, exclude_id=message_id)
            if transcript:
                parts.append(f"\nRecent channel history:\n{transcript}")
        parts.append(f"\n{sender} says:\n{text}")
        return "\n".join(parts)

    def _self_block(self, agent_name: str) -> str:
        """
        Where the agent runs and what its own tools reach.

        Without it the prompt describes the agent only as a channel member that
        talks through @mentions, and a model takes that literally: asked about
        its machine, it answered "I have no network or SSH access" while holding
        exec_command — then repeated it, since the transcript replays each turn.
        """
        agent = self.store.agent_by_qualified(agent_name) or {}
        machine = agent.get("machine_name") or ""
        extra = [t.strip() for t in str(agent.get("tools") or "").split(",") if t.strip()]
        # Core tools are registered by every runtime whatever the config lists.
        tools = list(dict.fromkeys(list(ToolRegistry.core_tool_names()) + extra))
        where = f"the machine '{machine}'" if machine else "your own machine"
        text = (
            f"You run on {where}. Your own tools ({', '.join(tools)}) act there: "
            "use them for anything about that machine, its files, or the hosts it "
            "can reach (a command such as ssh runs from it like any other). "
            "@mentions are only for the agents listed above — a hostname or "
            "user@host is not an agent, and the board will not deliver to it."
        )
        # Other agents cannot open your workspace, but they can fetch from it
        # by link. Say what this agent's links look like, so it can write one
        # instead of pasting a file.
        base = str(agent.get("url") or "").rstrip("/")
        if base:
            text += (
                f" Every file in your workspace is at {base}/files/<path inside "
                f"the workspace> — e.g. workspace/report.md is {base}/files/report.md. "
                "To give another agent a file, put its link in your reply; do "
                "not paste the file's contents."
            )
        text += (" Read another agent's file link (http://…/files/…) with "
                 "read_shared; to keep, edit or pass on that file, save it with "
                 "write_link instead of retyping it.")
        return text

    def _roster_block(self, channel_id: int, agent_name: str) -> str:
        """Who else is in this channel, as the agent sees it."""
        lines = []
        for member in self.store.members(channel_id):
            qualified = str(member.get("qualified") or member["name"])
            if qualified == agent_name:
                continue
            detail = member.get("description") or ""
            model = member.get("model") or member.get("framework") or ""
            suffix = f" — {detail}" if detail else (f" ({model})" if model else "")
            if not member.get("ready"):
                suffix += " [not ready]"
            lines.append(f"  @{qualified}{suffix}")
        if not lines:
            return "You are the only member of this channel."
        return "Other members of this channel:\n" + "\n".join(lines)

    async def relay(
        self,
        channel_id: int,
        sender: str,
        text: str,
        depth: int = 1,
        already: Optional[Set[str]] = None,
    ) -> List[str]:
        """
        Deliver an agent's mentions to the agents it named.

        This is the whole of agent-to-agent communication: the board reads the
        reply, finds the mentions and dispatches. A self-mention is dropped —
        it carries no information and would loop forever.

        ``already`` holds the targets this turn has contacted, and is both read
        and updated. One turn often names the same peer twice — once in the
        working text, once in the final answer — and each extra dispatch is a
        whole model call. Observed in practice: two dispatches 17 seconds
        apart, both spending the peer's full timeout. A genuinely new hand-off
        comes in a later turn, with a fresh set, so nothing is lost.
        """
        addressed = [m for m in mentions(text)]
        if not addressed:
            return []

        if self.store.is_halted(channel_id):
            self._post(channel_id, "system", "system",
                       "Channel is halted — nothing was relayed.")
            return []

        budget = int(self.store.setting("max_relay_hops", 0) or 0)
        if budget and depth > budget:
            self._post(
                channel_id, "system", "system",
                f"Relay stopped at hop {depth}: max_relay_hops is {budget}. "
                f"Raise it in Settings, or address the next agent yourself.",
            )
            return []

        members = self.store.members(channel_id)
        relayed = []
        for name in addressed:
            member, error = resolve_member(members, name)
            if member is None:
                # Said once, to the board: an agent mentioning something that is
                # not a member is usually prose, not an address.
                logger.info(f"relay: {error}")
                continue
            target = str(member.get("qualified") or member["name"])
            if target == sender:
                continue
            if already is not None and target in already:
                logger.info(
                    f"relay: @{target} already heard from @{sender} this turn; "
                    "not dispatching again"
                )
                continue
            if already is not None:
                already.add(target)
            self._spawn(
                channel_id,
                self.dispatch(channel_id, member, text, sender, depth=depth),
            )
            relayed.append(target)
        return relayed

    @staticmethod
    def _where_to_look(agent: Dict[str, Any]) -> str:
        """
        Where the real error is.

        A2A deliberately does not carry the exception text to clients — it
        routinely contains paths and prompt fragments — so a failure reaches
        the board as "the agent could not complete this request". The cause is
        in the agent's own log, and saying which file saves a hunt.
        """
        prefix = str(agent.get("prefix") or "")
        machine = str(agent.get("machine_name") or "")
        if not prefix:
            return ""
        where = f"{prefix}/logs/{agent['name']}.err.log"
        return f" — the reason is in {where}" + (f" on {machine}" if machine else "")

    async def dispatch(
        self, channel_id: int, agent: Dict[str, Any], text: str, sender: str,
        depth: int = 0, message_id: Optional[int] = None,
    ) -> None:
        """
        Run one turn against one agent, by submitting and polling.

        The budget bounds the *agent's* work; waiting for a human approval does
        not spend it, and nothing is held open while either happens.
        """
        name = str(agent.get("qualified") or agent["name"])
        url = str(agent.get("url") or "")
        if not url:
            self._post(channel_id, "system", "system",
                       f"@{name} has no A2A url recorded — recreate or refresh it.")
            return

        channel = self.store.channel(channel_id) or {}
        context_id = context_id_for(channel, agent)
        settings = self.store.settings()

        def number(key: str, fallback: float) -> float:
            # Not `settings.get(key) or fallback`: that turns a deliberate 0
            # into the fallback, so a budget of zero silently became 900.
            value = settings.get(key)
            return fallback if value is None else float(value)

        budget = number("agent_timeout_seconds", 1800)
        interval = number("poll_interval_seconds", 2)
        max_interval = number("poll_max_interval_seconds", 10)
        client = AIAgentClient(
            agent_url=url,
            poll_timeout=int(number("poll_request_timeout_seconds", 60)),
        )
        prompt = self._prompt_for(channel_id, name, text, sender, message_id)
        hint = self._where_to_look(agent)

        # Every approval this dispatch raised, so none is left pending if the
        # turn ends by another route. A stale entry would leave an Approve
        # button that answers nothing.
        raised: List[PendingApproval] = []
        last_status = ""
        posted: Set[str] = set()
        contacted: Set[str] = set()
        # Read once for the status/message decision; relay() re-reads, so a
        # membership change mid-turn still dispatches correctly.
        members = self.store.members(channel_id)

        key = (name, channel_id)
        self._turns[key] = self._turns.get(key, 0) + 1
        self.bus.publish({"type": "typing", "channel_id": channel_id, "agent": name, "on": True})
        try:
            task = await client.submit(prompt, context_id)
            deadline = asyncio.get_running_loop().time() + budget
            delay = interval

            while True:
                state = task.status.state
                parts = list(task.status.message.parts)

                if state == TaskState.TASK_STATE_COMPLETED:
                    answer = _part_text(parts) or _artifact_text(task)
                    said = await self._post_agent_text(
                        channel_id, name, answer, depth, posted, relayed=contacted)
                    if not said and not posted:
                        self._post(channel_id, "system", "system",
                                   f"@{name} finished without saying anything.")
                    return

                if state in (TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED):
                    self._post(channel_id, "system", "system",
                               f"@{name} failed: {_part_text(parts) or 'unknown error'}{hint}")
                    # Work in progress reaches the board as status, which is
                    # ephemeral. A turn that dies after the agent has written
                    # something substantial would otherwise leave no trace of
                    # it at all — and a mention inside it never relays.
                    return

                if state == TaskState.TASK_STATE_CANCELED:
                    self._post(channel_id, "system", "system", f"@{name} cancelled the task.")
                    return

                if state == TaskState.TASK_STATE_AUTH_REQUIRED:
                    self._post(channel_id, "system", "system",
                               f"@{name} needs authentication the board cannot provide.")
                    return

                if state == TaskState.TASK_STATE_INPUT_REQUIRED:
                    pending = self._register_approval(
                        channel_id, name, parts, task.id, task.context_id or context_id
                    )
                    if pending is None:
                        return
                    raised.append(pending)
                    approved = await self._await_decision(channel_id, name, pending)
                    task = await client.submit_tool_confirmation(
                        pending.task_id, pending.context_id, pending.function_id, approved,
                    )
                    # The human's thinking time is not the agent's budget.
                    deadline = asyncio.get_running_loop().time() + budget
                    delay = interval
                    continue

                # submitted / working / unspecified: keep looking
                status = _part_text(parts)
                if status and status != last_status:
                    last_status = status
                    if self._is_agent_talking(status, members):
                        await self._post_agent_text(
                            channel_id, name, status, depth, posted, relayed=contacted)
                    else:
                        self.bus.publish({
                            "type": "status", "channel_id": channel_id,
                            "agent": name, "text": status[:400],
                        })

                if asyncio.get_running_loop().time() > deadline:
                    self._post(
                        channel_id, "system", "system",
                        f"@{name} is still working after {budget:g}s, so the board "
                        f"stopped following task {task.id[:8]}. It has not been "
                        f"cancelled and may still finish — raise 'Agent reply "
                        f"timeout' in Settings, or message it again."
                    )
                    return

                await asyncio.sleep(delay)
                delay = min(delay * 1.4, max_interval)
                task = await client.get_task(task.id)

        except asyncio.CancelledError:
            self._post(channel_id, "system", "system", f"Dispatch to @{name} cancelled.")
            raise
        except Exception as exc:                       # noqa: BLE001 - surfaced in UI
            logger.exception(f"dispatch to '{name}' failed")
            self._post(channel_id, "system", "system",
                       self._explain_dispatch_failure(name, exc, budget) + hint)
        finally:
            for approval in raised:
                if self.pending.pop(approval.id, None) is not None:
                    self.bus.publish({
                        "type": "approval_resolved", "channel_id": channel_id,
                        "id": approval.id, "approved": False,
                    })
            left = self._turns.get(key, 1) - 1
            if left > 0:
                self._turns[key] = left
            else:
                self._turns.pop(key, None)
                self.bus.publish(
                    {"type": "typing", "channel_id": channel_id, "agent": name, "on": False}
                )

    # Longer than this and a WORKING update is the agent talking, not a
    # progress note. "Calling tools: web_request" is 30 characters; an outline
    # is thousands.
    SUBSTANTIVE_CHARS = 280

    def _is_agent_talking(
        self, text: str, members: List[Dict[str, Any]]
    ) -> bool:
        """
        Is this WORKING update the agent speaking, or a progress note?

        Two independent signals, because neither covers the other:

          - it addresses a member of the channel. "@reviewer.omarchy over to
            you" is thirty characters and is the most important thing an agent
            can say — missing it means the hand-off never happens;
          - it is long enough to be content. A finished outline mentions
            nobody, and losing it is just as bad.

        A mention only counts when it resolves to a member. Prose like "email
        me @ work" is not an address, and promoting on any @word would turn
        tool chatter into messages.
        """
        body = (text or "").strip()
        if len(body) >= self.SUBSTANTIVE_CHARS:
            return True
        return any(
            resolve_member(members, mention)[0] is not None
            for mention in mentions(body)
        )

    async def _post_agent_text(
        self,
        channel_id: int,
        name: str,
        text: str,
        depth: int,
        seen: Set[str],
        prefix: str = "",
        relayed: Optional[Set[str]] = None,
    ) -> bool:
        """
        Put an agent's own words on the board, once, and relay their mentions.

        The lean loop sends substantive text as a WORKING update whenever it
        expects to continue — so an answer, an outline, a hand-off to another
        agent all arrive as "status". Shown as a status line that is truncated
        and never stored, that text was lost and any @mention in it never
        relayed. Anything long enough to be the agent talking is therefore a
        message.

        Returns True when it posted. `seen` keeps the final answer from
        repeating what an intermediate update already said.
        """
        body = (text or "").strip()
        if not body:
            return False
        fingerprint = " ".join(body.split())[:2000]
        if fingerprint in seen:
            return False
        seen.add(fingerprint)
        self._post(channel_id, "agent", name, f"{prefix}{body}" if prefix else body)
        # The reply is also the agent's outbound mail: anyone it mentioned
        # hears from the board, one hop deeper.
        await self.relay(channel_id, name, body, depth=depth + 1, already=relayed)
        return True

    @staticmethod
    def _explain_dispatch_failure(name: str, exc: Exception, timeout: int) -> str:
        """
        Say what to do about it, not just what happened.

        A timeout here is the common one and the most misleading: the board
        stopped waiting, the agent did not stop working, and its answer is
        simply lost. The two timeouts also have to be ordered — this one covers
        a whole turn, while the agent's own request_timeout covers one model
        call — and a board limit at or below the agent's loses every long call.
        """
        kind = type(exc).__name__
        timed_out = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) \
            or "Timeout" in kind
        if timed_out:
            return (
                f"@{name} did not answer a poll within the HTTP timeout. The "
                f"agent is probably alive but its process is blocked — check "
                f"that it is running, and that 'Poll request timeout' in "
                f"Settings is long enough for it to answer tasks/get."
            )
        return f"@{name} could not be reached: {kind}: {exc}"

    def _register_approval(
        self,
        channel_id: int,
        name: str,
        parts,
        task_id: str,
        context_id: str,
    ) -> Optional[PendingApproval]:
        """Surface a tool-approval request in the channel. No waiting here."""
        function_id, tool_name, args = _approval_request(parts)
        if not function_id:
            # Silently skipping this left the agent waiting for an answer that
            # could never come, and the channel showing nothing at all.
            logger.warning(f"@{name}: INPUT_REQUIRED with no function id")
            self._post(
                channel_id, "system", "system",
                f"@{name} asked for approval but the request could not be read, "
                f"so there is nothing to approve. It is waiting; send it another "
                f"message to move it on.",
            )
            return None

        approval = PendingApproval(
            channel_id, name, tool_name, args,
            task_id=task_id, context_id=context_id, function_id=function_id,
        )
        self.pending[approval.id] = approval
        self._post(channel_id, "system", "system",
                   f"@{name} wants to run {tool_name}({_short_args(args)})")
        self.bus.publish({
            "type": "approval", "channel_id": channel_id, "approval": approval.as_dict()
        })
        return approval

    def _auto_decision(self, channel_id: int, approval: PendingApproval):
        """
        Answer without a human when the channel says to. None means ask.

        Per channel rather than per agent: `tools_approval_required: false` in
        agent.yaml is all-or-nothing, needs a restart, and travels with the
        agent to every channel it joins. This is revocable in one click and
        scoped to the conversation where you decided it.
        """
        level = self.store.auto_approve(channel_id)
        if level == "all":
            return True
        if level == "safe":
            return approval.tool not in SHELL_TOOLS
        return None

    async def _await_decision(
        self, channel_id: int, name: str, approval: PendingApproval
    ) -> bool:
        """
        Wait for the human. Nothing is held open while this blocks.

        A timeout counts as a denial: a tool call that nobody answered must not
        run just because the agent was still there, and must not hang forever
        either.
        """
        auto = self._auto_decision(channel_id, approval)
        if auto is not None:
            # Recorded, not silent: an approval nobody saw still belongs in the
            # transcript, and 'safe' refusing a shell call has to be visible or
            # it looks like the agent stalled.
            verdict = "auto-approved" if auto else (
                "auto-denied (shell commands still need a person)")
            self._post(channel_id, "system", "system",
                       f"{approval.tool} {verdict} for @{name}.")
            self.pending.pop(approval.id, None)
            self.bus.publish({
                "type": "approval_resolved", "channel_id": channel_id,
                "id": approval.id, "approved": auto,
            })
            return auto

        wait_for = int(self.store.setting("approval_timeout_seconds", 300))
        try:
            await asyncio.wait_for(approval.event.wait(), timeout=wait_for)
            approved = bool(approval.decision)
        except asyncio.TimeoutError:
            approved = False
            self._post(channel_id, "system", "system",
                       f"Approval for {approval.tool} timed out after {wait_for}s "
                       f"— denied. @{name} will be told no.")
        finally:
            self.pending.pop(approval.id, None)
            self.bus.publish({
                "type": "approval_resolved", "channel_id": channel_id,
                "id": approval.id, "approved": bool(approval.decision),
            })
        return approved

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        approval = self.pending.get(approval_id)
        if approval is None:
            return False
        approval.resolve(approved)
        return True

    # ----------------------------------------------------------- kill switch

    def halt(self, channel_id: int, halted: bool) -> Dict[str, Any]:
        """
        Stop (or resume) a channel.

        Cancels this process's in-flight dispatches at once, and refuses to
        relay anything further. Since every agent-to-agent hop is a dispatch
        from here, that is a real stop rather than a request to stop.
        """
        self.store.set_halted(channel_id, halted)
        cancelled = 0
        if halted:
            for task in list(self._tasks.get(channel_id, set())):
                if not task.done():
                    task.cancel()
                    cancelled += 1
            self._post(
                channel_id, "system", "system",
                f"Channel halted. {cancelled} in-flight dispatch(es) cancelled; "
                "no further message will be relayed until you resume.",
            )
        else:
            self._post(channel_id, "system", "system", "Channel resumed.")
        self.bus.publish({"type": "halt", "channel_id": channel_id, "halted": halted})
        return {"halted": halted, "cancelled": cancelled}

    def announce_auto_approve(self, channel_id: int, level: str) -> None:
        """Say so in the channel: changing who approves belongs in the record."""
        said = {
            "": "Tool approvals: asking every time.",
            "safe": "Tool approvals: auto-approving everything except shell "
                    "commands (exec_command, run_script).",
            "all": "Tool approvals: auto-approving everything, shell commands "
                   "included.",
        }[level]
        self._post(channel_id, "system", "system", said)

    # ------------------------------------------------- peer-reported traffic

    def record_peer_message(
        self, channel_id: int, sender: str, target: str, text: str, kind: str = "agent"
    ) -> Dict[str, Any]:
        """Append an agent-to-agent exchange that ``send_to_agent`` reported."""
        return self._post(channel_id, kind, sender, text, target=target)


# ---------------------------------------------------------------------------
# A2A event helpers (same shapes the example client handles)
# ---------------------------------------------------------------------------

def _unpack(event):
    if event.HasField("task"):
        task = event.task
        return (task.status.state, list(task.status.message.parts),
                task.id, task.context_id)
    if event.HasField("status_update"):
        update = event.status_update
        return (update.status.state, list(update.status.message.parts),
                update.task_id, update.context_id)
    return None, [], None, None


def _artifact_text(task) -> str:
    """
    Text from a completed task's artifacts.

    A2A lets an agent put its answer in an artifact instead of the final status
    message; the workflow executor does exactly that, so a board that only read
    status would show "finished without saying anything".
    """
    chunks = []
    for artifact in getattr(task, "artifacts", None) or []:
        for part in getattr(artifact, "parts", None) or []:
            if getattr(part, "text", ""):
                chunks.append(part.text)
    return "\n".join(chunks).strip()


def _part_text(parts) -> str:
    return "\n".join(p.text for p in (parts or []) if getattr(p, "text", "")).strip()


def _approval_request(parts) -> tuple:
    """(function_id, tool_name, args) from an INPUT_REQUIRED message."""
    for part in parts or []:
        if not part.HasField("data"):
            continue
        data = MessageToDict(part.data)
        if not isinstance(data, dict):
            continue
        request = data.get("function_response") or {}
        function_id = request.get("id")
        if function_id:
            # The executor flattens originalFunctionCall into name/args before
            # it goes on the wire (transports/a2a/executor.py); the nested form
            # is only a fallback for an agent that sends the raw shape.
            original = request.get("originalFunctionCall") or {}
            return (
                function_id,
                request.get("name") or original.get("name") or "a tool",
                request.get("args") or original.get("args") or {},
            )
    return None, "", {}


def _short_args(args: Dict[str, Any], limit: int = 160) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) <= limit else text[: limit - 1] + "…"
