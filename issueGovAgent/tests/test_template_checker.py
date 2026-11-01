"""模板和 Markdown 行为测试；使用固定样例及仓库真实模板。"""

import tempfile
import unittest
from pathlib import Path

from src.issue_checker import _fields, _sections, check_issue, load_templates


TEMPLATE_DIR = Path(__file__).resolve().parents[2] / ".github/ISSUE_TEMPLATE"


def template(name="缺陷", prefix="[Bug]: "):
    return {
        "name": name,
        "title": prefix,
        "body": [
            {"type": "markdown", "attributes": {"value": "填写说明"}},
            {"type": "textarea", "attributes": {
                "label": "问题描述", "placeholder": "请描述问题",
            }, "validations": {"required": True}},
            {"type": "input", "attributes": {"label": "环境"},
             "validations": {"required": True}},
            {"type": "textarea", "attributes": {"label": "备注"},
             "validations": {"required": False}},
            {"type": "input", "attributes": {"label": "联系方式"}},
        ],
    }


class TemplateTests(unittest.TestCase):
    def test_load_yaml_files_in_order_and_skip_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, content in {
                "b.yml": "name: 第二份\nbody: []\n",
                "a.yaml": "name: 第一份\nbody: []\n",
                "config.yml": "blank_issues_enabled: true\n",
                "config.yaml": "blank_issues_enabled: false\n",
                "README.md": "不是模板",
            }.items():
                (root / filename).write_text(content, encoding="utf-8")
            self.assertEqual(load_templates(root), [
                {"name": "第一份", "body": []}, {"name": "第二份", "body": []},
            ])

    def test_fields_keep_rules_and_optional_inputs(self):
        source = template()
        self.assertEqual(_fields(source), source["body"][1:])

    def test_invalid_template_sets_raise_instead_of_no_match(self):
        invalid_sets = [[], [{"name": "空模板", "body": []}],
                        [{"name": "说明", "body": [template()["body"][0]]}]]
        for kind in ["checkboxes", "dropdown"]:
            unsupported = template()
            unsupported["body"][1]["type"] = kind
            invalid_sets.append([unsupported])
        for templates in invalid_sets:
            with self.subTest(templates=templates), self.assertRaises(ValueError):
                check_issue("[Bug]: 启动失败", "", templates)

    def test_all_repository_templates_accept_required_fields_only(self):
        templates = load_templates(TEMPLATE_DIR)
        self.assertEqual(len(templates), 4)
        for item in templates:
            with self.subTest(template=item["name"]):
                body = "\n\n".join(
                    f"### {field['attributes']['label']}\n实际填写的内容"
                    for field in item["body"]
                    if field.get("validations", {}).get("required")
                )
                result = check_issue(item["title"] + "测试", body, templates)
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["matched_templates"], [item["name"]])
                self.assertEqual(len(result["checks"]), len(templates))


class SectionTests(unittest.TestCase):
    def test_nested_sections_include_body_but_not_child_labels(self):
        body = ("## 缺陷反馈\n总述\n### 问题描述\n启动失败\n"
                "#### Linux\n缺少动态库\n#### Windows\n闪退\n"
                "### 环境\nUbuntu\n## 其他\n结束")
        self.assertEqual(_sections(body), {
            "缺陷反馈": "总述\n启动失败\n缺少动态库\n闪退\nUbuntu",
            "问题描述": "启动失败\n缺少动态库\n闪退",
            "Linux": "缺少动态库", "Windows": "闪退",
            "环境": "Ubuntu", "其他": "结束",
        })

    def test_child_headings_without_body_leave_parent_empty(self):
        self.assertEqual(_sections("## 问题描述\n### Linux\n### Windows"),
                         {"问题描述": "", "Linux": "", "Windows": ""})

    def test_duplicate_sections_merge_content(self):
        self.assertEqual(_sections("### 日志\n第一次\n### 日志\n第二次"),
                         {"日志": "第一次\n第二次"})

    def test_code_quote_and_list_headings_do_not_create_sections(self):
        for body in ["```md\n### 问题描述\n示例\n```",
                     "> ### 问题描述\n> 示例", "- ### 问题描述\n\n  示例"]:
            with self.subTest(body=body):
                self.assertEqual(_sections(body), {})

    def test_comments_are_removed_but_literal_code_is_kept(self):
        self.assertEqual(_sections(
            "### 日志\n<!-- 填写\n说明 -->\n```html\n<!-- 实际日志 -->\n```"
        ), {"日志": "<!-- 实际日志 -->"})


