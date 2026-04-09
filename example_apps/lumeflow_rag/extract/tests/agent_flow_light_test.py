import unittest

from example_apps.lumeflow_rag.extract.agent_flow_light import SyncAgentOperatorImages
from example_apps.lumeflow_rag.extract.agent_flow_light import buildSyncAgentLightDag
from example_apps.lumeflow_rag.extract.agent_flow_light import buildSyncAgentLightSubmitJobRequest
from lumesof.lumeflow import Proto


class AgentFlowLightTests(unittest.TestCase):
    def test_buildSyncAgentLightDagUsesLightDagName(self) -> None:
        dag = buildSyncAgentLightDag(images=self._images())
        self.assertEqual(dag.name, "lumeflow-rag-sync-chat-loop-light-v1")
        self.assertCountEqual(
            [operator.name for operator in dag.operators],
            ["rag-llm-agent", "rag-chromadb-retriever"],
        )

    def test_buildSyncAgentLightSubmitJobRequestUsesLoopSemantics(self) -> None:
        request = buildSyncAgentLightSubmitJobRequest(
            owner="owner-light",
            cluster_id="11111111-1111-1111-1111-111111111111",
            app_id="light-chat-app",
            images=self._images(),
        )
        self.assertEqual(request.owner, "owner-light")
        self.assertEqual(request.app_id, "light-chat-app")
        self.assertTrue(request.job_dag.allow_loops)
        self.assertEqual(request.job_dag.name, "lumeflow-rag-sync-chat-loop-light-v1")
        self.assertEqual(sum(1 for link in request.job_dag.links if link.link_type == Proto.dag.Link.SYNC_INJECTOR), 1)
        self.assertEqual(sum(1 for link in request.job_dag.links if link.link_type == Proto.dag.Link.SYNC_RETRIEVER), 1)

    def _images(self) -> SyncAgentOperatorImages:
        return SyncAgentOperatorImages(
            llm_agent_image_url="registry/llm-agent@sha256:4444444444444444444444444444444444444444444444444444444444444444",
            chromadb_retriever_image_url="registry/chromadb-retriever@sha256:5555555555555555555555555555555555555555555555555555555555555555",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
