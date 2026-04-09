import unittest

from example_apps.lumeflow_rag.common.rag_uri import isPrivateHost
from example_apps.lumeflow_rag.common.rag_uri import parseRagUri
from example_apps.lumeflow_rag.common.rag_uri import validateRagUriEndpointAllowed


class RagUriTests(unittest.TestCase):
    def test_parseRagUriAcceptsHttpUri(self) -> None:
        parsed = parseRagUri(rag_uri="http://localhost:8000/tenant_index_v1")
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.host, "localhost")
        self.assertEqual(parsed.port, 8000)
        self.assertEqual(parsed.collection, "tenant_index_v1")

    def test_parseRagUriRejectsMissingCollection(self) -> None:
        with self.assertRaisesRegex(ValueError, "collection path"):
            parseRagUri(rag_uri="http://localhost:8000")

    def test_parseRagUriRejectsUnsupportedScheme(self) -> None:
        with self.assertRaisesRegex(ValueError, "http or https"):
            parseRagUri(rag_uri="ftp://localhost:8000/docs")

    def test_validateRagUriEndpointAllowedRejectsMissingAllowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            validateRagUriEndpointAllowed(
                rag_uri="https://vector.lume.dev:443/docs",
                allowed_endpoints={"http://localhost:8000"},
            )

    def test_isPrivateHostRecognizesCommonPrivateHosts(self) -> None:
        self.assertTrue(isPrivateHost(host="localhost"))
        self.assertTrue(isPrivateHost(host="127.0.0.1"))
        self.assertTrue(isPrivateHost(host="10.1.2.3"))
        self.assertFalse(isPrivateHost(host="example.com"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
