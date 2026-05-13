import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest
from a2a.types.a2a_pb2 import StreamResponse
from typing import Any, AsyncIterator

from declarative_agent_sdk.agent_logging import get_logger
logger = get_logger(__name__)

class AIAgentClient():
    def __init__(self, agent_url: str, timeout: int = 300):
        self.agent_url = agent_url
        self.timeout = timeout

    async def run(self, query: str) -> AsyncIterator[StreamResponse]:
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

            # 3. Prepare the message
            message = new_text_message(query, role=Role.ROLE_USER)

            request = SendMessageRequest(message=message)

            # 4. Send the request and iterate the stream
            logger.info(f"Sending query: {query}")
            async for event in client.send_message(request):
                logger.debug(f"Received event: {event}")
                yield event

