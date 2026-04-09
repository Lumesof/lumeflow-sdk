# Lumeflow Python SDK API Reference
Last Updated: April 9, 2026

This document describes the public Python SDK surface exported by:

- `lumesof.lumeflow`
- `lumesof.pylib`

It is written from SDK consumer perspective and is aligned with current exports in `lumesof/lumeflow.py` and `lumesof/pylib.py`.

## Quick Imports

```python
from lumesof.lumeflow import (
    AsyncJobClient,
    Client,
    ConfigQueryClientError,
    Graph,
    Job,
    JobClientTypeError,
    JobResolutionError,
    JobTypeMismatchError,
    Operator,
    OperatorImageDescriptor,
    OperatorPortDescriptor,
    Proto,
    SyncJobClient,
    Test,
    async_getRequiredStringConfigValue,
    graph_type,
    materialize,
    operator_ports,
    test,
)

from lumesof.pylib import Net

# optional convenience alias used in many operators
on_ingress = Operator.on_ingress
```

## SDK Mental Model

`lumesof.lumeflow` has four practical layers:

1. Job lifecycle layer (`Client`, `Job`, `SyncJobClient`, `AsyncJobClient`)
2. DAG construction layer (`Graph`, `@graph_type`, `@materialize`)
3. Operator runtime layer (`Operator`, `@operator_ports`, `@Operator.on_ingress`)
4. Proto contracts (`Proto.flow_server`, `Proto.dag`, `Proto.opnet_types`, `Proto.operator`)

If you are building end-user applications, you mostly touch layers 1-3 and use layer 4 for typed payload/message objects.

## Graph API (Annotations First)

### Why Graph Annotations Matter

The Graph API is annotation-driven. The runtime validates the class contract before DAG creation. If annotations are missing or inconsistent, class instantiation or materialization fails early with explicit errors.

### `@graph_type("sync" | "async")`

Applies to a `Graph` subclass and defines top-level semantics:

- `"sync"`: graph supports request/response behavior and may define an egress retriever link through `setEgress(...)`.
- `"async"`: graph accepts asynchronous ingress messages and does not support `setEgress(...)`.

Validation behavior:

- Value is normalized to lowercase.
- Only `"sync"` and `"async"` are accepted.
- Invalid values raise `ValueError` during class validation.

### `@materialize`

Marks the single method that constructs and returns the final `Proto.dag.Dag`.

Rules:

- A graph class must have exactly one `@materialize` method.
- The annotated method must return `Proto.dag.Dag`.
- Returning any other type raises `TypeError`.

At runtime, `Graph.materializeDag()` resets graph state, calls the annotated method, validates result type, then returns the `Dag`.

### Minimal Graph Example

```python
from lumesof.lumeflow import Graph, Proto, graph_type, materialize

@graph_type("async")
class MyGraph(Graph):
    @materialize
    def buildDag(self) -> Proto.dag.Dag:
        # create operators and links
        return self.createDag()
```

### `Graph` Methods

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `__init__()` | `None` | Validates annotation contract and initializes internal builder state. |
| `graphType` | `graphType()` | `str` | Class-level resolved graph type (`"sync"` or `"async"`). |
| `materializeMethodName` | `materializeMethodName()` | `str` | Class-level name of the `@materialize` method. |
| `materializeDag` | `materializeDag()` | `Proto.dag.Dag` | Runs the annotated builder method and returns validated DAG. |
| `createOperatorFromImageDescriptor` | `createOperatorFromImageDescriptor(name: str, descriptor: OperatorImageDescriptor)` | `OperatorDescriptor` | Registers operator and its declared port contract for later wiring checks. |
| `connect` | `connect(from_: dict[str, str], to: dict[str, str], linkName: str \| None = None)` | `None` | Wires producer to consumer; validates operator existence, direction, and payload compatibility. |
| `setIngress` | `setIngress(to: dict[str, str], linkName: str \| None = None)` | `None` | Creates injector link to an ingress port. Uses sync or async injector type based on `@graph_type`. |
| `setEgress` | `setEgress(from_: dict[str, str], linkName: str \| None = None)` | `None` | Creates sync retriever link from an egress port. Allowed only for `@graph_type("sync")`. |
| `setAllowLoops` | `setAllowLoops(value: bool)` | `None` | Enables/disables loop allowance in validator. |
| `getAllowLoops` | `getAllowLoops()` | `bool` | Returns loop allowance flag. |
| `setDagName` | `setDagName(name: str)` | `None` | Sets DAG name. |
| `createDag` | `createDag()` | `Proto.dag.Dag` | Returns serialized DAG from current graph state. |
| `validate` | `validate(allowLoop: bool = False)` | `bool` | Validates graph and loop state. |
| `protoPayload` | `protoPayload(schema: str)` | `Proto.opnet_types.OpNetPayloadType` | Convenience payload type constructor (PROTO format). |
| `jsonPayload` | `jsonPayload(schema: str)` | `Proto.opnet_types.OpNetPayloadType` | Convenience payload type constructor (JSON format). |

