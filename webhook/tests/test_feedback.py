"""新评论规范、模板复制及维护者提醒的离线回归测试。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from markdown_it import MarkdownIt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compliance
from errors import IssueProcessingError
import receiver

TEMPLATE = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
    {"type": "textarea", "attributes": {"label": "问题描述", "placeholder": "请输入"},
     "validations": {"required": True}},
    {"type": "textarea", "attributes": {"label": "复现步骤"}, "validations": {"required": True}}]}
SUGGESTION = {"status": "recommended", "template": "缺陷反馈", "reason": "启动失败，应补齐复现步骤。",
              "maintainer": None}


def reviewed_changes(title, body, template):
    """构造评论单元测试的模型复核缺项，独立于分析入口的路由测试。"""
    local_problems = compliance.check_issue(title, body, [template])["checks"][0]["problems"]
    problems = [{**p, "reason": "title_prefix" if p["reason"] == "title_prefix" else "body"}
                for p in local_problems]
    return {"status": "no_match", "recommendation": {
        "status": "needs_changes", "template": template["name"], "reason": "模板适用，仍需补充",
        "maintainer": None, "problems": problems}}


class FeedbackTests(unittest.TestCase):
    def recommend(self, suggestion):
        """模拟模型响应并通过实际推荐结果解析流程。

        Args:
            suggestion: 待校验的模型推荐 JSON 对象。
        Returns:
            经过 recommend_template 校验和规范化的推荐结果。
        """
        with patch.object(compliance.subprocess, "Popen") as popen, patch.object(compliance.os, "killpg"):
            process = popen.return_value.__enter__.return_value
            process.returncode = 0
            process.communicate.return_value = (json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                                           "result": json.dumps(suggestion)}), "")
            return compliance.recommend_template("启动失败", "崩溃了", [TEMPLATE], "key", "model", "https://model.example")

    def comment(self, recommendation, **kwargs):
        """为未匹配模板的测试样本生成最终评论。

        Args:
            recommendation: 已准备的模板推荐结果。
            kwargs: 传递给评论生成函数的其他参数。
        Returns:
            包含检查结果及相应字段的 Markdown 评论。
        """
        result = compliance.check_issue("启动失败", "崩溃了", [TEMPLATE])
        return compliance.format_compliance_check({**result, "recommendation": recommendation},
                                       templates=[TEMPLATE], **kwargs)

    def test_no_applicable_template_preserves_advice(self):
        """验证无适用模板时维护者建议仍保留供入库。

        Args:
            无。
        Returns:
            None；核对维护者建议没有丢失。
        """
        suggestion = {"status": "not_applicable", "template": None, "reason": "成员权限诉求不属于缺陷反馈。",
                      "maintainer": "建议新增成员权限申请模板，收集成员账号、申请权限与用途。<script> @untrusted"}
        result = self.recommend(suggestion)
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["maintainer"], suggestion["maintainer"])

    def test_maintainer_advice_accepts_plain_text_without_template_fields(self):
        """建议是自由文本，不要求模板字段或内部对象。"""
        text = '建议维护者在贡献指南中说明“申请权限”的处理流程。'
        result = self.recommend({"status": "not_applicable", "template": None,
                                 "reason": "现有模板无法覆盖权限申请。", "maintainer": text})
        self.assertEqual(result["maintainer"], text)

    def test_insufficient_info_lists_all_templates_in_closed_details(self):
        """信息不足时展示全部模板，每个折叠块均包含可复制的标题和正文。

        Args:
            无。
        Returns:
            None；核对原因、模板完整性及默认收起状态。
        """
        reason = "只有 test，请说明遇到的现象或希望增加的功能。"
        result = self.recommend({"status": "insufficient_info", "template": None, "reason": reason, "maintainer": None})
        self.assertEqual(result["status"], "insufficient_info")
        documentation = compliance.parse_template('---\nname: 文档反馈\ntitle: "[文档] "\n---\n### 文档链接\n请填写链接\n', 'doc.md')
        templates = [TEMPLATE, documentation]
        comment = compliance.format_compliance_check({"status": "no_match", "recommendation": result},
                                          templates=templates)
        self.assertIn("### 提交内容规范性检查：未通过", comment)
        self.assertIn(reason, comment)
        self.assertIn("请补充具体诉求", comment)
        self.assertEqual(comment.count("<details>"), 3)
        self.assertEqual(comment.count("</details>"), 3)
        self.assertIn("<summary>缺陷反馈：展开显示模板内容", comment)
        self.assertIn("<summary>文档反馈：展开显示模板内容", comment)
        self.assertNotIn("<details open", comment)
        fences = [t.content for t in MarkdownIt("commonmark").parse(comment) if t.type == "fence"]
        self.assertEqual(len(fences), 4)
        self.assertEqual(fences[0], "[Bug] \n")
        self.assertIn("### 复现步骤", fences[1])
        self.assertEqual(fences[2], "[文档] \n")
        self.assertEqual(fences[3], documentation["markdown_body"] + "\n")
        for text in ("@qiurq1", "#### 其他", "无需据此调整", "建议使用："):
            self.assertNotIn(text, comment)

    def test_recommendation_shows_only_the_selected_template(self):
        """即使存在多个候选，推荐成功时也只展示选定模板的折叠块。

        Args:
            无。
        Returns:
            None；核对唯一推荐和模板内容。
        """
        result = self.recommend(SUGGESTION)
        other = {**TEMPLATE, "name": "功能需求", "title": "[Feature] "}
        comment = compliance.format_compliance_check({"status": "no_match", "recommendation": result},
                                          templates=[other, TEMPLATE])
        self.assertEqual(comment.count("<details>"), 1)
        self.assertIn("<summary>展开显示模板内容，将模板内容复制到 Issue 中填写。</summary>", comment)
        self.assertIn("#### 模板推荐：缺陷反馈", comment)
        self.assertNotIn("查看仓库模板", comment)
        self.assertNotIn("模板完善建议", comment)
        self.assertNotIn("功能需求", comment)
        self.assertNotIn("[Feature]", comment)

    def test_template_name_cannot_break_the_summary(self):
        """模板名称作为纯文字展示，不得截断折叠块或引入额外标签。

        Args:
            无。
        Returns:
            None；核对摘要转义及代码块仍完整可读。
        """
        template = {**TEMPLATE, "name": '反馈</summary></details><script> @someone'}
        comment = "\n".join(compliance._format_template(template, show_name=True))
        rendered = MarkdownIt("commonmark").render(comment)
        self.assertEqual(rendered.count("<details>"), 1)
        self.assertEqual(rendered.count("</details>"), 1)
        self.assertEqual(rendered.count("</summary>"), 1)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("@someone", rendered)
        self.assertEqual(rendered.count("<pre><code"), 2)

    def test_recommended_template_is_copyable_with_nested_fences(self):
        """推荐只决定模板，代码围栏内保留模板原文，不采用模型生成的正文。

        Args:
            无。
        Returns:
            None；核对模板原文、嵌套围栏和说明文字的转义。
        """
        template = deepcopy(TEMPLATE)
        raw = '```python\nprint("<script> @someone")\n```\n[日志](https://example.com/log)'
        template["body"][0]["attributes"]["value"] = raw
        suggestion = {**SUGGESTION, "reason": '<script> [链接](https://example.com) @someone',
                      "draft": {"title": "模型生成的标题", "body": "模型填写的内容"}}
        result = self.recommend(suggestion)
        self.assertEqual(result["status"], "recommended")
        self.assertNotIn("draft", result)
        comment = compliance.format_compliance_check({"status": "no_match", "recommendation": result}, templates=[template])
        tokens = MarkdownIt("commonmark").parse(comment)
        fences = [t.content for t in tokens if t.type == "fence"]
        self.assertEqual(fences[0], "[Bug] \n")
        self.assertIn(raw, fences[1])
        self.assertNotIn("模型填写的内容", comment)
        visible = " ".join(t.content for t in tokens if t.type != "fence")
        self.assertNotIn("<script>", visible)
        self.assertNotIn("@someone", visible)
        self.assertIn("**模板正文（Markdown）**", comment)

    def test_needs_changes_provides_template_without_issue_content(self):
        """模型复核列出确认的缺项，复制内容仅来自模板。

        Args:
            无。
        Returns:
            None；核对原 Issue 的标题和正文未被填入模板。
        """
        raw = '```bash\necho "hello"\n```\n[日志](https://example.com/log)'
        result = reviewed_changes("[Bug] 启动失败", "### 问 题描述\n" + raw, TEMPLATE)
        self.assertEqual(result["status"], "no_match")
        comment = compliance.format_compliance_check(result, templates=[TEMPLATE])
        self.assertIn("按 **缺陷反馈** 模板检查，此 Issue 仍需修改以下内容：", comment)
        self.assertIn("#### 模板完善建议", comment)
        self.assertNotIn("建议使用：", comment)
        self.assertNotIn("#### 模板推荐", comment)
        self.assertIn("复现步骤：缺少此必填章节", comment)
        self.assertEqual(comment.count("<details>"), 1)
        fences = [t.content for t in MarkdownIt("commonmark").parse(comment) if t.type == "fence"]
        self.assertEqual(len(fences), 1)
        self.assertNotIn("**模板标题**", comment)
        self.assertNotIn(raw, fences[0])
        self.assertNotIn("启动失败", comment)
        self.assertIn("### 复现步骤", fences[0])
        self.assertIn("请输入", fences[0])
        self.assertNotIn("@qiurq1", comment)

    def test_needs_changes_only_displays_template_parts_with_problems(self):
        """验证 YAML 和 Markdown 复核结果仅展示存在问题的标题或正文。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        markdown = compliance.parse_template(
            '---\nname: 缺陷反馈\ntitle: "[Bug] "\n---\n'
            '### 问题描述（必填）\n<!-- 请填写 -->\n### 复现步骤（必填）\n<!-- 请填写 -->', 'bug.md')
        cases = [
            ('启动失败', '### 问题描述\n失败\n### 复现步骤\n运行命令', ['text']),
            ('[Bug] 启动失败', '### 问题描述\n失败', ['markdown']),
            ('启动失败', '### 问题描述\n失败\n### 复现步骤\n', ['text', 'markdown']),
        ]
        for template in (TEMPLATE, markdown):
            for title, body, expected_parts in cases:
                with self.subTest(format=template.get('format', 'yaml'), parts=expected_parts):
                    result = reviewed_changes(title, body, template)
                    self.assertEqual(result['status'], 'no_match')
                    comment = compliance.format_compliance_check(result, templates=[template])
                    fences = [t for t in MarkdownIt('commonmark').parse(comment) if t.type == 'fence']
                    self.assertEqual([t.info for t in fences], expected_parts)
                    self.assertEqual('**模板标题**' in comment, 'text' in expected_parts)
                    self.assertEqual('**模板正文（Markdown）**' in comment, 'markdown' in expected_parts)
                    if 'text' in expected_parts:
                        self.assertEqual(fences[0].content, '[Bug] \n')
                    if 'markdown' in expected_parts:
                        self.assertIn('### 复现步骤', fences[-1].content)
                    self.assertEqual(comment.count('<details>'), 1)
                    self.assertNotIn('查看仓库模板', comment)

    def test_body_field_named_title_is_not_mistaken_for_issue_title(self):
        """验证正文中名为 title 的字段不会被误判为 Issue 标题问题。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        template = {**TEMPLATE, 'body': [
            {'type': 'textarea', 'attributes': {'label': 'title'}, 'validations': {'required': True}}]}
        result = reviewed_changes('[Bug] 启动失败', '', template)
        comment = compliance.format_compliance_check(result, templates=[template])
        self.assertIn('**模板正文（Markdown）**', comment)
        self.assertNotIn('**模板标题**', comment)

    def test_yaml_template_preserves_instructions_fields_and_options(self):
        """YAML 转换跳过展示说明，保留字段提示、必填标记、预填值及选项。

        Args:
            无。
        Returns:
            None；核对不同字段类型在复制后仍可供作者填写。
        """
        template = {**TEMPLATE, "body": [
            {"type": "markdown", "attributes": {"value": "请先阅读[说明](https://example.com)"}},
            *TEMPLATE["body"],
            {"type": "input", "attributes": {"label": "版本", "description": "填写版本号", "value": "v1"}},
            {"type": "checkboxes", "attributes": {"label": "确认", "options": [{"label": "已验证", "required": True}]}},
            {"type": "dropdown", "attributes": {"label": "系统", "options": ["Linux", "Windows"], "default": 1},
             "validations": {"required": True}},
            {"type": "dropdown", "attributes": {"label": "平台", "options": [{"label": 1.0}, "GPU"], "multiple": True}},
            {"type": "textarea", "attributes": {"label": "日志", "render": "shell", "value": "echo test"}},
        ]}
        original = deepcopy(template)
        comment = "\n".join(compliance._format_template(template))
        fences = [t.content for t in MarkdownIt("commonmark").parse(comment) if t.type == "fence"]
        copied = {"title": fences[0].removesuffix("\n"), "body": fences[1].removesuffix("\n")}
        self.assertEqual(copied["title"], "[Bug] ")
        self.assertNotIn("请先阅读[说明](https://example.com)", copied["body"])
        for text in ("填写版本号", "v1", "<!-- 必填", "<!-- 选填",
                     "### 确认\n\n<!-- 必填 -->", "- [ ] 已验证 <!-- 必选 -->", "请选择一项：Linux、Windows", "\nWindows\n",
                     "可多选：1.0、GPU", "```shell\necho test\n```"):
            self.assertIn(text, copied["body"])
        self.assertNotIn("- [x]", copied["body"])
        self.assertEqual(template, original)

    def test_embedded_markdown_copy_and_recommendation_preserve_mixed_fields(self):
        """混合表单复制内嵌 Markdown 原文，后续普通字段不串位，推荐看到展开章节。

        Args:
            无。
        Returns:
            None；核对原文、填完可通过，以及传给 Agent 的真实章节规则。
        """
        raw = '## 问题（必填）\n<!-- 请填写 -->\n\n## 日志（选填）\n```text\n示例日志\n```\n'
        template = {**TEMPLATE, 'body': [
            {'type': 'markdown', 'attributes': {'value': '请先阅读说明。'}},
            {'type': 'textarea', 'attributes': {'label': None, 'description': '请描述详情', 'value': raw},
             'validations': {'required': True}},
            {'type': 'dropdown', 'attributes': {'label': '平台', 'options': [{'label': 'Linux'}, 'Windows'],
                                               'default': 0}, 'validations': {'required': True}},
        ]}
        original = deepcopy(template)
        comment = '\n'.join(compliance._format_template(template))
        fences = [t.content for t in MarkdownIt('commonmark').parse(comment) if t.type == 'fence']
        self.assertEqual(len(fences), 2)
        copied = fences[1]
        self.assertIn(raw, copied)
        self.assertEqual(copied.count('## 问题（必填）'), 1)
        self.assertIn('请描述详情', copied)
        self.assertNotIn('请先阅读说明。', copied)
        self.assertIn('### 平台', copied)
        self.assertIn('请选择一项：Linux、Windows', copied)
        self.assertIn('\nLinux\n', copied)
        self.assertEqual(compliance.check_issue('[Bug] 错误', copied, [template])['status'], 'no_match')
        filled = copied.replace('<!-- 请填写 -->', '启动失败')
        self.assertEqual(compliance.check_issue('[Bug] 错误', filled, [template])['status'], 'pass')
        with patch.object(compliance.subprocess, 'Popen') as popen, patch.object(compliance.os, 'killpg'):
            process = popen.return_value.__enter__.return_value
            process.returncode = 0
            process.communicate.return_value = (json.dumps({'type': 'result', 'subtype': 'success',
                'is_error': False, 'result': json.dumps(SUGGESTION)}), '')
            result = compliance.recommend_template('启动失败', '崩溃了', [template], 'key', 'model', 'https://model.example')
        self.assertEqual(result['status'], 'recommended')
        fields = json.loads(process.communicate.call_args.args[0])['templates'][0]['fields']
        self.assertEqual([(f['label'], f['required']) for f in fields],
                         [('问题（必填）', True), ('日志（选填）', False), ('平台', True)])
        self.assertEqual(template, original)
        for label in ('详细信息', '', None):
            with self.subTest(label=label):
                template['body'][1]['attributes']['label'] = label
                self.assertEqual('\n'.join(compliance._format_template(template)), comment)
                self.assertEqual(compliance.check_issue('[Bug] 错误', copied, [template])['status'], 'no_match')
                self.assertEqual(compliance.check_issue('[Bug] 错误', filled, [template])['status'], 'pass')

    def test_markdown_template_body_is_preserved(self):
        """Markdown 模板直接提供原始正文，包括说明、代码和选填章节。

        Args:
            无。
        Returns:
            None；核对复制内容与模板正文逐字相同。
        """
        body = "说明\n\n### 问题\n<!-- 请填写 -->\n\n### 日志（选填）\n```text\n示例\n```\n"
        template = compliance.parse_template('---\nname: 问题反馈\ntitle: "[问题] "\n---\n' + body, "issue.md")
        comment = "\n".join(compliance._format_template(template))
        fences = [t.content for t in MarkdownIt("commonmark").parse(comment) if t.type == "fence"]
        self.assertEqual(fences, ["[问题] \n", body + "\n"])

    def test_comment_fields_follow_each_outcome(self):
        """各判定仅展示约定字段，推荐失败不得误报通过或展示维护者建议。

        Args:
            无。
        Returns:
            None；核对结果非空、栏目顺序及维护者提及范围。
        """
        incomplete = reviewed_changes("[Bug] 失败", "### 问题描述\n失败", TEMPLATE)
        cases = [
            (incomplete, ["原因：部分匹配现有模板", "模板完善建议：缺陷反馈"]),
            ({"status": "no_match", "recommendation": SUGGESTION},
             ["原因：未匹配现有模板", "模板推荐：缺陷反馈"]),
            ({"status": "no_match", "recommendation": {"status": "insufficient_info", "reason": "请说明诉求"}},
             ["原因：未匹配现有模板，信息不足", "模板推荐：请补充信息后选择"]),
        ]
        for result, headings in cases:
            with self.subTest(result=result):
                comment = compliance.format_compliance_check(result, templates=[TEMPLATE])
                tokens = MarkdownIt("commonmark").parse(comment)
                actual = [tokens[i + 1].content for i, token in enumerate(tokens)
                          if token.type == "heading_open" and token.tag == "h4"]
                self.assertEqual(actual, headings)
                self.assertTrue(comment.startswith("### 提交内容规范性检查：未通过"))
                self.assertNotIn("@alice", comment)

    def test_compliance_formatter_handles_pass_and_rejects_template_errors(self):
        """验证统一格式化函数生成通过通知，并拒绝模板配置故障。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        for result in ({"status": "pass"},
                       {"status": "no_match", "recommendation": {"status": "not_applicable"}}):
            with self.subTest(result=result):
                self.assertIn("提交内容规范性检查：通过", compliance.format_compliance_check(result))
        with self.assertRaises(IssueProcessingError):
            compliance.format_compliance_check({"status": "template_error"})

    def test_external_template_placeholders_do_not_expand_inserted_content(self):
        """验证插入内容中的占位符不会再次展开，模板名称仍正确转义。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        template = {**TEMPLATE, "name": "Documentation|文档反馈",
                    "title": "[Documentation|文档反馈]: {number} $name"}
        recommendation = {**SUGGESTION, "template": template["name"], "reason": "请填写文档链接。"}
        comment = compliance.format_compliance_check({"status": "no_match", "recommendation": recommendation}, templates=[template])
        self.assertIn(r"#### 模板推荐：Documentation\|文档反馈", comment)
        fences = [t.content for t in MarkdownIt("commonmark").parse(comment) if t.type == "fence"]
        self.assertEqual(fences[0], template["title"] + "\n")
        self.assertIn("#### 原因：未匹配现有模板\n请填写文档链接。", comment)

    def test_service_failure_is_not_reported_as_pass_or_no_applicable_template(self):
        """验证推荐未完成或状态无效时拒绝生成评论，交由任务队列重试。

        Args:
            无。
        Returns:
            None；确保调用失败不被解释为通过或模板不适用。
        """
        for result in ({}, {"status": "unavailable"}, {"status": "unknown"}):
            with self.subTest(result=result), self.assertRaises(IssueProcessingError) as raised:
                self.comment(result)
            self.assertTrue(raised.exception.retryable)
        with self.assertRaises(IssueProcessingError) as raised:
            compliance.format_compliance_check({"status": "unknown"})
        self.assertTrue(raised.exception.retryable)

    def test_malformed_structured_results_cannot_pass(self):
        """验证错误推荐状态、模板及维护者建议被拒绝。

        Args:
            无。
        Returns:
            None；无效推荐均抛出可重试错误。
        """
        invalid = [
            {**SUGGESTION, "status": "unknown"},
            {**SUGGESTION, "status": "ambiguous"},
            {**SUGGESTION, "template": ["缺陷反馈", "功能需求"]},
            {**SUGGESTION, "template": None},
            {**SUGGESTION, "template": "不存在的模板"},
            {**SUGGESTION, "reason": ""},
            {**SUGGESTION, "maintainer": {"reason": "建议", "fields": "字段"}},
        ]
        no_template = {"status": "not_applicable", "template": None, "reason": "不适用", "maintainer": None}
        invalid.extend({**no_template, "maintainer": value}
                       for value in (None, "", " ", 1, ["建议"], {"reason": "建议", "fields": ["账号"]}, "字" * 501))
        invalid.append({**no_template, "status": "insufficient_info", "template": "缺陷反馈"})
        for suggestion in invalid:
            with self.subTest(suggestion=suggestion):
                with self.assertRaises(IssueProcessingError) as raised:
                    self.recommend(suggestion)
                self.assertTrue(raised.exception.retryable)

    def test_maintainer_yaml_fixed_path_and_validation(self):
        """验证维护者配置的读取位置、去重和错误处理。

        Args:
            无。
        Returns:
            None；核对有效、空白、格式错误及缺失配置的行为。
        """
        env = {"WEBHOOK_SECRET": "secret", "ATOMGIT_TOKEN": "token", "REPOSITORIES": "ignored/legacy",
               "AGENT_API_KEY": "model-key", "LLM_BASE_URL": "https://models.example", "LLM_MODEL": "model"}
        with tempfile.TemporaryDirectory() as directory, patch.object(receiver, "__file__", str(Path(directory) / "receiver.py")):
            path = Path(directory) / "runtime" / "repository.yml"
            path.parent.mkdir()
            path.write_text("example/repo:\n  - alice\n  - bob\n  - alice\nexample/other:\n  - carol\nexample/empty: []\n")
            expected = {"example/repo": ("alice", "bob"), "example/other": ("carol",), "example/empty": ()}
            config = receiver.Config.from_env(env)
            self.assertEqual(config.maintainers, expected)
            self.assertEqual(config.repositories, tuple(expected))
            for empty in ("", "# 没有维护者\n", "{}"):
                path.write_text(empty)
                with self.assertRaisesRegex(ValueError, "至少填写一个"):
                    receiver.Config.from_env(env)
            for bad in ("qiurq1", "[]", "example/repo: alice", "123: [alice]",
                        "example/repo: [1]", "example/repo: ["):
                path.write_text(bad)
                with self.subTest(value=bad), self.assertRaisesRegex(ValueError, "仓库及维护者配置格式无效"):
                    receiver.Config.from_env(env)
            path.unlink()
            with self.assertRaisesRegex(ValueError, "请先运行 manage.py init"):
                receiver.Config.from_env(env)
