import unittest

from example_apps.lumeflow_rag.ingest.launch_indexer_light_main import _buildParser
from example_apps.lumeflow_rag.ingest.launch_indexer_light_main import _resolveIndexerLightImages


class LaunchIndexerLightMainTests(unittest.TestCase):
    def test_buildParserParsesRequiredUrl(self) -> None:
        parser = _buildParser()
        args = parser.parse_args(["--url", "https://example.com/doc"])
        self.assertEqual(args.url, "https://example.com/doc")

    def test_resolveIndexerLightImagesUsesExpectedDefaults(self) -> None:
        parser = _buildParser()
        args = parser.parse_args(["--url", "https://example.com/doc"])
        images = _resolveIndexerLightImages(args)
        self.assertIn("@sha256:", images.structure_extractor_image_url)
        self.assertIn("@sha256:", images.chunker_image_url)
        self.assertIn("@sha256:", images.chromadb_indexer_image_url)

    def test_resolveIndexerLightImagesHonorsOverrides(self) -> None:
        parser = _buildParser()
        args = parser.parse_args(
            [
                "--url",
                "https://example.com/doc",
                "--structure-extractor-image",
                "custom/structure:latest",
                "--chunker-image",
                "custom/chunker:latest",
                "--chromadb-indexer-image",
                "custom/indexer:latest",
            ]
        )
        images = _resolveIndexerLightImages(args)
        self.assertEqual(images.structure_extractor_image_url, "custom/structure:latest")
        self.assertEqual(images.chunker_image_url, "custom/chunker:latest")
        self.assertEqual(images.chromadb_indexer_image_url, "custom/indexer:latest")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