Endpoint shape for `connect`, `setIngress`, `setEgress`:

```python
{"operator_name": "my-operator", "port_name": "my-port"}
```

## Operator API (Annotations First)

### Why Operator Annotations Matter

Operator dispatch is also annotation-driven. The class-level port contract and handler registrations are cross-validated. This prevents drift between declared ports and actual code handlers.

### `@operator_ports({...})`

Declares the operator ingress/egress contract.

Required top-level keys:

- `ingress`
- `egress`

Each key must map to a list of port specs with exactly:

- `name`: non-empty string
- `serialization_format`: currently only PROTO is supported
- `type_url`: non-empty protobuf type URL (canonicalized as needed)

Example:

```python
from lumesof.lumeflow import Proto, Operator, operator_ports

@operator_ports(
    {
        "ingress": [
            {
                "name": "input",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": "type.googleapis.com/example.Request",
            }
        ],
        "egress": [
            {
                "name": "output",
                "serialization_format": Proto.opnet_types.OpNetPayloadType.PROTO,
                "type_url": "type.googleapis.com/example.Response",
            }
        ],
    }
)
class MyOperator(Operator):
    ...
```

### `@Operator.on_ingress("port-name")`

Registers a handler for one ingress port.

`on_delivery` is not a separate exported decorator in the current SDK surface.
Use `Operator.on_ingress(...)` directly (or alias it locally as `on_ingress`).

Required handler signature:

```python
def handle(self, *, input_port, message):
    ...
```

or

```python
async def handle(self, *, input_port, message):
    ...
```

Important constraints:

- `input_port` and `message` must be keyword-only.
- No extra trailing parameters are allowed.
- Every `@Operator.on_ingress(...)` port must exist in `@operator_ports(...)["ingress"]`.
- Every declared ingress port must have exactly one handler.

### `Operator` Methods

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `__init__()` | `None` | Verifies class has a valid `@operator_ports` contract. |
| `on_ingress` | `@Operator.on_ingress(input_port: str)` | decorator | Declares ingress handler binding metadata. |
| `handlers` | `handlers()` | `Mapping[str, Callable[..., Any]]` | Bound handlers by input port name. |
| `dispatch` | `dispatch(input_port: str, message: Message)` | `Any` | Invokes bound handler; may return value or awaitable. |
| `async_connect` | `async_connect(uri: str)` | `None` | Connects operator to sidecar (`tcp://...` or `unix:///...`). |
| `async_runUntilStopped` | `async_runUntilStopped(uri: str)` | `None` | Connects and runs receive/publish loops until stopped. |
| `async_onStart` | `async_onStart()` | `None` | Optional subclass hook after stream readiness. |
| `async_emit` | `async_emit(output_port: str, message_context: MessageContext \| None = None, message: Message)` | `None` | Emits protobuf message to specified egress port. |
| `currentMessageContext` | `currentMessageContext()` | `MessageContext \| None` | Access current request-scoped message context. |
| `async_waitUntilTerminated` | `async_waitUntilTerminated()` | `None` | Waits for stream termination. |
| `async_shutdown` | `async_shutdown()` | `None` | Shuts down channel/relay/task resources. |
| `onPublishError` | `onPublishError(port: str, error: google.rpc.status_pb2.Status)` | `None` | Override hook for publish failures. |

