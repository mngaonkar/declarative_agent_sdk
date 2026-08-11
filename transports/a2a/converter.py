from typing import Any

from a2a import types as A2ATypes
from google.genai import types as AdkTypes
from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)


class A2AConverter:
    def __init__(self):
        pass

    def convert_parts_to_a2a(self, parts: list[AdkTypes.Part]) -> list[A2ATypes.Part]:
        """Converts ADK parts to A2A parts."""
        a2a_parts = []
        for part in parts:
            if not part:
                continue
            if part.text:
                a2a_parts.append(A2ATypes.Part(text=part.text))
            elif part.function_call:
                v = struct_pb2.Value()
                v.struct_value.update({"function_call": part.function_call.model_dump()})
                a2a_parts.append(A2ATypes.Part(data=v))
            elif part.function_response:
                v = struct_pb2.Value()
                v.struct_value.update({"function_response": part.function_response.model_dump()})
                a2a_parts.append(A2ATypes.Part(data=v))
            else:
                logger.warning(f"Received part with unsupported type: {part}")
        return a2a_parts

    def convert_parts_to_adk(self, parts: list[A2ATypes.Part]) -> list[AdkTypes.Part]:
        """Converts A2A parts to ADK parts."""
        adk_parts = []
        for part in parts:
            if not part:
                continue
            if hasattr(part, "text") and part.text:
                adk_parts.append(AdkTypes.Part(text=part.text))
            elif hasattr(part, "data") and part.data:
                # part.data is a protobuf Value; MessageToDict unwraps it to a plain dict
                data_dict: Any = MessageToDict(part.data)
                if not isinstance(data_dict, dict):
                    logger.warning(f"Received data part with non-dict content: {part.data}")
                    continue
                fn_resp = data_dict.get("function_response") or data_dict.get("functionResponse")
                if isinstance(fn_resp, dict):
                    adk_parts.append(AdkTypes.Part(function_response=AdkTypes.FunctionResponse(
                        id=fn_resp.get("id", ""),
                        name=fn_resp.get("name", ""),
                        response=fn_resp.get("response", {}),
                    )))
                else:
                    logger.warning(f"Received data part with no function_response key: {data_dict}")
            else:
                logger.warning(f"Received part with unsupported type or missing data: {part}")
        return adk_parts
