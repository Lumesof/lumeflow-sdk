# Lumeflow Python SDK API Reference
Last Updated: March 31, 2026

This reference is organized for SDK consumers by import path and object API.
It documents the public `lumesof.lumeflow` surface, plus transitive returned-object APIs and protobuf contracts used in method signatures.

## Primary Import Path

```python
from lumesof.lumeflow import (
    AsyncJobClient,
    Client,
    FlowGraph,
    Job,
    JobClientTypeError,
    JobResolutionError,
    JobTypeMismatchError,
    Operator,
    Proto,
    SyncJobClient,
)
```

## Object API By Import Path

### `lumesof.lumeflow.Client`

Top-level SDK entrypoint for flow lifecycle operations.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `Client(*, flowServerTarget: str, configServiceTarget: str = "", endpointRemapper: Callable[[str], str] \| None = None)` | `None` | Use `tcp://host:port` or `unix:///path` targets. |
| `async_submitJob` | `async_submitJob(request: Proto.flow_server.SubmitJobRequest, *, startJob: bool = False)` | `Job` | Optional `startJob=True` waits/starts automatically. |
| `async_getJob` | `async_getJob(*, jobId: str)` | `Job` | Raises `JobResolutionError` if not found in active jobs. |
| `async_rehydrateJob` | `async_rehydrateJob(*, jobId: str)` | `Job` | Alias of `async_getJob`. |
| `async_startJob` | `async_startJob(*, jobId: str)` | `Proto.flow_server.StartJobResponse` | Direct start RPC. |
| `async_startJobWhenReady` | `async_startJobWhenReady(*, jobId: str, timeoutSeconds: float = 60.0)` | `Proto.flow_server.StartJobResponse` | Polls until `CREATED`; raises `RuntimeError`/`TimeoutError` on failure paths. |
| `async_cancelJob` | `async_cancelJob(*, jobId: str, reason: str = "")` | `Proto.flow_server.CancelJobResponse` | Cancels by job id. |
| `async_getJobStatus` | `async_getJobStatus(*, jobId: str)` | `Proto.flow_server.GetJobStatusResponse` | Current status lookup. |
| `async_listJobs` | `async_listJobs()` | `Proto.flow_server.ListResponse` | Lists active jobs. |
| `async_injectMessage` | `async_injectMessage(*, jobId: str, message: Proto.opnet.OpNetMessage)` | `Proto.flow_server.InjectMessageResponse` | Async-job message injection. |
| `async_close` | `async_close()` | `None` | Closes SDK-owned `SyncJobClient` instances. |

### `lumesof.lumeflow.Job`

Handle object representing one submitted flow job.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `id` | `id()` | `str` | Flow job id. |
| `type` | `type()` | `Literal["sync", "async"]` | Normalized job type. |
| `client` | `client(*, type: Literal["auto", "sync", "async"] = "auto")` | `SyncJobClient \| AsyncJobClient` | Raises `JobClientTypeError` for invalid selector and `JobTypeMismatchError` for incompatible type requests. |
| `syncClient` | `syncClient()` | `SyncJobClient` | Equivalent to `client(type="sync")`. |
| `asyncClient` | `asyncClient()` | `AsyncJobClient` | Equivalent to `client(type="async")`. |
| `async_cancel` | `async_cancel(*, reason: str = "")` | `Proto.flow_server.CancelJobResponse` | Delegates to root `Client`. |
| `async_startWhenReady` | `async_startWhenReady(*, timeoutSeconds: float = 60.0)` | `Proto.flow_server.StartJobResponse` | Waits for `CREATED` then starts. |
| `async_getStatus` | `async_getStatus()` | `Proto.flow_server.GetJobStatusResponse` | Current job status. |
| `async_getStatusName` | `async_getStatusName()` | `str` | Enum name for current status. |
| `statusName` | `statusName(status: int)` | `str` | Converts numeric status enum to symbolic name. |
| `async_waitForStatus` | `async_waitForStatus(*, targetStatus: int, timeoutSeconds: float = 120.0, pollIntervalSeconds: float = 0.5)` | `Proto.flow_server.GetJobStatusResponse` | Raises `RuntimeError` if terminal non-target state occurs first; `TimeoutError` on timeout. |

### `lumesof.lumeflow.SyncJobClient`

