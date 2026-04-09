# Interactive Chat App

Use `./lumecode/apps/lumeflow_rag/extract/chat_agent_light.sh` to run the terminal chat app over DAG
`lumeflow-rag-sync-chat-loop-light-v1` with pinned operator digests.

## Prerequisites

- Cluster is running and deployed.
- RAG index has content ingested.

## Launch

```bash
./lumecode/apps/lumeflow_rag/extract/chat_agent_light.sh \
  --public-host="${PUBLIC_HOST}"
```

## Runtime Controls

- Type a message and press Enter to send a single-turn request.
- Type `/quit` to exit.

## Common Flags

- `--public-host`: externally reachable node IP/host.
- `--flow-server`: explicit flow server target.
- `--config-service`: explicit config service target.
- `--conversation-id`: optional conversation id passed through to the request payload.
- `--rag-uri`: override resolved rag URI.
- `--llm-agent-image`: override LLM operator image.
- `--chromadb-retriever-image`: override retriever operator image.
