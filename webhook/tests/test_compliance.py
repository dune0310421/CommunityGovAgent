"""格式检查和隔离模型调用回归测试，不连接真实模型服务。"""
import json
import os
from pathlib import Path
import sys
import unittest
import yaml
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compliance
from errors import IssueProcessingError
TEMPLATE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "repository_templates"
TOKEN = "test-platform-token"
TEMPLATE = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
    {"type": "textarea", "attributes": {"label": "问题描述", "placeholder": "请输入"},
     "validations": {"required": True}}]}

SUGGESTION = {"status": "recommended", "template": "缺陷反馈", "reason": "运行失败",
              "maintainer": None}

class ComplianceTests(unittest.TestCase):
    def test_title_prefix_trailing_whitespace_is_optional(self):
        """真实 CANNLab 模板允许省略分隔空格，但仍检查前缀内容。"""
        path = TEMPLATE_FIXTURES / "cannlab_template.yml"
        template = compliance.parse_template(path.read_text(), path.name)
        body = "\n".join(
            f"### {field['attributes']['label']}\n已补充实际信息"
            for field in template["body"]
            if field.get("validations", {}).get("required")
        )
        for separator in ("", " ", "  ", "\t", "\u3000"):
            with self.subTest(separator=separator):
                result = compliance.check_issue(
                    f"[CANNLab]:{separator}没有8卡选项，只能启动单卡", body, [template])
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["checks"][0]["problems"], [])
        for title in ("[CANNLab]没有8卡选项", "[CANNLab] 没有8卡选项", "[CANNLab]：没有8卡选项"):
            with self.subTest(title=title):
                self.assertEqual(compliance.check_issue(title, body, [template])['status'], 'pass')
        for title in ("[CANNLabX]:没有8卡选项", "[CANNLabX]没有8卡选项"):
            with self.subTest(title=title):
                result = compliance.check_issue(title, body, [template])
                self.assertIn("title_prefix", [p["reason"] for p in result["checks"][0]["problems"]])

    def test_format_results_and_code_block_headings(self):
        """验证格式检查的两种结论，并确认代码块中的标题不被当作有效章节。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for title, body, status in (
            ("[Bug] 错误", "### 问题描述\n启动失败", "pass"),
            ("[Bug] 错误", "### 问题描述\n请输入", "no_match"),
            ("[Bug] 错误", "```\n### 问题描述\n伪造正文\n```", "no_match"),
            ("其他", "没有章节", "no_match"),
        ):
            with self.subTest(body=body):
                self.assertEqual(compliance.check_issue(title, body, [TEMPLATE])["status"], status)

    def test_invalid_template_fields_are_rejected(self):
        """验证分析入口拒绝不支持的字段和无效属性，包括非必填字段。

        Args:
            无。
        Returns:
            None；无效模板未抛出 ValueError 时测试失败。
        """
        for body in (None, [{"type": "dropdown"}], [{"type": "textarea"}],
                     [{"type": "textarea", "attributes": {"label": " "}}],
                     [{"type": "textarea", "attributes": {"label": 123}}],
                     [{"type": "textarea", "attributes": {"label": "描述", "placeholder": 123}}],
                     [{"type": "textarea", "attributes": {"label": "描述"}, "validations": []}]):
            with self.subTest(body=body), self.assertRaises(ValueError):
                compliance.check_issue("[Bug] 错误", "正文", [{**TEMPLATE, "body": body}])

    def test_model_failure_redacts_both_credentials(self):
        """验证模型调用失败后的反馈不会泄露平台令牌或模型密钥。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        with patch.dict(os.environ, {"ATOMGIT_TOKEN": TOKEN}, clear=True), patch.object(
            compliance.subprocess, "Popen", side_effect=OSError("agent-key " + TOKEN),
        ):
            with self.assertRaises(IssueProcessingError) as raised:
                compliance.recommend_template(
                    "未知", "描述", [TEMPLATE], "agent-key", "custom-model", "https://models.example")
        self.assertTrue(raised.exception.retryable)
        self.assertIn("推荐程序运行失败", str(raised.exception))
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertNotIn("agent-key", str(raised.exception))
        self.assertTrue(compliance.RECOMMEND_PROMPT.is_file())


    def test_model_configuration_and_credentials_reach_isolated_claude_process(self):
        """验证模型地址、模型名和 Bearer 密钥传入隔离进程，且不会继承无关凭据。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        suggestion = SUGGESTION
        envelope = {"type": "result", "subtype": "success", "is_error": False,
                    "result": json.dumps(suggestion)}
        inherited = {"ATOMGIT_TOKEN": TOKEN, "ANTHROPIC_AUTH_TOKEN": "unrelated-token",
                     "ANTHROPIC_API_KEY": "unrelated-key",
                     "ANTHROPIC_BASE_URL": "https://wrong.example"}
        with patch.dict(os.environ, inherited, clear=True), patch.object(
            compliance.subprocess, "Popen",
        ) as popen, patch.object(compliance.os, "killpg"):
            process = popen.return_value.__enter__.return_value
            process.communicate.return_value = (json.dumps(envelope), "")
            process.returncode = 0
            result = compliance.recommend_template(
                "未知", "描述", [TEMPLATE], "agent-key", "custom-model",
                "https://models.example/anthropic")
        self.assertEqual(result, {"status": "recommended", **suggestion})
        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertEqual(command[command.index("--model") + 1], "custom-model")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://models.example/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "agent-key")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ATOMGIT_TOKEN", env)
        self.assertNotIn("agent-key", " ".join(command))
        self.assertEqual(json.loads(process.communicate.call_args.args[0])["issue"]["title"], "未知")


    def test_incomplete_model_configuration_never_launches_claude(self):
        """验证模型配置不完整时抛出可重试错误，且不启动 Claude Code。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for key, model, url in (
            ("", "custom-model", "https://models.example"),
            ("agent-key", " ", "https://models.example"),
            ("agent-key", "custom-model", " "),
        ):
            with self.subTest(model=model, url=url), patch.object(
                compliance.subprocess, "Popen",
            ) as popen:
                with self.assertRaises(IssueProcessingError) as raised:
                    compliance.recommend_template("未知", "描述", [TEMPLATE], key, model, url)
            self.assertTrue(raised.exception.retryable)
            popen.assert_not_called()

    def test_markdown_sections_distinguish_content_from_instructions(self):
        """验证引用、注释和子标题不冒充填写内容，代码和重复章节中的正文仍有效。

        Args:
            无。
        Returns:
            None；章节判断与预期不符时测试失败。
        """
        for body, status in (
            ("> ### 问题描述\n> 引用中的内容", "no_match"),
            ("### 问题描述\n<!-- 仅有填写说明 -->", "no_match"),
            ("### 问题描述\n#### 子标题", "no_match"),
            ("### 问题描述\n_No response_", "no_match"),
            ("### 问题描述\n```python\nraise RuntimeError()\n```", "pass"),
            ("### 问题描述\n#### 子标题\n实际问题", "pass"),
            ("### 问题描述\n\n### 问题描述\n补充内容", "pass"),
        ):
            with self.subTest(body=body):
                self.assertEqual(compliance.check_issue("[Bug] 错误", body, [TEMPLATE])["status"], status)

    def test_issue_subheadings_do_not_truncate_template_field(self):
        """模板字段之间的自定义同级小标题仍计入前一个字段。"""
        body = """### 问题描述
已确认问题现象。

### 重现步骤
### Static reproduction

1. 打开示例。
2. 执行命令。

### Runtime reproduction

运行同一命令并观察结果。

### 预期结果
应正确释放资源。
"""
        template = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
            {"type": "textarea", "attributes": {"label": "问题描述"},
             "validations": {"required": True}},
            {"type": "textarea", "attributes": {"label": "重现步骤"},
             "validations": {"required": True}},
            {"type": "textarea", "attributes": {"label": "预期结果"},
             "validations": {"required": True}},
        ]}
        result = compliance.check_issue("[Bug] 资源未释放", body, [template])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["checks"][0]["problems"], [])

    def test_incomplete_title_match_does_not_select_a_template(self):
        """标题匹配但缺字段时统一未匹配，不预先锁定候选或生成评论。"""
        other = {**TEMPLATE, "name": "功能请求", "title": "[Feature] "}
        for title in ("[Bug] 错误", "[Bug]错误", "  [ B ug ]\u3000错误"):
            for templates in ([other, TEMPLATE], [TEMPLATE, other]):
                with self.subTest(title=title, names=[t["name"] for t in templates]):
                    result = compliance.check_issue(title, "### 问题描述\n请输入", templates)
                    self.assertEqual(result["status"], "no_match")
                    self.assertEqual(result["matched_templates"], [])
                    with self.assertRaises(IssueProcessingError):
                        compliance.format_compliance_check(result)
                    complete = compliance.check_issue(title, "### 问题描述\n实际问题", templates)
                    self.assertEqual(complete["status"], "pass")
                    self.assertEqual(complete["matched_templates"], ["缺陷反馈"])
        result = compliance.check_issue("未知标题", "### 问题描述\n实际问题", [TEMPLATE, other])
        self.assertEqual(result["matched_templates"], [])
        self.assertEqual(result["status"], "no_match")

    def test_markdown_and_optional_fields_do_not_become_required(self):
        """验证说明字段和可选字段不影响必填检查，空模板列表明确报错。

        Args:
            无。
        Returns:
            None；模板必填规则处理错误时测试失败。
        """
        template = {**TEMPLATE, "body": [
            {"type": "markdown", "attributes": {"value": "填写说明"}},
            *TEMPLATE["body"], {"type": "input", "attributes": {"label": "可选信息"}},
        ]}
        result = compliance.check_issue("[Bug] 错误", "### 问题描述\n实际问题", [template])
        self.assertEqual(result["status"], "pass")
        with self.assertRaises(ValueError):
            compliance.check_issue("标题", "正文", [])

    def test_invalid_model_output_retries_without_leaking_details(self):
        """验证损坏响应、失败状态及无效推荐均抛出可重试错误，且不泄露原始输出或凭据。

        Args:
            无。
        Returns:
            None；错误输出被接受或敏感内容泄漏时测试失败。
        """
        envelope = {"type": "result", "subtype": "success", "is_error": False,
                    "result": json.dumps(SUGGESTION)}
        cases = [("agent-key " + TOKEN, 0), ("[]", 0),
                 (json.dumps(envelope), 1),
                 (json.dumps({**envelope, "is_error": True}), 0),
                 (json.dumps({**envelope, "subtype": "error_max_turns"}), 0),
                 (json.dumps({**envelope, "result": "not JSON"}), 0)]
        for suggestion in ({}, [], None, {**SUGGESTION, "template": "集合外模板"},
                           {**SUGGESTION, "template": 123},
                           {**SUGGESTION, "reason": " "},
                           {**SUGGESTION, "reason": 123},
                           {**SUGGESTION, "reason": "x" * 501}):
            cases.append((json.dumps({**envelope, "result": json.dumps(suggestion)}), 0))
        for output, returncode in cases:
            with self.subTest(output=output, returncode=returncode), patch.object(
                compliance.subprocess, "Popen",
            ) as popen, patch.object(compliance.os, "killpg"):
                process = popen.return_value.__enter__.return_value
                process.communicate.return_value = (output, "agent-key " + TOKEN)
                process.returncode = returncode
                with self.assertRaises(IssueProcessingError) as raised:
                    compliance.recommend_template("未知", "正文", [TEMPLATE], "agent-key", "model", "https://models.example")
            self.assertTrue(raised.exception.retryable)
            self.assertIn("模型未返回有效的推荐结果", str(raised.exception))
            self.assertNotIn("agent-key", str(raised.exception))
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_model_timeout_kills_process_group_and_removes_temporary_directory(self):
        """验证模型超时后清理进程组和临时配置目录，并抛出可重试错误。

        Args:
            无。
        Returns:
            None；清理步骤遗漏或超时未进入重试时测试失败。
        """
        with patch.object(compliance.subprocess, "Popen") as popen, patch.object(compliance.os, "killpg") as kill:
            process = popen.return_value.__enter__.return_value
            process.pid = 12345
            process.communicate.side_effect = compliance.subprocess.TimeoutExpired("claude", compliance.LLM_TIMEOUT)
            with self.assertRaises(IssueProcessingError) as raised:
                compliance.recommend_template("未知", "正文", [TEMPLATE], "key", "model", "https://models.example")
            kill.assert_called_once_with(12345, compliance.signal.SIGKILL)
            process.wait.assert_called_once()
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertFalse(Path(popen.call_args.kwargs["cwd"]).exists())
        self.assertTrue(raised.exception.retryable)

    def test_template_fixtures_and_required_dropdown_sections(self):
        """验证六个固定模板样例，以及必填下拉章节缺失或为空时的提示。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        directory = TEMPLATE_FIXTURES
        templates = [yaml.safe_load(path.read_text()) for path in sorted(directory.glob("*.yml"))]
        self.assertEqual(len(templates), 6)
        self.assertEqual(compliance.check_issue("test", "test", templates)["status"], "no_match")
        for template in templates:
            with self.subTest(template=template["name"]):
                sections = {}
                for field in template["body"]:
                    if field.get("validations", {}).get("required"):
                        attrs = field["attributes"]
                        sections[attrs["label"]] = attrs["options"][0] if field["type"] == "dropdown" else "已补充实际信息"
                body = "\n".join(f"### {label}\n{value}" for label, value in sections.items())
                self.assertEqual(compliance.check_issue(template["title"] + "测试", body, templates)["status"], "pass")
                for field in template["body"]:
                    if field["type"] != "dropdown" or not field.get("validations", {}).get("required"):
                        continue
                    label = field["attributes"]["label"]
                    for value, reason in ((None, "missing_section"), ("_No response_", "empty_section")):
                        incomplete = "\n".join(f"### {key}\n{value if key == label else text}"
                            for key, text in sections.items() if key != label or value is not None)
                        result = compliance.check_issue(template["title"] + "测试", incomplete, [template])
                        self.assertEqual(result["status"], "no_match")
                        self.assertIn({"field": label, "reason": reason,
                            "message": "缺少此必填章节" if value is None else "此必填章节为空或仅含占位内容"},
                            result["checks"][0]["problems"])

    def test_dropdown_attributes_and_optional_fields(self):
        """验证下拉属性、多选内容及选填字段的处理。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        field = {"type": "dropdown", "attributes": {"label": "类型", "options": ["缺陷", "需求"]},
                 "validations": {"required": True}}
        template = {**TEMPLATE, "body": [field]}
        for options in (None, [], "缺陷", [None], [" "]):
            bad = {**field, "attributes": {**field["attributes"], "options": options}}
            with self.subTest(options=options), self.assertRaises(ValueError):
                compliance.check_issue("[Bug] 测试", "### 类型\n缺陷", [{**template, "body": [bad]}])
        for multiple in (False, True):
            choice = {**field, "attributes": {**field["attributes"], "multiple": multiple}}
            selected = "缺陷, 需求" if multiple else "缺陷"
            self.assertEqual(compliance.check_issue("[Bug] 测试", "### 类型\n" + selected,
                [{**template, "body": [choice]}])["status"], "pass")
        optional = {**field, "validations": {"required": False}}
        self.assertEqual(compliance.check_issue("[Bug] 测试", "", [{**template, "body": [optional]}])["status"], "pass")



