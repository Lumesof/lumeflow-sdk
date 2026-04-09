# Writing an Operator

An **Operator** is a long-running async process that connects to an **OperatorSidecar** over gRPC, receives typed protobuf messages on named input ports, performs work, and emits protobuf messages on named output ports. The sidecar handles routing — the operator only deals with message logic.

---

## 1. Define your proto messages

Add your request/response message types to `lumecode/apps/lumeflow_rag/operators.proto`:

```proto
message MyRequest {
  string input = 1;
}

message MyResponse {
  string output = 1;
}
```

Then declare the proto library in `BUILD.bazel` (the shared `operators_py_pb2` target already covers all messages in `operators.proto`).

---

## 2. Implement the operator

Create `lumecode/apps/lumeflow_rag/my_operator.py`.

```python
import argparse
import asyncio
import logging
from typing import Optional

from example_apps.lumeflow_rag.common import operators_pb2
from lumecode.lumeflow.runtime.v1.operator.operator import Operator
from lumecode.lumeflow.runtime.v1.opnet.opnet_types_pb2 import Result

LOG = logging.getLogger(__name__)
on_ingress = Operator.on_ingress


class MyOperator(Operator):
    """One-line description of what this operator does."""

    def __init__(self) -> None:
        super().__init__()

    @on_ingress(
        "my_input_port",
    )
    async def async_handle(
        self,
        *,
        input_port: str,
        message: operators_pb2.MyRequest,
    ) -> Result:
        if not message.input:
            return Result(ok=False, message="input is required")

        # Do work.
        result_text = message.input.upper()

        await self.async_emit(
            output_port="my_output_port",
            message=operators_pb2.MyResponse(output=result_text),
        )
        return Result(ok=True, message="done")


async def _asyncMain(sidecar_uri: str) -> None:
    operator = MyOperator()
    await operator.async_runUntilStopped(sidecar_uri)


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Start a MyOperator and connect it to an OperatorSidecar."
    )
    parser.add_argument(
        "--sidecar-uri",
        default="tcp://127.0.0.1:50051",
        help="OperatorSidecar URI to connect to (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(_asyncMain(args.sidecar_uri))
    except KeyboardInterrupt:
        LOG.info("MyOperator interrupted, shutting down.")


if __name__ == "__main__":
    main()


__all__ = ["MyOperator"]
```

### Handler rules

The `@on_ingress` decorator registers a method as the handler for a named input port. The framework validates the signature at class definition time:

- All parameters after `self` must be **keyword-only** (`*` separator required).
- Exactly two keyword params: `input_port: str` and `message: <YourProtoType>`.
- Must return `Result`.
- One handler per port — registering two handlers for the same port raises `ValueError`.

The message schema is declared in `@operator_ports(...)` for the ingress port. The runtime uses that contract to unpack the `google.protobuf.Any` payload before calling your handler.

### Emitting messages

`async_emit` sends a proto message to an output port. The sidecar routes it to whatever node is linked on the other end.

```python
await self.async_emit(
    output_port="some_port",
    message=operators_pb2.SomeProto(...),
)
```

You can emit multiple times from a single handler (e.g., to batch results), or emit to different output ports.

### Startup hook

Override `async_onStart` to run code once after the sidecar connection is established:

```python
async def async_onStart(self) -> None:
    LOG.info("Connected, initializing resources.")
    self._client = await setup_client()
```

---

## 3. Add BUILD targets

In `lumecode/apps/lumeflow_rag/BUILD.bazel`, add a library, a binary, an OCI image, and an OCI publish target:

```starlark
lumesof_py_library(
    name = "my_operator",
    srcs = ["my_operator.py"],
    lumesof_deps = [
        "//lumecode/lumeflow/runtime/v1/operator:operator",
    ],
    std_deps = [
        "//example_apps/lumeflow_rag/common:operators_py_pb2",
    ],
    requirements = ["requirements.txt"],
    visibility = ["//visibility:public"],
)

lumesof_py_binary(
    name = "my_operator_bin",
    srcs = ["my_operator.py"],
    main = "my_operator.py",
    lumesof_deps = [
        ":my_operator",
    ],
    std_deps = [
        "//example_apps/lumeflow_rag/common:operators_py_pb2",
        "//lumecode/lumeflow/runtime/v1/operator:operator_py_pb2",
        "//lumecode/lumeflow/runtime/v1/operator:operator_py_grpc",
        "//lumecode/lumeflow/runtime/v1/opnet:opnet_core_py_pb2",
        "//lumecode/lumeflow/runtime/v1/opnet:opnet_core_py_grpc",
    ],
    pip_install = ":site_packages",
)

lumesof_oci_image(
    name = "my_operator_image",
    binary = ":my_operator_bin",
)

lumesof_oci_image_publish(
    name = "my_operator_publish",
    image = ":my_operator_image",
)
```

