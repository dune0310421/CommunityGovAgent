"""AtomGit 适配回归测试；禁止真实网络调用和发布评论。"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("atomgit_checker", ROOT / "issue_checker.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
TOKEN = "atomgit-test-token"
REPOSITORY = "example/project"
ISSUE_PATH = "/repos/example/project/issues/12"
COMMENTS_PATH = ISSUE_PATH + "/comments"
BODY = checker.MARKER + "\n新结果"
TEMPLATE = {
    "name": "缺陷反馈", "title": "[Bug] ",
    "body": [{"type": "textarea", "attributes": {"label": "问题描述"},
              "validations": {"required": True}}],
}
ISSUE = {"title": "[Bug] 启动失败", "body": "### 问题描述\n启动时退出。"}


def comment(body=checker.MARKER + "\n旧结果", login="action-bot", identifier=99):
    return {"id": identifier, "body": body, "user": {"id": "69ec338eb06841500aced516", "login": login}}


class AtomGitTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(checker.request, "urlopen",
                             side_effect=AssertionError("测试禁止真实网络请求"))
        guard.start()
        self.addCleanup(guard.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.templates = Path(self.directory.name)
        (self.templates / "bug.yml").write_text(yaml.safe_dump(TEMPLATE), encoding="utf-8")

    def run_main(self, responses, **overrides):
        env = {"ATOMGIT_TOKEN": TOKEN, "REPOSITORY": REPOSITORY,
               "ISSUE_NUMBER": "12", "TEMPLATE_DIR": str(self.templates)}
        env.update(overrides)
        with patch.dict(os.environ, env, clear=True), patch.object(
            checker, "atomgit_request", side_effect=responses,
        ) as api, contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()):
            checker.main()
        return json.loads(stdout.getvalue()), api

    def update(self, responses, body=BODY):
        with patch.object(checker, "atomgit_request", side_effect=responses) as api:
            action = checker.update_comment(REPOSITORY, 12, body, TOKEN)
        return action, api

    def test_request_uses_v5_bearer_and_json(self):
        for method, data in [("GET", None), ("POST", {"body": "中文"}),
                             ("PATCH", {"body": "更新"})]:
            with self.subTest(method=method), patch.object(
                checker.request, "urlopen", return_value=io.BytesIO(b'{}'),
            ) as http:
                self.assertEqual(checker.atomgit_request(method, ISSUE_PATH, TOKEN, data), {})
            req = http.call_args.args[0]
            self.assertEqual(req.full_url, "https://api.atomgit.com/api/v5" + ISSUE_PATH)
            self.assertEqual(req.get_method(), method)
            self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
            self.assertEqual(req.get_header("Accept"), "application/json")
            self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertNotIn(TOKEN, req.full_url)
            self.assertIsNone(req.get_header("X-github-api-version"))
            self.assertEqual(json.loads(req.data) if req.data else None, data)
            self.assertEqual(http.call_args.kwargs["timeout"], 30)

    def test_empty_update_response_is_accepted(self):
        with patch.object(checker.request, "urlopen", return_value=io.BytesIO(b"")):
            self.assertEqual(checker.atomgit_request("PATCH", "/test", TOKEN, {}), {})

    def test_http_failure_is_not_treated_as_success(self):
        for status in (401, 403, 404, 429, 500):
            error = HTTPError("https://api.atomgit.com", status, "error", {}, io.BytesIO())
            self.addCleanup(error.close)
            with self.subTest(status=status), patch.object(
                checker.request, "urlopen",
                side_effect=error,
            ), self.assertRaises(HTTPError):
                checker.atomgit_request("GET", ISSUE_PATH, TOKEN)

    def test_creates_comment_without_user_lookup(self):
        action, api = self.update([[], {"id": 99}])
        self.assertEqual(action, "created")
        self.assertEqual(api.call_args_list, [
            call("GET", COMMENTS_PATH + "?per_page=100&page=1&direction=asc", TOKEN),
            call("POST", COMMENTS_PATH, TOKEN, {"body": BODY}),
        ])

    def test_updates_action_bot_comment_without_bot_type(self):
        action, api = self.update([[comment()], {}])
        self.assertEqual(action, "updated")
        self.assertEqual(api.call_args_list[-1], call(
            "PATCH", "/repos/example/project/issues/comments/99", TOKEN, {"body": BODY},
        ))

    def test_unchanged_comment_does_not_write(self):
        action, api = self.update([[comment(body=BODY)]])
        self.assertEqual(action, "unchanged")
        self.assertEqual([c.args[0] for c in api.call_args_list], ["GET"])

    def test_foreign_or_unmarked_comments_are_preserved(self):
        unrelated = [comment(login="other"), comment(body="另一工作流"),
                     comment(body="引用 " + checker.MARKER), comment(body=None),
                     {"id": 21, "body": BODY}]
        action, api = self.update([unrelated, {}])
        self.assertEqual(action, "created")
        self.assertNotIn("PATCH", [c.args[0] for c in api.call_args_list])

    def test_target_on_second_page_is_updated(self):
        first = [comment(login="other", identifier=i) for i in range(100)]
        action, api = self.update([first, [comment()], {}])
        self.assertEqual(action, "updated")
        self.assertEqual(api.call_args_list[1], call(
            "GET", COMMENTS_PATH + "?per_page=100&page=2&direction=asc", TOKEN,
        ))

    def test_full_last_page_is_followed_by_empty_page_before_creation(self):
        first = [comment(login="other", identifier=i) for i in range(100)]
        action, api = self.update([first, [], {}])
        self.assertEqual(action, "created")
        self.assertEqual([c.args[0] for c in api.call_args_list], ["GET", "GET", "POST"])

    def test_read_or_update_failure_never_falls_back_to_creation(self):
        for responses in ([URLError("list failed")],
                          [[comment()], URLError("update failed")]):
            with self.subTest(responses=responses), patch.object(
                checker, "atomgit_request", side_effect=responses,
            ) as api, self.assertRaises(URLError):
                checker.update_comment(REPOSITORY, 12, BODY, TOKEN)
            self.assertNotIn("POST", [c.args[0] for c in api.call_args_list])

    def test_default_publishes_and_writes_atomgit_summary(self):
        summary = self.templates / "summary.md"
        output, api = self.run_main([ISSUE, ISSUE, [], {}], ATOMGIT_STEP_SUMMARY=str(summary))
        self.assertEqual(output["result"]["status"], "pass")
        self.assertEqual(output["comment_action"], "created")
        self.assertEqual([c.args[0] for c in api.call_args_list], ["GET", "GET", "GET", "POST"])
        self.assertIn(output["comment_body"], summary.read_text(encoding="utf-8"))

    def test_publishing_rechecks_latest_issue_and_creates_comment(self):
        output, api = self.run_main([ISSUE, ISSUE, [], {}])
        self.assertEqual(output["comment_action"], "created")
        self.assertEqual(api.call_args_list[:2], [call("GET", ISSUE_PATH, TOKEN)] * 2)
        self.assertEqual(api.call_args_list[-1].args[0], "POST")

    def test_changed_issue_skips_all_comment_operations(self):
        for key in ("title", "body"):
            with self.subTest(key=key):
                output, api = self.run_main([ISSUE, {**ISSUE, key: "已编辑"}])
                self.assertEqual(output["comment_action"], "skipped_changed")
                self.assertEqual(api.call_count, 2)

    def test_partial_match_does_not_call_model(self):
        with patch.object(checker, "recommend_template") as recommend:
            issue = {**ISSUE, "body": "### 问题描述\n"}
            output, _ = self.run_main([issue, issue, [], {}])
        recommend.assert_not_called()
        self.assertEqual(output["result"]["status"], "partial_match")
        self.assertIn("https://atomgit.com/example/project/issues", output["comment_body"])
        self.assertNotIn("github.com", output["comment_body"])

    def test_no_match_calls_recommendation_with_configured_model(self):
        suggestion = {"status": "recommended", "template": "缺陷反馈", "reason": "运行失败"}
        with patch.object(checker, "recommend_template", return_value=suggestion) as recommend:
            issue = {"title": "崩溃", "body": "运行失败"}
            output, _ = self.run_main([issue, issue, [], {}],
                                      AGENT_API_KEY="glm-key", LLM_MODEL="custom-model")
        self.assertEqual(recommend.call_args.args[-2:], ("glm-key", "custom-model"))
        self.assertEqual(output["result"]["recommendation"], suggestion)

    def test_missing_model_key_retains_format_feedback(self):
        issue = {"title": "崩溃", "body": None}
        output, _ = self.run_main([issue, issue, [], {}])
        self.assertEqual(output["result"]["status"], "no_match")
        self.assertEqual(output["result"]["recommendation"]["status"], "unavailable")

    def test_invalid_issue_number_fails_before_api_calls(self):
        for number in ("Ia12B", "abc", "../12", "#12", "0", "-1", "1.2", "12; echo secret"):
            env = {"ATOMGIT_TOKEN": TOKEN, "REPOSITORY": REPOSITORY,
                   "ISSUE_NUMBER": number, "TEMPLATE_DIR": str(self.templates)}
            with self.subTest(number=number), patch.dict(os.environ, env, clear=True), patch.object(
                checker, "atomgit_request",
            ) as api:
                with self.assertRaisesRegex(ValueError, "Issue 编号必须是正整数"):
                    checker.main()
                api.assert_not_called()

    def test_warning_uses_atomgit_actions_flag(self):
        issue = {"title": "崩溃", "body": None}
        for flag, expected_warning in (("true", True), ("false", False), ("", False)):
            env = {"ATOMGIT_TOKEN": TOKEN, "REPOSITORY": REPOSITORY,
                   "ISSUE_NUMBER": "12", "TEMPLATE_DIR": str(self.templates),
                   "ATOMGIT_ACTIONS": flag, "ATOMGIT_RUN_ID": "123"}
            with self.subTest(flag=flag), patch.dict(os.environ, env, clear=True), patch.object(
                checker, "atomgit_request", side_effect=[issue, issue, [], {}],
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
                checker.main()
            self.assertEqual(stderr.getvalue().startswith("::warning title=Issue Agent::"), expected_warning)

    def test_workflow_environment_runs_checker(self):
        workflow = yaml.load((ROOT.parents[1] / "workflows/issue-compliance.yml").read_text(), Loader=yaml.BaseLoader)
        bindings = workflow["jobs"]["issue_compliance"]["steps"][-1]["env"]
        # 模拟平台解析表达式，使用 YML 声明的变量名启动真实主流程。
        resolved = {"${{ secrets.ATOMGIT_TOKEN }}": TOKEN,
                    "${{ secrets.GLM_CODING_API_KEY }}": "",
                    "${{ vars.LLM_MODEL || 'glm-5.3' }}": "glm-5.3",
                    "${{ atomgit.repository }}": REPOSITORY,
                    "${{ atomgit.event.issue.number || inputs.issue_number }}": "12",
                    ".gitcode/ISSUE_TEMPLATE": str(self.templates)}
        env = {name: resolved[value] for name, value in bindings.items()}
        with patch.dict(os.environ, env, clear=True), patch.object(
            checker, "atomgit_request", side_effect=[ISSUE, ISSUE, [], {}],
        ) as api, contextlib.redirect_stdout(io.StringIO()) as stdout:
            checker.main()
        self.assertEqual(json.loads(stdout.getvalue())["comment_action"], "created")
        api.assert_called_with("POST", COMMENTS_PATH, TOKEN,
                               {"body": json.loads(stdout.getvalue())["comment_body"]})

    def test_empty_templates_fail_without_writing_comments(self):
        (self.templates / "bug.yml").unlink()
        with self.assertRaisesRegex(ValueError, "至少需要一个模板"):
            self.run_main([ISSUE])

    def test_model_failure_redacts_both_credentials(self):
        with patch.dict(os.environ, {"ATOMGIT_TOKEN": TOKEN}, clear=True), patch.object(
            checker.subprocess, "Popen", side_effect=OSError("glm-key " + TOKEN),
        ):
            result = checker.recommend_template("未知", "描述", [TEMPLATE], "glm-key")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn(TOKEN, result["reason"])
        self.assertNotIn("glm-key", result["reason"])
        self.assertTrue(checker.RECOMMEND_PROMPT.is_file())

    def test_workflow_paths_resolve_in_deployment_layout(self):
        # BaseLoader 避免 YAML 1.1 把 on 当成布尔值。
        workflow = yaml.load((ROOT.parents[1] / "workflows/issue-compliance.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"issues", "workflow_dispatch"})
        self.assertEqual(set(workflow["on"]["workflow_dispatch"]["inputs"]), {"issue_number"})
        steps = workflow["jobs"]["issue_compliance"]["steps"]
        self.assertEqual([s["uses"] for s in steps if "uses" in s],
                         ["checkout", "setup-python", "setup-node"])
        repository_root = ROOT.parents[2]
        self.assertTrue((repository_root / steps[-1]["run"].split()[-1]).is_file())
        install = next(s["run"] for s in steps if "pip install" in s.get("run", ""))
        self.assertTrue((repository_root / install.split()[-1]).is_file())


if __name__ == "__main__":
    unittest.main()
