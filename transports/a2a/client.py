import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message, new_message
from a2a.types.a2a_pb2 import GetTaskRequest, Role, SendMessageConfiguration, SendMessageRequest
from a2a.types.a2a_pb2 import StreamResponse, Task
from typing import Any, AsyncIterator, Optional
from a2a.types import Part, Message
from google.protobuf import struct_pb2

from declarative_agent_sdk.core.agent_logging import get_logger


def _data_value(data: dict) -> struct_pb2.Value:
    v = struct_pb2.Value()
    v.struct_value.update(data)
    return v
logger = get_logger(__name__)

class AIAgentClient():
    def __init__(self, agent_url: str, timeout: int = 300, poll_timeout: int = 60):
        """
        Args:
            timeout: HTTP timeout for the streaming call (pattern A). With a
                single scalar this is httpx's *read* timeout, i.e. the longest
                silence tolerated between SSE chunks, not a total duration.
            poll_timeout: HTTP timeout for submit and poll requests (pattern
                B). Those answer immediately, so it is short on purpose: a
                long-running task is waited for by polling, not by holding a
                request open.
        """
        self.agent_url = agent_url
        self.timeout = timeout
        self.poll_timeout = poll_timeout
        self._card: Optional[Any] = None

    async def send_message(
        self,
        message: Message
    ) -> AsyncIterator[StreamResponse]:
        async with httpx.AsyncClient(timeout=self.timeout) as httpx_client:
            # 1. Discover the agent (fetch Agent Card)
            logger.info(f"Discovering agent at: {self.agent_url}")
            resolver = A2ACardResolver(
                httpx_client=httpx_client,
                base_url=self.agent_url,
            )
            agent_card = await resolver.get_agent_card()
            logger.info(f"Connected to: {agent_card}")

            # 2. Create client — reuse the same httpx_client so the timeout applies
            config = ClientConfig(streaming=True, httpx_client=httpx_client)
            client = await create_client(agent=agent_card, client_config=config)
            assert client is not None, "Failed to create A2A client"

            request = SendMessageRequest(message=message)

            # 4. Send the request and iterate the stream
            logger.info(
                "Sending message: text=%s task_id=%s context_id=%s",
                message,
                message.task_id,
                message.context_id,
            )
            async for event in client.send_message(request):
                logger.debug(f"Received event: {event}")
                yield event

    async def run(self, query: str, context_id: str) -> AsyncIterator[StreamResponse]:
        message = new_text_message(text=query, role=Role.ROLE_USER)
        message.context_id = context_id
        async for event in self.send_message(message):
            yield event


    async def send_tool_confirmation(
        self,
        task_id: str,
        context_id: str,
        function_id: str,
        approved: bool = False
    ) -> AsyncIterator[StreamResponse]:
        unblock_message = new_message(
            parts=[Part(data=_data_value({
                "function_response": {
                    "id": function_id,
                    "name": "adk_request_confirmation",
                    "response": {"confirmed": approved},
                }
            }))],
            context_id=context_id,
            task_id=task_id,
            role=Role.ROLE_USER,
        )
        async for event in self.send_message(
            unblock_message
        ):
            yield event




    # ------------------------------------------------------------------
    # Pattern B: submit and poll
    #
    # Nothing is held open. The agent is asked to return as soon as it has a
    # task id (`return_immediately`), and its progress is read with
    # `tasks/get`. This is what a turn measured in minutes needs: an SSE
    # stream's read timeout is a limit on *silence*, and a single long model
    # call is silent — so pattern A fails a task that is working perfectly.
    # ------------------------------------------------------------------

    async def _session(self, http: httpx.AsyncClient):
        """A non-streaming client, reusing the agent card once fetched."""
        if self._card is None:
            resolver = A2ACardResolver(httpx_client=http, base_url=self.agent_url)
            self._card = await resolver.get_agent_card()
            logger.debug(f"resolved agent card for {self.agent_url}")
        client = await create_client(
            agent=self._card,
            client_config=ClientConfig(streaming=False, httpx_client=http),
        )
        assert client is not None, "Failed to create A2A client"
        return client

    async def _send_and_take_task(self, message: Message) -> Task:
        """Send one message non-blocking and return the Task it opened."""
        async with httpx.AsyncClient(timeout=self.poll_timeout) as http:
            client = await self._session(http)
            request = SendMessageRequest(
                message=message,
                configuration=SendMessageConfiguration(return_immediately=True),
            )
            last: Optional[Task] = None
            async for event in client.send_message(request):
                if event.HasField("task"):
                    last = event.task
                    break
                if event.HasField("status_update"):
                    # Some servers answer with an update before the task body;
                    # the id is what matters, so fetch the task itself.
                    task_id = event.status_update.task_id
                    if task_id:
                        return await self.get_task(task_id)
            if last is None:
                raise RuntimeError(
                    "agent accepted the message but returned no task — it may "
                    "not support return_immediately"
                )
            return last

    async def submit(self, query: str, context_id: str) -> Task:
        """Start a turn and return as soon as there is a task to poll."""
        message = new_text_message(text=query, role=Role.ROLE_USER)
        message.context_id = context_id
        return await self._send_and_take_task(message)

    async def submit_tool_confirmation(
        self,
        task_id: str,
        context_id: str,
        function_id: str,
        approved: bool = False,
    ) -> Task:
        """Answer a tool-approval request and return the task to keep polling."""
        message = new_message(
            parts=[Part(data=_data_value({
                "function_response": {
                    "id": function_id,
                    "name": "adk_request_confirmation",
                    "response": {"confirmed": approved},
                }
            }))],
            context_id=context_id,
            task_id=task_id,
            role=Role.ROLE_USER,
        )
        return await self._send_and_take_task(message)

    async def get_task(self, task_id: str, history_length: int = 0) -> Task:
        """Read a task's current state. Short request, by design."""
        async with httpx.AsyncClient(timeout=self.poll_timeout) as http:
            client = await self._session(http)
            return await client.get_task(
                GetTaskRequest(id=task_id, history_length=history_length)
            )