class FieldBoundaryTests(unittest.TestCase):
    def test_scalar_fields_stop_at_unknown_headings_at_any_level(self):
        for kind in ('dropdown', 'input'):
            template = {'name': '测试', 'title': '[Test]', 'body': [
                {'type': kind, 'attributes': {'label': 'Platform（平台）', 'options': ['Linux']},
                 'validations': {'required': True}},
                {'type': 'textarea', 'attributes': {'label': '备注'},
                 'validations': {'required': True}},
            ]}
            key = compliance._section_key('Platform（平台）')
            for level in range(1, 7):
                body = ('### 平台\nLinux\n\n' + '#' * level +
                        ' 期望结果\n这段内容属于另外的章节。\n\n### 备注\n补充信息')
                with self.subTest(kind=kind, level=level):
                    sections = compliance._sections(
                        body, normalize_hints=True, field_keys={key, '备注'},
                        field_names={'平台': key, '备注': '备注'},
                        field_types={key: kind, '备注': 'textarea'})
                    self.assertEqual(sections, {key: 'Linux', '备注': '补充信息'})
                    self.assertEqual(compliance.check_issue('[Test] test', body, [template])['status'], 'pass')

    def test_unknown_section_does_not_fill_an_empty_scalar_field(self):
        for kind in ('dropdown', 'input'):
            template = {'name': '测试', 'title': '[Test]', 'body': [
                {'type': kind, 'attributes': {'label': '平台', 'options': ['Linux']},
                 'validations': {'required': True}},
            ]}
            with self.subTest(kind=kind):
                result = compliance.check_issue(
                    '[Test] test', '### 平台\n\n### 其他信息\nLinux', [template])
                self.assertEqual([p['reason'] for p in result['checks'][0]['problems']], ['empty_section'])

    def test_textarea_keeps_internal_headings_at_any_level(self):
        template = {'name': '测试', 'title': '[Test]', 'body': [
            {'type': 'textarea', 'attributes': {'label': '问题描述'},
             'validations': {'required': True}},
            {'type': 'dropdown', 'attributes': {'label': '平台', 'options': ['Linux']},
             'validations': {'required': True}},
        ]}
        for level in range(1, 7):
            body = ('### 问题描述\n\n' + '#' * level +
                    ' 实际现象\n程序崩溃。\n\n### 平台\nLinux')
            with self.subTest(level=level):
                self.assertEqual(compliance.check_issue('[Test] test', body, [template])['status'], 'pass')

    def test_code_and_quote_headings_do_not_end_scalar_fields(self):
        body = ('### 平台\nLinux\n\n'
                '\x60\x60\x60markdown\n# 代码中的标题\n\x60\x60\x60\n'
                '> # 引用中的标题\n\n'
                '补充说明\n\n### 期望结果\n其他章节内容')
        for kind in ('dropdown', 'input'):
            with self.subTest(kind=kind):
                sections = compliance._sections(
                    body, normalize_hints=True, field_keys={'平台'},
                    field_types={'平台': kind})
                self.assertIn('补充说明', sections['平台'])
                self.assertNotIn('其他章节内容', sections['平台'])


