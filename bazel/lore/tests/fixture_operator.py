from __future__ import annotations

from lumesof.lumeflow import opnet_types_pb2
from lumesof.lumeflow import Operator, operator_ports


@operator_ports(
    {
        "ingress": [
            {
                "name": "chunk",
                "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
                "type_url": "pkg.ChunkRequest",
            },
        ],
        "egress": [
            {
                "name": "store",
                "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
                "type_url": "pkg.StoreRequest",
            },
        ],
    }
)
class FixtureOperator(Operator):
    @Operator.on_ingress("chunk")
    def handle(self, *, input_port: str, message):
        del input_port
        return message


def main() -> int:
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
