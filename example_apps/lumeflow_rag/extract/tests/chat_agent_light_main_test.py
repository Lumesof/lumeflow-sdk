import unittest
from types import SimpleNamespace

from example_apps.lumeflow_rag.extract.chat_agent_light_main import _resolveLightAgentImages
from example_apps.lumeflow_rag.extract.chat_agent_light_main import _resolveOwnerAndAppId
from example_apps.lumeflow_rag.extract.chat_agent_light_main import buildParser


class ChatAgentLightMainTests(unittest.TestCase):
    def test_buildParserIncludesSkipTtyCheckFlag(self) -> None:
        parser = buildParser()
        args = parser.parse_args(["--skip-tty-check"])
        self.assertTrue(args.skip_tty_check)

    def test_buildParserIncludesReuseExistingFlag(self) -> None:
        parser = buildParser()
        args = parser.parse_args(["--reuse-existing"])
        self.assertTrue(args.reuse_existing)

    def test_resolveOwnerAndAppIdFallsBackToDefaults(self) -> None:
        owner, appId = _resolveOwnerAndAppId(
            SimpleNamespace(
                owner="   ",
                app_id="",
            )
        )
        self.assertEqual(owner, "lumeflow-rag-agent-light")
        self.assertEqual(appId, "lumeflow-rag-agent-light-local")

    def test_resolveLightAgentImagesUsesExpectedDefaults(self) -> None:
        parser = buildParser()
        args = parser.parse_args([])
        images = _resolveLightAgentImages(args)
        self.assertIn("@sha256:", images.llm_agent_image_url)
        self.assertIn("@sha256:", images.chromadb_retriever_image_url)

    def test_resolveLightAgentImagesHonorsOverrides(self) -> None:
        parser = buildParser()
        args = parser.parse_args(
            [
                "--llm-agent-image",
                "custom/llm:latest",
                "--chromadb-retriever-image",
                "custom/retriever:latest",
            ]
        )
        images = _resolveLightAgentImages(args)
        self.assertEqual(images.llm_agent_image_url, "custom/llm:latest")
        self.assertEqual(images.chromadb_retriever_image_url, "custom/retriever:latest")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