class TemplateSelectionTests(unittest.TestCase):
    def test_bug_report_does_not_select_documentation_from_generic_heading(self):
        directory = TEMPLATE_FIXTURES
        templates = [compliance.parse_template(p.read_text(), p.name) for p in sorted(directory.glob("*.yml"))]
        title = "[Bug-Report|缺陷反馈]: 首次 PR 欢迎消息中的 CLA FAQ 链接包含格式化占位符"
        body = "### 问题描述\n欢迎消息生成的链接包含格式化占位符。\n### 预期行为\n链接能打开。"
        result = compliance.check_issue(title, body, templates)
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["matched_templates"], [])
        with self.assertRaises(IssueProcessingError) as raised:
            compliance.format_compliance_check(result)
        self.assertTrue(raised.exception.retryable)
        bug = {**TEMPLATE, "title": "[Bug-Report|缺陷反馈]: "}
        for candidates in ([bug, *templates], [*templates, bug]):
            result = compliance.check_issue(title, "### 预期行为\n链接能打开。", candidates)
            self.assertEqual(result["status"], "no_match")
            self.assertEqual(result["matched_templates"], [])
            with self.assertRaises(IssueProcessingError):
                compliance.format_compliance_check(result)
            self.assertEqual(compliance.check_issue(title, body, candidates)["status"], "pass")

    def test_complete_documentation_structure_requires_matching_title(self):
        path = TEMPLATE_FIXTURES / "documentation_template.yml"
        template = compliance.parse_template(path.read_text(), path.name)
        headings = [field["attributes"]["label"] for field in template["body"]
                    if field.get("validations", {}).get("required")]
        contents = {field["attributes"]["label"]: field["attributes"]["options"][0]
                    if field["type"] == "dropdown" else "实际内容" for field in template["body"]
                    if field.get("validations", {}).get("required")}
        body = "\n".join(f"### {label}\n{contents[label]}" for label in headings)
        result = compliance.check_issue("没有模板前缀", body, [template])
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["matched_templates"], [])
        self.assertEqual([p["reason"] for p in result["checks"][0]["problems"]], ["title_prefix"])
        with self.assertRaises(IssueProcessingError):
            compliance.format_compliance_check(result)
        for missing in headings:
            with self.subTest(missing=missing):
                incomplete = "\n".join(f"### {label}\n{contents[label]}" for label in headings if label != missing)
                self.assertEqual(compliance.check_issue("没有模板前缀", incomplete, [template])["status"], "no_match")
        # 内容为空仍能识别结构，但必须继续提示必填内容为空。
        empty = "\n".join(f"### {label}\n" for label in headings)
        result = compliance.check_issue("没有模板前缀", empty, [template])
        self.assertEqual(result["status"], "no_match")
        self.assertIn("empty_section", [p["reason"] for p in result["checks"][0]["problems"]])
        other = {**template, "name": "另一模板", "title": "[Other]: "}
        for templates in ([template, other], [other, template]):
            result = compliance.check_issue("没有模板前缀", body, templates)
            self.assertEqual(result["status"], "no_match")
            self.assertEqual(result["matched_templates"], [])
            result = compliance.check_issue("[Other]: 测试", empty, templates)
            self.assertEqual(result["matched_templates"], [])

    def test_required_checkbox_section_counts_but_optional_sections_do_not(self):
        template = {**TEMPLATE, "body": [*TEMPLATE["body"], {
            "type": "checkboxes", "attributes": {"label": "确认项", "options": [
                {"label": "已阅读", "required": True}]}}, {
            "type": "textarea", "attributes": {"label": "备注"}}]}
        result = compliance.check_issue("未知标题", "### 问题描述\n实际问题\n### 确认项\n- [ ] 已阅读", [template])
        self.assertEqual(result["status"], "no_match")
        self.assertIn("unchecked_required", [p["reason"] for p in result["checks"][0]["problems"]])
        result = compliance.check_issue("未知标题", "### 问题描述\n实际问题\n### 备注\n说明", [template])
        self.assertEqual(result["status"], "no_match")

    def test_templates_without_title_still_require_filled_sections(self):
        template = {**TEMPLATE, "title": ""}
        result = compliance.check_issue("任意标题", "### 问 题描述\n请输入", [template])
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["matched_templates"], [])
        self.assertIn("empty_section", [p["reason"] for p in result["checks"][0]["problems"]])
        complete = compliance.check_issue("任意标题", "### 问题描述\n实际内容", [template])
        self.assertEqual(complete["status"], "pass")
        self.assertEqual(complete["matched_templates"], [template["name"]])


class SectionHintMatchingTests(unittest.TestCase):
    def test_hint_removal_preserves_required_content_and_template(self):
        for hint in ('（必填）', '(必选)', '[Required]', '(MANDATORY / 必填)',
                     '（Optional / 选填）', '（可选）'):
            for kind in ('yaml', 'markdown'):
                with self.subTest(hint=hint, kind=kind):
                    label = '问题描述 ' + hint
                    if kind == 'yaml':
                        template = {**TEMPLATE, 'body': [{
                            'type': 'textarea', 'attributes': {'label': label, 'placeholder': '请输入'},
                            'validations': {'required': True}}]}
                    else:
                        template = compliance.parse_template(
                            f'### {label}\n<!-- 此项必填 -->\n请输入', 'bug.md')
                    original = json.dumps(template, ensure_ascii=False)
                    for heading in (label, '问题描述', '问题描述 (required)'):
                        for content, reason in (('启动失败', None), ('', 'empty_section'),
                                                ('请输入', 'empty_section')):
                            result = compliance.check_issue('[Bug] 错误', f'### {heading}\n{content}', [template])
                            self.assertEqual(result['status'], 'pass' if reason is None else 'no_match')
                            if reason:
                                self.assertEqual(result['checks'][0]['problems'], [
                                    {'field': label, 'reason': reason, 'message': '此必填章节为空或仅含占位内容'}])
                    self.assertEqual(json.dumps(template, ensure_ascii=False), original)

    def test_real_name_differences_and_fake_headings_still_fail(self):
        for label, body in (
            ('问题描述（必填）', '### 问题分析\n启动失败'),
            ('问题描述（必填）', '```md\n### 问题描述\n启动失败\n```'),
            ('问题描述（必填）', '> ### 问题描述\n> 启动失败'),
            ('版本（必填字段）', '### 版本\nv1'),
            ('版本（运行环境）', '### 版本\nv1'),
        ):
            with self.subTest(label=label, body=body):
                template = {**TEMPLATE, 'body': [{'type': 'textarea',
                    'attributes': {'label': label}, 'validations': {'required': True}}]}
                result = compliance.check_issue('[Bug] 错误', body, [template])
                self.assertEqual(result['checks'][0]['problems'][0]['reason'], 'missing_section')

    def test_body_identification_checkbox_and_dropdown_use_same_section_keys(self):
        template = {'name': '确认', 'title': '[确认]', 'body': [
            {'type': 'checkboxes', 'attributes': {'label': '确认（必填）',
                'options': [{'label': '已阅读（必选）', 'required': True}]}, 'validations': {'required': True}},
            {'type': 'dropdown', 'attributes': {'label': '平台 (Mandatory / 必填)',
                'options': ['Linux', 'Windows']}, 'validations': {'required': True}}]}
        body = '### 确认\n- [x] 已阅读（必选）\n### 平台\nLinux'
        self.assertEqual(compliance.check_issue('[确认]问题', body, [template])['status'], 'pass')
        result = compliance.check_issue('其他标题', body, [template])
        self.assertEqual(result['matched_templates'], [])
        self.assertEqual([p['reason'] for p in result['checks'][0]['problems']], ['title_prefix'])
        for changed, reason in ((body.replace('[x]', '[ ]'), 'unchecked_required'),
                                (body.replace('已阅读（必选）', '已阅读'), 'unchecked_required'),
                                (body.replace('Linux', 'Unknown'), 'invalid_option')):
            result = compliance.check_issue('[确认]问题', changed, [template])
            self.assertIn(reason, [p['reason'] for p in result['checks'][0]['problems']])
        no_prefix = {**template, 'title': ''}
        self.assertEqual(compliance.check_issue('问题', '### 确认\n- [ ] 已阅读（必选）', [no_prefix])['status'],
                         'no_match')