Object-oriented sync request/response client for synchronous jobs.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `SyncJobClient(*, appId: str, configServiceTarget: str, endpointRemapper: Callable[[str], str] \| None = None)` | `None` | Requires non-empty `appId` and `configServiceTarget`. |
| `async_call` | `async_call(*, payload: bytes, payloadTypeUrl: str, payloadSerializationFormat: int = OpNetPayloadType.PROTO, deadlineTsUnixMs: int = 0, timeoutMs: int = 5000)` | `bytes` | Raises `RuntimeError` when bridge response is not `ok`. |
| `async_close` | `async_close()` | `None` | Closes underlying bridge session/delegate. |

### `lumesof.lumeflow.AsyncJobClient`

Object-oriented async message injection client for asynchronous jobs.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `AsyncJobClient(*, rootClient: Client, jobId: str)` | `None` | Internal construction via `Job.asyncClient()` is typical usage. |
| `async_injectMessage` | `async_injectMessage(*, message: Proto.opnet.OpNetMessage)` | `Proto.flow_server.InjectMessageResponse` | Injects one message into the bound async job. |

### `lumesof.lumeflow.FlowGraph` (`LumeFlow`)

Graph-builder API used to create and validate DAG payloads for submission.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `FlowGraph(*, backend: object \| None = None, dag: Proto.dag.Dag \| None = None, name: str \| None = None)` | `None` | Can initialize empty or hydrate from existing `Dag`. |
| `getLink` | `getLink(*, name: str)` | `_FlowLinkPlan` | Returns/creates link plan object. |
| `getOperator` | `getOperator(*, name: str, info: dict[str, str] \| None = None, is_entrypoint: bool = False)` | `OperatorDescriptor` | Returns/creates operator descriptor. |
| `createOperator` | `createOperator(*, name: str, info: dict[str, str], is_entrypoint: bool = False)` | `OperatorDescriptor` | Raises on duplicate operator name. |
| `protoPayload` | `protoPayload(schema: str)` | `Proto.opnet.OpNetPayloadType` | Serialization format set to `PROTO`. |
| `jsonPayload` | `jsonPayload(schema: str)` | `Proto.opnet.OpNetPayloadType` | Serialization format set to `JSON`. |
| `addProducer` | `addProducer(*, link_name: str, operator: OperatorDescriptor, port: str, payload_type: OpNetPayloadType)` | `_FlowLinkPlan` | Attaches producer endpoint to link. |
| `addConsumer` | `addConsumer(*, link_name: str, operator: OperatorDescriptor, port: str, payload_type: OpNetPayloadType)` | `_FlowLinkPlan` | Attaches consumer endpoint to link. |
| `createDag` | `createDag()` | `Proto.dag.Dag` | Validates and serializes graph. |
| `validate` | `validate(*, allowLoop: bool = False)` | `bool` | Returns loop-detected flag; raises `ValueError` on invalid graph. |

### `lumesof.lumeflow.Operator`

Base class for writing runtime operators with sidecar connectivity and typed dispatch.

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `__init__` | `__init__()` | `None` | Subclasses must define `EMIT_PORTS` and `BRIDGE_EMIT_PORTS`. |
| `on` | `@Operator.on(input_port: str, type: str \| None = None, bridge: bool = False)` | decorator | Marks handler for an input port. |
| `handlers` | `handlers()` | `Mapping[str, Callable[..., Any]]` | Bound handlers keyed by input port. |
| `dispatch` | `dispatch(*, input_port: str, message: Message)` | `Any` | Invokes handler and returns handler result/awaitable. |
| `async_connect` | `async_connect(uri: str)` | `None` | Connects to OperatorSidecar (`tcp://` or `unix:///`). |
| `async_runUntilStopped` | `async_runUntilStopped(uri: str)` | `None` | Connects and blocks until stream termination. |
| `async_onStart` | `async_onStart()` | `None` | Subclass hook after connection is established. |
| `async_emit` | `async_emit(*, output_port: str, message_context: MessageContext \| None = None, message: Message)` | `None` | Emits one message on output port. |
| `currentMessageContext` | `currentMessageContext()` | `MessageContext \| None` | Current request-scoped message context. |
| `async_waitUntilTerminated` | `async_waitUntilTerminated()` | `None` | Wait for active stream shutdown/termination. |
| `async_shutdown` | `async_shutdown()` | `None` | Teardown for streams/channel/resources. |
| `onPublishError` | `onPublishError(*, port: str, error: google.rpc.status_pb2.Status)` | `None` | Override hook for publish failures. |

### `lumesof.lumeflow` Exceptions