## Lifecycle API (`Client`, `Job`, Clients)

### `Client`

Top-level entry point for job lifecycle operations.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `Client(flowServerTarget: str, configServiceTarget: str = "", endpointRemapper: Callable[[str], str] \| None = None)` | `None` | `flowServerTarget` accepts `tcp://` or `unix:///`. |
| `async_submitJob` | `async_submitJob(request: Proto.flow_server.SubmitJobRequest, startJob: bool = False)` | `Job` | Submit job and optionally auto-start when ready. |
| `async_getJob` | `async_getJob(jobId: str)` | `Job` | Rehydrate active job by id. |
| `async_rehydrateJob` | `async_rehydrateJob(jobId: str)` | `Job` | Alias of `async_getJob`. |
| `async_startJob` | `async_startJob(jobId: str)` | `Proto.flow_server.StartJobResponse` | Direct start RPC. |
| `async_startJobWhenReady` | `async_startJobWhenReady(jobId: str, timeoutSeconds: float = 60.0)` | `Proto.flow_server.StartJobResponse` | Polls for `CREATED`; raises on timeout/terminal states. |
| `async_cancelJob` | `async_cancelJob(jobId: str, reason: str = "")` | `Proto.flow_server.CancelJobResponse` | Cancel by job id. |
| `async_getJobStatus` | `async_getJobStatus(jobId: str)` | `Proto.flow_server.GetJobStatusResponse` | Read status. |
| `async_listJobs` | `async_listJobs()` | `Proto.flow_server.ListResponse` | List active jobs. |
| `async_injectMessage` | `async_injectMessage(jobId: str, message: Proto.opnet_types.OpNetMessage)` | `Proto.flow_server.InjectMessageResponse` | Inject one async message. |
| `async_close` | `async_close()` | `None` | Closes SDK-owned sync bridge clients. |

### `Job`

Handle for one submitted job.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `id` | `id()` | `str` | Flow job id. |
| `type` | `type()` | `Literal["sync", "async"]` | Normalized job type. |
| `client` | `client(type: Literal["auto", "sync", "async"] = "auto")` | `SyncJobClient \| AsyncJobClient` | Type-safe client selection. |
| `syncClient` | `syncClient()` | `SyncJobClient` | Equivalent to `client(type="sync")`. |
| `asyncClient` | `asyncClient()` | `AsyncJobClient` | Equivalent to `client(type="async")`. |
| `async_cancel` | `async_cancel(reason: str = "")` | `Proto.flow_server.CancelJobResponse` | Cancel this job. |
| `async_startWhenReady` | `async_startWhenReady(timeoutSeconds: float = 60.0)` | `Proto.flow_server.StartJobResponse` | Wait/start helper. |
| `async_getStatus` | `async_getStatus()` | `Proto.flow_server.GetJobStatusResponse` | Current status. |
| `async_getStatusName` | `async_getStatusName()` | `str` | Symbolic enum status name. |
| `statusName` | `statusName(status: int)` | `str` | Converts enum numeric value to symbolic name. |
| `async_waitForStatus` | `async_waitForStatus(targetStatus: int, timeoutSeconds: float = 120.0, pollIntervalSeconds: float = 0.5)` | `Proto.flow_server.GetJobStatusResponse` | Poll until target status; raises on timeout/terminal mismatch. |

### `SyncJobClient`

Bridge request/response client for sync jobs.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `SyncJobClient(appId: str, configServiceTarget: str, endpointRemapper: Callable[[str], str] \| None = None)` | `None` | Requires non-empty `appId` and `configServiceTarget`. |
| `async_call` | `async_call(payload: bytes, payloadTypeUrl: str, payloadSerializationFormat: int = OpNetPayloadType.PROTO, deadlineTsUnixMs: int = 0, timeoutMs: int = 5000)` | `bytes` | Returns raw response payload; raises on non-`ok` bridge response. |
| `async_close` | `async_close()` | `None` | Closes underlying bridge delegate/session. |