class SectionFormattingTests(unittest.TestCase):
    @staticmethod
    def template(*labels):
        return {**TEMPLATE, 'body': [
            {'type': 'textarea', 'attributes': {'label': label, 'placeholder': '请输入'},
             'validations': {'required': True}} for label in labels]}

    def test_case_colons_and_hints_do_not_change_content_validation(self):
        template = self.template('Environment / 环境信息 (Mandatory / 必填)：')
        original = json.dumps(template, ensure_ascii=False)
        for heading in ('ENVIRONMENT / 环境信息:', 'environment：', '环境信息：(required)：',
                        '环境信息 / Environment', 'Environment（环境信息）'):
            for content, reason in (('Linux', None), ('', 'empty_section'), ('请输入', 'empty_section')):
                with self.subTest(heading=heading, content=content):
                    result = compliance.check_issue('[Bug] 错误', f'### {heading}\n{content}', [template])
                    problems = result['checks'][0]['problems']
                    self.assertEqual([p['reason'] for p in problems], [] if reason is None else [reason])
        self.assertEqual(json.dumps(template, ensure_ascii=False), original)

    def test_bilingual_parentheses_allow_complete_single_language_names(self):
        template = self.template('Document Link（文档链接）', 'Issues Section（问题文档片段）')
        for first, second in (('文档链接', '问题文档片段'), ('DOCUMENT LINK:', 'Issues Section：'),
                              ('文档链接 / Document Link', '问题文档片段（Issues Section）')):
            with self.subTest(first=first, second=second):
                body = f'## {first}\nhttps://example.com/doc\n## {second}\n有问题的片段'
                self.assertEqual(compliance.check_issue('[Bug] 错误', body, [template])['status'], 'pass')

    def test_slash_options_match_individually_without_synonyms(self):
        template = self.template('Related log / screenshot / 日志 / 截图 (Mandatory / 必填)')
        for heading in ('日志', '截图', 'screenshot', 'Related log', 'Related log / 日志',
                        '日志 / 结果', '日志 / 截图', 'Related log / screenshot',
                        '日志 / 截图 / Related log / screenshot'):
            with self.subTest(heading=heading):
                self.assertEqual(compliance.check_issue('[Bug] 错误', f'### {heading}\n日志内容',
                                                        [template])['status'], 'pass')
        for label, heading in (
            ('Related log / screenshot / 日志 / 截图', '结果'),
            ('Related log / screenshot / 日志 / 截图', 'log'),
            ('Describe the expected behavior / 预期结果', 'Expected behavior'),
            ('预期结果', '实际结果'), ('重现步骤', '复现步骤'),
            ('问题描述', '问题描述（当前行为）'),
            ('版本（运行环境）', '版本'), ('Version (Windows)', 'Version'),
            ('版本（开发/生产）', '生产'),
        ):
            with self.subTest(label=label, heading=heading):
                result = compliance.check_issue('[Bug] 错误', f'### {heading}\n内容', [self.template(label)])
                self.assertEqual([p['reason'] for p in result['checks'][0]['problems']], ['missing_section'])

    def test_slash_options_still_require_content_and_unique_field(self):
        template = self.template('日志／截图 (Mandatory / 必填)')
        for content in ('', '请输入', '<!-- 日志待补 -->'):
            result = compliance.check_issue('[Bug] 错误', f'### 日志\n{content}', [template])
            self.assertEqual([p['reason'] for p in result['checks'][0]['problems']], ['empty_section'])
        template = self.template('日志 / 截图', '日志 / 附件')
        result = compliance.check_issue('[Bug] 错误', '### 日志\n实际日志', [template])
        self.assertEqual([p['reason'] for p in result['checks'][0]['problems']],
                         ['missing_section', 'missing_section'])
        template = self.template('日志 / 截图', '环境 / 平台')
        result = compliance.check_issue('[Bug] 错误', '### 日志 / 环境\n实际内容', [template])
        self.assertEqual([p['reason'] for p in result['checks'][0]['problems']],
                         ['missing_section', 'missing_section'])

    def test_business_parentheses_are_preserved_with_full_width_normalization(self):
        template = self.template('版本（运行环境）')
        result = compliance.check_issue('[Bug] 错误', '### 版本(运行环境)：\nv1', [template])
        self.assertEqual(result['status'], 'pass')

    def test_either_complete_language_can_match_without_synonyms(self):
        template = self.template('Describe the expected behavior / 预期结果 (Mandatory / 必填)')
        for heading in ('Expected behavior / 预期结果 (Mandatory / 必填)',
                        '预期结果 / Expected behavior', 'Expected behavior（预期结果）',
                        'Describe the expected behavior / 期望行为'):
            for content, reasons in (('应正常启动', []), ('', ['empty_section']),
                                     ('请输入', ['empty_section'])):
                with self.subTest(heading=heading, content=content):
                    result = compliance.check_issue('[Bug] 错误', f'### {heading}\n{content}', [template])
                    self.assertEqual([p['reason'] for p in result['checks'][0]['problems']], reasons)

    def test_conflicting_language_matches_do_not_fill_either_field(self):
        template = self.template('Expected behavior / 预期结果', 'Actual behavior / 实际结果')
        for heading in ('Expected behavior / 实际结果', '预期结果（Actual behavior）'):
            with self.subTest(heading=heading):
                result = compliance.check_issue('[Bug] 错误', f'### {heading}\n内容', [template])
                self.assertEqual([p['reason'] for p in result['checks'][0]['problems']],
                                 ['missing_section', 'missing_section'])

    def test_ambiguous_short_names_do_not_fill_multiple_fields(self):
        template = self.template('Build environment / 环境', 'Runtime environment / 环境')
        result = compliance.check_issue('[Bug] 错误', '### 环境\nLinux', [template])
        self.assertEqual([p['reason'] for p in result['checks'][0]['problems']],
                         ['missing_section', 'missing_section'])
        template = self.template('Environment / 环境', '环境')
        result = compliance.check_issue('[Bug] 错误', '### 环境\nLinux', [template])
        self.assertEqual([p['field'] for p in result['checks'][0]['problems']], ['Environment / 环境'])

    def test_single_language_sections_keep_separate_content_boundaries(self):
        template = self.template('Description / 问题描述', 'Environment / 环境信息')
        body = '### 问题描述：\n<!-- 尚未填写 -->\n### 环境信息：\nLinux'
        result = compliance.check_issue('[Bug] 错误', body, [template])
        self.assertEqual([(p['field'], p['reason']) for p in result['checks'][0]['problems']],
                         [('Description / 问题描述', 'empty_section')])

    def test_checkbox_and_dropdown_fields_keep_option_validation(self):
        template = {**TEMPLATE, 'body': [
            {'type': 'checkboxes', 'attributes': {'label': 'Confirmation / 确认项',
                'options': [{'label': 'I agree', 'required': True}]}},
            {'type': 'dropdown', 'attributes': {'label': 'Platform（平台）', 'options': ['Linux']},
             'validations': {'required': True}}]}
        body = '### Confirm / 确认项：\n- [x] I agree\n### PLATFORM（运行平台）:\nLinux'
        self.assertEqual(compliance.check_issue('[Bug] 错误', body, [template])['status'], 'pass')
        for modified, reason in ((body.replace('[x]', '[ ]'), 'unchecked_required'),
                                 (body.replace('I agree', 'i agree'), 'unchecked_required'),
                                 (body.replace('Linux', 'Other'), 'invalid_option')):
            result = compliance.check_issue('[Bug] 错误', modified, [template])
            self.assertIn(reason, [p['reason'] for p in result['checks'][0]['problems']])

    def test_markdown_templates_and_fake_headings(self):
        template = compliance.parse_template('### Environment / 环境信息 (必填)\n请输入', 'bug.md')
        self.assertEqual(compliance.check_issue('错误', '### 环境信息：\nLinux', [template])['status'], 'pass')
        for body in ('```md\n### 环境信息：\nLinux\n```', '> ### 环境信息：\n> Linux'):
            result = compliance.check_issue('错误', body, [template])
            self.assertEqual([p['reason'] for p in result['checks'][0]['problems']], ['missing_section'])

    def test_issue_title_prefix_is_not_normalized_like_section_names(self):
        template = {**self.template('Environment / 环境信息'), 'title': '[Bug]: '}
        for title in ('[bug]: 错误', '[BugX] 错误'):
            result = compliance.check_issue(title, '### 环境信息\nLinux', [template])
            self.assertIn('title_prefix', [p['reason'] for p in result['checks'][0]['problems']])

    def test_optional_title_colon_keeps_prefix_boundaries_and_body_checks(self):
        for prefix in ('[Bug]: ', '[Bug]：'):
            template = {**self.template('日志 / 截图'), 'title': prefix}
            for title in ('[Bug]错误', '[Bug] 错误', '[Bug]:错误', '[Bug]：错误'):
                with self.subTest(prefix=prefix, title=title):
                    result = compliance.check_issue(title, '### 日志\n实际日志', [template])
                    self.assertEqual(result['status'], 'pass')
                    result = compliance.check_issue(title, '### 日志\n', [template])
                    self.assertEqual(result['status'], 'no_match')
                    self.assertEqual([p['reason'] for p in result['checks'][0]['problems']], ['empty_section'])
        template = {**self.template('日志'), 'title': 'Bug: '}
        for title in ('Bug 错误', 'Bug：错误', 'Bug:错误'):
            self.assertEqual(compliance.check_issue(title, '### 日志\n实际日志', [template])['status'], 'pass')
        for title in ('Buggy', 'Bugfix: 错误'):
            result = compliance.check_issue(title, '### 日志\n实际日志', [template])
            self.assertIn('title_prefix', [p['reason'] for p in result['checks'][0]['problems']])


