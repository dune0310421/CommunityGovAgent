"""分析总入口及各项检查评论汇总的离线测试。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
import json
import yaml
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis
import compliance
from errors import IssueProcessingError

TEMPLATE = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
    {"type": "textarea", "attributes": {"label": "问题描述"},
     "validations": {"required": True}}]}

TEMPLATE_FILE = {"filename": "bug.yml", "source": "example/repo/.gitcode/ISSUE_TEMPLATE/bug.yml",
                 "content": yaml.safe_dump(TEMPLATE)}


class AnalysisTests(unittest.TestCase):
    def test_pass_after_previous_comment_notifies_without_maintainer_advice(self):
        """此前有评论时，两类通过结果均通知通过，维护者建议仍单独返回。

        Args:
            无。
        Returns:
            None；通过文案、发布决定或建议隔离不正确时测试失败。
        """
        for title, body, status in (("[Bug] 启动失败", "### 问题描述\n启动失败", "pass"),
                                    ("权限申请", "申请访问仓库", "no_match")):
            with self.subTest(status=status), patch.object(compliance, "recommend_template", return_value={
                    "status": "not_applicable", "reason": "模板未覆盖权限申请", "maintainer": "建议新增权限申请模板"}), patch.object(
                    compliance, "format_compliance_check", wraps=compliance.format_compliance_check) as format_check:
                result = analysis.analyze_issue(title, body, [TEMPLATE_FILE], "key",
                                                repository="example/repo", number=18, previously_commented=True)
            self.assertEqual(result["summary"]["status"], status)
            self.assertTrue(result["publish_comment"])
            self.assertIn("### 提交内容规范性检查：通过", result["comment"])
            self.assertNotIn("建议新增", result["comment"])
            self.assertEqual(result["maintainer_suggestion"] is not None, status == "no_match")
            self.assertEqual(format_check.call_count, 1)

    def test_fully_matched_issue_does_not_request_recommendation(self):
        """本地完全匹配直接通过，即使正文语义与标题类型不同也不调用模型。"""
        with patch.object(compliance, "recommend_template") as recommend:
            result = analysis.analyze_issue("[Bug] 新功能", "### 问题描述\n希望增加新功能", [TEMPLATE_FILE], "",
                                            repository="example/repo", number=18)
        self.assertEqual(result["summary"], {"status": "pass", "recommendation_status": None})
        self.assertEqual(result["comment"], "")
        self.assertFalse(result["publish_comment"])
        self.assertIsNone(result["maintainer_suggestion"])
        recommend.assert_not_called()

    def test_section_formatting_preserves_local_pass(self):
        template = {**TEMPLATE, 'body': [{'type': 'textarea',
            'attributes': {'label': 'Environment / 环境信息 (Mandatory / 必填)'},
            'validations': {'required': True}}]}
        file = {**TEMPLATE_FILE, 'content': yaml.safe_dump(template)}
        for body, status in (('### 环境信息：\nLinux', 'pass'),
                             ('### Runtime environment / 环境信息：\nLinux', 'pass')):
            with self.subTest(body=body), patch.object(compliance, 'recommend_template') as recommend:
                result = analysis.analyze_issue('[Bug] 错误', body, [file], '',
                                                repository='example/repo', number=18)
                self.assertEqual(result['summary'], {'status': status, 'recommendation_status': None})
                self.assertEqual(result['publish_comment'], status != 'pass')
                recommend.assert_not_called()

    def test_slash_option_and_missing_title_colon_do_not_invoke_model(self):
        template = {**TEMPLATE, 'title': '[Bug]: ', 'body': [{'type': 'textarea',
            'attributes': {'label': 'Related log / screenshot / 日志 / 截图 (Mandatory / 必填)'},
            'validations': {'required': True}}]}
        file = {**TEMPLATE_FILE, 'content': yaml.safe_dump(template)}
        for body, status in (('### 日志 / 结果\n错误输出', 'pass'),
                             ('### Related log / 日志\n错误输出', 'pass')):
            with self.subTest(body=body), patch.object(compliance, 'recommend_template') as recommend:
                result = analysis.analyze_issue('[Bug] 错误', body, [file], '',
                                                repository='example/repo', number=18)
                self.assertEqual(result['summary'], {'status': status, 'recommendation_status': None})
                self.assertEqual(result['publish_comment'], status != 'pass')
                recommend.assert_not_called()

    def test_unmatched_issue_attaches_recommendation_without_changing_status(self):
        """未匹配时传递 Issue 与模型参数，并保留每种推荐结果。

        Args:
            无。
        Returns:
            None；验证推荐成功、不适用和信息不足均不覆盖基础检查状态。
        """
        for status in ("recommended", "not_applicable", "insufficient_info"):
            suggestion = {"status": status, "template": "缺陷反馈" if status == "recommended" else None,
                          "reason": "测试推荐结果", "maintainer": (
                              "建议新增申请模板，收集申请账号。"
                              if status == "not_applicable" else None)}
            with self.subTest(status=status), patch.object(
                    compliance, "recommend_template", return_value=suggestion) as recommend:
                result = analysis.analyze_issue("启动失败", "退出了", [TEMPLATE_FILE], "key", "model", "https://model.example",
                                                repository="example/repo", number=18, maintainers=("alice",))
                self.assertEqual(result["summary"], {"status": "no_match", "recommendation_status": status})
                if status == "not_applicable":
                    self.assertEqual(result["comment"], "")
                else:
                    self.assertIn("### 提交内容规范性检查", result["comment"])
                self.assertEqual(result["publish_comment"], status != "not_applicable")
                if status == "not_applicable":
                    self.assertNotIn("给维护者的建议", result["comment"])
                    self.assertNotIn("@alice", result["comment"])
                    saved = result["maintainer_suggestion"]
                    self.assertEqual(saved, {
                        "reason": suggestion["reason"],
                        "suggestion": suggestion["maintainer"],
                        "mtr": json.dumps(["alice"]),
                    })
                else:
                    self.assertIsNone(result["maintainer_suggestion"])
                recommend.assert_called_once_with("启动失败", "退出了", [TEMPLATE], "key", "model", "https://model.example")

    def test_refined_documentation_category_passes_without_comment(self):
        """复现 issue-gov-test2 #9 的类别细化，检查通过且不调用模型或生成评论。

        Args:
            无。
        Returns:
            None；细化后的文档类型仍被判错或产生公开评论时测试失败。
        """
        path = Path(__file__).resolve().parent / 'fixtures/repository_templates/documentation_template.yml'
        file = {'filename': path.name, 'source': 'qiurq1/issue-gov-test2/.gitcode/ISSUE_TEMPLATE/' + path.name,
                'content': path.read_text()}
        body = ('### 文档类型\n缺陷修正（描述不准确）\n'
                '### 涉及文档路径或链接\ndocs/robot/robot使用指南.md\n'
                '### 问题描述\n/approve 权限说明与治理文档不一致\n'
                '### 期望内容\n按 PR 类型明确审批人权限')
        with patch.object(compliance, 'recommend_template') as recommend:
            result = analysis.analyze_issue('[文档]: /approve 权限说明不一致', body, [file], '',
                                            repository='qiurq1/issue-gov-test2', number=9)
        self.assertEqual(result['summary']['status'], 'pass')
        self.assertFalse(result['publish_comment'])
        self.assertEqual(result['comment'], '')
        self.assertIsNone(result['maintainer_suggestion'])
        recommend.assert_not_called()

    def test_recommendation_failure_stops_comment_generation(self):
        """推荐异常传递到任务层，不继续生成可发布的评论。

        Args:
            无。
        Returns:
            None；核对错误可重试且评论生成函数未执行。
        """
        with patch.object(compliance, "recommend_template", side_effect=IssueProcessingError("模型响应超时")), patch.object(
                compliance, "format_compliance_check") as format_check:
            with self.assertRaises(IssueProcessingError) as raised:
                analysis.analyze_issue("未知", "正文", [TEMPLATE_FILE], "key", repository="example/repo", number=11)
        self.assertTrue(raised.exception.retryable)
        format_check.assert_not_called()

    def test_comment_uses_reviewed_problems(self):
        """复核后只发布模型确认的缺项，不能带回本地字面匹配的误报。"""
        problem = {"field": "问题描述", "reason": "body", "message": "请补充问题描述字段"}
        review = {"status": "needs_changes", "template": "缺陷反馈", "reason": "缺陷模板适用",
                  "maintainer": None, "problems": [problem]}
        section = "### 提交内容规范性检查：未通过"
        with patch.object(compliance, "recommend_template", return_value=review), patch.object(
                compliance, "format_compliance_check", return_value=section) as format_check:
            output = analysis.analyze_issue("【Bug】启动失败", "缺少章节", [TEMPLATE_FILE], "key",
                                            repository="example/repo", number=18, maintainers=("alice",))
        self.assertEqual(output["comment"], section)
        expected = compliance.check_issue("【Bug】启动失败", "缺少章节", [TEMPLATE])
        expected["recommendation"] = review
        format_check.assert_called_once_with(expected, templates=[TEMPLATE])

    def test_missing_templates_save_advice_for_configured_maintainers(self):
        """缺少模板时静默记录维护者建议，未配置名单时仍保留建议。

        Args:
            无。
        Returns:
            None；核对状态、建议接收人和未调用模型或格式检查。
        """
        for maintainers in ((), ("alice", "bob")):
            with self.subTest(maintainers=maintainers), patch.object(compliance, "recommend_template") as recommend, patch.object(
                    compliance, "check_issue") as check:
                result = analysis.analyze_issue("标题", "正文", [], "key", repository="example/repo", number=18,
                                                maintainers=maintainers, previously_commented=True)
            self.assertEqual(result["summary"], {"status": "template_error", "recommendation_status": None})
            self.assertFalse(result["publish_comment"])
            self.assertEqual(result["comment"], "")
            saved = result["maintainer_suggestion"]
            self.assertIn("未找到可用的 YAML 或 Markdown Issue 模板", saved["reason"])
            self.assertIn("example/repo", saved["reason"])
            self.assertIn("请修复", saved["suggestion"])
            self.assertEqual(json.loads(saved["mtr"]), list(maintainers))
            recommend.assert_not_called()
            check.assert_not_called()

    def test_invalid_template_files_save_source_and_diagnosis(self):
        """本地或公共模板故障说明来源和固定诊断，不用其他模板继续评判 Issue。

        Args:
            无。
        Returns:
            None；验证语法、元信息、字段问题均入库，且不保存模板正文。
        """
        cases = [("bug.yml", "name: [private-template-value", "YAML 语法无效"),
                 ("bug.md", "---\nname: 缺陷\n", "Markdown front matter 未闭合"),
                 ("bug.yml", "null", "模板 name 必须为非空文字"),
                 ("bug.yml", "name: bad\ntitle: 123", "模板 title 必须为文字"),
                 ("bug.yml", yaml.safe_dump({**TEMPLATE, "body": [{"type": "dropdown", "attributes": {
                     "label": "类型", "options": []}}]}), "body 第 1 项：options 必须为非空数组")]
        for owner in ("example/repo", "example/.gitcode"):
            for filename, content, message in cases:
                source = owner + "/.gitcode/ISSUE_TEMPLATE/" + filename
                files = [TEMPLATE_FILE, {"filename": filename, "source": source, "content": content}]
                with self.subTest(source=source, content=content), patch.object(compliance, "recommend_template") as recommend, patch.object(
                        compliance, "check_issue") as check:
                    result = analysis.analyze_issue("标题", "正文", files, "key", repository="example/repo", number=18,
                                                    maintainers=("alice",))
                self.assertEqual(result["summary"]["status"], "template_error")
                self.assertFalse(result["publish_comment"])
                self.assertEqual(result["comment"], "")
                saved = result["maintainer_suggestion"]
                self.assertIn(source, saved["reason"])
                self.assertIn(message, saved["reason"])
                self.assertEqual(json.loads(saved["mtr"]), ["alice"])
                self.assertNotIn("private-template-value", saved["reason"])
                recommend.assert_not_called()
                check.assert_not_called()

    def test_template_diagnostics_save_all_files_with_configured_recipients(self):
        """多个故障文件一并记录，接收人只来自配置。

        Args:
            无。
        Returns:
            None；验证所有错误均保留且没有公开评论。
        """
        files = [{"filename": "bad.yml", "source": "example/repo/@mallory<script>.yml", "content": "name: ["},
                 {"filename": "other.md", "source": "example/.gitcode/other.md", "content": "---\nname: broken"}]
        result = analysis.analyze_issue("标题", "正文", files, "", repository="example/repo", number=18, maintainers=("alice",))
        self.assertFalse(result["publish_comment"])
        self.assertEqual(result["comment"], "")
        saved = result["maintainer_suggestion"]
        self.assertEqual(json.loads(saved["mtr"]), ["alice"])
        for text in (files[0]["source"], files[1]["source"], "YAML 语法无效", "Markdown front matter 未闭合"):
            self.assertIn(text, saved["reason"])

    def test_mixed_template_files_are_parsed_without_mutating_originals(self):
        """统一入口解析 YAML 与 Markdown 原文，原始文件记录保持不变。

        Args:
            无。
        Returns:
            None；验证解析后模板交给检查模块，完整匹配时不调用推荐。
        """
        files = [TEMPLATE_FILE, {"filename": "docs.md", "source": "example/repo/.gitcode/ISSUE_TEMPLATE/docs.md",
                                "content": '---\nname: 文档反馈\ntitle: "[Docs] "\n---\n### 文档位置\n请填写链接'}]
        original = deepcopy(files)
        parsed = [compliance.parse_template(file["content"], file["filename"]) for file in files]
        with patch.object(compliance, "check_issue", wraps=compliance.check_issue) as check, patch.object(
                compliance, "recommend_template") as recommend:
            result = analysis.analyze_issue("[Bug] 启动失败", "### 问题描述\n启动失败", files, "key",
                                            repository="example/repo", number=18)
        check.assert_called_once_with("[Bug] 启动失败", "### 问题描述\n启动失败", parsed)
        self.assertEqual(files, original)
        self.assertEqual(result["summary"]["status"], "pass")
        recommend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