### `AsyncJobClient`

Message injection helper for async jobs.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `AsyncJobClient(rootClient: Client, jobId: str)` | `None` | Typically obtained via `Job.asyncClient()`. |
| `async_injectMessage` | `async_injectMessage(message: Proto.opnet_types.OpNetMessage)` | `Proto.flow_server.InjectMessageResponse` | Inject one opnet message into bound async job. |

### Exceptions

| Exception | Fields | Raised When |
| --- | --- | --- |
| `JobResolutionError` | `jobId`, `reason` | Job cannot be resolved to an active Flow job. |
| `JobClientTypeError` | `requestedType` | Unsupported `Job.client(type=...)` selector. |
| `JobTypeMismatchError` | `jobId`, `requestedType`, `actualType` | Requested sync/async client mismatches actual job type. |
| `ConfigQueryClientError` | error-specific | Config query lookup failed. |

## Proto Contracts (`Proto` Namespace)

Import:

```python
from lumesof.lumeflow import Proto
```

### `Proto.flow_server`

Used for flow lifecycle requests/responses.

Common types:

- `SubmitJobRequest`
- `StartJobRequest` and `StartJobResponse`
- `CancelJobRequest` and `CancelJobResponse`
- `GetJobStatusRequest` and `GetJobStatusResponse`
- `ListRequest` and `ListResponse`
- `InjectMessageRequest` and `InjectMessageResponse`
- `JobStatus`

### `Proto.dag`

Used for DAG graph materialization.

Common types:

- `Dag`
- `Operator`
- `Port`
- `Link`
- `OperatorInstancePolicy`
- `Port.Direction`
- `Link.LinkType`

### `Proto.opnet_types`

Used for runtime payload/message typing and operator publish/dispatch.

Common types:

- `OpNetMessage`
- `MessageContext`
- `OpNetPayloadType`
- `OpNetPayloadType.SerializationFormat`
- `Result`

### `Proto.operator`

Operator sidecar proto contract module. This is mainly needed when interacting directly with low-level operator-sidecar messages/RPCs.

## Public Helper Namespaces

### `lumesof.pylib.Net`

`Net` exposes `NetTarget` parser/holder utilities.

```python
from lumesof.pylib import Net

parsed = Net.NetTarget.parseTarget("tcp://127.0.0.1:50070")
```

Use this surface when you need strict target parsing/normalization for `tcp://` and `unix:///` endpoints.

### `lumesof.lumeflow.Test` and `lumesof.lumeflow.test`

`Test` is a lazy namespace exposing test utilities for standalone operator and graph testing.

Standalone operator-sidecar helpers:

- `standalone_operator_sidecar`:
  - Role: standalone app entrypoint that runs an `Operator` against file-backed ingress/egress with sidecar semantics.
  - Typical usage: packaged as an app image and run by `StandaloneOperatorTestDriver`.
- `FileBackedOpNetProxy`:
  - Role: `OpNetProxy` implementation backed by ingress files (read-once queue) and egress files (append-only).
  - Constructor:
    - `FileBackedOpNetProxy(ingressFilesByPort, egressFilesByPort, ingressPortOrder, ackReportUdsPath)`
  - Common runtime methods:
    - `async_setup(...)`
    - `async_getOpNetMessage()`
    - `async_putOpNetMessage(...)`
    - `async_ack(...)`
- `encodeDelimitedMessage`:
  - Role: helper that length-prefixes raw protobuf bytes for file-backed framing.
- `decodeDelimitedDeliverRequests`:
  - Role: helper that decodes length-prefixed `DeliverRequest` records from file payload bytes.

Standalone operator test contracts:

- `IngressGenerator`:
  - Role: user extension point to generate ingress test data.
  - Method users implement:
    - `generate(writableFilePath) -> Sequence[(message_id, "ack"|"nack")]`
- `EgressVerifier`:
  - Role: user extension point to validate egress test data.
  - Method users implement:
    - `verify(readableFilePath) -> None` (raise while output is invalid/incomplete)
