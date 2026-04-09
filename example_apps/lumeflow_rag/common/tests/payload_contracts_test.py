import unittest

from example_apps.lumeflow_rag.common.payload_contracts import buildRagUri
from example_apps.lumeflow_rag.common.payload_contracts import buildRagUriFromConfig
from example_apps.lumeflow_rag.common.payload_contracts import normalizeAgentPayload
from example_apps.lumeflow_rag.common.payload_contracts import normalizeIngestPayload
from example_apps.lumeflow_rag.common.payload_contracts import RagRequestPolicy
from example_apps.lumeflow_rag.common.payload_contracts import resolveChromaConfigDefaults


class PayloadContractsTests(unittest.TestCase):
    def test_normalizeIngestPayloadAcceptsValidPayload(self) -> None:
        payload = normalizeIngestPayload(
            payload={
                "source_uri": "https://example.com/doc",
                "rag_uri": "http://localhost:8000/acme_kb_v1",
                "embedding_model": "nomic-embed-text",
                "content": "hello world",
                "metadata": {"tenant": "acme"},
            }
        )
        self.assertEqual(payload.source_uri, "https://example.com/doc")
        self.assertEqual(payload.rag_uri, "http://localhost:8000/acme_kb_v1")
        self.assertEqual(payload.embedding_model, "nomic-embed-text")
        self.assertEqual(payload.metadata, {"tenant": "acme"})

    def test_normalizeIngestPayloadRejectsMessageIdField(self) -> None:
        with self.assertRaisesRegex(ValueError, "infra-internal"):
            normalizeIngestPayload(
                payload={
                    "source_uri": "https://example.com/doc",
                    "rag_uri": "http://localhost:8000/acme_kb_v1",
                    "embedding_model": "nomic-embed-text",
                    "content": "hello world",
                    "message_id": "legacy-message-id",
                }
            )

    def test_normalizeAgentPayloadAppliesDefaults(self) -> None:
        payload = normalizeAgentPayload(
            payload={
                "rag_uri": "http://localhost:8000/acme_kb_v1",
                "prompt": "what is the SLA?",
                "serving_uri": "http://localhost:11434",
            },
            default_model="qwen2.5:3b",
        )
        self.assertEqual(payload.model, "qwen2.5:3b")
        self.assertEqual(payload.conversation_id, "")
        self.assertEqual(payload.response_id, "")
        self.assertEqual(payload.top_k, 15)
        self.assertEqual(payload.request_timeout_sec, 300)

    def test_normalizeAgentPayloadRejectsPromptOverPolicyBound(self) -> None:
        with self.assertRaisesRegex(ValueError, "prompt exceeds policy max length"):
            normalizeAgentPayload(
                payload={
                    "rag_uri": "http://localhost:8000/acme_kb_v1",
                    "prompt": "hello world",
                    "serving_uri": "http://localhost:11434",
                },
                default_model="qwen2.5:3b",
                policy=RagRequestPolicy(max_prompt_chars=5),
            )

    def test_normalizeAgentPayloadRejectsTopKOutOfPolicyBounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "top_k out of policy bounds"):
            normalizeAgentPayload(
                payload={
                    "rag_uri": "http://localhost:8000/acme_kb_v1",
                    "prompt": "what is the SLA?",
                    "serving_uri": "http://localhost:11434",
                    "top_k": 500,
                },
                default_model="qwen2.5:3b",
                policy=RagRequestPolicy(max_top_k=100),
            )

    def test_normalizeAgentPayloadRejectsRequestTimeoutOutOfPolicyBounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "request_timeout_sec out of policy bounds"):
            normalizeAgentPayload(
                payload={
                    "rag_uri": "http://localhost:8000/acme_kb_v1",
                    "prompt": "what is the SLA?",
                    "serving_uri": "http://localhost:11434",
                    "request_timeout_sec": 3600,
                },
                default_model="qwen2.5:3b",
                policy=RagRequestPolicy(max_request_timeout_sec=600),
            )

    def test_normalizeAgentPayloadRejectsEndpointOutsideAllowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            normalizeAgentPayload(
                payload={
                    "rag_uri": "http://localhost:8000/acme_kb_v1",
                    "prompt": "what is the SLA?",
                    "serving_uri": "http://localhost:11434",
                },
                default_model="qwen2.5:3b",
                policy=RagRequestPolicy(
                    allowed_rag_endpoints=frozenset({"http://chromadb:8000"}),
                ),
            )

    def test_normalizeIngestPayloadRejectsContentOverPolicyBound(self) -> None:
        with self.assertRaisesRegex(ValueError, "content exceeds policy max length"):
            normalizeIngestPayload(
                payload={
                    "source_uri": "https://example.com/doc",
                    "rag_uri": "http://localhost:8000/acme_kb_v1",
                    "embedding_model": "nomic-embed-text",
                    "content": "abcdef",
                },
                policy=RagRequestPolicy(max_ingest_content_chars=5),
            )

    def test_buildRagUriUsesStandardCollectionConvention(self) -> None:
        rag_uri = buildRagUri(
            endpoint="http://chromadb:8000",
            tenant_or_project="Acme",
            kb_or_index="Support Index",
            version="2",
        )
        self.assertEqual(rag_uri, "http://chromadb:8000/acme_support-index_v2")

    def test_resolveChromaConfigDefaultsUsesFallbacks(self) -> None:
        defaults = resolveChromaConfigDefaults(configByKey={})
        self.assertEqual(defaults.endpoint, "http://chromadb:8000")
        self.assertEqual(defaults.default_collection, "default")

    def test_resolveChromaConfigDefaultsUsesExportedValues(self) -> None:
        defaults = resolveChromaConfigDefaults(
            configByKey={
                "rag.chroma.endpoint": "http://chromadb-external:8000",
                "rag.chroma.default_collection": "tenant-default",
            }
        )
        self.assertEqual(defaults.endpoint, "http://chromadb-external:8000")
        self.assertEqual(defaults.default_collection, "tenant-default")

    def test_buildRagUriFromConfigUsesExportedEndpoint(self) -> None:
        rag_uri = buildRagUriFromConfig(
            configByKey={"rag.chroma.endpoint": "http://chromadb-external:8000"},
            tenant_or_project="Acme",
            kb_or_index="Support Index",
            version="3",
        )
        self.assertEqual(rag_uri, "http://chromadb-external:8000/acme_support-index_v3")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
