from enum import Enum
from a2a.types import Part
from google.protobuf.json_format import ParseDict
from google.protobuf import struct_pb2

def _data_part(data: dict) -> Part:
    return Part(data=ParseDict(data, struct_pb2.Value()))

class ResponseType(Enum):
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_INPUT_REQUIRED = "TASK_INPUT_REQUIRED"