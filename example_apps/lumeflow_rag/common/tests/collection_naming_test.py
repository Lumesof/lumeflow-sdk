import unittest

from example_apps.lumeflow_rag.common.collection_naming import buildCollectionName
from example_apps.lumeflow_rag.common.collection_naming import parseCollectionName


class CollectionNamingTests(unittest.TestCase):
    def test_buildCollectionNameNormalizesSegments(self) -> None:
        collection_name = buildCollectionName(
            tenant_or_project="Acme_Corp",
            kb_or_index="Support FAQs",
            version="1.0",
        )
        self.assertEqual(collection_name, "acme-corp_support-faqs_v1-0")

    def test_parseCollectionNameAcceptsValidName(self) -> None:
        tenant, kb, version = parseCollectionName(collection_name="acme_support_v2")
        self.assertEqual((tenant, kb, version), ("acme", "support", "v2"))

    def test_parseCollectionNameRejectsInvalidSegmentCount(self) -> None:
        with self.assertRaisesRegex(ValueError, "3 underscore-separated"):
            parseCollectionName(collection_name="only_two")

    def test_parseCollectionNameRejectsInvalidVersion(self) -> None:
        with self.assertRaisesRegex(ValueError, "version"):
            parseCollectionName(collection_name="acme_support_release")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
