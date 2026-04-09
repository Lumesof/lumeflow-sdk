import unittest

from example_apps.lumeflow_rag.ingest.ingest_flow_light import AsyncIngestOperatorImages
from example_apps.lumeflow_rag.ingest.ingest_flow_light import buildAsyncIngestLightDag
from example_apps.lumeflow_rag.ingest.ingest_flow_light import buildAsyncIngestLightSubmitJobRequest
from lumesof.lumeflow import Proto


class IngestFlowLightTests(unittest.TestCase):
    def test_buildAsyncIngestLightDagUsesLightDagName(self) -> None:
        dag = buildAsyncIngestLightDag(images=self._images())
        self.assertEqual(dag.name, "lumeflow-rag-async-ingest-light-v1")
        self.assertCountEqual(
            [operator.name for operator in dag.operators],
            ["rag-structure-extractor", "rag-chunker", "rag-chromadb-indexer"],
        )

    def test_buildAsyncIngestLightSubmitJobRequestUsesAsyncSemantics(self) -> None:
        request = buildAsyncIngestLightSubmitJobRequest(
            owner="owner-light",
            cluster_id="11111111-1111-1111-1111-111111111111",
            images=self._images(),
        )
        self.assertEqual(request.owner, "owner-light")
        self.assertEqual(request.app_id, "")
        self.assertFalse(request.job_dag.allow_loops)
        self.assertEqual(request.job_dag.name, "lumeflow-rag-async-ingest-light-v1")
        self.assertEqual(sum(1 for link in request.job_dag.links if link.link_type == Proto.dag.Link.ASYNC_INJECTOR), 1)
        self.assertEqual(sum(1 for link in request.job_dag.links if link.link_type == Proto.dag.Link.SYNC_RETRIEVER), 0)

    def _images(self) -> AsyncIngestOperatorImages:
        return AsyncIngestOperatorImages(
            structure_extractor_image_url="registry/structure-extractor@sha256:1111111111111111111111111111111111111111111111111111111111111111",
            chunker_image_url="registry/chunker@sha256:2222222222222222222222222222222222222222222222222222222222222222",
            chromadb_indexer_image_url="registry/chromadb-indexer@sha256:3333333333333333333333333333333333333333333333333333333333333333",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
