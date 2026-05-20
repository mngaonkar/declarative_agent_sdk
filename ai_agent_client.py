import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message, new_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest
from a2a.types.a2a_pb2 import StreamResponse
from typing import Any, AsyncIterator
from a2a.types import Part
from google.protobuf import struct_pb2

from declarative_agent_sdk.agent_logging import get_logger


def _data_value(data: dict) -> struct_pb2.Value:
    v = struct_pb2.Value()
    v.struct_value.update(data)
    return v
logger = get_logger(__name__)

class AIAgentClient():
    def __init__(self, agent_url: str, timeout: int = 300):
        self.agent_url = agent_url
        self.timeout = timeout

    async def send_message(
        self,
        message: Message,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
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
            config = ClientConfig(streaming=False, httpx_client=httpx_client)
            client = await create_client(agent=agent_card, client_config=config)
            assert client is not None, "Failed to create A2A client"

            request = SendMessageRequest(message=message)

            # 4. Send the request and iterate the stream
            logger.info(
                "Sending message: text=%s task_id=%s context_id=%s",
                message,
                task_id,
                context_id,
            )
            async for event in client.send_message(request):
                logger.debug(f"Received event: {event}")
                yield event

    async def run(self, query: str) -> AsyncIterator[StreamResponse]:
        message = new_text_message(text=query, role=Role.ROLE_USER)
        async for event in self.send_message(message):
            yield event

    async def send_tool_approval(
        self,
        task_id: str,
        context_id: str,
        function_id: str
    ) -> AsyncIterator[StreamResponse]:
        unblock_message = new_message(
            parts=[Part(data=_data_value({
                "function_response": {
                    "id": function_id,
                    "name": "adk_request_confirmation",
                    "response": {"confirmed": True},
                }
            }))],
            context_id=context_id,
            task_id=task_id,
            role=Role.ROLE_USER,
        )
        async for event in self.send_message(
            unblock_message,
            task_id=task_id,
            context_id=context_id,
        ):
            yield event

    async def send_tool_denial(
        self,
        task_id: str,
        context_id: str,
        function_id: str
    ) -> AsyncIterator[StreamResponse]:
        unblock_message = new_message(
            parts=[Part(data=_data_value({
                "function_response": {
                    "id": function_id,
                    "name": "adk_request_confirmation",
                    "response": {"confirmed": False},
                }
            }))],
            context_id=context_id,
            task_id=task_id,
            role=Role.ROLE_USER,
        )
        async for event in self.send_message(
            unblock_message,
            task_id=task_id,
            context_id=context_id,
        ):
            yield event

