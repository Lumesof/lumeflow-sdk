# Writing A Lumeflow Application

Last Updated: April 9, 2026

This guide explains how to design, build, package, test, and invoke a Lumeflow application.
It is written as a concept-first walkthrough, with code snippets only where they help clarify the model.

## 1) What A Lumeflow Application Is

A Lumeflow application is a directed dataflow graph.

- Operators are the nodes (processing steps).
- Links are the edges (data movement contracts between operators).
- A submitted graph becomes a job running in a Lumeflow cluster.

The cluster is responsible for launching operator instances, wiring connectivity, and delivering typed payloads along links.
Your application code focuses on message schemas, operator logic, and graph topology.

For runtime architecture background, see [ARCHITECTURE.md](./ARCHITECTURE.md).

## 2) Running Example: Even/Odd Classifier

This guide uses a simple app with two operators:

1. `even-odd-sorter`: receives a number and routes it to either `even_out` or `odd_out`.
2. `even-odd-verifier`: receives the routed number and emits a verdict (`pass`/`fail`).

### Sync Version (Request/Response)

```text
caller request
   |
   v
[SYNC_INJECTOR link: link-input]
   |
   v
even-odd-sorter (input)
   |                 \
   | even numbers      \ odd numbers
   v                    v
link-even             link-odd
   |                    |
   v                    v
even-odd-verifier (even_in / odd_in)
   |
   v
[SYNC_RETRIEVER link: link-output]
   |
   v
caller response
```

### Async Version (Fire-and-Process)

```text
producer/injector
   |
   v
[ASYNC_INJECTOR link: link-input]
   |
   v
even-odd-async-sorter
   |                 \
   v                  v
link-even           link-odd
   |                  |
   v                  v
even-odd-async-evaluator
   |
   v
(out-of-band effect, e.g. collector write/report)
```

### Message Types In This Example

- `NumberMessage`: input number for sync flow.
- `VerdictMessage`: sync response payload.
- `NumberWithPortMessage`: async payload that includes destination collector port.

Mapping to links in sync flow:

1. `link-input` (`SYNC_INJECTOR`) carries `NumberMessage` from caller into graph ingress.
2. `link-even` and `link-odd` carry `NumberMessage` between operators.
3. `link-output` (`SYNC_RETRIEVER`) carries `VerdictMessage` back to caller.

This mapping is the key to understanding Lumeflow: links are not only connections, they define external boundary semantics and payload contracts.

## 3) Sync vs Async Graphs (When To Use Which)

### Sync graphs

Use sync graphs when the caller needs a direct response to continue work.
Typical use cases include online agentic APIs, interactive workflows, and request/response services.

Important sync properties:

1. External ingress is represented by a `SYNC_INJECTOR` link.
2. External egress is represented by a `SYNC_RETRIEVER` link.
3. Jobs must carry a non-empty `app_id`.
4. `app_id` must be unique among active applications in the cluster.
5. The cluster uses `app_id` for bridge RPC routing to the correct active job.

In plain terms: sync graphs are for "send request, wait for answer" behavior.

### Async graphs

Use async graphs when eventual completion is sufficient and the caller should not block.
Typical use cases include ingestion pipelines, analysis jobs, enrichment tasks, and background fanout.

Important async properties:

1. External ingress is represented by an `ASYNC_INJECTOR` link.
2. There is no sync retriever path.
3. Work is injected as OpNet messages and processed asynchronously.
4. The graph itself does not provide per-item synchronous completion checks.
5. Completion is usually observed through out-of-band mechanisms (storage side effects, callbacks, logs, metrics, collector services).

In plain terms: async graphs are for "submit work, process eventually" behavior.

### Decision rule

- Choose sync when you need the response to complete the caller's next step.
- Choose async when eventual completion is enough and blocking would reduce throughput or user experience.

## 4) Recommended Authoring Workflow

Design first, then implement.

1. Define the processing steps as a graph: what operators exist and how data should flow.
2. Define message contracts for each edge (payload schemas).
3. Implement operators with explicit ingress/egress ports.
4. Build/package operators.
5. Instantiate operators and connect them in a graph class.
6. Build/package the graph.
7. Test operators and graph in standalone drivers.
8. Submit to cluster and invoke via sync bridge or async injection.

This is the same pattern used by production-oriented app packages such as `lumecode/apps/lumeflow_rag`.

## 5) Define Message Contracts

Define payload schemas first because they become the contracts for both ports and links.

Minimal proto set for this guide:

```proto
syntax = "proto3";

package example.even_odd.v1;

message NumberMessage {
  int64 number = 1;
}

message VerdictMessage {
  int64 number = 1;
  string verdict = 2;
}

message NumberWithPortMessage {
  int64 number = 1;
  int32 port = 2;
}
```

Guideline: one port should carry one payload schema contract.

## 6) Implement Operators

### Operator and Port Concepts

An operator is a processing unit implemented by subclassing `Operator`.
Ports are declared with `@operator_ports(...)` and handlers are bound with `@Operator.on_ingress(...)`.

Each port declaration includes:

1. `name`
2. direction (`ingress` or `egress`)
3. serialization format (`PROTO` today)
4. payload `type_url`

The runtime validates that:

1. every declared ingress port has a handler,
2. every handler corresponds to a declared ingress port,
3. payload typing is compatible when links are wired.

### Sync Sorter Example (Annotated)

```python
from google.protobuf.message import Message
from lumesof.lumeflow import Operator, Proto, operator_ports
from example.even_odd.v1 import even_odd_pb2

on_ingress = Operator.on_ingress


@operator_ports(
    {
        "ingress": [
            {
                "name": "input",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
        ],
        "egress": [
            {
                "name": "even_out",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
            {
                "name": "odd_out",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
        ],
    }
)
class EvenOddSyncSorterOperator(Operator):
    @on_ingress("input")
    async def handleInput(self, *, input_port: str, message: Message) -> None:
        if not isinstance(message, even_odd_pb2.NumberMessage):
            raise TypeError("Expected NumberMessage")
        outputPort = "even_out" if message.number % 2 == 0 else "odd_out"
        await self.async_emit(output_port=outputPort, message=message)
```

What to notice:

1. one ingress handler,
2. deterministic routing to one of two egress ports,
3. port contracts and handler behavior align exactly.