The extra `std_deps` on the binary (operator proto + gRPC stubs) are required at runtime even though they're transitive through the library — the binary target needs them bundled explicitly.

And a test target:

```starlark
lumesof_py_test(
    name = "my_operator_test",
    srcs = ["tests/my_operator_test.py"],
    main = "tests/my_operator_test.py",
    lumesof_deps = [
        ":my_operator",
    ],
    std_deps = [
        "//example_apps/lumeflow_rag/common:operators_py_pb2",
    ],
)
```

---

## 4. Testing

The testing pattern is straightforward: instantiate the operator directly, mock `async_emit` by replacing it, and call the handler method by name.

Create `lumecode/apps/lumeflow_rag/tests/my_operator_test.py`:

```python
import unittest
from typing import List, Tuple

from example_apps.lumeflow_rag.common import operators_pb2
from example_apps.lumeflow_rag.ingest.my_operator import MyOperator


class MyOperatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_response_on_valid_input(self):
        operator = MyOperator()
        emitted: List[Tuple[str, object]] = []

        async def fake_emit(*, output_port: str, message):
            emitted.append((output_port, message))

        operator.async_emit = fake_emit  # type: ignore[assignment]

        request = operators_pb2.MyRequest(input="hello")
        result = await operator.async_handle(
            input_port="my_input_port",
            message=request,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(emitted), 1)
        port, msg = emitted[0]
        self.assertEqual(port, "my_output_port")
        self.assertEqual(msg.output, "HELLO")

    async def test_returns_error_on_empty_input(self):
        operator = MyOperator()
        operator.async_emit = lambda **_: None  # type: ignore[assignment]

        result = await operator.async_handle(
            input_port="my_input_port",
            message=operators_pb2.MyRequest(input=""),
        )

        self.assertFalse(result.ok)
        self.assertIn("required", result.message)


if __name__ == "__main__":
    unittest.main()
```

Key points:
- Use `unittest.IsolatedAsyncioTestCase` for async handlers.
- Replace `async_emit` with a fake coroutine to capture emissions without needing a live sidecar.
- Call the handler method directly by its Python name — no sidecar connection required.
- Test error paths (empty/invalid input) separately from the happy path.

Run with Bazel:

```bash
bazel test //example_apps/lumeflow_rag:my_operator_test
```

---

## 5. Running locally

The operator connects to an OperatorSidecar, so you need a sidecar running before starting the operator. For local development the `operator_sidecar` binary in `lumecode/lumeflow/runtime/v1` provides a standalone sidecar.

Start the sidecar:

```bash
bazel run //lumecode/lumeflow/runtime/v1/sidecar:operator_sidecar -- --bind tcp://127.0.0.1:50051
```

Start the operator:

```bash
bazel run //example_apps/lumeflow_rag:my_operator_bin -- --sidecar-uri tcp://127.0.0.1:50051
```

The operator connects, registers its ports with the sidecar, and waits for incoming messages. You can send test messages using the `operator_sidecar_client` binary also in `lumecode/apps/lumeflow_rag`.

If your operator depends on external services (e.g., ChromaDB, Ollama), start them first with Docker Compose. See `lumecode/apps/lumeflow_rag/docker-compose.yaml` for an example that starts ChromaDB and Ollama together.

---

## 6. Packaging as a Docker container

The `lumesof_oci_image` and `lumesof_oci_image_publish` BUILD targets build and push the container image.

Build the image locally:

```bash
bazel build //example_apps/lumeflow_rag:my_operator_image
```

Push to the registry:

```bash
bazel run //example_apps/lumeflow_rag:my_operator_publish
```

The container's entrypoint is the binary built by `my_operator_bin`. Pass `--sidecar-uri` at runtime to point it at the sidecar:

```bash
docker run my_operator_image --sidecar-uri tcp://sidecar-host:50051
```

When running in a Docker network alongside other services, use the container's service name as the host:

```yaml
# docker-compose.yaml excerpt
services:
  my_operator:
    image: my_operator_image
    command: ["--sidecar-uri", "tcp://operator-sidecar:50051"]
    depends_on:
      - operator-sidecar
```