class WhitespaceMatchingTests(unittest.TestCase):
    def test_title_and_bilingual_sections_ignore_all_whitespace(self):
        template = {**TEMPLATE, "title": " [CANN Lab] : ", "body": [{
            "type": "textarea", "attributes": {"label": "Describe behavior / 问题描述 (必填)"},
            "validations": {"required": True}}]}
        for space in ("", " ", "  ", "\t", "\u3000", "\u00a0"):
            with self.subTest(space=space):
                title = space + f"[CANN{space}Lab]{space}:没有8卡选项"
                body = f"### Describe{space}behavior{space}/{space}问题{space}描述{space}(必填)\n启动失败"
                self.assertEqual(compliance.check_issue(title, body, [template])["status"], "pass")
        result = compliance.check_issue("其他", "### Describebehavior/问题 描述(必填)\n启动失败", [template])
        self.assertEqual(result["matched_templates"], [])
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["checks"][0]["problems"][0]["reason"], "title_prefix")

    def test_equivalent_headings_merge_without_changing_code_or_display_labels(self):
        body = "### 问 题描述\n```python\nif ready:\n    run('a b')\n```\n### 问题 描述\n补充"
        self.assertEqual(compliance._sections(body), {"问题描述": "if ready:\n    run('a b')\n补充"})
        self.assertEqual(compliance.check_issue("[Bug]测试", body, [TEMPLATE])["status"], "pass")
        template = compliance.parse_template("### 问题 描述\n请 填 写", "bug.md")
        field = template["body"][0]
        self.assertEqual(field["attributes"], {"label": "问题 描述", "placeholder": "请 填 写"})
        result = compliance.check_issue("测试", "### 问题描述\n请填写", [template])
        self.assertEqual(result["checks"][0]["problems"][0]["field"], "问题 描述")

    def test_checkbox_labels_ignore_whitespace_but_still_require_checked_choice(self):
        template = {"name": "确认", "body": [{"type": "checkboxes", "attributes": {
            "label": "确认 项", "options": [{"label": "已阅读 **说明** 和 协议", "required": True}]},
            "validations": {"required": True}}]}
        for choice, expected in (("- [x] 已 阅读说明\t和\u3000协议", "pass"),
                                 ("- [X] 已阅读说明和\n  协议", "pass"),
                                 ("- [ ] 已阅读说明和协议", "no_match"),
                                 ("- [x] 未阅读说明和协议", "no_match")):
            with self.subTest(choice=choice):
                result = compliance.check_issue("确认", "### 确 认项\n" + choice, [template])
                self.assertEqual(result["status"], expected)

    def test_yaml_and_markdown_placeholders_ignore_whitespace(self):
        markdown = compliance.parse_template("### 问题 描述\n请 输 入", "bug.md")
        for template in (TEMPLATE, markdown):
            for content in ("请 输\t入", "请\n输入", "_No\u3000response_", "Noresponse", "\u00a0\u3000\t"):
                with self.subTest(template=template["name"], content=content):
                    result = compliance.check_issue("[Bug] 测试", "### 问题描述\n" + content, [template])
                    self.assertEqual(result["status"], "no_match")
                    self.assertEqual(result["checks"][0]["problems"][0]["reason"], "empty_section")

    def test_allowed_values_and_prefills_use_same_whitespace_rules(self):
        for kind in ("dropdown", "input", "textarea"):
            attrs = {"label": "选择 项"}
            attrs.update({"options": ["No response"]} if kind == "dropdown" else {"value": "No response"})
            template = {"name": "合法预填", "body": [{"type": kind, "attributes": attrs,
                                                     "validations": {"required": True}}]}
            for content in ("Noresponse", "No\t\u3000response"):
                with self.subTest(kind=kind, content=content):
                    result = compliance.check_issue("测试", "### 选择项\n" + content, [template])
                    self.assertEqual(result["status"], "pass")
            self.assertEqual(compliance.check_issue("测试", "### 选择项\n\u3000", [template])["status"], "no_match")

    def test_optional_and_instruction_markers_ignore_whitespace(self):
        for instruction in ("填 写 说明", "Submission\t Instructions"):
            for marker in ("（选 填）", "[ op tional ]", ": Op tional"):
                with self.subTest(instruction=instruction, marker=marker):
                    template = compliance.parse_template(
                        f"## {instruction}\n说明\n### 示例\n示例\n## 问题 描述\n请填写\n"
                        f"## 附加 {marker}\n### 日 志\n请填写", "bug.md")
                    self.assertEqual([f["attributes"]["label"] for f in template["body"]], ["问题 描述", "日 志"])
                    self.assertEqual(compliance.check_issue("测试", "### 问题描述\n失败", [template])["status"], "pass")

    def test_whitespace_only_title_rule_cannot_pass_empty_template(self):
        template = {"name": "空模板", "title": " \t\u3000", "body": []}
        result = compliance.check_issue("任意标题", "内容", [template])
        self.assertEqual(result["status"], "no_match")
        self.assertIn("limitation", result["checks"][0])