### Sync Verifier Example (Annotated)

```python
from google.protobuf.message import Message
from lumesof.lumeflow import Operator, Proto, operator_ports
from example.even_odd.v1 import even_odd_pb2

on_ingress = Operator.on_ingress


@operator_ports(
    {
        "ingress": [
            {
                "name": "even_in",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
            {
                "name": "odd_in",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
        ],
        "egress": [
            {
                "name": "output",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.VerdictMessage.DESCRIPTOR.full_name,
            },
        ],
    }
)
class EvenOddSyncVerifierOperator(Operator):
    @on_ingress("even_in")
    async def handleEvenInput(self, *, input_port: str, message: Message) -> None:
        await self._async_emitVerdict(message=message, expectedEven=True)

    @on_ingress("odd_in")
    async def handleOddInput(self, *, input_port: str, message: Message) -> None:
        await self._async_emitVerdict(message=message, expectedEven=False)

    async def _async_emitVerdict(self, *, message: Message, expectedEven: bool) -> None:
        if not isinstance(message, even_odd_pb2.NumberMessage):
            raise TypeError("Expected NumberMessage")
        verdict = "pass" if (message.number % 2 == 0) == expectedEven else "fail"
        await self.async_emit(
            output_port="output",
            message=even_odd_pb2.VerdictMessage(number=message.number, verdict=verdict),
        )
```

What to notice:

1. two ingress ports model two expected data paths,
2. each path is verified independently,
3. all results converge to one response port.

For complete concrete examples in this repo, see:

- `lumecode/lumeflow/runtime/v1/operator/tests/even_odd_sync_sorter_operator.py`
- `lumecode/lumeflow/runtime/v1/operator/tests/even_odd_sync_verifier_operator.py`

## 7) Package Operators With `lore_operator`

Use `lore_operator` (from `//bazel/lore:rules.bzl`) after you have:

1. a `lumesof_py_library` target for source,
2. a `lumesof_py_binary` target for runtime entrypoint.

Minimal pattern:

```bzl
load("//bazel/lore:rules.bzl", "lore_operator", "lore_operator_publish")

lore_operator(
    name = "even_odd_sync_sorter_operator",
    binary = ":even_odd_sync_sorter_operator_bin",
    operator_src = ":even_odd_sync_sorter_operator_lib",
    operator_class = "EvenOddSyncSorterOperator",
    publisher = "lumesof",
    slug = "even-odd-sync-sorter",
    description = "Routes numbers to even/odd ports",
    category = "demo",
    version = "0.1.0",
    visibility = "private",
    python_import_path = "example_apps.even_odd.image_descriptors.even_odd_sync_sorter_operator",
    target_visibility = ["//visibility:public"],
)

lore_operator_publish(
    name = "even_odd_sync_sorter_operator_publish",
    operator = ":even_odd_sync_sorter_operator",
)
```

### Artifacts produced by `lore_operator`

Per [bazel/lore/rules.bzl](/home/baba/lumesof/bazel/lore/rules.bzl), this flow produces:

1. an OCI image artifact for the operator binary,
2. OCI labels carrying LORe metadata,
3. a generated Python descriptor module exposing `_IMAGE_DESCRIPTOR` (`OperatorImageDescriptor`),
4. provider metadata consumed by graph rules.

That generated descriptor module is what graph code imports to instantiate operator nodes with accurate port metadata.

## 8) Define The Graph With Graph API

A `Graph` class defines topology, ingress/egress boundaries, and wiring.

### Sync graph example

```python
from lumesof.lumeflow import Graph, Proto, graph_type, materialize


@graph_type("sync")
class SyncEvenOddGraph(Graph):
    @materialize
    def buildDag(self) -> Proto.dag.Dag:
        self.setDagName("sync-even-odd-v1")

        # create operators from generated descriptor modules
        self.createOperatorFromImageDescriptor(name="even-odd-sorter", descriptor=sorterDescriptor)
        self.createOperatorFromImageDescriptor(name="even-odd-verifier", descriptor=verifierDescriptor)

        # external ingress -> sorter
        self.setIngress(
            to={"operator_name": "even-odd-sorter", "port_name": "input"},
            linkName="link-input",
        )

        # internal links
        self.connect(
            from_={"operator_name": "even-odd-sorter", "port_name": "even_out"},
            to={"operator_name": "even-odd-verifier", "port_name": "even_in"},
            linkName="link-even",
        )
        self.connect(
            from_={"operator_name": "even-odd-sorter", "port_name": "odd_out"},
            to={"operator_name": "even-odd-verifier", "port_name": "odd_in"},
            linkName="link-odd",
        )

        # verifier -> external egress
        self.setEgress(
            from_={"operator_name": "even-odd-verifier", "port_name": "output"},
            linkName="link-output",
        )

        return self.createDag()
```

Graph responsibilities to remember:

1. `@graph_type("sync"|"async")` defines boundary semantics.
2. `@materialize` marks the DAG builder method.
3. `setIngress` defines external entry path.
4. `setEgress` is sync-only and defines external response path.
5. `connect` wires internal operator-to-operator dataflow.

## 9) Package Graph With `lore_graph`

Use `lore_graph` to package graph library + publish pipeline metadata.

```bzl
load("//bazel/lore:rules.bzl", "lore_graph", "lore_graph_publish")

lore_graph(
    name = "sync_even_odd_graph",
    graph = "sync_even_odd_graph.py",
    graph_class = "SyncEvenOddGraph",
    operators = [
        ":even_odd_sync_sorter_operator",
        ":even_odd_sync_verifier_operator",
    ],
    target_visibility = ["//visibility:public"],
)

lore_graph_publish(
    name = "sync_even_odd_graph_publish",
    graph = ":sync_even_odd_graph",
)
```

### Artifacts produced by `lore_graph`

From [bazel/lore/rules.bzl](/home/baba/lumesof/bazel/lore/rules.bzl):

1. a Python graph library target,
2. extracted graph manifest metadata,
3. generated operator publish targets for listed operators,
4. generated publisher module `<target>_publisher` with async `Publisher.publish()`,
5. graph-info metadata target used by `lore_graph_publish`.

