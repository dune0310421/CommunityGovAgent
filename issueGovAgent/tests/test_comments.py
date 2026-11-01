"""检查结果如何展示为评论，不调用网络。"""

import unittest

from src.issue_checker import MARKER, format_comment


def check(name, *reasons):
    return {"template": name, "problems": [
        {"field": f"字段{i}", "reason": reason, "message": f"{name}问题{i}"}
        for i, reason in enumerate(reasons)
    ]}


class CommentTests(unittest.TestCase):
    def render(self, checks, status="no_match", matched=None):
        return format_comment({"status": status, "matched_templates": matched or [],
                               "checks": checks}, "example/project")

    def test_pass_lists_matches_and_omits_unmatched_problems(self):
        body = self.render([check("模板A"), check("模板B"), check("模板C", "title_prefix")],
                           status="pass", matched=["模板A", "模板B"])
        self.assertTrue(body.startswith(MARKER + "\n"))
        self.assertIn("匹配模板：模板A、模板B。", body)
        self.assertNotIn("模板C", body)
        self.assertNotIn("<details>", body)
        self.assertIn("本检查仅验证格式", body)

    def test_known_prefix_shows_all_problems_for_matching_candidates_only(self):
        body = self.render([
            check("缺陷", "missing_section", "empty_section"),
            check("另一个候选", "empty_section"),
            check("咨询", "missing_section", "title_prefix", "empty_section"),
        ])
        self.assertIn("<details><summary>缺陷</summary>", body)
        self.assertIn("<details><summary>另一个候选</summary>", body)
        self.assertIn("- 字段0：缺陷问题0", body)
        self.assertIn("- 字段1：缺陷问题1", body)
        self.assertNotIn("咨询", body)
        self.assertEqual(body.count("</details>"), 2)
        self.assertIn("[查看可用模板](https://github.com/example/project/issues/new/choose)", body)

    def test_unknown_prefix_shows_every_template_and_preserves_later_problems(self):
        body = self.render([
            check("缺陷", "title_prefix", "missing_section"),
            check("咨询", "title_prefix", "empty_section"),
        ])
        for name in ["缺陷", "咨询"]:
            self.assertIn(f"<summary>{name}</summary>", body)
            self.assertIn(f"- 字段0：{name}问题0", body)
            self.assertIn(f"- 字段1：{name}问题1", body)
        self.assertIn("未匹配现有模板的完整格式", body)
        self.assertIn("本检查仅验证格式", body)


if __name__ == "__main__":
    unittest.main()
