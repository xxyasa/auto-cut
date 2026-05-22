import unittest
from pathlib import Path
from unittest.mock import patch

from autocut.remix import (
    build_remix_plan,
    build_remix_source,
    build_script_units,
    export_remix_plan,
    remix_export_segments,
    remix_plan_from_items,
    remix_plan_from_ordered_ids,
    score_remix_plan,
)


class RemixTests(unittest.TestCase):
    def test_filters_non_product_live_ops(self):
        result = {
            "transcript": [
                {"start": 0.0, "end": 3.0, "text": "我们马上下播最后排单一下"},
                {"start": 3.0, "end": 7.0, "text": "这把哈利波特联名透明伞图案非常精致"},
            ]
        }

        units = build_script_units(
            result,
            product="Pinkypinky联名哈利波特磨砂窗透明伞",
            selling_points=["缤纷精致图案"],
        )

        self.assertTrue(units[0]["excluded"])
        self.assertEqual(units[0]["exclude_reason"], "非产品介绍/直播操作")
        self.assertFalse(units[1]["excluded"])

    def test_local_plan_can_reorder_without_changing_text(self):
        result = {
            "transcript": [
                {"start": 0.0, "end": 4.0, "text": "这把哈利波特联名透明伞是学院风设计"},
                {"start": 4.0, "end": 9.0, "text": "加厚防水PEO伞布雨天通勤也很方便"},
                {"start": 9.0, "end": 13.0, "text": "加强加固伞骨打开以后很稳"},
                {"start": 13.0, "end": 18.0, "text": "今天福利优惠拍下就能带走"},
            ]
        }

        source = build_remix_source(
            result,
            {
                "product": "Pinkypinky联名哈利波特磨砂窗透明伞",
                "selling_points": ["加强加固伞骨", "加厚防水PEO伞布"],
                "brand_terms": ["Pinkypinky", "哈利波特", "透明伞"],
            },
            target_duration=15,
        )
        plan = source["default_plan"]

        self.assertEqual(plan["items"][0]["text"], "这把哈利波特联名透明伞是学院风设计")
        self.assertEqual(plan["items"][-1]["text"], "今天福利优惠拍下就能带走")
        self.assertIn("这把哈利波特联名透明伞是学院风设计", plan["script_text"])
        self.assertEqual(plan["items"][-1]["id"], "s004")
        self.assertIn("品牌/热词：Pinkypinky、哈利波特、透明伞", source["prompt"])
        self.assertIn("投放场景：千川短视频带货", source["prompt"])
        self.assertIn("第一条必须是正常开头", source["prompt"])
        self.assertIn("为什么第一句不突兀", source["prompt"])
        self.assertIn("可以选择“有优惠/有折扣/有活动/福利”", source["prompt"])
        self.assertIn("禁止选择或描述具体售价", source["prompt"])
        self.assertNotIn("价格福利", source["prompt"])
        self.assertIn("总时长控制在15秒左右", source["prompt"])
        self.assertIn("目标时长：15秒左右", source["prompt"])
        self.assertIn("在15秒左右接收到足够多的卖点信息", source["prompt"])
        self.assertNotIn("30秒内", source["prompt"])

    def test_price_claims_are_filtered_but_discount_words_are_allowed(self):
        result = {
            "transcript": [
                {"start": 0.0, "end": 3.0, "text": "这款手机包有五个颜色可以选"},
                {"start": 3.0, "end": 6.0, "text": "今天有优惠有折扣活动"},
                {"start": 6.0, "end": 9.0, "text": "直播间到手价只要39元"},
            ]
        }

        units = build_script_units(result, product="手机包", selling_points=["五个颜色"])

        self.assertFalse(units[1]["excluded"])
        self.assertEqual(units[1]["role"], "close")
        self.assertTrue(units[2]["excluded"])
        self.assertEqual(units[2]["exclude_reason"], "价格信息/具体售价")

    def test_live_audience_interactions_are_filtered(self):
        result = {
            "transcript": [
                {"start": 0.0, "end": 2.5, "text": "有宝宝问这个伞是不是防水"},
                {"start": 2.5, "end": 5.0, "text": "这把透明伞雨天通勤很方便"},
            ]
        }

        units = build_script_units(result, product="透明伞", selling_points=["防水"])

        self.assertTrue(units[0]["excluded"])
        self.assertEqual(units[0]["exclude_reason"], "直播互动/回复观众")
        self.assertFalse(units[1]["excluded"])

    def test_other_link_products_are_filtered_without_target_hit(self):
        result = {
            "transcript": [
                {"start": 0.0, "end": 3.0, "text": "另一款手机包在三号链接"},
                {"start": 3.0, "end": 6.0, "text": "这款透明伞是哈利波特学院风设计"},
            ]
        }

        units = build_script_units(
            result,
            product="哈利波特透明伞",
            selling_points=["学院风设计"],
        )

        self.assertTrue(units[0]["excluded"])
        self.assertEqual(units[0]["exclude_reason"], "其他链接/非主品")
        self.assertFalse(units[1]["excluded"])

    def test_brand_terms_are_valid_target_terms_without_product_name(self):
        result = {
            "transcript": [
                {"start": 0.0, "end": 3.0, "text": "这把哈利波特学院伞图案非常精致"},
            ]
        }

        source = build_remix_source(
            result,
            {
                "product": "",
                "selling_points": [],
                "brand_terms": ["哈利波特", "学院伞"],
            },
            target_duration=15,
        )
        quality = score_remix_plan(
            source["default_plan"],
            {
                "product": "",
                "selling_points": [],
                "brand_terms": ["哈利波特", "学院伞"],
            },
            target_duration=15,
        )

        self.assertFalse(source["units"][0]["excluded"])
        self.assertNotIn("未出现目标商品", [risk["label"] for risk in quality["risks"]])

    def test_scores_risky_remix_plan_with_actionable_labels(self):
        plan = {
            "target_duration": 25,
            "duration": 16.0,
            "script_text": "有宝宝问这个是不是防水\n另一款手机包在三号链接",
            "items": [
                {"text": "有宝宝问这个是不是防水", "role": "proof", "duration": 8.0},
                {"text": "另一款手机包在三号链接", "role": "proof", "duration": 8.0},
            ],
        }

        quality = score_remix_plan(
            plan,
            {"product": "哈利波特透明伞", "selling_points": ["学院风设计"]},
            target_duration=25,
        )

        self.assertEqual(quality["level"], "risk")
        self.assertIn("直播互动", [risk["label"] for risk in quality["risks"]])
        self.assertIn("疑似非主品", [risk["label"] for risk in quality["risks"]])

    def test_scores_clean_remix_plan_as_good(self):
        plan = {
            "target_duration": 25,
            "duration": 22.0,
            "script_text": "这款哈利波特透明伞是学院风设计\n伞面图案非常精致\n雨天通勤也很方便\n礼袋包装送人很合适",
            "items": [
                {"text": "这款哈利波特透明伞是学院风设计", "role": "identity", "duration": 5.0},
                {"text": "伞面图案非常精致", "role": "selling_point", "duration": 5.0},
                {"text": "雨天通勤也很方便", "role": "demo", "duration": 5.0},
                {"text": "礼袋包装送人很合适", "role": "close", "duration": 7.0},
            ],
        }

        quality = score_remix_plan(
            plan,
            {"product": "哈利波特透明伞", "selling_points": ["学院风设计", "伞面图案"]},
            target_duration=25,
        )

        self.assertEqual(quality["level"], "good")
        self.assertGreaterEqual(quality["score"], 90)

    def test_remix_units_use_clean_text_for_prompt(self):
        result = {
            "transcript": [
                {
                    "start": 0.0,
                    "end": 4.0,
                    "text": "这样的一个伞面给大家去看一下它的学院图案这样子的等等",
                    "clean_text": "伞面它的学院图案",
                    "filler_ratio": 0.5,
                }
            ]
        }

        source = build_remix_source(
            result,
            {
                "product": "Pinkypinky联名哈利波特磨砂窗透明伞",
                "selling_points": ["缤纷精致图案"],
                "brand_terms": ["哈利波特"],
            },
        )

        self.assertEqual(source["units"][0]["text"], "伞面它的学院图案")
        self.assertNotIn("这样子的等等", source["prompt"])

    def test_adjacent_short_asr_segments_are_merged_for_remix_prompt(self):
        result = {
            "transcript": [
                {"start": 0.0, "end": 1.4, "text": "这款透明伞"},
                {"start": 1.65, "end": 3.2, "text": "是哈利波特学院风设计"},
                {"start": 3.45, "end": 5.0, "text": "伞面图案非常精致"},
            ]
        }

        units = build_script_units(
            result,
            product="哈利波特透明伞",
            selling_points=["学院风设计", "伞面图案"],
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["source_index"], 0)
        self.assertEqual(units[0]["source_indexes"], [0, 1, 2])
        self.assertIn("这款透明伞，是哈利波特学院风设计", units[0]["text"])
        self.assertEqual(units[0]["duration"], 5.0)

    def test_prompt_marks_risky_opening_lines(self):
        result = {
            "transcript": [
                {"start": 0.0, "end": 2.0, "text": "像这把伞也做到的"},
                {"start": 2.0, "end": 5.0, "text": "这把哈利波特联名透明伞是学院风设计"},
            ]
        }

        source = build_remix_source(
            result,
            {
                "product": "Pinkypinky联名哈利波特磨砂窗透明伞",
                "selling_points": ["缤纷精致图案"],
                "brand_terms": ["哈利波特"],
            },
        )

        self.assertIn("s001 | 2.0s | 角色:identity | 开头慎用:承接句/可能缺少前文", source["prompt"])

    def test_model_order_uses_ids_exactly(self):
        units = [
            {"id": "s001", "source_index": 0, "start": 0.0, "end": 3.0, "duration": 3.0, "text": "第一句", "role": "identity", "score": 5, "excluded": False},
            {"id": "s002", "source_index": 1, "start": 3.0, "end": 6.0, "duration": 3.0, "text": "第二句", "role": "hook", "score": 8, "excluded": False},
        ]

        plan = remix_plan_from_ordered_ids(units, ["s002", "s001"], target_duration=6)

        self.assertEqual(plan["ordered_ids"], ["s002", "s001"])
        self.assertEqual(plan["script_text"], "第二句\n第一句")

    def test_model_order_is_capped_to_target_duration(self):
        units = [
            {
                "id": f"s{index:03d}",
                "source_index": index - 1,
                "start": float((index - 1) * 5),
                "end": float(index * 5),
                "duration": 5.0,
                "text": f"第{index}句",
                "role": "selling_point",
                "score": 5,
                "excluded": False,
            }
            for index in range(1, 8)
        ]

        plan = remix_plan_from_ordered_ids(
            units,
            ["s001", "s002", "s003", "s004", "s005", "s006", "s007"],
            target_duration=25,
        )

        self.assertEqual(plan["duration"], 25.0)
        self.assertEqual(plan["ordered_ids"], ["s001", "s002", "s003", "s004", "s005"])
        self.assertEqual(plan["duration_window"]["max"], 28.0)

    def test_model_order_keeps_opening_and_fits_closer_to_target(self):
        units = [
            {"id": "s044", "source_index": 43, "start": 140.28, "end": 159.62, "duration": 19.34, "text": "开头身份", "role": "identity", "score": 12, "excluded": False},
            {"id": "s048", "source_index": 47, "start": 193.32, "end": 201.72, "duration": 8.4, "text": "长款式介绍", "role": "proof", "score": 0, "excluded": False},
            {"id": "s026", "source_index": 25, "start": 57.38, "end": 59.46, "duration": 2.08, "text": "非一次性", "role": "identity", "score": 20, "excluded": False},
            {"id": "s013", "source_index": 12, "start": 33.34, "end": 35.48, "duration": 2.14, "text": "非PVC", "role": "proof", "score": 0, "excluded": False},
            {"id": "s014", "source_index": 13, "start": 35.48, "end": 37.18, "duration": 1.7, "text": "POE材质", "role": "selling_point", "score": 15, "excluded": False},
        ]

        plan = remix_plan_from_ordered_ids(
            units,
            ["s044", "s048", "s026", "s013", "s014"],
            target_duration=25,
        )

        self.assertEqual(plan["ordered_ids"], ["s044", "s026", "s013", "s014"])
        self.assertEqual(plan["duration"], 25.26)

    def test_model_order_short_plan_is_filled_from_remaining_units(self):
        units = [
            {"id": "s001", "source_index": 0, "start": 0.0, "end": 13.0, "duration": 13.0, "text": "补充卖点", "role": "proof", "score": 0, "excluded": False},
            {"id": "s002", "source_index": 1, "start": 13.0, "end": 19.2, "duration": 6.2, "text": "核心卖点", "role": "selling_point", "score": 35, "excluded": False},
            {"id": "s003", "source_index": 2, "start": 19.2, "end": 24.88, "duration": 5.68, "text": "外观颜色", "role": "appearance", "score": 6, "excluded": False},
        ]

        plan = remix_plan_from_ordered_ids(units, ["s003", "s002"], target_duration=30)

        self.assertEqual(plan["duration"], 24.88)
        self.assertEqual(set(plan["ordered_ids"]), {"s001", "s002", "s003"})

    def test_manual_remix_items_keep_source_ranges(self):
        plan = remix_plan_from_items(
            [
                {
                    "id": "manual_p001",
                    "start": 8.2,
                    "end": 12.4,
                    "text": "手动替换进来的原视频口播",
                    "manual_source": True,
                    "source_piece_id": "p001",
                }
            ],
            target_duration=25,
        )

        self.assertEqual(plan["ordered_ids"], ["manual_p001"])
        self.assertTrue(plan["items"][0]["manual_source"])
        self.assertEqual(plan["items"][0]["start"], 8.2)

    def test_export_remix_plan_removes_word_level_fillers(self):
        source_video = Path("source.mp4")
        output_path = Path("out.mp4")
        result = {
            "media": {"path": str(source_video)},
            "transcript": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "这样子的伞面很精致",
                    "clean_text": "伞面很精致",
                    "words": [
                        {"word": "这样子的", "start": 0.0, "end": 0.4},
                        {"word": "伞面", "start": 0.4, "end": 0.9},
                        {"word": "很精致", "start": 0.9, "end": 2.0},
                    ],
                }
            ],
        }
        plan = {
            "items": [
                {
                    "id": "s001",
                    "source_index": 0,
                    "start": 0.0,
                    "end": 2.0,
                    "duration": 2.0,
                    "text": "伞面很精致",
                }
            ]
        }

        captured = {}

        def fake_export(_, __, ranges):
            captured["ranges"] = ranges
            return None

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("autocut.remix.media.export_clip_segments", side_effect=fake_export),
        ):
            warning = export_remix_plan(result, plan, output_path)

        self.assertIsNone(warning)
        self.assertGreater(captured["ranges"][0]["start"], 0.0)

    def test_remix_export_segments_uses_same_clean_ranges(self):
        result = {
            "media": {"path": "source.mp4"},
            "transcript": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "这样子的伞面很精致",
                    "clean_text": "伞面很精致",
                    "words": [
                        {"word": "这样子的", "start": 0.0, "end": 0.4},
                        {"word": "伞面", "start": 0.4, "end": 0.9},
                        {"word": "很精致", "start": 0.9, "end": 2.0},
                    ],
                }
            ],
        }
        plan = {
            "items": [
                {
                    "id": "s001",
                    "source_index": 0,
                    "start": 0.0,
                    "end": 2.0,
                    "duration": 2.0,
                    "text": "伞面很精致",
                }
            ]
        }

        segments = remix_export_segments(result, plan)

        self.assertEqual(len(segments), 1)
        self.assertGreater(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["text"], "伞面很精致")


if __name__ == "__main__":
    unittest.main()