In practice, `lore_graph_publish` runs the graph dependency publish pipeline (operator dependency publishing + graph metadata context).

## 10) Invoke A Sync Graph Through The Bridge Client

For sync graphs, the caller sends request payload bytes and waits for response payload bytes.

```python
from lumesof.lumeflow import Client, Proto

sdk = Client(
    flowServerTarget="tcp://127.0.0.1:50070",
    configServiceTarget="tcp://127.0.0.1:50074",
)

job = await sdk.async_submitJob(syncSubmitRequest, startJob=True)
syncClient = job.syncClient()

responsePayload = await syncClient.async_call(
    payload=requestBytes,
    payloadTypeUrl=requestTypeUrl,
    payloadSerializationFormat=int(Proto.opnet_types.OpNetPayloadType.PROTO),
    timeoutMs=5000,
)
```

Operational notes:

1. `app_id` is required for sync jobs.
2. `app_id` is the routing key used by bridge infrastructure.
3. `app_id` must be unique among currently active jobs in the cluster.
4. `SYNC_INJECTOR` and `SYNC_RETRIEVER` links form the external request/response boundary.

## 11) Submit Work To An Async Graph

For async graphs, you submit/inject work and do not wait for direct per-item response.

```python
from lumesof.lumeflow import Client, Proto

sdk = Client(
    flowServerTarget="tcp://127.0.0.1:50070",
    configServiceTarget="tcp://127.0.0.1:50074",
)

job = await sdk.async_submitJob(asyncSubmitRequest, startJob=True)
await job.asyncClient().async_injectMessage(message=opnetMessage)
```

Operational notes:

1. Async jobs usually use empty `app_id`.
2. `ASYNC_INJECTOR` defines the external non-blocking ingress boundary.
3. The API provides eventual processing semantics, not sync request completion semantics.
4. Per-item completion is typically checked out-of-band (storage effects, report collectors, metrics, logs).

## 12) Standalone Testing Without Deploying Full App Stacks

Lumeflow SDK exposes test drivers through `lumesof.lumeflow.Test`.

### `StandaloneOperatorTestDriver`

Use this to validate one operator in isolation through standalone sidecar runtime.

You provide:

1. ingress generators (`IngressGenerator.generate(...)`),
2. egress verifiers (`EgressVerifier.verify(...)`),
3. operator image/name/node metadata.

Then call `await driver.async_run()`.

Reference test:

- `lumecode/lumeflow/runtime/v1/test_utils/tests/standalone_operator_sidecar_cartond_test.py`

### `StandaloneGraphTestDriver`

Use this for end-to-end graph tests on local-hosted runtime components.

Sync mode contract (`SyncGeneratorAndVerifier`):

1. `generate() -> Any | None`
2. `verify(message: Any) -> bool`

Async mode contract (`AsyncGeneratorAndVerifier`):

1. `startVerifier()`
2. `generate() -> Any | None`
3. `getVerificationResult() -> bool`

Behavior highlights:

1. Async verifier runs on a dedicated event loop thread.
2. Driver auto-cancels test jobs in cleanup paths.
3. Driver enforces graph/generator mode compatibility.
4. Driver can consume a generated graph publisher (`<graph>_publisher.Publisher`) via `BasePublisher`.

Reference test:

- `lumecode/lumeflow/runtime/v1/flow_server/tests/flow_job_lifecycle_test.py`

## 13) Bazel Command Cookbook

Build graph/operator targets (real repo targets):

```bash
bazel build //lumecode/apps/lumeflow_rag/ingest:async_ingest_light_graph
bazel build //lumecode/apps/lumeflow_rag/extract:sync_agent_light_graph
bazel build //lumecode/lumeflow/runtime/v1/operator:even_odd_sync_sorter_operator
```

Run graph publish pipelines:

```bash
bazel run //lumecode/apps/lumeflow_rag/ingest:async_ingest_light_graph_publish
bazel run //lumecode/apps/lumeflow_rag/extract:sync_agent_light_graph_publish
```

Run focused tests (operators, graphs, standalone drivers):

```bash
bazel test //lumecode/apps/lumeflow_rag/ingest:ingest_flow_light_test
bazel test //lumecode/apps/lumeflow_rag/extract:agent_flow_light_test
bazel test //lumecode/lumeflow/runtime/v1/operator:even_odd_operator_test
bazel test //lumecode/lumeflow/runtime/v1/test_utils:standalone_operator_sidecar_cartond_test
bazel test //lumecode/lumeflow/runtime/v1/flow_server:flow_job_lifecycle_test
```

## 14) Source References

Key files to study:

- Graph + operators in app package:
  - [lumecode/apps/lumeflow_rag/ingest/ingest_flow_light.py](/home/baba/lumesof/lumecode/apps/lumeflow_rag/ingest/ingest_flow_light.py)
  - [lumecode/apps/lumeflow_rag/extract/agent_flow_light.py](/home/baba/lumesof/lumecode/apps/lumeflow_rag/extract/agent_flow_light.py)
- LORe rule definitions:
  - [bazel/lore/rules.bzl](/home/baba/lumesof/bazel/lore/rules.bzl)
- Descriptor/publisher generators:
  - [bazel/lore/generate_image_descriptor_lib.py](/home/baba/lumesof/bazel/lore/generate_image_descriptor_lib.py)
  - [bazel/lore/generate_graph_publisher_lib.py](/home/baba/lumesof/bazel/lore/generate_graph_publisher_lib.py)
- Runtime architecture:
  - [ARCHITECTURE.md](/home/baba/lumesof/lumesof/lumeflow_documentation/ARCHITECTURE.md)

If you follow the sequence in this guide, you move from concept -> contracts -> operators -> packaged artifacts -> graph -> invocation -> verification with a clear mental model at each stage.

## 15) Annotation Deep Dive

This section expands on the annotations and class contracts that make Lumeflow code declarative.

### 15.1 `@operator_ports(...)`

`@operator_ports(...)` declares the public IO contract of an operator.

Why this matters:

1. It gives the runtime a machine-readable contract for wiring links.
2. It allows the graph author to discover valid ingress/egress ports.
3. It allows contract checks before runtime message processing.
4. It documents exactly what a component consumes and emits.

Contract fields and meaning:

