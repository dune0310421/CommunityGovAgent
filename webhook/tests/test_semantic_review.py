"""语义复核的模型契约与完整分析流程回归；模拟模型输出，不连接在线服务。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis
import compliance
from errors import IssueProcessingError

TEMPLATE = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
    {"type": "textarea", "attributes": {"label": "问题描述"}, "validations": {"required": True}},
    {"type": "textarea", "attributes": {"label": "预期结果"}, "validations": {"required": True}},
    {"type": "input", "attributes": {"label": "背景信息"}, "validations": {"required": True}},
    {"type": "dropdown", "attributes": {"label": "触发场景", "options": ["训练", "推理"]},
     "validations": {"required": True}},
    {"type": "checkboxes", "attributes": {"label": "确认项", "options": [
        {"label": "已阅读说明", "required": True}]}}]}
BODY = ("### 问题描述\n启动失败\n### 预期结果\n正常启动\n### 背景信息\nLinux\n"
        "### 触发场景\n训练\n### 确认项\n- [x] 已阅读说明")
PASS = {"status": "pass", "template": "缺陷反馈", "reason": "各字段及选项与模板要求等价且完整", "maintainer": None}


def template_file(template):
    return {"filename": "issue.yml", "source": "example/repo/issue.yml", "content": yaml.safe_dump(template)}


class SemanticReviewTests(unittest.TestCase):
    def setUp(self):
        self.popen = self.enterContext(patch.object(compliance.subprocess, "Popen"))
        self.enterContext(patch.object(compliance.os, "killpg"))
        self.process = self.popen.return_value.__enter__.return_value
        self.process.returncode = 0

    def respond(self, result):
        self.process.communicate.return_value = (json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": json.dumps(result)}), "")

    def analyze(self, title, body, *, templates=None, **kwargs):
        return analysis.analyze_issue(title, body, templates or [template_file(TEMPLATE)],
            "key", "model", "https://model.example", repository="example/repo", number=18, **kwargs)

    def test_exact_match_never_launches_model(self):
        result = self.analyze("[Bug] 新功能", BODY.replace("启动失败", "希望扩展功能"))
        self.assertEqual(result["summary"], {"status": "pass", "recommendation_status": None})
        self.popen.assert_not_called()

    def test_equivalent_inputs_share_one_review_path(self):
        """覆盖标题、字段、下拉、复选框改写进入复核并消费通过结果的路径。"""
        cases = [
            ("大赛 [Bug] 启动失败", BODY),
            ("【缺陷】启动失败", BODY),
            ("[Bug] 启动失败", BODY.replace("问题描述", "问题描述（当前行为）")
             .replace("预期结果", "期望结果").replace("背景信息", "背景")),
            ("[Bug] 启动失败", BODY.replace("\n训练\n", "\n训练；单机任务\n")),
            ("[Bug] 启动失败", BODY.replace("已阅读说明", "已阅读说明（已确认当前版本）")),
            ("【缺陷】启动失败", BODY.replace("问题描述", "当前行为")
             .replace("预期结果", "期望结果").replace("背景信息", "背景")
             .replace("触发场景", "运行阶段").replace("确认项", "提交确认")),
        ]
        local_statuses = set()
        for title, body in cases:
            with self.subTest(title=title, body=body):
                self.popen.reset_mock()
                local = compliance.check_issue(title, body, [TEMPLATE])
                self.assertNotEqual(local["status"], "pass")
                local_statuses.add(local["status"])
                self.respond(PASS)
                result = self.analyze(title, body)
                self.popen.assert_called_once()
                self.assertEqual(result["summary"], {"status": "pass", "recommendation_status": "pass"})
                self.assertFalse(result["publish_comment"])
                self.assertEqual(result["comment"], "")
        self.assertEqual(local_statuses, {"no_match"})

    def test_semantic_pass_updates_existing_comment(self):
        self.respond(PASS)
        result = self.analyze("【缺陷】启动失败", BODY, previously_commented=True)
        self.assertTrue(result["publish_comment"])
        self.assertEqual(result["comment"], "### 提交内容规范性检查：通过")
        self.assertIsNone(result["maintainer_suggestion"])

    def test_only_reviewed_missing_field_is_published(self):
        self.respond({**PASS, "status": "needs_changes", "reason": "缺陷模板适用",
            "problems": [{"field": "背景信息", "reason": "body", "message": "请补充背景信息字段"}]})
        result = self.analyze("大赛 【缺陷】启动失败", BODY.replace("预期结果", "期望结果")
                              .replace("### 背景信息\nLinux\n", ""))
        self.assertEqual(result["summary"], {"status": "no_match", "recommendation_status": "needs_changes"})
        self.assertIn("背景信息：请补充背景信息字段", result["comment"])
        self.assertNotIn("标题应以", result["comment"])
        self.assertNotIn("**模板标题**", result["comment"])
        self.assertIn("**模板正文（Markdown）**", result["comment"])

    def test_title_only_problem_does_not_show_body(self):
        self.respond({**PASS, "status": "needs_changes", "problems": [
            {"field": "title", "reason": "title_prefix", "message": "请补充缺陷类型标识"}]})
        result = self.analyze("启动失败", BODY)
        self.assertIn("**模板标题**", result["comment"])
        self.assertNotIn("**模板正文（Markdown）**", result["comment"])

    def test_body_field_named_title_is_still_a_body_problem(self):
        template = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
            {"type": "input", "attributes": {"label": "title"}, "validations": {"required": True}}]}
        self.respond({**PASS, "status": "needs_changes", "problems": [
            {"field": "title", "reason": "body", "message": "请填写正文中的 title 字段"}]})
        result = self.analyze("[Bug] 启动失败", "", templates=[template_file(template)])
        self.assertIn("请填写正文中的 title 字段", result["comment"])
        self.assertIn("**模板正文（Markdown）**", result["comment"])
        self.assertNotIn("**模板标题**", result["comment"])

    def test_unchecked_required_and_invalid_dropdown_remain_actionable(self):
        self.respond({**PASS, "status": "needs_changes", "problems": [
            {"field": "触发场景", "reason": "body", "message": "请选择训练或推理"},
            {"field": "确认项", "reason": "body", "message": "请勾选已阅读说明"}]})
        result = self.analyze("[Bug] 启动失败", BODY.replace("\n训练\n", "\n未知阶段\n").replace("[x]", "[ ]"))
        self.assertEqual(result["summary"]["status"], "no_match")
        self.assertIn("请选择训练或推理", result["comment"])
        self.assertIn("请勾选已阅读说明", result["comment"])

    def test_review_selects_template_without_local_title_priority(self):
        review_template = {"name": "代码检视", "title": "[Review] ", "body": [
            {"type": "textarea", "attributes": {"label": "评审意见"}, "validations": {"required": True}}]}
        self.respond({"status": "recommended", "template": "代码检视", "maintainer": None,
                      "reason": "标题声明缺陷，但正文主要是跟踪 PR 评审意见，应使用代码检视模板"})
        body = "### 评审意见\n请统一接口命名"
        self.assertEqual(compliance.check_issue("[Bug] 检视", body, [TEMPLATE, review_template])["matched_templates"], [])
        result = self.analyze("[Bug] 检视", body, templates=[template_file(TEMPLATE), template_file(review_template)])
        self.assertEqual(result["summary"], {"status": "no_match", "recommendation_status": "recommended"})
        self.assertIn("#### 模板推荐：代码检视", result["comment"])
        self.assertNotIn("### 预期结果", result["comment"])
        payload = json.loads(self.process.communicate.call_args.args[0])
        self.assertEqual([t["name"] for t in payload["templates"]], ["缺陷反馈", "代码检视"])

    def test_needs_changes_can_be_not_applicable_or_insufficient_info(self):
        for status in ("not_applicable", "insufficient_info"):
            with self.subTest(status=status):
                self.respond({"status": status, "template": None, "reason": "当前诉求无法使用缺陷模板",
                              "maintainer": "建议补充权限申请模板" if status == "not_applicable" else None})
                result = self.analyze("[Bug] 申请", "申请权限", maintainers=("alice",))
                self.assertEqual(result["summary"], {"status": "no_match", "recommendation_status": status})
                self.assertEqual(result["publish_comment"], status == "insufficient_info")
                if status == "not_applicable":
                    self.assertEqual(result["maintainer_suggestion"]["suggestion"], "建议补充权限申请模板")
                    self.assertEqual(json.loads(result["maintainer_suggestion"]["mtr"]), ["alice"])
                else:
                    self.assertIsNone(result["maintainer_suggestion"])
                    self.assertIn("请补充具体诉求", result["comment"])

    def test_payload_preserves_multiple_optional_and_required_option_constraints(self):
        template = deepcopy(TEMPLATE)
        template["body"][3]["attributes"]["multiple"] = True
        template["body"][3]["validations"]["required"] = False
        template["body"].append({"type": "textarea", "attributes": {"label": "补充说明"}})
        markdown = {"filename": "doc.md", "source": "doc.md", "content": (
            '---\nname: 文档反馈\ntitle: "[Docs] "\n---\n### 文档链接（必填）\n<!-- 填写链接 -->')}
        self.respond(PASS)
        self.analyze("大赛 [Bug] 启动失败", BODY, templates=[template_file(template), markdown])
        candidates = json.loads(self.process.communicate.call_args.args[0])["templates"]
        fields = {f["label"]: f for f in candidates[0]["fields"]}
        self.assertTrue(fields["触发场景"]["multiple"])
        self.assertFalse(fields["触发场景"]["required"])
        self.assertEqual(fields["触发场景"]["options"], ["训练", "推理"])
        self.assertEqual(fields["确认项"]["options"], [{"label": "已阅读说明", "required": True}])
        self.assertFalse(fields["补充说明"]["required"])
        self.assertIn("### 文档链接（必填）", candidates[1]["markdown_body"])
        self.assertTrue(candidates[1]["fields"][0]["required"])

    def test_single_json_fence_is_accepted_without_relaxing_field_validation(self):
        decision = {**PASS, "status": "needs_changes", "problems": [
            {"field": "问题描述", "reason": "body", "message": "请补充问题描述"}]}
        for fence in ("```json", "```", "```JSON"):
            with self.subTest(fence=fence):
                self.respond(decision)
                envelope = json.loads(self.process.communicate.return_value[0])
                envelope["result"] = f"  {fence}\n{envelope['result']}\n```\n"
                self.process.communicate.return_value = (json.dumps(envelope), "")
                result = self.analyze("[Bug] 启动失败", "")
                self.assertEqual(result["summary"]["recommendation_status"], "needs_changes")
                self.assertIn("请补充问题描述", result["comment"])

        self.respond({**decision, "problems": [{"field": None, "reason": "body", "message": "补充字段"}]})
        envelope = json.loads(self.process.communicate.return_value[0])
        envelope["result"] = f"```json\n{envelope['result']}\n```"
        self.process.communicate.return_value = (json.dumps(envelope), "")
        with self.assertRaises(IssueProcessingError):
            self.analyze("[Bug] 启动失败", "")

    def test_fenced_json_with_extra_prose_or_multiple_objects_is_rejected(self):
        raw = json.dumps(PASS)
        for content in (f"结果如下：\n```json\n{raw}\n```",
                        f"```json\n{raw}\n```\n以上为结果",
                        f"```json\n{raw}\n{raw}\n```",
                        f"```json\n{raw}\n```\n```json\n{raw}\n```"):
            with self.subTest(content=content):
                self.respond(PASS)
                envelope = json.loads(self.process.communicate.return_value[0])
                envelope["result"] = content
                self.process.communicate.return_value = (json.dumps(envelope), "")
                with self.assertRaises(IssueProcessingError):
                    self.analyze("【缺陷】启动失败", BODY)

    def test_display_field_names_need_not_match_template_labels(self):
        template = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
            {"type": "textarea", "attributes": {
                "label": "Related log / screenshot / 日志 / 截图 (Mandatory / 必填)"},
             "validations": {"required": True}}]}
        for field in ("Related log / screenshot / 日志 / 截图", "日志 / 截图"):
            with self.subTest(field=field):
                self.respond({**PASS, "status": "needs_changes", "problems": [
                    {"field": field, "reason": "body", "message": "请补充日志或截图"}]})
                result = self.analyze("[Bug] 启动失败", "", templates=[template_file(template)])
                self.assertIn(field + "：请补充日志或截图", result["comment"])
                self.assertIn(template["body"][0]["attributes"]["label"], result["comment"])
                self.assertNotIn("**模板标题**", result["comment"])

        self.respond({**PASS, "status": "needs_changes", "problems": [
            {"field": "标题", "reason": "title_prefix", "message": "请补充缺陷类型标识"}]})
        result = self.analyze("启动失败", BODY)
        self.assertIn("标题：请补充缺陷类型标识", result["comment"])
        self.assertIn("**模板标题**", result["comment"])
        self.assertNotIn("**模板正文（Markdown）**", result["comment"])

    def test_invalid_review_problems_retry_without_publishing(self):
        problem = {"field": "问题描述", "reason": "body", "message": "请补充问题描述"}
        cases = [None, [], ["问题"], [{}],
                 *[[{**problem, "field": value}] for value in (None, 123, [], "", "  ")],
                 [{**problem, "reason": "猜测"}], [{**problem, "reason": "missing_section"}], [{**problem, "message": ""}],
                 [{**problem, "message": None}]]
        for problems in cases:
            with self.subTest(problems=problems):
                self.respond({**PASS, "status": "needs_changes", "problems": problems})
                with patch.object(compliance, "format_compliance_check") as formatter:
                    with self.assertRaises(IssueProcessingError) as error:
                        self.analyze("[Bug] 启动失败", "缺少字段")
                    self.assertTrue(error.exception.retryable)
                    formatter.assert_not_called()

    def test_model_failure_does_not_fall_back_to_local_rejection(self):
        with patch.object(compliance, "recommend_template", side_effect=IssueProcessingError("模型响应超时")), patch.object(
                compliance, "format_compliance_check") as formatter:
            with self.assertRaises(IssueProcessingError):
                self.analyze("[Bug] 启动失败", "缺少字段")
            formatter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
