"""Opt-in integration with the approved local models and real Milvus Lite."""

from contextlib import closing
from datetime import date
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from saas_support_agent.knowledge.models import LocalModels
from saas_support_agent.knowledge.retrieval import PolicyIndex, PolicyKnowledge, build_index
from saas_support_agent.merchant_demo import DEMO_IDENTITY, seed_demo
from saas_support_agent.merchant_support import MerchantSupport
from saas_support_agent.repositories.database import Database


@unittest.skipUnless(os.getenv("SAAS_RAG_INTEGRATION") == "1", "需要已准备好的可选 RAG 依赖和模型")
class MerchantRagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = TemporaryDirectory(prefix="merchant-rag-test-")
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        cls.catalog = cls.root / "policies/catalog.json"
        shutil.copytree("data/merchant_policies", cls.catalog.parent)
        cls.index_root = cls.root / "index"
        cls.models = LocalModels(Path(os.getenv("SAAS_RAG_MODEL_DIR", "runtime/rag/models")))
        build_index(cls.index_root, cls.catalog, cls.models)
        cls.index = PolicyIndex(cls.index_root, cls.catalog, cls.models)
        cls.addClassCleanup(cls.index.close)

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(Path(temp.name) / "merchant.sqlite3")
        self.db.initialize()
        seed_demo(self.db)
        self.agent = MerchantSupport(self.db)
        self.knowledge = PolicyKnowledge(self.index, self.agent.notifications.service, today=lambda: date(2026, 11, 6))
        self.agent.knowledge = self.knowledge

    def test_merchant_knowledge_cites_current_policy_and_pdf(self):
        result = self.agent.handle(DEMO_IDENTITY, "退货超期能不能受理？")
        self.assertEqual(result.status, "answered")
        source = result.evidence[0]
        self.assertEqual((source["version"], source["file"], source["section"]),
                         ("2.0", "returns-v2.md", "超期退货"))
        self.assertIn(source["text"], (self.catalog.parent / source["file"]).read_text())
        result = self.agent.handle(DEMO_IDENTITY, "咨询 What is RETURN-LABEL-42?")
        source = next(row for row in result.evidence if row["file"] == "shipping-label.pdf")
        self.assertEqual(source["page"], 1)

    def test_all_modes_filter_ungranted_and_inactive_versions(self):
        question = "蓝箱签收凭证和超期退货"
        scope = self.knowledge.scope(DEMO_IDENTITY, question)
        eligible = [self.index.chunks[key] for key in scope]
        self.assertFalse(any(c["workspace_id"] and c["shop_id"] != "SHOP-1" for c in eligible))
        self.assertFalse(any(c["document_id"] == "returns" and c["version"] != "2.0" for c in eligible))
        for mode in ("dense", "hybrid", "rerank"):
            with self.subTest(mode=mode):
                self.assertTrue({row["id"] for row, _ in self.index.search(question, scope, mode)} <= scope)

    def test_revocation_during_retrieval_does_not_leak(self):
        original = self.index.search

        def retrieve_then_revoke(*args):
            hits = original(*args)
            with closing(self.db.connect()) as conn:
                conn.execute("DELETE FROM shop_access")
            return hits

        question = "咨询 SHOP-1 蓝箱签收凭证？"
        with patch.object(self.index, "search", side_effect=retrieve_then_revoke):
            result = self.agent.handle(DEMO_IDENTITY, question)
        self.assertEqual(result.status, "denied")
        self.assertFalse(result.evidence)

    def test_denied_no_evidence_and_model_failure_have_distinct_results(self):
        denied = self.agent.handle(DEMO_IDENTITY, "咨询 SHOP-2 紫盒验收码？")
        absent = self.agent.handle(DEMO_IDENTITY, "咨询火星天气预报？")
        with patch.object(self.models, "scores", side_effect=RuntimeError("injected")):
            failure = self.agent.handle(DEMO_IDENTITY, "退货超期能不能受理？")
        self.assertEqual((denied.status, absent.status, failure.error_code),
                         ("denied", "no_evidence", "knowledge_unavailable"))
        self.assertFalse(any(result.evidence for result in (denied, absent, failure)))

    def test_failed_index_validation_does_not_publish(self):
        marker = self.index_root / "current"
        original = marker.read_text()
        with patch("saas_support_agent.knowledge.retrieval._open_index", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                build_index(self.index_root, self.catalog, self.models)
        self.assertEqual(marker.read_text(), original)
        self.assertEqual(self.agent.handle(DEMO_IDENTITY, "退货超期能不能受理？").status, "answered")

    def test_changed_source_or_catalog_fails_closed(self):
        for path in (self.catalog.parent / "returns-v2.md", self.catalog):
            previous = path.read_bytes()
            try:
                path.write_bytes(previous + b"\n")
                result = self.agent.handle(DEMO_IDENTITY, "退货超期能不能受理？")
                self.assertEqual(result.error_code, "knowledge_unavailable")
                self.assertFalse(result.evidence)
            finally:
                path.write_bytes(previous)