1. `name`: symbolic identifier for a port, used in link wiring.
2. `serialization_format`: payload encoding type (for now typically `PROTO`).
3. `type_url`: schema identity of the payload type.
4. ingress vs egress: direction of the port and allowed operations.

Practical guidance:

1. Keep one schema per logical port.
2. Avoid overloading a single port with unrelated message unions.
3. Prefer explicit extra ports over one "multiplexed" mega-port.
4. Keep names aligned with business meaning (`raw_chunks`, `ranked_chunks`, `response`).

### 15.2 `@Operator.on_ingress("<port>")`

`@Operator.on_ingress("<port>")` binds a handler method to a declared ingress port.

Why this matters:

1. It is the runtime dispatch table for inbound messages.
2. It makes message handling per-port explicit and testable.
3. It avoids hidden runtime routing logic.

Common shape:

```python
@Operator.on_ingress("input")
async def handleInput(self, *, input_port: str, message: Message) -> None:
    ...
```

Behavioral expectations:

1. Validate message type quickly.
2. Perform deterministic processing.
3. Emit zero, one, or many outputs.
4. Fail fast on malformed payloads.

### 15.3 `@graph_type("sync" | "async")`

`@graph_type(...)` declares graph boundary semantics.

`sync` implies:

1. A request/response topology with retriever semantics.
2. Bridge-routed calls mapped by `app_id`.
3. Caller waits for a response payload.

`async` implies:

1. Fire-and-process topology.
2. Work submitted by injection semantics.
3. Completion observed through side effects/collectors.

### 15.4 `@materialize`

`@materialize` marks the method that constructs and returns a `Dag`.

Why this matters:

1. It gives rules and runtime a canonical graph-build entrypoint.
2. It ensures graph definition is explicit and reproducible.
3. It avoids side-channel DAG construction in constructors.

### 15.5 `on_delivery` Clarification

Operators can use delivery-related annotations/APIs where applicable in runtime behavior.
When you see operators using `on_delivery`, they are binding to runtime delivery events rather than regular ingress handlers.
In this authoring guide, we focus on `on_ingress` because it is the primary dataflow authoring path.
If your operator relies on delivery callbacks, document that explicitly in the operator contract and tests.

### 15.6 Annotation Design Checklist

Use this quick checklist before you package any operator:

1. Did you declare every ingress and egress port with `@operator_ports`?
2. Does every ingress port have exactly one `@on_ingress` handler?
3. Does every handler validate payload type before processing?
4. Are emitted payload schemas aligned with egress `type_url` definitions?
5. Are port names business-meaningful and consistent across graph links?
6. Are sync/async semantics reflected correctly at the graph boundary?

## 16) Full Sync App Walkthrough (Bottom-Up)

This section shows an end-to-end sync example with fuller code and narrative.

### 16.1 Proposed package layout

```text
example_apps/even_odd_sync/
  even_odd_pb2.py                 # generated from proto (checked in or generated in build)
  even_odd_sync_sorter_operator.py
  even_odd_sync_verifier_operator.py
  sync_even_odd_graph.py
  tests/
    even_odd_sync_sorter_operator_test.py
    even_odd_sync_verifier_operator_test.py
    sync_even_odd_graph_test.py
  BUILD.bazel
```

### 16.2 Proto schema (expanded)

```proto
syntax = "proto3";

package example.even_odd.v1;

message NumberMessage {
  int64 number = 1;
}

message VerdictMessage {
  int64 number = 1;
  string verdict = 2;
  string reason = 3;
}

message BatchRequest {
  repeated NumberMessage items = 1;
}

message BatchVerdict {
  repeated VerdictMessage items = 1;
}
```

Why include batch types in a simple app:

1. It demonstrates how schemas evolve over time.
2. It shows future extension points without changing core topology.
3. It encourages up-front contract design.

### 16.3 Sorter operator (fuller example)

```python
from google.protobuf.message import Message
from lumesof.lumeflow import Operator, Proto, operator_ports
from example.even_odd.v1 import even_odd_pb2

on_ingress = Operator.on_ingress


@operator_ports(
    {
        "ingress": [
            {
                "name": "input",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
        ],
        "egress": [
            {
                "name": "even_out",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
            {
                "name": "odd_out",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
        ],
    }
)
class EvenOddSyncSorterOperator(Operator):
    @on_ingress("input")
    async def handleInput(self, *, input_port: str, message: Message) -> None:
        if not isinstance(message, even_odd_pb2.NumberMessage):
            raise TypeError(f"Expected NumberMessage, got {type(message)}")

        outputPort = "even_out" if message.number % 2 == 0 else "odd_out"
        await self.async_emit(output_port=outputPort, message=message)
```

Implementation notes:

1. No hidden mutable state for deterministic routing.
2. Immediate type checking gives clearer operator failures.
3. Emits exactly one output per input in this model.

### 16.4 Verifier operator (fuller example)

```python
from google.protobuf.message import Message
from lumesof.lumeflow import Operator, Proto, operator_ports
from example.even_odd.v1 import even_odd_pb2

on_ingress = Operator.on_ingress


@operator_ports(
    {
        "ingress": [
            {
                "name": "even_in",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
            {
                "name": "odd_in",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberMessage.DESCRIPTOR.full_name,
            },
        ],
        "egress": [
            {
                "name": "output",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.VerdictMessage.DESCRIPTOR.full_name,
            },
        ],
    }
)
class EvenOddSyncVerifierOperator(Operator):
    @on_ingress("even_in")
    async def handleEvenInput(self, *, input_port: str, message: Message) -> None:
        await self._async_emitVerdict(message=message, expectedEven=True)

    @on_ingress("odd_in")
    async def handleOddInput(self, *, input_port: str, message: Message) -> None:
        await self._async_emitVerdict(message=message, expectedEven=False)

    async def _async_emitVerdict(self, *, message: Message, expectedEven: bool) -> None:
        if not isinstance(message, even_odd_pb2.NumberMessage):
            raise TypeError(f"Expected NumberMessage, got {type(message)}")

        isEven = message.number % 2 == 0
        passes = isEven == expectedEven
        verdict = "pass" if passes else "fail"
        reason = f"number={message.number}, expectedEven={expectedEven}, isEven={isEven}"

        await self.async_emit(
            output_port="output",
            message=even_odd_pb2.VerdictMessage(
                number=message.number,
                verdict=verdict,
                reason=reason,
            ),
        )
```