class CheckIssueTests(unittest.TestCase):
    def check(self, body, title="[Bug]: 启动失败"):
        return check_issue(title, body, [template()])

    def test_title_error_does_not_stop_collecting_field_problems(self):
        result = self.check("### 环境\n", title="启动失败")
        self.assertEqual(result, {
            "status": "no_match", "matched_templates": [],
            "checks": [{"template": "缺陷", "problems": [
                {"field": "title", "reason": "title_prefix",
                 "message": "标题应以 '[Bug]: ' 开头"},
                {"field": "问题描述", "reason": "missing_section",
                 "message": "缺少此必填章节（不代表正文没有相关信息）"},
                {"field": "环境", "reason": "empty_section",
                 "message": "此必填章节为空或仅含占位内容"},
            ]}],
        })

    def test_empty_content_and_exact_placeholders_fail(self):
        for content in ["", " \n ", "<!-- 待补充 -->", "_No response_",
                        "No response", "请描述问题", "#### Linux", "```\n```"]:
            with self.subTest(content=content):
                result = self.check(f"### 问题描述\n{content}\n\n### 环境\nLinux")
                self.assertEqual(result["status"], "no_match")
                self.assertEqual([(p["field"], p["reason"]) for p in
                                  result["checks"][0]["problems"]],
                                 [("问题描述", "empty_section")])

    def test_code_images_nested_and_placeholder_with_answer_count(self):
        for content in ["```python\n# 实际日志\n```", "    实际日志\n",
                        "![日志](https://example.com/log.png)",
                        "#### Linux\n启动失败", "请描述问题：启动失败"]:
            with self.subTest(content=content):
                result = self.check(f"### 问题描述\n\n{content}\n\n### 环境\nLinux")
                self.assertEqual(result["status"], "pass")

    def test_optional_missing_or_empty_and_extra_sections_are_allowed(self):
        for suffix in ["", "\n### 备注\n\n### 联系方式\n", "\n### 额外章节\n其他内容"]:
            with self.subTest(suffix=suffix):
                self.assertEqual(self.check(
                    "### 问题描述\n启动失败\n### 环境\nLinux" + suffix
                )["checks"], [{"template": "缺陷", "problems": []}])

    def test_same_or_higher_heading_stops_empty_section(self):
        for next_heading in ["### 环境", "## 环境"]:
            with self.subTest(next_heading=next_heading):
                result = self.check(f"### 问题描述\n{next_heading}\nLinux")
                self.assertEqual([p["reason"] for p in result["checks"][0]["problems"]],
                                 ["empty_section"])

    def test_heading_level_is_not_checked_but_label_must_match(self):
        self.assertEqual(self.check("# 问题描述\n失败\n## 环境\nLinux")["status"], "pass")
        result = self.check("### 故障描述\n失败\n### 环境\nLinux")
        self.assertEqual(result["checks"][0]["problems"][0]["reason"], "missing_section")

    def test_prefix_space_is_significant_and_absent_prefix_is_unrestricted(self):
        body = "### 问题描述\n失败\n### 环境\nLinux"
        self.assertEqual(self.check(body, title="[Bug]:失败")["status"], "no_match")
        unrestricted = template()
        del unrestricted["title"]
        self.assertEqual(check_issue("任意标题", body, [unrestricted])["status"], "pass")

    def test_overlapping_templates_all_match_and_other_failures_are_retained(self):
        result = check_issue("[Bug]: 失败", "### 问题描述\n失败\n### 环境\nLinux", [
            template("模板A"), template("模板B"), template("模板C", "[Question]: "),
        ])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["matched_templates"], ["模板A", "模板B"])
        self.assertEqual(result["checks"][2]["problems"][0]["reason"], "title_prefix")


if __name__ == "__main__":
    unittest.main()
