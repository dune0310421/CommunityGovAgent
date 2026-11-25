"""服务器完整治理流程：模拟 AtomGit API，不发真实评论。"""
import base64
from dataclasses import replace
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atomgit
from errors import IssueProcessingError
import worker
import compliance
import receiver

TEMPLATE = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
    {"type": "textarea", "attributes": {"label": "问题描述"}, "validations": {"required": True}}]}
ISSUE = {"title": "[Bug] 崩溃", "body": "### 问题描述\n启动后退出", "state": "open"}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        """为每个流程测试创建临时服务配置和模型配置，并禁止真实网络访问。

        Args:
            无。
        Returns:
            None；测试环境保存到实例属性并注册自动清理。
        """
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = receiver.Config("secret", "platform-key", ("example/repo",), str(Path(temp.name)/"events.db"),
                                      agent_api_key="model-key", llm_model="custom-model", llm_base_url="https://models.example", maintainers={"example/repo": ("qiurq1",)})
        receiver.initialize(self.config)
        guard = patch.object(atomgit, "build_opener", side_effect=AssertionError("No live requests"))
        guard.start()
        self.addCleanup(guard.stop)

    def responses(self, issue=ISSUE, latest=None):
        """按完整处理流程的请求顺序构造模拟 AtomGit 响应。

        Args:
            issue: 首次读取的 Issue 字典。
            latest: 发布前复查的 Issue；为 None 时使用 issue。
        Returns:
            Issue、默认分支、模板、复查结果及评论操作的模拟响应列表。
        """
        return [issue, {"default_branch": "main"},
                [{"type": "file", "name": "bug.yml"}],
                {"encoding": "base64", "content": base64.b64encode(yaml.safe_dump(TEMPLATE).encode()).decode()},
                issue if latest is None else latest, {}]

    def test_webhook_to_passing_check_without_comment(self):
        """验证通过检查后完成任务，不发表评论且不调用 Actions。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        app = receiver.create_app(self.config)
        payload = {"object_kind": "issue", "uuid": "event", "project": {"path_with_namespace": "example/repo"},
                   "object_attributes": {"iid": 12, "action": "open"}}
        with patch.object(atomgit.AtomGit, "request", side_effect=self.responses()) as api:
            with app.test_client() as client:
                self.assertEqual(client.post("/webhook", json=payload, headers={"X-GitCode-Token": "secret"}).status_code, 202)
            api.assert_not_called()
            worker.process_one(self.config, client=atomgit.AtomGit(self.config.token, request_interval=0))
        self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))
        with receiver.connect(self.config) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM issue_checks").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM maintainer_suggestions").fetchone()[0], 0)
        self.assertFalse(any("/actions/" in c.args[1] for c in api.call_args_list))
        row = receiver.queue_snapshot(self.config)["recent"][0]
        self.assertEqual(row["state"], "completed")
        self.assertEqual(json.loads(row["result"])["comment_action"], "suppressed")

    def test_process_issue_uses_one_analysis_entry_and_preserves_summary(self):
        """Worker 只调用一次分析入口，并原样发布评论、转交结果摘要。

        Args:
            无。
        Returns:
            None；验证无需了解具体检查字段即可处理新的分析结果。
        """
        output = {"summary": {"status": "no_match", "additional_check": "completed"},
                  "comment": "统一分析入口生成的完整评论", "publish_comment": True,
                  "maintainer_suggestion": None}
        with patch.object(atomgit.AtomGit, "request", side_effect=self.responses()) as api, patch.object(
                worker, "analyze_issue", return_value=output) as analyze:
            result = worker._process_issue(self.config, "example/repo", 12,
                                          client=atomgit.AtomGit(self.config.token, request_interval=0))
        analyze.assert_called_once_with(
            ISSUE["title"], ISSUE["body"], [{"filename": "bug.yml",
                "source": "example/repo/.gitcode/ISSUE_TEMPLATE/bug.yml", "content": yaml.safe_dump(TEMPLATE)}],
            self.config.agent_api_key,
            self.config.llm_model, self.config.llm_base_url,
            repository="example/repo", number=12, maintainers=("qiurq1",), previously_commented=False)
        self.assertEqual(api.call_args.args[2]["body"].split("\n\n<!-- issue-gov:")[0], output["comment"])
        self.assertEqual(result, {**output["summary"], "comment_action": "created"})
        self.assertNotIn("comment_action", output["summary"])

    def test_worker_obeys_analysis_comment_decision_without_interpreting_status(self):
        """新增检查状态时，Worker 仍只消费分析入口给出的发布决定。"""
        output = {"summary": {"status": "additional_check_complete"}, "comment": "分析结果",
                  "publish_comment": False, "maintainer_suggestion": None}
        with patch.object(atomgit.AtomGit, "request", side_effect=self.responses()) as api, patch.object(
                worker, "analyze_issue", return_value=output):
            result = worker._process_issue(self.config, "example/repo", 12,
                                          client=atomgit.AtomGit(self.config.token, request_interval=0))
        self.assertEqual(result, {**output["summary"], "comment_action": "suppressed"})
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))
        with receiver.connect(self.config) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM issue_checks").fetchone()[0], 1)

    def test_markdown_and_checkbox_templates_complete_governance(self):
        """验证 Markdown 按章节检查，无必填章节时推荐，复选框通过时不发表评论。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        cases = (
            ("question.md", "### 问题描述\n请填写", ISSUE, "pass", None, "通过模板规范检查"),
            ("question.md", "### 问题描述\n请填写", {**ISSUE, "body": "### 问题描述\n请填写"},
             "no_match", "needs_changes", "此必填章节为空或仅含占位内容"),
            ("question.md", "请填写问题描述", ISSUE, "no_match", "recommended", "未匹配现有模板"),
            ("check.yml", yaml.safe_dump({"name": "确认", "title": "[Bug] ", "body": [
                {"type": "checkboxes", "attributes": {"label": "确认项", "options": [
                    {"label": "已阅读说明", "required": True}]}}]}),
             {**ISSUE, "body": "### 确认项\n- [x] 已阅读说明"}, "pass", None, "通过模板规范检查"),
        )
        for filename, content, issue, status, review_status, message in cases:
            with self.subTest(filename=filename):
                with receiver.connect(self.config) as db:
                    db.execute("DELETE FROM issue_checks")
                responses = self.responses(issue=issue)
                responses[2] = [{"type": "file", "name": filename}]
                responses[3] = {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}
                with patch.object(atomgit.AtomGit, "request", side_effect=responses) as api, patch.object(
                        compliance, "recommend_template", return_value={
                            "status": review_status,
                            "template": "question", "reason": "适合描述当前问题",
                            "problems": [{"field": "问题描述", "reason": "body",
                                          "message": "此必填章节为空或仅含占位内容"}]}) as model:
                    result = worker._process_issue(self.config, "example/repo", 12, client=atomgit.AtomGit(self.config.token, request_interval=0))
                self.assertEqual(result["status"], status)
                self.assertEqual(result["comment_action"], "suppressed" if status == "pass" else "created")
                if status == "pass":
                    self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))
                else:
                    comment = api.call_args.args[2]["body"]
                    self.assertIn(message, comment)
                if status != "pass":
                    model.assert_called_once()
                    self.assertEqual(result["recommendation_status"], review_status)
                    self.assertIn("提交内容规范性检查：未通过", comment)
                else:
                    model.assert_not_called()

    def test_template_faults_save_advice_silently_and_complete_task(self):
        """模板缺失、解析失败和字段错误均通过完整任务流程静默保存建议。

        Args:
            无。
        Returns:
            None；验证不发布评论，摘要区别于通过或不通过，不调用模型。
        """
        app = receiver.create_app(self.config)
        payload = {"object_kind": "issue", "uuid": "invalid-template", "project": {"path_with_namespace": "example/repo"},
                   "object_attributes": {"iid": 12, "action": "open"}}
        invalid = yaml.safe_dump({**TEMPLATE, "body": [{"type": "textarea", "attributes": {"label": 123}}]})
        for content in (None, "name: [", invalid):
            with self.subTest(content=content):
                with receiver.connect(self.config) as db:
                    db.execute("DELETE FROM events")
                    db.execute("DELETE FROM issue_checks")
                with app.test_client() as http:
                    self.assertEqual(http.post("/webhook", json=payload, headers={"X-GitCode-Token": "secret"}).status_code, 202)
                if content is None:
                    responses = [ISSUE, {"default_branch": "main", "namespace": {"type": "personal"}},
                                 *[[] for _ in range(6)], ISSUE, {}]
                else:
                    responses = self.responses()
                    responses[3] = {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}
                with patch.object(atomgit.AtomGit, "request", side_effect=responses) as api, patch.object(
                        compliance, "recommend_template") as model:
                    client = atomgit.AtomGit(self.config.token, request_interval=0)
                    self.assertTrue(worker.process_one(self.config, client=client))
                    self.assertFalse(worker.process_one(self.config, client=client))
                row = receiver.queue_snapshot(self.config)["recent"][0]
                self.assertEqual(row["state"], "completed")
                self.assertEqual(row["attempts"], 1)
                self.assertIsNone(row["last_error"])
                result = json.loads(row["result"])
                self.assertEqual(result["status"], "template_error")
                self.assertIsNone(result["recommendation_status"])
                self.assertEqual(result["comment_action"], "suppressed")
                self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))
                with receiver.connect(self.config) as db:
                    saved = db.execute("SELECT * FROM maintainer_suggestions WHERE id = ?",
                                       (result["maintainer_suggestion_id"],)).fetchone()
                self.assertEqual(saved["repository"], "example/repo")
                self.assertEqual(saved["issue_number"], 12)
                self.assertEqual(json.loads(saved["mtr"]), ["qiurq1"])
                self.assertIn("请修复", saved["suggestion"])
                self.assertTrue(saved["reason"])
                model.assert_not_called()

    def test_template_advice_database_failure_retries_before_completion(self):
        """验证建议写库失败时重试，成功后只保留一条建议，全程不发布评论。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        app = receiver.create_app(self.config)
        payload = {"object_kind": "issue", "uuid": "notice-retry", "project": {"path_with_namespace": "example/repo"},
                   "object_attributes": {"iid": 12, "action": "open"}}
        with app.test_client() as http:
            self.assertEqual(http.post("/webhook", json=payload, headers={"X-GitCode-Token": "secret"}).status_code, 202)
        client = atomgit.AtomGit(self.config.token, request_interval=0)
        save = worker._save_maintainer_suggestion
        def fail_after_insert(*args):
            """插入建议后模拟写库错误，用于验证事务回滚。

                Args:
                    *args: 原样传递给建议保存函数的位置参数。
                Returns:
                    无正常返回，插入后立即抛出异常。
                Raises:
                    sqlite3.OperationalError: 模拟写库失败。
                """
            save(*args)
            raise sqlite3.OperationalError("模拟写库失败")
        with patch.object(client, "request", return_value=ISSUE) as api, patch.object(client, "templates", return_value=[]):
            with patch.object(worker, "_save_maintainer_suggestion", side_effect=fail_after_insert):
                self.assertTrue(worker.process_one(self.config, now=100, client=client))
            self.assertEqual(receiver.queue_snapshot(self.config)["recent"][0]["state"], "queued")
            with receiver.connect(self.config) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM maintainer_suggestions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM issue_checks").fetchone()[0], 0)
            self.assertFalse(worker.process_one(self.config, now=129, client=client))
            self.assertTrue(worker.process_one(self.config, now=130, client=client))
            self.assertEqual(receiver.queue_snapshot(self.config)["recent"][0]["state"], "completed")
            self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))
        with receiver.connect(self.config) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM maintainer_suggestions").fetchone()[0], 1)

    def test_no_match_calls_central_model_and_posts_recommendation(self):
        """验证模板无法匹配时使用统一模型配置推荐，并发布含建议的评论。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        issue = {**ISSUE, "title": "启动失败", "body": "崩溃了"}
        recommendation = {"status": "recommended", "template": "缺陷反馈", "reason": "运行异常"}
        with patch.object(atomgit.AtomGit, "request", side_effect=self.responses(issue)) as api, patch.object(
            compliance, "recommend_template", return_value=recommendation) as model:
            result = worker._process_issue(self.config, "example/repo", 12, client=atomgit.AtomGit(self.config.token, request_interval=0))
        self.assertEqual(model.call_args.args[-3:], ("model-key", "custom-model", "https://models.example"))
        self.assertEqual(result["recommendation_status"], "recommended")
        comment = api.call_args.args[2]["body"]
        self.assertIn("#### 模板推荐：", comment)
        self.assertIn("**模板正文（Markdown）**", comment)
        self.assertNotIn("崩溃了", comment)
        self.assertNotIn("启动失败", comment)

    def test_shared_heading_with_wrong_prefix_runs_recommendation(self):
        """标题不匹配时，共用章节不能阻止推荐或强制套用另一模板。"""
        issue = {**ISSUE, "title": "[Bug-Report|缺陷反馈]: 欢迎消息链接错误"}
        documentation = {**TEMPLATE, "name": "文档反馈", "title": "[文档]: "}
        responses = self.responses(issue)
        responses[3] = {"encoding": "base64", "content": base64.b64encode(
            yaml.safe_dump(documentation).encode()).decode()}
        with patch.object(atomgit.AtomGit, "request", side_effect=responses) as api, patch.object(
            compliance, "recommend_template", return_value={"status": "not_applicable", "reason": "没有适合的模板",
                "maintainer": "现有模板仅覆盖文档，建议新增工具缺陷反馈模板，收集问题描述和复现步骤。"},
        ) as model:
            result = worker._process_issue(self.config, "example/repo", 12,
                                          client=atomgit.AtomGit(self.config.token, request_interval=0))
        model.assert_called_once()
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["recommendation_status"], "not_applicable")
        self.assertEqual(result["comment_action"], "suppressed")
        self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))
        with receiver.connect(self.config) as db:
            row = db.execute("SELECT * FROM maintainer_suggestions").fetchone()
        self.assertIn("建议新增工具缺陷反馈模板", row["suggestion"])
        self.assertEqual(json.loads(row["mtr"]), ["qiurq1"])
        self.assertEqual(result["maintainer_suggestion_id"], row["id"])

    def test_suggestion_records_only_the_current_repository_maintainers(self):
        """验证入库建议仅关联当前仓库配置的维护者。

        Args:
            无。
        Returns:
            None；核对多仓库配置及未配置维护者时的评论。
        """
        repositories = ("example/one", "example/two", "example/unconfigured")
        config = replace(self.config, repositories=repositories,
                         maintainers={"example/one": ("alice", "bob"), "example/two": ("carol",)})
        issue = {**ISSUE, "title": "成员权限申请", "body": "申请权限"}
        recommendation = {"status": "not_applicable", "reason": "无适用模板",
                          "maintainer": "建议新增成员申请模板，收集申请账号。"}
        client = atomgit.AtomGit(config.token, request_interval=0)
        with patch.object(client, "request", side_effect=self.responses(issue)[:-1] * 3) as api, patch.object(
                compliance, "recommend_template", return_value=recommendation):
            for repository in repositories:
                worker._process_issue(config, repository, 12, client=client)
        self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))
        with receiver.connect(config) as db:
            records = {row["repository"]: dict(row) for row in db.execute("SELECT * FROM maintainer_suggestions")}
        for repository, expected in zip(repositories, (("alice", "bob"), ("carol",), ())):
            self.assertEqual(json.loads(records[repository]["mtr"]), list(expected))
            self.assertIn("建议新增成员申请模板", records[repository]["suggestion"])

    def test_needs_changes_calls_model_before_publishing(self):
        """未完全匹配统一复核，评论只包含复核确认的问题。"""
        issue = {**ISSUE, "body": ""}
        review = {"status": "needs_changes", "template": "缺陷反馈", "reason": "标题已声明缺陷",
                  "problems": [{"field": "问题描述", "reason": "body", "message": "请补充问题描述"}]}
        with patch.object(atomgit.AtomGit, "request", side_effect=self.responses(issue)) as api, patch.object(
                compliance, "recommend_template", return_value=review) as model:
            result = worker._process_issue(self.config, "example/repo", 12,
                                          client=atomgit.AtomGit(self.config.token, request_interval=0))
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["recommendation_status"], "needs_changes")
        self.assertIn("请补充问题描述", api.call_args.args[2]["body"])
        model.assert_called_once()

    def test_missing_model_key_retries_without_comment(self):
        """验证模型密钥缺失时抛出可重试错误，不发布评论。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        issue = {**ISSUE, "title": "启动失败", "body": "崩溃了"}
        with patch.object(atomgit.AtomGit, "request", side_effect=self.responses(issue)) as api:
            with self.assertRaises(IssueProcessingError) as raised:
                worker._process_issue(replace(self.config, agent_api_key=""), "example/repo", 12, client=atomgit.AtomGit(self.config.token, request_interval=0))
        self.assertTrue(raised.exception.retryable)
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_edit_during_analysis_retries_without_stale_comment(self):
        """验证分析期间 Issue 内容被修改时抛出可重试错误，不发布过时评论。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        responses = self.responses(latest={**ISSUE, "body": "补充了信息"})
        with patch.object(atomgit.AtomGit, "request", side_effect=responses) as api, self.assertRaises(IssueProcessingError) as error:
            worker._process_issue(self.config, "example/repo", 12, client=atomgit.AtomGit(self.config.token, request_interval=0))
        self.assertTrue(error.exception.retryable)
        self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))

    def test_closed_issue_and_pull_request_never_receive_comment(self):
        """验证已关闭或检查期间关闭的 Issue 跳过评论，Pull Request 被拒绝。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for issue, fail in (({**ISSUE, "state": "closed"}, False), ({**ISSUE, "pull_request": {}}, True)):
            with patch.object(atomgit.AtomGit, "request", side_effect=[issue]) as api:
                if fail:
                    with self.assertRaises(IssueProcessingError):
                        worker._process_issue(self.config, "example/repo", 12, client=atomgit.AtomGit(self.config.token, request_interval=0))
                else:
                    self.assertEqual(worker._process_issue(self.config, "example/repo", 12, client=atomgit.AtomGit(self.config.token, request_interval=0))["comment_action"], "skipped_closed")
            self.assertEqual(api.call_count, 1)
        with patch.object(atomgit.AtomGit, "request", side_effect=self.responses(latest={**ISSUE, "state": "closed"})) as api:
            self.assertEqual(worker._process_issue(self.config, "example/repo", 12, client=atomgit.AtomGit(self.config.token, request_interval=0))["comment_action"], "skipped_closed")
        self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))

    def test_removed_repository_is_rejected_before_network(self):
        """验证未接入仓库在任何网络请求前被拒绝。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        with patch.object(atomgit.AtomGit, "request") as api, self.assertRaises(IssueProcessingError):
            worker._process_issue(self.config, "outside/repo", 12, client=atomgit.AtomGit(self.config.token, request_interval=0))
        api.assert_not_called()

    def test_issue_edits_create_new_comments_but_duplicate_events_do_not(self):
        """正文修改后即使检查结果相同也新增评论，重复投递同一事件仍去重。"""
        app = receiver.create_app(self.config)
        client = atomgit.AtomGit(self.config.token, request_interval=0)
        issue = {**ISSUE, "body": "缺少章节"}
        edited = {**issue, "body": issue["body"] + "\n\n."}
        review = {"status": "needs_changes", "template": "缺陷反馈", "reason": "缺陷模板适用",
                  "problems": [{"field": "问题描述", "reason": "body", "message": "请补充问题描述"}]}
        with patch.object(client, "request", side_effect=[*self.responses(issue), edited, edited, {}]) as api, patch.object(
                compliance, "recommend_template", return_value=review):
            with app.test_client() as receiver_client:
                for index in range(2):
                    payload = {"object_kind": "issue", "uuid": f"edit-{index}",
                               "project": {"path_with_namespace": "example/repo"},
                               "object_attributes": {"iid": 12, "action": "update"},
                               "changes": {"body": {"previous": "旧内容", "current": "新内容"}}}
                    for expected_status in (202, 200):
                        response = receiver_client.post("/webhook", json=payload,
                                                        headers={"X-GitCode-Token": "secret"})
                        self.assertEqual(response.status_code, expected_status)
                    self.assertTrue(worker.process_one(self.config, client=client))
                    self.assertFalse(worker.process_one(self.config, client=client))
        posts = [c for c in api.call_args_list if c.args[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].args[2]["body"].split("\n\n<!-- issue-gov:")[0],
                         posts[1].args[2]["body"].split("\n\n<!-- issue-gov:")[0])
        self.assertNotEqual(posts[0].args[2], posts[1].args[2])
        self.assertFalse(any(c.args[0] == "PATCH" or c.args[1] == "/user" or
                             (c.args[0] == "GET" and "/comments" in c.args[1])
                             for c in api.call_args_list))

    def test_model_failures_requeue_and_only_publish_after_success(self):
        """模型异常通过真实分析流程重新入队，恢复成功后仅发布一次评论。

        Args:
            无。
        Returns:
            None；核对超时、启动失败、损坏响应和配置缺失的延迟重试及成功状态。
        """
        app = receiver.create_app(self.config)
        payload = {"object_kind": "issue", "uuid": "retry-event", "project": {"path_with_namespace": "example/repo"},
                   "object_attributes": {"iid": 12, "action": "open"}}
        issue = {**ISSUE, "title": "未知", "body": "正文"}
        files = [{"filename": "bug.yml", "source": "example/repo/bug.yml", "content": yaml.safe_dump(TEMPLATE)}]
        suggestion = {"status": "recommended", "template": "缺陷反馈", "reason": "运行失败", "maintainer": None}
        output = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "result": json.dumps(suggestion)})
        for failure in ("timeout", "startup", "invalid_json", "missing_key", "missing_model", "missing_url"):
            with self.subTest(failure=failure):
                with receiver.connect(self.config) as db:
                    db.execute("DELETE FROM events")
                    db.execute("DELETE FROM issue_checks")
                with app.test_client() as http:
                    self.assertEqual(http.post("/webhook", json=payload, headers={"X-GitCode-Token": "secret"}).status_code, 202)
                config = self.config
                if failure == "missing_key":
                    config = replace(config, agent_api_key="")
                elif failure == "missing_model":
                    config = replace(config, llm_model="")
                elif failure == "missing_url":
                    config = replace(config, llm_base_url="")
                client = atomgit.AtomGit(config.token, request_interval=0)
                with patch.object(client, "request", return_value=issue), patch.object(client, "templates", return_value=files), patch.object(
                        client, "create_comment", return_value="created") as publish, patch.object(
                        compliance.subprocess, "Popen") as popen, patch.object(compliance.os, "killpg"):
                    process = popen.return_value.__enter__.return_value
                    process.returncode = 0
                    process.communicate.return_value = ("not JSON", "model-key platform-key")
                    if failure == "timeout":
                        process.communicate.side_effect = compliance.subprocess.TimeoutExpired("claude", compliance.LLM_TIMEOUT)
                    elif failure == "startup":
                        popen.side_effect = OSError("model-key platform-key")
                    with self.assertLogs(worker.LOG, level="WARNING") as logs:
                        self.assertTrue(worker.process_one(config, now=100, client=client))
                    row = receiver.queue_snapshot(config)["recent"][0]
                    self.assertEqual(row["state"], "queued")
                    self.assertEqual(row["attempts"], 1)
                    self.assertIsNone(row["result"])
                    self.assertTrue(row["last_error"])
                    self.assertNotIn("model-key", row["last_error"] + str(logs.output))
                    self.assertNotIn("platform-key", row["last_error"] + str(logs.output))
                    publish.assert_not_called()
                    self.assertFalse(worker.process_one(config, now=129, client=client))
                    popen.side_effect = None
                    process.communicate.side_effect = None
                    process.communicate.return_value = (output, "")
                    self.assertTrue(worker.process_one(self.config, now=130, client=client))
                    row = receiver.queue_snapshot(self.config)["recent"][0]
                    self.assertEqual(row["state"], "completed")
                    self.assertEqual(row["attempts"], 2)
                    self.assertIsNone(row["last_error"])
                    self.assertEqual(json.loads(row["result"])["recommendation_status"], "recommended")
                    publish.assert_called_once()
                    self.assertIn("#### 模板推荐：缺陷反馈", publish.call_args.args[2])
                    self.assertNotIn("模板推荐未完成", publish.call_args.args[2])

    def test_shared_client_reuses_templates_across_queued_events(self):
        """验证连续任务共用客户端，复用模板缓存。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        app = receiver.create_app(self.config)
        with app.test_client() as receiver_client:
            for number in (34, 35):
                response = receiver_client.post("/webhook", headers={"X-GitCode-Token": "secret"}, json={
                    "object_kind": "issue", "uuid": str(number),
                    "project": {"path_with_namespace": "example/repo"},
                    "object_attributes": {"iid": number, "action": "open"}})
                self.assertEqual(response.status_code, 202)
        client = atomgit.AtomGit(self.config.token)
        with patch.object(client, "request", side_effect=[*self.responses()[:-1], ISSUE, ISSUE]) as api:
            self.assertTrue(worker.process_one(self.config, client=client))
            self.assertTrue(worker.process_one(self.config, client=client))
        self.assertEqual(api.call_count, 7)
        self.assertEqual(sum(call.args[1] == "/user" for call in api.call_args_list), 0)
        rows = receiver.queue_snapshot(self.config)["recent"]
        self.assertEqual({row["issue_number"] for row in rows}, {34, 35})
        self.assertTrue(all(row["state"] == "completed" for row in rows))

    def test_remote_dropdown_template_completes_without_model(self):
        """使用固定样例模拟远端模板，验证检查通过时不发表评论或调用模型。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        path = Path(__file__).resolve().parent / "fixtures/repository_templates/documentation_template.yml"
        template = yaml.safe_load(path.read_text())
        sections = []
        for field in template["body"]:
            if field.get("validations", {}).get("required"):
                attrs = field["attributes"]
                value = attrs["options"][0] if field["type"] == "dropdown" else "实际填写内容"
                sections.append(f"### {attrs['label']}\n{value}")
        issue = {"title": template["title"] + "测试", "body": "\n".join(sections), "state": "open"}
        responses = self.responses(issue)
        responses[3] = {"encoding": "base64", "content": base64.b64encode(path.read_bytes()).decode()}
        with patch.object(atomgit.AtomGit, "request", side_effect=responses) as api, \
                patch.object(compliance, "recommend_template") as model:
            result = worker._process_issue(self.config, "example/repo", 35, client=atomgit.AtomGit(self.config.token, request_interval=0))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["comment_action"], "suppressed")
        self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))
        model.assert_not_called()
