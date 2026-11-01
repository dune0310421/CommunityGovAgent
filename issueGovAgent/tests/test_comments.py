"""检查结果如何展示为评论，不调用网络。"""

import unittest

from src.issue_checker import MARKER, check_issue, format_comment


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
        ], status="partial_match", matched=["缺陷", "另一个候选"])
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


    def test_body_only_matches_show_only_corresponding_templates(self):
        templates = [
            {"name": "缺陷", "title": "[Bug] ", "body": [
                {"type": "textarea", "attributes": {"label": "问题描述"},
                 "validations": {"required": True}},
                {"type": "input", "attributes": {"label": "日志"}},
            ]},
            {"name": "文档", "title": "[Docs] ", "body": [
                {"type": "textarea", "attributes": {"label": "文档位置"},
                 "validations": {"required": True}},
            ]},
            {"name": "咨询", "title": "[Question] ", "body": [
                {"type": "textarea", "attributes": {"label": "问题描述"},
                 "validations": {"required": True}},
            ]},
        ]
        cases = [
            ("### 问题描述\n启动失败", ["缺陷", "咨询"]),
            ("### 问题描述\n", ["缺陷", "咨询"]),
            ("### 日志\n", ["缺陷"]),  # 空的选填章节也限定候选。
            ("### 文档位置\nREADME", ["文档"]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                result = check_issue("未写前缀", source, templates)
                self.assertEqual(result["status"], "partial_match")
                self.assertEqual(result["matched_templates"], expected)
                body = format_comment(result, "example/project")
                for template in templates:
                    self.assertEqual(f"<summary>{template['name']}</summary>" in body,
                                     template["name"] in expected)
                self.assertIn("标题应以", body)

    def test_title_match_takes_priority_over_body_match_in_either_order(self):
        templates = [
            {"name": "文档", "title": "[Docs] ", "body": [
                {"type": "textarea", "attributes": {"label": "文档位置"},
                 "validations": {"required": True}},
            ]},
            {"name": "缺陷", "title": "[Bug] ", "body": [
                {"type": "textarea", "attributes": {"label": "问题描述"},
                 "validations": {"required": True}},
            ]},
        ]
        for ordered in [templates, templates[::-1]]:
            with self.subTest(order=[t["name"] for t in ordered]):
                result = check_issue("[Bug] 启动失败", "### 文档位置\nREADME", ordered)
                self.assertEqual(result["status"], "partial_match")
                self.assertEqual(result["matched_templates"], ["缺陷"])
                body = format_comment(result, "example/project")
                self.assertIn("<summary>缺陷</summary>", body)
                self.assertNotIn("<summary>文档</summary>", body)

    def test_unrestricted_prefix_is_not_a_title_match(self):
        templates = [
            {"name": "通用", "body": [
                {"type": "textarea", "attributes": {"label": "说明"},
                 "validations": {"required": True}},
            ]},
            {"name": "文档", "title": "[Docs] ", "body": [
                {"type": "textarea", "attributes": {"label": "文档位置"},
                 "validations": {"required": True}},
            ]},
        ]
        result = check_issue("无前缀", "### 文档位置\n", templates)
        self.assertEqual(result["matched_templates"], ["文档"])
        body = format_comment(result, "example/project")
        self.assertNotIn("<summary>通用</summary>", body)
        self.assertIn("<summary>文档</summary>", body)


if __name__ == "__main__":
    unittest.main()