class TemplateFormatTests(unittest.TestCase):
    def test_five_form_types_prefill_render_multiselect_and_metadata(self):
        """验证五种表单组件、预填值、代码块、多选和元数据兼容。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        template = compliance.parse_template('''
name: 完整表单
description: 所有正式表单元素
title: '[Test] '
labels: [test]
assignees: tester
body:
  - type: markdown
    attributes:
      value: '## 说明标题不会成为必填章节'
  - type: input
    id: version
    attributes:
      label: 版本
      value: '1.0'
    validations:
      required: true
  - type: textarea
    attributes:
      label: 日志
      render: bash
      value: echo test
    validations:
      required: true
  - type: dropdown
    attributes:
      label: 平台
      options: [Linux, Windows]
      multiple: true
      default: 0
    validations:
      required: true
  - type: checkboxes
    attributes:
      label: 确认
      options:
        - label: 已搜索已有问题
          required: true
        - label: 附加说明
''', 'all.yaml')
        body = '### 版本\n1.0\n### 日志\n```bash\necho test\n```\n### 平台\nLinux, Windows\n### 确认\n- [x] 已搜索已有问题\n- [ ] 附加说明'
        result = compliance.check_issue('[Test] 测试', body, [template])
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(len(compliance._fields(template)), 4)
        self.assertEqual(template['labels'], ['test'])

    def test_dropdown_documented_option_variants_and_yaml_words(self):
        """验证官方下拉选项写法，并保留 Yes、No 等文字。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        for options, expected in (
            ('[1.0, 2.0]', ['1.0', '2.0']),
            ('[Yes, No, On, Off]', ['Yes', 'No', 'On', 'Off']),
            ('[{label: Linux}, {label: Windows}]', ['Linux', 'Windows']),
        ):
            with self.subTest(options=options):
                template = compliance.parse_template('name: 选项\nbody:\n- type: dropdown\n  attributes:\n'
                    '    label: 类型\n    options: ' + options + '\n  validations:\n    required: true\n', 'choice.yml')
                self.assertEqual(compliance._fields(template)[0]['attributes']['options'], expected)
                self.assertEqual(compliance.check_issue('测试', '### 类型\n' + expected[0], [template])['status'], 'pass')
        # 定制解析器不能改变应用中其他 PyYAML 调用的行为。
        self.assertIs(yaml.safe_load('value: yes')['value'], True)

    def checkbox_template(self, group_required=False):
        """创建复选框测试模板。

        Args:
            group_required: 是否将整个复选框组设为必填。
        Returns:
            包含一个必选项和一个可选项的模板字典。
        """
        return {'name': '确认', 'title': '[Check] ', 'body': [{'type': 'checkboxes',
            'attributes': {'label': '确认项', 'options': [
                {'label': '已阅读 **说明** 和 [协议](https://example.test)', 'required': True},
                {'label': '可选项'}]}, 'validations': {'required': group_required}}]}

    def test_required_checkbox_options_apply_even_when_group_is_optional(self):
        """验证复选框组可选时，单个必选项仍须勾选。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        template = self.checkbox_template()
        selected = '已阅读 **说明** 和 [协议](https://example.test)'
        for body in ('', '### 确认项\n- [ ] ' + selected, '### 确认项\n- [x] 可选项'):
            with self.subTest(body=body):
                self.assertEqual(compliance.check_issue('[Check] test', body, [template])['status'], 'no_match')
        for selected in (selected, '已阅读 说明 和 协议'):
            self.assertEqual(compliance.check_issue('[Check] test', '### **确认项**\n- [X] ' + selected,
                                                [template])['status'], 'pass')

    def test_code_quotes_other_sections_and_plain_text_cannot_fake_checkboxes(self):
        """验证代码、引用、其他章节和普通文字不能冒充有效勾选。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        template = self.checkbox_template()
        choice = '[x] 已阅读 说明 和 协议'
        for body in ('### 确认项\n```\n- ' + choice + '\n```',
                     '### 确认项\n> - ' + choice,
                     '### 确认项\n' + choice,
                     '### 确认项\n- [ ] 已阅读 说明 和 协议\n### 其他\n- ' + choice):
            with self.subTest(body=body):
                self.assertEqual(compliance.check_issue('[Check] test', body, [template])['status'], 'no_match')

    def test_checkbox_group_required_needs_one_known_checked_option(self):
        """验证必填复选框组至少有一个有效勾选。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        template = self.checkbox_template(True)
        for option in template['body'][0]['attributes']['options']:
            option.pop('required', None)
        for content, status in (('- [ ] 可选项', 'no_match'), ('- [x] 陌生选项', 'no_match'),
                                ('- [x] 可选项', 'pass')):
            self.assertEqual(compliance.check_issue('[Check] test', '### 确认项\n' + content,
                                                [template])['status'], status)
        template['body'][0]['validations']['required'] = False
        self.assertEqual(compliance.check_issue('[Check] test', '', [template])['status'], 'pass')

    def test_unnamed_textarea_checks_embedded_markdown_sections(self):
        """真实 YAML 文本框按内嵌章节检查，空白与说明不算填写，选填项可省略。

        Args:
            无。
        Returns:
            None；核对章节规则、标题匹配及模板原文未被改写。
        """
        path = Path(__file__).with_name('fixtures') / 'markdown_textarea.yml'
        raw = path.read_text()
        template = compliance.parse_template(raw, path.name)
        self.assertEqual(template, yaml.safe_load(raw))
        fields = compliance._fields(template)
        self.assertEqual([f['validations']['required'] for f in fields], [True, False, False, False])
        title = template['title'] + '启动失败'
        for body, status, reason in (
            ('#### 一、问题描述（必填）\n启动失败', 'pass', None),
            ('#### 一、问题描述\n启动失败', 'pass', None),
            ('#### 一、问题描述\n<!-- 请填写 -->', 'no_match', 'empty_section'),
            ('', 'no_match', 'missing_section'),
            ('#### 一、问题描述（必填）\n<!-- 请填写 -->', 'no_match', 'empty_section'),
            (template['body'][1]['attributes']['value'], 'no_match', 'empty_section'),
            ('```md\n#### 一、问题描述（必填）\n启动失败\n```', 'no_match', 'missing_section'),
        ):
            with self.subTest(body=body):
                result = compliance.check_issue(title, body, [template])
                self.assertEqual(result['status'], status)
                if reason:
                    self.assertEqual([p['reason'] for p in result['checks'][0]['problems']], [reason])
        result = compliance.check_issue('启动失败', '#### 一、问题描述（必填）\n启动失败', [template])
        self.assertEqual(result['status'], 'no_match')
        self.assertEqual(template, yaml.safe_load(raw))

    def test_markdown_value_is_checked_with_or_without_label(self):
        """有无 label 都按 value 章节检查，原样占位与仅填写外层 label 不算通过。

        Args:
            无。
        Returns:
            None；验证不同名称、显式选填与没有 Markdown 标题时的预填行为。
        """
        markdown = '## 描述\n请输入问题\n## 补充（选填）\n### 日志\n请填写日志'
        for attrs in ({'value': markdown}, {'label': None, 'value': markdown},
                      {'label': '', 'value': markdown}, {'label': '  ', 'value': markdown},
                      {'label': '详细信息', 'value': markdown}):
            template = {'name': '问题反馈', 'title': '[Bug]', 'body': [
                {'type': 'textarea', 'attributes': attrs, 'validations': {'required': True}}]}
            with self.subTest(attrs=attrs):
                self.assertEqual(compliance.check_issue('[Bug] 错误', '## 描述\n启动失败', [template])['status'], 'pass')
                self.assertEqual(compliance.check_issue('[Bug] 错误', markdown, [template])['status'], 'no_match')
                self.assertEqual(compliance.check_issue('[Bug] 错误', '## 详细信息\n已填写', [template])['status'], 'no_match')
                template['body'][0]['validations']['required'] = False
                self.assertEqual(compliance.check_issue('[Bug] 错误', '', [template])['status'], 'pass')
        template['body'][0]['attributes'] = {'label': '内容', 'value': '默认内容'}
        template['body'][0]['validations']['required'] = True
        self.assertEqual(compliance.check_issue('[Bug] 错误', '## 内容\n默认内容', [template])['status'], 'pass')
        self.assertEqual(compliance.check_issue('[Bug] 错误', '## 描述\n启动失败', [template])['status'], 'no_match')

    def test_unnamed_fields_still_reject_uncheckable_or_invalid_values(self):
        """仅放宽可提取 Markdown 填写章节的文本框，不掩盖其他模板错误。

        Args:
            无。
        Returns:
            None；无章节、错误类型和代码渲染等无效输入仍须报错。
        """
        for kind, attrs, validations in (
            ('textarea', {'value': ''}, {}),
            ('textarea', {'value': '请填写问题'}, {}),
            ('textarea', {'value': '### 填写说明\n说明'}, {}),
            ('textarea', {'value': '```md\n### 描述\n```'}, {}),
            ('textarea', {'value': '> ### 描述'}, {}),
            ('textarea', {'value': 123}, {}),
            ('textarea', {'label': 123, 'value': '### 描述'}, {}),
            ('textarea', {'value': '### 描述', 'render': 'markdown'}, {}),
            ('textarea', {'value': '### 描述', 'description': []}, {}),
            ('textarea', {'value': '### 描述'}, {'required': 'true'}),
            ('input', {'value': '### 描述'}, {}),
        ):
            template = {'name': '无效模板', 'body': [
                {'type': kind, 'attributes': attrs, 'validations': validations}]}
            with self.subTest(template=template), self.assertRaises(ValueError):
                compliance.parse_template(yaml.safe_dump(template, allow_unicode=True), 'bad.yml')

    def test_markdown_headings_are_required_with_or_without_front_matter(self):
        """验证 Markdown 章节默认必填，缺失、空白和原样提示不会通过。

        Args:
            无。
        Returns:
            None；核对字段提取、格式状态和具体问题。
        """
        for header in ('', '---\nname: 文档反馈\ntitle: "[Doc] "\n---\n'):
            template = compliance.parse_template(header + '### 描述\n请描述问题\n### 环境\n<!-- 请填写 -->', 'question.md')
            complete = '### 描述\n启动时崩溃\n### 环境\nLinux'
            self.assertEqual(compliance.check_issue('[Doc] test', complete, [template])['status'], 'pass')
            for body, field, reason in (
                ('### 环境\nLinux', '描述', 'missing_section'),
                ('### 描述\n<!-- 空 -->\n### 环境\nLinux', '描述', 'empty_section'),
                ('### 描述\n请描述问题\n### 环境\nLinux', '描述', 'empty_section'),
                ('### 描述\n请描述问题  \n\n### 环境\nLinux', '描述', 'empty_section'),
                ('### 描述\n实际问题\n### 环境\n_No response_', '环境', 'empty_section'),
            ):
                with self.subTest(header=header, body=body):
                    result = compliance.check_issue('[Doc] test', body, [template])
                    self.assertEqual(result['status'], 'no_match')
                    self.assertTrue(any(p['field'] == field and p['reason'] == reason
                                        for p in result['checks'][0]['problems']))
            self.assertEqual(compliance.check_issue('其他', '无章节', [template])['status'], 'no_match')

    def test_markdown_optional_groups_and_instructions_are_not_required(self):
        """验证分组不重复检查，选填标记向子节继承，说明章节不作输入。

        Args:
            无。
        Returns:
            None；核对字段集合及只填写必填章节时的检查结果。
        """
        for marker in ('（选填）', '(可选)', '[optional]', ': Optional'):
            template = compliance.parse_template(
                '# 缺陷反馈\n## 填写说明\n请按模板提交\n### 示例\n示例文字\n'
                '## 问题描述\n请填写\n## 补充信息' + marker + '\n### 日志\n请附日志', 'bug.md')
            fields = compliance._fields(template)
            self.assertEqual([f['attributes']['label'] for f in fields], ['问题描述', '日志'])
            self.assertEqual([f['validations']['required'] for f in fields], [True, False])
            self.assertEqual(compliance.check_issue('test', '## 问题描述\n启动失败', [template])['status'], 'pass')
        template = compliance.parse_template('## 可选功能行为\n请描述', 'feature.md')
        self.assertTrue(compliance._fields(template)[0]['validations']['required'])

    def test_markdown_ignores_fake_headings_and_checks_code_content(self):
        """验证伪标题不产生字段，代码正文可填写，但原样示例不算填写。

        Args:
            无。
        Returns:
            None；核对真实标题识别、格式归一化及代码占位检查。
        """
        template = compliance.parse_template('```md\n### 代码中的标题\n```\n> ### 引用标题\n\n'
            '日志\n----\n```bash\nexample command\n```\n## **问题描述**\n<!-- 说明 -->', 'logs.md')
        self.assertEqual([f['attributes']['label'] for f in compliance._fields(template)], ['日志', '问题描述'])
        for content, expected in (('actual command', 'pass'), ('example command', 'no_match'), ('', 'no_match')):
            body = '### 日志\n```bash\n' + content + '\n```\n### 问题描述\n启动失败'
            self.assertEqual(compliance.check_issue('test', body, [template])['status'], expected)
        result = compliance.check_issue('test', '> ### 日志\n> 内容\n### 问题描述\n启动失败', [template])
        self.assertEqual(result['checks'][0]['problems'][0]['reason'], 'missing_section')

    def test_markdown_without_required_sections_remains_no_match(self):
        """验证没有标题、只有说明或只有选填章节时仍不误报通过。

        Args:
            无。
        Returns:
            None；核对 no_match、空匹配列表及限制说明。
        """
        for body in ('请说明你的使用场景。', '### 填写说明\n请填写', '### 日志（选填）\n请填写'):
            template = compliance.parse_template('---\ntitle: "[Doc] "\n---\n' + body, 'question.md')
            result = compliance.check_issue('[Doc] test', '### 日志（选填）\n实际日志', [template])
            self.assertEqual(result['status'], 'no_match')
            self.assertEqual(result['matched_templates'], [])
            self.assertIn('没有可检查的必填章节', result['checks'][0]['limitation'])
            with self.assertRaises(IssueProcessingError) as raised:
                compliance.format_compliance_check(result)
            self.assertTrue(raised.exception.retryable)

    def test_markdown_and_instruction_only_templates_do_not_override_yaml_failures(self):
        """验证规则不足的模板不会掩盖 YAML 必填检查失败。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        markdown = compliance.parse_template('### 填写说明\n说明', 'question.md')
        empty = compliance.parse_template('name: 说明\nbody:\n- type: markdown\n  attributes:\n    value: 谢谢', 'info.yml')
        required = compliance.parse_template('name: 缺陷\ntitle: "[Bug] "\nbody:\n- type: input\n  attributes:\n'
                                  '    label: 描述\n  validations:\n    required: true', 'bug.yml')
        result = compliance.check_issue('[Bug] test', '', [markdown, empty, required])
        self.assertEqual(result['status'], 'no_match')
        self.assertEqual(result['matched_templates'], [])
        markdown['title'] = '[Doc] '
        for templates in ([markdown, required], [required, markdown]):
            result = compliance.check_issue('[Doc] test', '### 描述\n实际内容', templates)
            self.assertEqual(result['status'], 'no_match')
            self.assertEqual(result['matched_templates'], [])
        for template in (empty, compliance.parse_template('name: 空表单', 'empty.yml'),
                         compliance.parse_template('name: 可选表单\nbody:\n- type: input\n  attributes:\n    label: 描述', 'optional.yml')):
            result = compliance.check_issue('test', '### 描述\n实际内容', [template])
            self.assertEqual(result['status'], 'no_match')
            self.assertEqual(result['matched_templates'], [])

    def test_malformed_unknown_types_and_unsafe_yaml_are_rejected(self):
        """验证语法错误、未知组件和不安全 YAML 标签会被拒绝。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        for text in ('name: [broken', '!!python/object/apply:os.system [echo bad]',
                     'name: bad\nbody: [{type: future-widget, attributes: {label: Test}}]',
                     'name: bad\nbody: [{type: checkboxes, attributes: {label: Test, options: [Yes]}}]'):
            with self.subTest(text=text), self.assertRaises((ValueError, yaml.YAMLError)):
                compliance.parse_template(text, 'bad.yml')
        for text in ('---\nname: missing end\n', '---\n[]\n---\ntext', '---\nfalse\n---\ntext'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                compliance.parse_template(text, 'bad.md')

    def test_prefill_and_codeblock_are_content_but_empty_fence_is_not(self):
        """验证预填值和代码正文算有效内容，空代码块不算。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        template = compliance.parse_template('name: 日志\nbody:\n- type: textarea\n  attributes:\n'
            '    label: 日志\n    render: bash\n    value: echo test\n  validations:\n    required: true', 'logs.yml')
        for content, status in (('```bash\necho test\n```', 'pass'), ('```bash\n\n```', 'no_match')):
            self.assertEqual(compliance.check_issue('test', '### 日志\n' + content, [template])['status'], status)


