import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import load_settings  # noqa: E402
from lead_intelligence import build_rule_based_intelligence  # noqa: E402
from scraper import ScanRuntime, determine_scan_depth  # noqa: E402
from storage import LeadStorage  # noqa: E402


class LeadRadarTests(unittest.TestCase):
    def test_default_posts_per_group_is_100(self) -> None:
        with patch.dict(os.environ, {"FB_POSTS_PER_GROUP": "", "POSTS_PER_GROUP_LIMIT": ""}, clear=False):
            settings = load_settings()
        self.assertEqual(settings.fb_posts_per_group, 100)
        self.assertEqual(settings.posts_per_group_limit, 100)

    def test_env_posts_per_group_override_works(self) -> None:
        with patch.dict(os.environ, {"FB_POSTS_PER_GROUP": "140"}, clear=False):
            settings = load_settings()
        self.assertEqual(settings.fb_posts_per_group, 140)
        self.assertEqual(settings.posts_per_group_limit, 140)

    def test_cli_posts_per_group_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LeadStorage(Path(temp_dir) / "test.db")
            with patch.dict(os.environ, {"FB_POSTS_PER_GROUP": "100"}, clear=False):
                settings = load_settings()
            runtime = ScanRuntime(
                rescan=False,
                debug_scan=False,
                loose=False,
                save_debug_leads=False,
                send_telegram=False,
                posts_per_group_override=150,
            )
            depth, quality = determine_scan_depth(runtime, settings, storage, "קבוצה", "https://facebook.com/groups/x")
        self.assertEqual(depth, 150)
        self.assertEqual(quality, 50)

    def test_heat_score_detects_urgent_couple_private_pool_post(self) -> None:
        result = build_rule_based_intelligence(
            "מחפש צימר לזוג למחר עם בריכה פרטית חובה, דחוף ושקט",
            matched_keywords=["זוג", "בריכה פרטית", "למחר"],
            post_timestamp="2 hours ago",
        )
        self.assertGreaterEqual(result.heat_score, 80)
        self.assertEqual(result.heat_label, "hot")
        self.assertEqual(result.decision_bucket, "show")

    def test_negative_signals_reduce_heat(self) -> None:
        positive = build_rule_based_intelligence("מחפשת מקום לזוג להיום עם בריכה פרטית ושקט")
        negative = build_rule_based_intelligence("מחפש הכי זול עד 500 למסיבה עם מנגל וכלב")
        self.assertGreater(positive.heat_score, negative.heat_score)
        self.assertEqual(negative.decision_bucket, "hidden")

    def test_owner_advertising_is_hard_rejected(self) -> None:
        result = build_rule_based_intelligence("הוילה שלנו פנויה לסופ\"ש, לפרטים והזמנות בפרטי")
        self.assertEqual(result.lead_type, "owner_advertiser")
        self.assertEqual(result.decision_bucket, "hidden")
        self.assertTrue(result.owner_advertisement)

    def test_strong_recommendation_request_can_reach_review_or_show(self) -> None:
        result = build_rule_based_intelligence("המלצות על מקום לזוג עם בריכה פרטית קרוב לרחובות לשבת")
        self.assertIn(result.lead_type, {"recommendation_request", "guest_seeker"})
        self.assertIn(result.decision_bucket, {"review", "show"})

    def test_storage_can_filter_hot_leads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LeadStorage(Path(temp_dir) / "test.db")
            storage.save_lead(
                group_name="A",
                group_url="https://facebook.com/groups/a",
                author="Author A",
                post_url="https://facebook.com/groups/a/posts/1",
                post_text="text a",
                cleaned_text="text a",
                matched_keywords=["זוג"],
                keyword_score=5,
                ai_score=None,
                ai_category=None,
                ai_reason_he=None,
                suggested_reply_he=None,
                heat_score=91,
                heat_label="hot",
                heat_level="hot",
                heat_reasons_json='["Urgent today/tomorrow request"]',
                relevance_score=84,
                decision_bucket="show",
                status="new",
            )
            storage.save_lead(
                group_name="B",
                group_url="https://facebook.com/groups/b",
                author="Author B",
                post_url="https://facebook.com/groups/b/posts/2",
                post_text="text b",
                cleaned_text="text b",
                matched_keywords=["מקום"],
                keyword_score=2,
                ai_score=None,
                ai_category=None,
                ai_reason_he=None,
                suggested_reply_he=None,
                heat_score=35,
                heat_label="cold",
                heat_level="cold",
                heat_reasons_json='["Low urgency"]',
                relevance_score=40,
                decision_bucket="hidden",
                status="new",
            )
            self.assertEqual(storage.count_filtered_leads(heat_label="hot", show_all=True), 1)
            hot_leads = storage.list_leads(heat_label="hot", show_all=True, include_owner_ads=True)
            self.assertEqual(len(hot_leads), 1)
            strong_leads = storage.list_leads(decision_bucket="show", show_all=True, include_owner_ads=True)
            self.assertEqual(len(strong_leads), 1)


if __name__ == "__main__":
    unittest.main()