Implementation notes:

1. Shared helper avoids duplicated verdict logic.
2. `reason` string improves debuggability in integration runs.
3. Explicit `expectedEven` expresses link semantics.

### 16.5 Sync graph definition (fuller example)

```python
from lumesof.lumeflow import Graph, Proto, graph_type, materialize
from example_apps.even_odd_sync.image_descriptors import (
    even_odd_sync_sorter_operator as sorter_module,
)
from example_apps.even_odd_sync.image_descriptors import (
    even_odd_sync_verifier_operator as verifier_module,
)


@graph_type("sync")
class SyncEvenOddGraph(Graph):
    @materialize
    def buildDag(self) -> Proto.dag.Dag:
        self.setDagName("sync-even-odd-v1")

        sorter = self.createOperatorFromImageDescriptor(
            name="even-odd-sorter",
            descriptor=sorter_module._IMAGE_DESCRIPTOR,
        )
        verifier = self.createOperatorFromImageDescriptor(
            name="even-odd-verifier",
            descriptor=verifier_module._IMAGE_DESCRIPTOR,
        )

        self.setIngress(
            to={"operator_name": sorter.name, "port_name": "input"},
            linkName="link-input",
        )
        self.connect(
            from_={"operator_name": sorter.name, "port_name": "even_out"},
            to={"operator_name": verifier.name, "port_name": "even_in"},
            linkName="link-even",
        )
        self.connect(
            from_={"operator_name": sorter.name, "port_name": "odd_out"},
            to={"operator_name": verifier.name, "port_name": "odd_in"},
            linkName="link-odd",
        )
        self.setEgress(
            from_={"operator_name": verifier.name, "port_name": "output"},
            linkName="link-output",
        )

        return self.createDag()
```

Why this is a good starter graph:

1. One ingress, one egress, and two internal links.
2. Branching and reconverging topology is easy to visualize.
3. It exercises both operator dispatch and link correctness.

### 16.6 Sync BUILD.bazel example (fuller)

```bzl
load("//bazel/lore:rules.bzl", "lore_graph", "lore_graph_publish", "lore_operator", "lore_operator_publish")
load("//bazel/python:rules.bzl", "lumesof_py_library", "lumesof_py_test")

lumesof_py_library(
    name = "even_odd_sync_sorter_operator_lib",
    srcs = ["even_odd_sync_sorter_operator.py"],
    lumesof_deps = [
        "//lumesof:lumeflow",
    ],
    visibility = ["//visibility:public"],
)

lumesof_py_library(
    name = "even_odd_sync_verifier_operator_lib",
    srcs = ["even_odd_sync_verifier_operator.py"],
    lumesof_deps = [
        "//lumesof:lumeflow",
    ],
    visibility = ["//visibility:public"],
)

lore_operator(
    name = "even_odd_sync_sorter_operator",
    binary = ":even_odd_sync_sorter_operator_bin",
    operator_src = ":even_odd_sync_sorter_operator_lib",
    operator_class = "EvenOddSyncSorterOperator",
    publisher = "lumesof",
    slug = "even-odd-sync-sorter",
    description = "Routes numbers to even/odd ports",
    category = "demo",
    version = "0.1.0",
    visibility = "private",
    python_import_path = "example_apps.even_odd_sync.image_descriptors.even_odd_sync_sorter_operator",
    target_visibility = ["//visibility:public"],
)

lore_operator_publish(
    name = "even_odd_sync_sorter_operator_publish",
    operator = ":even_odd_sync_sorter_operator",
)

lore_operator(
    name = "even_odd_sync_verifier_operator",
    binary = ":even_odd_sync_verifier_operator_bin",
    operator_src = ":even_odd_sync_verifier_operator_lib",
    operator_class = "EvenOddSyncVerifierOperator",
    publisher = "lumesof",
    slug = "even-odd-sync-verifier",
    description = "Verifies parity path correctness",
    category = "demo",
    version = "0.1.0",
    visibility = "private",
    python_import_path = "example_apps.even_odd_sync.image_descriptors.even_odd_sync_verifier_operator",
    target_visibility = ["//visibility:public"],
)

lore_operator_publish(
    name = "even_odd_sync_verifier_operator_publish",
    operator = ":even_odd_sync_verifier_operator",
)

lumesof_py_library(
    name = "sync_even_odd_graph_lib",
    srcs = ["sync_even_odd_graph.py"],
    lumesof_deps = [
        "//lumesof:lumeflow",
    ],
    deps = [
        ":even_odd_sync_sorter_operator",
        ":even_odd_sync_verifier_operator",
    ],
    visibility = ["//visibility:public"],
)

lore_graph(
    name = "sync_even_odd_graph",
    graph = "sync_even_odd_graph.py",
    graph_class = "SyncEvenOddGraph",
    operators = [
        ":even_odd_sync_sorter_operator",
        ":even_odd_sync_verifier_operator",
    ],
    target_visibility = ["//visibility:public"],
)

lore_graph_publish(
    name = "sync_even_odd_graph_publish",
    graph = ":sync_even_odd_graph",
)

lumesof_py_test(
    name = "sync_even_odd_graph_test",
    srcs = ["tests/sync_even_odd_graph_test.py"],
    lumesof_deps = [
        "//lumesof:lumeflow",
    ],
    deps = [
        ":sync_even_odd_graph",
    ],
)
```

### 16.7 Sync invocation call flow

End-to-end request sequence:

1. Caller serializes `NumberMessage(number=7)`.
2. Bridge calls sync injector on `link-input`.
3. Sorter routes to `odd_out`.
4. Link `link-odd` delivers to verifier `odd_in`.
5. Verifier emits `VerdictMessage(number=7, verdict="pass", ...)`.
6. `link-output` retriever returns bytes to bridge.
7. Caller deserializes verdict and handles result.

### 16.8 Sync standalone graph test pattern