class RequiredAndDropdownTests(unittest.TestCase):
    def test_required_hints_override_false_without_mutating_template(self):
        """文字必填标记覆盖 false，名称、描述和占位提示均有效且不改写源模板。

        Args:
            无。
        Returns:
            None；缺失、占位与真实填写分别得到预期检查结果。
        """
        for key in ('label', 'description', 'placeholder', 'value'):
            for marker in ('信息（必填）', '必须提供信息', '信息 Required', 'Mandatory',
                           'Please fill this required field', 'You must fill this field', '信息 *', '*信息'):
                attrs = {'label': '信息', key: marker}
                template = {'name': '测试', 'title': '[Test]', 'body': [
                    {'type': 'textarea', 'attributes': attrs, 'validations': {'required': False}}]}
                original = json.dumps(template)
                label = attrs['label']
                with self.subTest(key=key, marker=marker):
                    result = compliance.check_issue('[Test] 问题', '', [template])
                    self.assertEqual(result['status'], 'no_match')
                    self.assertEqual(result['checks'][0]['problems'][0]['reason'], 'missing_section')
                    result = compliance.check_issue('[Test] 问题', f'### {label}\n实际问题', [template])
                    self.assertEqual(result['status'], 'pass')
                    self.assertEqual(json.dumps(template), original)
        for text in ('非必填', '不是必填', '无需必填', '不必填写', 'Not required',
                     'not a required field', 'not mandatory', 'non-mandatory', '信息（可选）', '**信息**',
                     '```python\nrequired = True\n```'):
            with self.subTest(optional=text):
                self.assertFalse(compliance._required_hint(text))
        self.assertTrue(compliance._required_hint('不是必填；但另一个说明明确写了必填'))

    def test_real_public_templates_require_prose_marked_fields(self):
        """真实咨询和任务模板中的必填文字在配置缺失或为 false 时仍生效。

        Args:
            无。
        Returns:
            None；核对新增必填项、缺项提示及修正后的最终评论。
        """
        for filename, expected in (
            ('public_question.yml', ['问题描述 / Question Description (Mandatory / 必填)']),
            ('public_help_wanted.yml', ['任务背景 / Background (Mandatory / 必填)',
                '任务目标 / Goals (Mandatory / 必填)', '难度级别 / Difficulty Level (Mandatory / 必填)',
                '任务类型 / Task Type (Mandatory / 必填)']),
        ):
            path = Path(__file__).with_name('fixtures') / filename
            template = compliance.parse_template(path.read_text(), path.name)
            fields = [f for f in compliance._fields(template) if f.get('validations', {}).get('required')]
            self.assertEqual([f['attributes']['label'] for f in fields], expected)
            result = compliance.check_issue(template['title'] + '测试', '', [template])
            self.assertEqual(result['status'], 'no_match')
            comment = compliance.format_compliance_check({"status": "no_match", "recommendation": {
                "status": "needs_changes", "template": template["name"],
                "problems": result["checks"][0]["problems"]}}, templates=[template])
            self.assertIn('提交内容规范性检查：未通过', comment)
            self.assertIn('缺少此必填章节', comment)
            body = '\n'.join('### ' + f['attributes']['label'] + '\n' +
                (f['attributes']['options'][0] if f['type'] == 'dropdown' else '实际问题') for f in fields)
            self.assertEqual(compliance.check_issue(template['title'] + '测试', body, [template])['status'], 'pass')

    def test_embedded_required_sections_override_optional_parent(self):
        """章节标题和注释中的必填指示不被可选分组或外层 false 覆盖。

        Args:
            无。
        Returns:
            None；仅要求具体必填章节，其他选填章节可以省略。
        """
        markdown = ('## 补充（可选）\n### 描述（必填）\n<!-- 请描述 -->\n'
                    '### 日志（可选）\n<!-- 此项必填 -->\n### 环境（可选）\n请输入环境')
        for label in (None, '详细信息'):
            template = {'name': '测试', 'title': '[Test]', 'body': [{'type': 'textarea',
                'attributes': {'label': label, 'value': markdown}, 'validations': {'required': False}}]}
            fields = compliance._fields(template)
            self.assertEqual([f['validations']['required'] for f in fields], [True, True, False])
            self.assertEqual(compliance.check_issue('[Test] 问题', '', [template])['status'], 'no_match')
            body = '### 描述（必填）\n启动失败\n### 日志（可选）\n错误日志'
            self.assertEqual(compliance.check_issue('[Test] 问题', body, [template])['status'], 'pass')
        template = compliance.parse_template('## 描述（必填）\n### 详情（可选）\n请填写', 'bug.md')
        self.assertTrue(compliance._fields(template)[0]['validations']['required'])

    def test_checkbox_text_and_boolean_required_are_combined(self):
        """复选项的必选文字与 required 配置均需满足，选填组不能覆盖它们。

        Args:
            无。
        Returns:
            None；遗漏任何必选项均失败，两个必选项均勾选才通过。
        """
        template = {'name': '测试', 'title': '[Test]', 'body': [{'type': 'checkboxes',
            'attributes': {'label': '确认', 'options': [
                {'label': '已阅读（必选）', 'required': False}, {'label': '已搜索', 'required': True},
                {'label': '其他（非必选）'}]}, 'validations': {'required': False}}]}
        for content, status in (('- [x] 已搜索', 'no_match'),
                                ('- [x] 已阅读（必选）', 'no_match'),
                                ('- [x] 已阅读（必选）\n- [x] 已搜索', 'pass')):
            result = compliance.check_issue('[Test] 问题', '### 确认\n' + content, [template])
            self.assertEqual(result['status'], status)

    def test_dropdown_checks_single_multiple_and_optional_values(self):
        """下拉单选与逗号分隔的多选只允许候选值，并保留空白兼容。

        Args:
            无。
        Returns:
            None；选填可以留空，但已填写的非法值仍显示 invalid_option。
        """
        for multiple in (False, True):
            for required in (False, True):
                template = {'name': '测试', 'title': '[Test]', 'body': [{'type': 'dropdown',
                    'attributes': {'label': '平台', 'options': ['Linux', 'Windows', 'Other, custom', 'No response'],
                                   'multiple': multiple}, 'validations': {'required': required}}]}
                for content, valid in (('Linux', True), ('Other, custom', True), ('Noresponse', True),
                                       ('Linux, Windows', multiple),
                                       ('Linux, Alien', False), ('Alien', False), ('Linux,', False),
                                       (',Linux', False), ('Linux,,Windows', False)):
                    with self.subTest(multiple=multiple, required=required, content=content):
                        result = compliance.check_issue('[Test] 问题', '### 平台\n' + content, [template])
                        self.assertEqual(result['status'], 'pass' if valid else 'no_match')
                        if not valid:
                            self.assertEqual(result['checks'][0]['problems'][0]['reason'], 'invalid_option')
                for body in ('', '### 平台\n', '### 平台\n_No response_'):
                    result = compliance.check_issue('[Test] 问题', body, [template])
                    self.assertEqual(result['status'], 'no_match' if required else 'pass')

    def test_dropdown_accepts_unique_categories_with_refined_annotations(self):
        """下拉类别唯一时允许改写括号说明，单选和多选仍拒绝未知类别。

        Args:
            无。
        Returns:
            None；类别细化误判或非法类别被放行时测试失败。
        """
        options = ['缺陷修正（链接失效/命令错误/描述不准确）', '内容补充（缺少关键说明/步骤缺失）', '新增文档']
        for multiple in (False, True):
            template = {'name': '文档反馈', 'title': '[文档]', 'body': [{'type': 'dropdown',
                'attributes': {'label': '文档类型', 'options': options, 'multiple': multiple},
                'validations': {'required': True}}]}
            for value, valid in (
                (options[0], True), ('缺陷修正（描述不准确）', True), ('缺陷修正 (权限说明不准确)', True),
                ('缺陷修正', True), ('新增文档（补充安装指南）', True),
                ('缺陷修正（描述不准确）, 内容补充（缺少步骤）', multiple),
                ('缺陷修正（描述不准确）, 未知', False), ('缺陷修正,', False),
                ('其它（缺陷修正）', False), ('非缺陷修正（描述不准确）', False),
                ('缺陷修正错误', False), ('缺陷修正（描述不准确', False)):
                with self.subTest(multiple=multiple, value=value):
                    result = compliance.check_issue('[文档] 权限说明', '### 文档类型\n' + value, [template])
                    self.assertEqual(result['status'], 'pass' if valid else 'no_match')
                    if not valid:
                        self.assertEqual(result['checks'][0]['problems'][0]['reason'], 'invalid_option')

    def test_dropdown_keeps_annotations_that_distinguish_candidates(self):
        """括号是区分同名候选的必要信息时，不允许简写或任意改写。

        Args:
            无。
        Returns:
            None；不同平台选项被混淆时测试失败。
        """
        template = {'name': '平台反馈', 'title': '[Test]', 'body': [{'type': 'dropdown',
            'attributes': {'label': '平台', 'options': ['Linux（x86）', 'Linux（ARM）', 'Windows']},
            'validations': {'required': True}}]}
        for value, valid in (('Linux（x86）', True), ('Linux（ARM）', True), ('Linux', False),
                             ('Linux（未知架构）', False), ('Windows（版本 11）', True)):
            with self.subTest(value=value):
                result = compliance.check_issue('[Test] 问题', '### 平台\n' + value, [template])
                self.assertEqual(result['status'], 'pass' if valid else 'no_match')

    def test_required_config_still_overrides_optional_label(self):
        """字段名写可选也不能抵消 required true 的必填配置。

        Args:
            无。
        Returns:
            None；空正文必须提示缺少必填章节。
        """
        template = {'name': '测试', 'title': '[Test]', 'body': [{'type': 'input',
            'attributes': {'label': '信息（可选）'}, 'validations': {'required': True}}]}
        self.assertEqual(compliance.check_issue('[Test] 问题', '', [template])['status'], 'no_match')


    def test_adjacent_notice_requires_every_checkbox(self):
        """shmem 的全部勾选说明要求每项确认，单项勾选不能通过。

        Args:
            无。
        Returns:
            None；验证具体漏勾提示、最终评论和原模板不被改写。
        """
        labels = ['我已确认这是待做的需求，而非反馈一个新需求。', '我将按模板要求填写详细信息。']
        template = {'name': 'Task|任务', 'title': '[Task|任务]: ', 'body': [
            {'type': 'markdown', 'attributes': {'value':
                '> 该 Issue 提交前，需要检查确认清单事项，**必须全部确认且勾选**，否则此 Issue 可能会被关闭。'}},
            {'type': 'checkboxes', 'attributes': {'label': '提交前确认清单',
                'options': [{'label': label} for label in labels]}, 'validations': {'required': True}}]}
        original = json.dumps(template)
        for selected in ([], [0], [1], [0, 1]):
            with self.subTest(selected=selected):
                body = '### 提交前确认清单\n' + '\n'.join(
                    f"- [{'x' if index in selected else ' '}] {label}" for index, label in enumerate(labels))
                result = compliance.check_issue('[Task|任务]: 测试', body, [template])
                self.assertEqual(result['status'], 'pass' if len(selected) == 2 else 'no_match')
                for index, label in enumerate(labels):
                    if index not in selected:
                        self.assertIn('请勾选必选项：' + label,
                                      [p['message'] for p in result['checks'][0]['problems']])
                if len(selected) == 1:
                    comment = compliance.format_compliance_check({"status": "no_match", "recommendation": {
                        "status": "needs_changes", "template": template["name"],
                        "problems": result["checks"][0]["problems"]}}, templates=[template])
                    self.assertIn('提交内容规范性检查：未通过', comment)
                    self.assertIn('请勾选必选项', comment)
        self.assertEqual(json.dumps(template), original)

    def test_all_checkbox_notice_is_local_and_ignores_code_and_optional_wording(self):
        """全部勾选说明仅作用于紧邻组，普通说明和代码示例不产生强制要求。

        Args:
            无。
        Returns:
            None；验证未勾选的选填项不会因无关说明被误报。
        """
        group = {'type': 'checkboxes', 'attributes': {'label': '确认', 'options': [
            {'label': '第一项', 'required': False}, {'label': '第二项'}]}, 'validations': {'required': True}}
        for notice, required in (('请全部勾选', True), ('务必所有确认并勾选', True),
                                 ('无需全部勾选', False), ('至少勾选一项', False),
                                 ('```text\n必须全部确认且勾选\n```', False)):
            template = {'name': '测试', 'body': [
                {'type': 'markdown', 'attributes': {'value': notice}}, group]}
            with self.subTest(notice=notice):
                self.assertEqual([o['required'] for o in compliance._fields(template)[0]['attributes']['options']],
                                 [required, required])
        template = {'name': '测试', 'body': [
            {'type': 'markdown', 'attributes': {'value': '必须全部确认且勾选'}}, group, group]}
        fields = compliance._fields(template)
        self.assertTrue(all(o['required'] for o in fields[0]['attributes']['options']))
        self.assertFalse(any(o['required'] for o in fields[1]['attributes']['options']))
        template['body'].insert(1, {'type': 'input', 'attributes': {'label': '其他内容'}})
        self.assertFalse(any(o['required'] for o in compliance._fields(template)[1]['attributes']['options']))