- `StandaloneOperatorTestDriver`:
  - Role: orchestrates one operator container + standalone sidecar harness under cartond.
  - Main API:
    - `addIngressGenerator(port=..., generator=...)`
    - `addEgressVerifier(port=..., verifier=...)`
    - `async_run()`

Standalone graph test contracts:

- `SyncGeneratorAndVerifier`:
  - Role: request/response test contract for sync graphs.
  - Methods users implement:
    - `generate() -> Any | None`
    - `verify(message: Any) -> bool`
- `AsyncGeneratorAndVerifier`:
  - Role: injection + deferred verification contract for async graphs.
  - Methods users implement:
    - `startVerifier() -> None`
    - `generate() -> Any | None`
    - `getVerificationResult() -> bool`
- `BasePublisher`:
  - Role: abstraction for publishing graph dependencies before standalone graph runs.
  - Method users implement:
    - `publish() -> None`
- `FlowServerFactoryContext`:
  - Role: configuration/context object passed into custom flow-server factories used by standalone graph tests.
  - Contains cluster/session and dependency clients/targets needed to build `FlowServerService`.
- `StandaloneGraphRunContext`:
  - Role: runtime context optionally provided to generator/verifier implementations.
  - Core fields:
    - `clusterId`, `jobId`, `sessionMaker`, `flowServerStub`
- `StandaloneGraphTestDriver`:
  - Role: end-to-end standalone graph runner (publish -> submit -> start -> sync call or async inject -> verify -> cleanup).
  - Main API:
    - constructor with `graphObject`, `publisher`, and sync/async generator-verifier contract
    - `async_run()`

`test` is an alias of `Test`.

## Verbose Annotation Walkthrough With RAG Patterns

`lumecode/apps/lumeflow_rag` shows the canonical style.

### Graph annotations in practice

- `AsyncIngestLightGraph` uses `@graph_type("async")` and `@materialize`.
- It calls `setIngress(...)` and `connect(...)` to model one-way ingest flow.
- It does not call `setEgress(...)` because async graphs are message-driven.

`SyncAgentLightGraph` uses `@graph_type("sync")` and `@materialize`:

- It sets `setAllowLoops(True)` for retrieval loop wiring.
- It calls both `setIngress(...)` and `setEgress(...)` to expose request/response behavior.

### Operator annotations in practice

The operators in this app family define `@operator_ports(...)` with explicit protobuf type URLs for each ingress/egress port, then implement exactly matching handlers via `@Operator.on_ingress(...)`.

This pairing is critical because:

- Graph wiring validates directional and payload compatibility by reading descriptor port contracts.
- Runtime dispatch validates that each declared ingress can be executed by a concrete handler.
- Contract drift is caught at startup/materialization time instead of silently failing during traffic.

## End-to-End Snippets

### Submit async ingest and inject a message

```python
from lumesof.lumeflow import Client

sdk = Client(
    flowServerTarget="tcp://127.0.0.1:50070",
    configServiceTarget="tcp://127.0.0.1:50074",
)
job = await sdk.async_submitJob(submitRequest, startJob=False)
await job.async_startWhenReady()
await job.asyncClient().async_injectMessage(message=opnetMessage)
await sdk.async_close()
```

### Submit sync graph and call bridge API

```python
from lumesof.lumeflow import Client, Proto

sdk = Client(
    flowServerTarget="tcp://127.0.0.1:50070",
    configServiceTarget="tcp://127.0.0.1:50074",
)
job = await sdk.async_submitJob(submitRequest, startJob=False)
await job.async_startWhenReady()

payload = await job.syncClient().async_call(
    payload=requestBytes,
    payloadTypeUrl="type.googleapis.com/example.Request",
    payloadSerializationFormat=int(Proto.opnet_types.OpNetPayloadType.PROTO),
    timeoutMs=5000,
)
await sdk.async_close()
```

## Notes and Compatibility

- Preferred graph API symbol is `Graph` (not `FlowGraph`).
- Preferred proto module for OpNet messages is `Proto.opnet_types`.
- Public operator proto namespace is available as `Proto.operator`.
- If you need config lookups from SDK surface, use `async_getRequiredStringConfigValue(...)` and handle `ConfigQueryClientError`.