```python
from lumesof.lumeflow import Test
from example.even_odd.v1 import even_odd_pb2


class SyncInputOutputGenerator(Test.SyncGeneratorAndVerifier):
    def __init__(self) -> None:
        self._next = 0
        self._limit = 10

    async def generate(self):
        if self._next >= self._limit:
            return None
        value = self._next
        self._next += 1
        return even_odd_pb2.NumberMessage(number=value)

    async def verify(self, message) -> bool:
        if not isinstance(message, even_odd_pb2.VerdictMessage):
            return False
        expected = "pass"
        return message.verdict == expected
```

Why this contract is useful:

1. It models iterative request/response workloads.
2. It gives a clear boolean correctness signal per response.
3. It keeps test authoring lightweight for sync graphs.

## 17) Full Async App Walkthrough (Bottom-Up)

This section mirrors the sync walkthrough with async graph semantics.

### 17.1 Async graph mental model

Async graph processing is submit-and-process:

1. Inject message.
2. Runtime routes through graph.
3. Operators perform work.
4. Completion is observed externally.

Typical external observers:

1. DB write count.
2. Collector endpoint receipts.
3. Queue sink size.
4. Report file artifacts.
5. Metrics/log assertions.

### 17.2 Async operators (example)

```python
from google.protobuf.message import Message
from lumesof.lumeflow import Operator, Proto, operator_ports
from example.even_odd.v1 import even_odd_pb2

on_ingress = Operator.on_ingress


@operator_ports(
    {
        "ingress": [
            {
                "name": "input",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberWithPortMessage.DESCRIPTOR.full_name,
            },
        ],
        "egress": [
            {
                "name": "even_out",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberWithPortMessage.DESCRIPTOR.full_name,
            },
            {
                "name": "odd_out",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberWithPortMessage.DESCRIPTOR.full_name,
            },
        ],
    }
)
class EvenOddAsyncSorterOperator(Operator):
    @on_ingress("input")
    async def handleInput(self, *, input_port: str, message: Message) -> None:
        if not isinstance(message, even_odd_pb2.NumberWithPortMessage):
            raise TypeError(f"Expected NumberWithPortMessage, got {type(message)}")

        outputPort = "even_out" if message.number % 2 == 0 else "odd_out"
        await self.async_emit(output_port=outputPort, message=message)
```

```python
from google.protobuf.message import Message
from lumesof.lumeflow import Operator, Proto, operator_ports
from example.even_odd.v1 import even_odd_pb2

on_ingress = Operator.on_ingress


@operator_ports(
    {
        "ingress": [
            {
                "name": "even_in",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberWithPortMessage.DESCRIPTOR.full_name,
            },
            {
                "name": "odd_in",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.NumberWithPortMessage.DESCRIPTOR.full_name,
            },
        ],
        "egress": [
            {
                "name": "collector_out",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": even_odd_pb2.VerdictMessage.DESCRIPTOR.full_name,
            },
        ],
    }
)
class EvenOddAsyncEvaluatorOperator(Operator):
    @on_ingress("even_in")
    async def handleEvenInput(self, *, input_port: str, message: Message) -> None:
        await self._async_emitVerdict(message=message, expectedEven=True)

    @on_ingress("odd_in")
    async def handleOddInput(self, *, input_port: str, message: Message) -> None:
        await self._async_emitVerdict(message=message, expectedEven=False)

    async def _async_emitVerdict(self, *, message: Message, expectedEven: bool) -> None:
        if not isinstance(message, even_odd_pb2.NumberWithPortMessage):
            raise TypeError(f"Expected NumberWithPortMessage, got {type(message)}")

        isEven = message.number % 2 == 0
        verdict = "pass" if isEven == expectedEven else "fail"
        await self.async_emit(
            output_port="collector_out",
            message=even_odd_pb2.VerdictMessage(
                number=message.number,
                verdict=verdict,
                reason=f"async path check, expectedEven={expectedEven}, isEven={isEven}",
            ),
        )
```

### 17.3 Async graph definition (example)

```python
from lumesof.lumeflow import Graph, Proto, graph_type, materialize
from example_apps.even_odd_async.image_descriptors import (
    even_odd_async_evaluator_operator as evaluator_module,
)
from example_apps.even_odd_async.image_descriptors import (
    even_odd_async_sorter_operator as sorter_module,
)


@graph_type("async")
class AsyncEvenOddGraph(Graph):
    @materialize
    def buildDag(self) -> Proto.dag.Dag:
        self.setDagName("async-even-odd-v1")

        sorter = self.createOperatorFromImageDescriptor(
            name="even-odd-async-sorter",
            descriptor=sorter_module._IMAGE_DESCRIPTOR,
        )
        evaluator = self.createOperatorFromImageDescriptor(
            name="even-odd-async-evaluator",
            descriptor=evaluator_module._IMAGE_DESCRIPTOR,
        )

        self.setAsyncIngress(
            to={"operator_name": sorter.name, "port_name": "input"},
            linkName="link-input",
        )
        self.connect(
            from_={"operator_name": sorter.name, "port_name": "even_out"},
            to={"operator_name": evaluator.name, "port_name": "even_in"},
            linkName="link-even",
        )
        self.connect(
            from_={"operator_name": sorter.name, "port_name": "odd_out"},
            to={"operator_name": evaluator.name, "port_name": "odd_in"},
            linkName="link-odd",
        )

        return self.createDag()
```

### 17.4 Async standalone verification pattern

The async driver separates generation from verification lifecycle:

1. Start verifier service/collector once.
2. Generate messages until exhaustion.
3. Query verifier result at end.

```python
from lumesof.lumeflow import Test
from example.even_odd.v1 import even_odd_pb2


class AsyncStreamGenerator(Test.AsyncGeneratorAndVerifier):
    def __init__(self) -> None:
        self._next = 0
        self._limit = 50
        self._seen = 0

    async def startVerifier(self) -> None:
        # Start collector server / side channel checker here.
        self._seen = 0

    async def generate(self):
        if self._next >= self._limit:
            return None
        value = self._next
        self._next += 1
        return even_odd_pb2.NumberWithPortMessage(number=value, port=1)

    async def getVerificationResult(self) -> bool:
        # Replace with real collector result check.
        return True
```

### 17.5 Async failure modes to test

Include these scenarios in async tests:

1. malformed payload types,
2. missing required fields,
3. very large payload size,
4. duplicate injections,
5. evaluator restarts mid-stream,
6. collector temporarily unavailable,
7. backpressure on downstream sink.