| Exception | Fields | Raised When |
| --- | --- | --- |
| `JobResolutionError` | `jobId`, `reason` | Job id cannot be resolved to an active job. |
| `JobClientTypeError` | `requestedType` | Unsupported `Job.client(type=...)` selector is passed. |
| `JobTypeMismatchError` | `jobId`, `requestedType`, `actualType` | Requested sync/async client is incompatible with the job's type. |

## Returned Object APIs

### `_FlowLinkPlan` (returned by `FlowGraph.getLink`, `FlowGraph.addProducer`, `FlowGraph.addConsumer`)

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `setLinkType` | `setLinkType(*, linkType: str)` | `_FlowLinkPlan` | Supports `REGULAR`, `INJECTOR`, `DEADEND`. |
| `getLinkType` | `getLinkType()` | `str` | Current normalized link type. |
| `addProducer` | `addProducer(operator: OperatorDescriptor, *, port: str, payload_type: OpNetPayloadType)` | `_FlowLinkPlan` | Adds producer attachment. |
| `addConsumer` | `addConsumer(operator: OperatorDescriptor, *, port: str, payload_type: OpNetPayloadType)` | `_FlowLinkPlan` | Adds consumer attachment. |

### `OperatorDescriptor` (returned by `FlowGraph.getOperator`, `FlowGraph.createOperator`)

| Method | Signature | Returns | Notes |
| --- | --- | --- | --- |
| `async_getLabels` | `async_getLabels()` | `dict[str, str]` | Fetches image labels from registry config blob. |
| `async_getLumesofOperatorInfo` | `async_getLumesofOperatorInfo()` | `dict[str, Any]` | Parses `lumesof_operator_info` label JSON object. |

Fields:
- `name: str`
- `info: Mapping[str, str]` (must include `url`)

## Protobuf Contracts Exposed By This API

Import the public proto namespace from the SDK surface:

```python
from lumesof.lumeflow import Proto
```

### `Proto.flow_server`

Common request/response and enum contracts surfaced by SDK methods:
- `SubmitJobRequest`
- `StartJobResponse`
- `CancelJobResponse`
- `GetJobStatusResponse`
- `ListResponse` (and `ActiveJob` entries)
- `InjectMessageResponse`
- `JobStatus`

### `Proto.dag`

DAG contracts used by `FlowGraph`:
- `Dag`
- `Operator`
- `Port`
- `OperatorInstancePolicy`
- `Link`
- `Port.Direction`
- `Link.LinkType`

### `Proto.opnet`

Message and payload contracts used by sync/async and operator APIs:
- `OpNetMessage`
- `MessageContext`
- `OpNetPayloadType`
- `OpNetPayloadType.SerializationFormat`
- `Result`

## Usage Examples

### Submit a job and poll status

```python
from lumesof.lumeflow import Client, Proto

client = Client(
    flowServerTarget="tcp://127.0.0.1:50051",
    configServiceTarget="tcp://127.0.0.1:50052",
)

request = Proto.flow_server.SubmitJobRequest(owner="sdk-user", cluster_id="local")
job = await client.async_submitJob(request)
status_name = await job.async_getStatusName()
```

### Build a graph and create `Dag`

```python
from lumesof.lumeflow import FlowGraph

flow = FlowGraph(name="demo")
producer = flow.createOperator(name="producer", info={"url": "registry/x:tag"}, is_entrypoint=True)
consumer = flow.createOperator(name="consumer", info={"url": "registry/y:tag"})
payload_type = flow.protoPayload("type.googleapis.com/example.Msg")

flow.addProducer(link_name="main", operator=producer, port="out", payload_type=payload_type)
flow.addConsumer(link_name="main", operator=consumer, port="in", payload_type=payload_type)

dag = flow.createDag()
```

### Use sync and async job clients

```python
from lumesof.lumeflow import Proto

job = await client.async_getJob(jobId="job-123")

if job.type() == "sync":
    sync_client = job.syncClient()
    response_bytes = await sync_client.async_call(
        payload=b"...",
        payloadTypeUrl="type.googleapis.com/example.Request",
    )
else:
    async_client = job.asyncClient()
    opnet_message = Proto.opnet.OpNetMessage()
    await async_client.async_injectMessage(message=opnet_message)
```

## Behavioral Notes

- `Job.client(type="auto")` chooses client type based on the job metadata seen from Flow server.
- `SyncJobClient` requires non-empty `app_id` and configured `configServiceTarget`.
- `Client.async_startJobWhenReady` and `Job.async_waitForStatus` are polling APIs that can raise `TimeoutError` and terminal-state `RuntimeError`.
- `Operator.async_emit` requires an active, ready sidecar stream; calling before connect/ready raises runtime errors.