## 18) `lore_operator` And `lore_graph` Artifact Deep Dive

This section explains what the build rules generate and why those artifacts matter.

### 18.1 Why generated descriptor modules exist

Graph code should not hardcode low-level image metadata.
Generated descriptor modules provide stable import surfaces with packaged metadata.

Benefits:

1. less manual metadata drift,
2. better reproducibility,
3. graph code reads at "domain level" not image-manifest level.

### 18.2 `lore_operator` lifecycle at build/publish time

High-level flow:

1. Build Python operator binary.
2. Build OCI image from operator runtime closure.
3. Attach operator metadata labels.
4. Generate descriptor module for graph consumption.
5. Publish image/metadata using publish target.

Practical implication:

You do not manually write the descriptor class for each operator; rule generation does that.

### 18.3 `lore_graph` lifecycle at build/publish time

High-level flow:

1. Package graph class and metadata.
2. Collect referenced operator descriptors.
3. Generate graph publisher module.
4. Publish graph metadata and dependencies through publish flow.

Generated publisher:

1. module path: `<target_path>.<target_name>_publisher`,
2. class: `Publisher`,
3. behavior: async publish method(s) for graph and dependency publishing.

### 18.4 How this relates to externalization

When authoring externalizable apps:

1. Use public SDK imports (`lumesof.lumeflow`, `lumesof.pylib`).
2. Avoid monorepo-internal imports in app code.
3. Keep BUILD dependencies compatible with wheel-backed flows.
4. Ensure generated artifacts can be consumed from external repos.

## 19) Standalone Drivers: Detailed Usage

This section expands day-to-day usage of test drivers.

### 19.1 `StandaloneOperatorTestDriver`

Use this when validating one operator contract at a time.

Test shape:

1. Define input generator(s) per ingress behavior.
2. Define egress verifier(s) per output contract.
3. Provide operator identity and image metadata.
4. Run driver until generators exhaust.

What this catches well:

1. schema mismatch,
2. port misbinding,
3. unexpected fanout,
4. handler logic regressions,
5. serialization edge cases.

### 19.2 `StandaloneGraphTestDriver` sync mode

Sync mode loop:

1. `generate()` request,
2. inject through bridge semantics,
3. collect response,
4. `verify(response)`,
5. repeat until `None`.

Expected when used correctly:

1. deterministic request/response assertions,
2. strong per-message correctness checks,
3. clear failure localization to graph path.

### 19.3 `StandaloneGraphTestDriver` async mode

Async mode loop:

1. call `startVerifier()` once,
2. generate messages until exhausted,
3. inject each into graph,
4. call `getVerificationResult()` once.

Runtime behavior details:

1. verifier lifecycle is decoupled from per-message generation,
2. verifier can run in its own event loop thread,
3. cleanup path cancels job in `finally`,
4. mode mismatches produce early driver errors.

### 19.4 Driver troubleshooting

If sync tests hang:

1. check `app_id` uniqueness,
2. verify sync ingress/retriever link names,
3. confirm graph is typed `sync`,
4. validate payload serialization format and type_url.

If async tests "pass but no output":

1. ensure verifier was started,
2. ensure collector route is reachable,
3. verify generator did not return `None` early,
4. inspect operator output port names vs graph links.

## 20) Bridge And Injection API Mapping

This section gives a concept map from graph type to caller API.

### 20.1 Sync map

Graph boundary:

1. `SYNC_INJECTOR` input link,
2. `SYNC_RETRIEVER` output link.

Caller behavior:

1. submit/start job,
2. send sync call payload,
3. wait for returned payload,
4. decode and verify response.

### 20.2 Async map

Graph boundary:

1. `ASYNC_INJECTOR` input link,
2. no sync retriever output requirement.

Caller behavior:

1. submit/start job,
2. inject messages,
3. optionally poll independent verifier/collector.

### 20.3 Type recommendation for generators

For reusable test drivers, `Any` payload typing is often practical at interface boundaries.
Concrete tests should still validate and downcast into expected proto/message classes.

## 21) Graph Design Heuristics

Before writing code, use these architecture checks.

### 21.1 Boundary checks

1. Is the graph clearly sync or async?
2. Is ingress/egress strategy explicit?
3. Is `app_id` strategy documented for sync workloads?

### 21.2 Operator checks

1. Does each operator have a single clear responsibility?
2. Are ports named by semantics, not generic names?
3. Is data contract stable and versioned?

### 21.3 Link checks

1. Are link names stable and meaningful?
2. Are there accidental cycles?
3. Are fanout/fanin patterns intentional?

### 21.4 Reliability checks

1. How are duplicates handled?
2. Is idempotency required?
3. What is the retry behavior?
4. Where is dead-letter/error routing handled?

### 21.5 Observability checks

1. Are operator-level counters/logs defined?
2. Are output counts and unique IDs observable?
3. Are sync latency and async throughput visible?

## 22) Build And Test Command Reference (Expanded)

### 22.1 Build operators

```bash
bazel build //lumecode/apps/lumeflow_rag/ingest:chunker_operator
bazel build //lumecode/apps/lumeflow_rag/ingest:structured_extractor_operator
bazel build //lumecode/apps/lumeflow_rag/ingest:chromadb_indexer_operator_nomic_embed_text_v1_5
```

### 22.2 Build graphs

```bash
bazel build //lumecode/apps/lumeflow_rag/ingest:async_ingest_light_graph
bazel build //lumecode/apps/lumeflow_rag/extract:sync_agent_light_graph
```

### 22.3 Publish graphs

```bash
bazel run //lumecode/apps/lumeflow_rag/ingest:async_ingest_light_graph_publish
bazel run //lumecode/apps/lumeflow_rag/extract:sync_agent_light_graph_publish
```

### 22.4 Run standalone operator tests

```bash
bazel test //lumecode/apps/lumeflow_rag/ingest:chunker_operator_test
bazel test //lumecode/apps/lumeflow_rag/ingest:structured_extractor_operator_test
bazel test //lumecode/apps/lumeflow_rag/ingest:chromadb_indexer_operator_test
```

### 22.5 Run standalone graph tests

```bash
bazel test //lumecode/apps/lumeflow_rag/ingest:ingest_flow_light_test
bazel test //lumecode/apps/lumeflow_rag/extract:agent_flow_light_test
```

### 22.6 Run full test suites when needed

```bash
bazel test //lumecode/lumeflow/runtime/v1/test_utils:all
bazel test //lumecode/lumeflow/runtime/v1/flow_server:all
```

### 22.7 Debugging flags and useful patterns

Examples:

```bash
bazel test //target --test_output=all
bazel test //target --test_arg=--log_level=INFO
bazel build //target --subcommands
```

Use targeted runs first, then expand scope.

## 23) Practical Troubleshooting Playbook

### 23.1 Symptom: sync call times out

Check:

1. sync graph has `setIngress` and `setEgress`,
2. link names match expected bridge wiring,
3. operator is emitting on expected egress,
4. payload type_url matches retriever expectations.

### 23.2 Symptom: async ingestion count too low/high

Check:

1. per-batch input sizes at chunker/indexer,
2. unique document hash counts,
3. upstream extractor output multiplicity,
4. duplicate retries or replay behavior.

### 23.3 Symptom: index item count unexpectedly large

Check:

1. chunk size/overlap settings,
2. extraction granularity (element explosion),
3. document duplication before chunking,
4. indexer flush/retry duplication.

### 23.4 Symptom: publish target fails with missing executable

Check:

1. generated publish script path exists and is executable,
2. runfiles layout includes publish scripts,
3. target naming matches generated publish module expectations.

### 23.5 Symptom: model install fails in read-only site-packages

Mitigations:

1. configure model cache/install to writable path,
2. pre-bake model assets into OCI image,
3. verify runtime user permissions and mount strategy.

## 24) Migration Notes For Existing Apps

If you are migrating a legacy app to the current rules/test model:

1. move operator and graph code into clear app subdirectories,
2. ensure generated descriptor/publisher imports are updated,
3. adopt standalone operator and graph tests,
4. switch externalizable targets to public SDK imports,
5. verify publish and invocation paths with focused smoke tests.

## 25) Author Checklist (Pre-PR)

Operator checklist:

1. `@operator_ports` complete and accurate.
2. all ingress handlers bound with `@on_ingress`.
3. type checks present and clear.
4. output schemas align with egress contracts.
5. standalone operator tests pass.

Graph checklist:

1. graph type correct (`sync` or `async`).
2. ingress/egress boundaries explicit.
3. links and port names consistent.
4. standalone graph tests pass.
5. publish target runs successfully.

SDK/externalization checklist:

1. imports use public surfaces where needed.
2. BUILD deps are compatible with externalizable expectations.
3. docs/examples avoid internal-only APIs for public consumption.

## 26) FAQ

### 26.1 Should I start with operator code or graph code?

Start with graph design and message contracts, then operator code.
This avoids rework and keeps contracts explicit from day one.

### 26.2 Can one operator emit different payload types on one egress?

Technically possible with envelope patterns, but discouraged for readability and compatibility.
Prefer separate egress ports per schema when possible.

### 26.3 How do I decide sync vs async quickly?

If the caller must block waiting for a direct result, choose sync.
If eventual processing is acceptable, choose async.

### 26.4 How should I name links?

Use semantic names that survive refactors:

1. `link-document-input`
2. `link-chunked-output`
3. `link-ranking-output`
4. `link-response-output`

Avoid generic names like `link1`, `link2`.

### 26.5 What should I log inside operators?

At minimum:

1. batch size in,
2. batch size out,
3. per-document size summaries where relevant,
4. unique document counters/hashes for dedupe checks,
5. major state transitions.

### 26.6 How do I avoid duplicate indexing?

Approach:

1. include deterministic document IDs/hashes,
2. log unique count versus total count,
3. enforce idempotent writes in downstream indexers,
4. validate retry behavior does not duplicate side effects.

## 27) Extended Reading

Architecture and APIs:

1. [ARCHITECTURE.md](/home/baba/lumesof/lumesof/lumeflow_documentation/ARCHITECTURE.md)
2. [API_REFERENCE.md](/home/baba/lumesof/lumesof/lumeflow_documentation/API_REFERENCE.md)

Rules and generators:

1. [bazel/lore/rules.bzl](/home/baba/lumesof/bazel/lore/rules.bzl)
2. [generate_image_descriptor_lib.py](/home/baba/lumesof/bazel/lore/generate_image_descriptor_lib.py)
3. [generate_graph_publisher_lib.py](/home/baba/lumesof/bazel/lore/generate_graph_publisher_lib.py)

App examples:

1. [ingest_flow_light.py](/home/baba/lumesof/lumecode/apps/lumeflow_rag/ingest/ingest_flow_light.py)
2. [agent_flow_light.py](/home/baba/lumesof/lumecode/apps/lumeflow_rag/extract/agent_flow_light.py)
3. [EXTERNALIZATION_REQUIREMENTS.md](/home/baba/lumesof/cheatsheets/EXTERNALIZATION_REQUIREMENTS.md)

## 28) One-Page Quickstart (Reference)

Use this when you already understand concepts and need execution order.

1. Define proto payload contracts.
2. Implement operators with ports and ingress handlers.
3. Add `lore_operator` targets for each operator.
4. Implement graph class with `@graph_type` and `@materialize`.
5. Add `lore_graph` target with operator deps.
6. Add standalone operator tests.
7. Add standalone graph tests.
8. Build operators and graph.
9. Run tests.
10. Publish graph artifacts.
11. Submit job and invoke (sync) or inject (async).
12. Validate outcomes via bridge response or verifier side channel.

## 29) Common Anti-Patterns

Avoid these patterns in new applications:

1. ambiguous port names (`in`, `out`, `misc`),
2. missing schema type validation in handlers,
3. overloading one operator with unrelated responsibilities,
4. using sync flow for batch/offline workloads,
5. relying on logs only without assertions in tests,
6. skipping standalone tests and debugging only in full cluster.

## 30) Final Notes

The intended mental model is simple and repeatable:

1. Design the graph first.
2. Lock down contracts.
3. Implement operators cleanly.
4. Package with rule-generated artifacts.
5. Verify with standalone drivers.
6. Publish and run with clear sync/async expectations.

When this sequence is followed, Lumeflow applications stay understandable as they scale.
