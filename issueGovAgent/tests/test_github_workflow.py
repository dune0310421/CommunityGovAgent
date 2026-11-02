"""GitHub 请求、评论更新与 main 流程；全部请求在本地模拟。"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

from src.issue_checker import BOT_LOGIN, MARKER, github_request, main, update_comment


TOKEN = "test-token"
REPOSITORY = "example/project"
ISSUE_PATH = "/repos/example/project/issues/12"
COMMENTS_PATH = ISSUE_PATH + "/comments"
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / ".github/ISSUE_TEMPLATE"
ISSUE = {"title": "[Question|问题咨询]: 如何构建？",
         "body": "### Describe the question / 问题描述 (Mandatory / 必填)\n如何构建？"}


def comment(body=MARKER + "\n旧结果", login=BOT_LOGIN, kind="Bot", identifier=99):
    return {"id": identifier, "body": body, "user": {"login": login, "type": kind}}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        # 未显式模拟的网络调用直接失败，避免测试意外访问真实平台。
        guard = patch("src.issue_checker.request.urlopen",
                      side_effect=AssertionError("测试不允许真实网络请求"))
        guard.start()
        self.addCleanup(guard.stop)


class GitHubRequestTests(OfflineTests):
    def test_methods_use_explicit_token_url_and_json_body(self):
        for method, payload, response in [
            ("GET", None, [{"id": 1}]),
            ("POST", {"body": "新评论"}, {"id": 2}),
            ("PATCH", {"body": "更新评论"}, {"id": 2}),
        ]:
            with self.subTest(method=method), patch.dict(
                os.environ, {"GH_TOKEN": "different-environment-token"}, clear=True
            ), patch("src.issue_checker.request.urlopen", return_value=io.BytesIO(
                json.dumps(response).encode("utf-8")
            )) as http:
                result = github_request(method, "/repos/example/project/issues/12", TOKEN, payload)
            self.assertEqual(result, response)
            req = http.call_args.args[0]
            self.assertEqual(req.get_method(), method)
            self.assertEqual(req.full_url, "https://api.github.com" + ISSUE_PATH)
            self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
            self.assertEqual(req.get_header("Accept"), "application/vnd.github+json")
            if payload is None:
                self.assertIsNone(req.data)
            else:
                self.assertEqual(json.loads(req.data), payload)
                self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertGreater(http.call_args.kwargs["timeout"], 0)

    def test_request_errors_propagate(self):
        with patch("src.issue_checker.request.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(URLError):
                github_request("GET", ISSUE_PATH, TOKEN)


class UpdateCommentTests(OfflineTests):
    def update(self, responses, body=MARKER + "\n新结果"):
        with patch("src.issue_checker.github_request", side_effect=responses) as api:
            action = update_comment(REPOSITORY, 12, body, TOKEN)
        return action, api

    def test_no_existing_comment_creates_one(self):
        body = MARKER + "\n新结果"
        action, api = self.update([[], {"id": 99}], body)
        self.assertEqual(action, "created")
        self.assertEqual(api.call_args_list, [
            call("GET", COMMENTS_PATH + "?per_page=100&page=1", TOKEN),
            call("POST", COMMENTS_PATH, TOKEN, {"body": body}),
        ])

    def test_changed_comment_is_replaced_by_comment_id(self):
        body = MARKER + "\n新结果"
        action, api = self.update([[comment()], {}], body)
        self.assertEqual(action, "updated")
        self.assertEqual(api.call_args_list, [
            call("GET", COMMENTS_PATH + "?per_page=100&page=1", TOKEN),
            call("PATCH", "/repos/example/project/issues/comments/99", TOKEN, {"body": body}),
        ])

    def test_identical_comment_does_not_write(self):
        body = MARKER + "\n结果"
        action, api = self.update([[comment(body)]], body)
        self.assertEqual(action, "unchanged")
        api.assert_called_once_with("GET", COMMENTS_PATH + "?per_page=100&page=1", TOKEN)

    def test_comments_without_matching_identity_and_leading_marker_are_preserved(self):
        unrelated = [
            comment(login="alice", kind="User"),
            comment(login="other[bot]"),
            comment(kind="User"),
            comment(body="其他 workflow 的检查结果"),
            comment(body="<!-- another-workflow -->\n检查结果"),
            comment(body="引用标记：" + MARKER),
            comment(body=None),
            {"id": 99, "body": MARKER},
        ]
        for existing in unrelated:
            with self.subTest(existing=existing):
                action, api = self.update([[existing], {"id": 100}])
                self.assertEqual(action, "created")
                self.assertEqual([c.args[0] for c in api.call_args_list], ["GET", "POST"])

    def test_target_on_second_page_is_updated_without_creating(self):
        first_page = [comment(body="其他评论", identifier=i) for i in range(100)]
        action, api = self.update([first_page, [comment(identifier=150)], {}])
        self.assertEqual(action, "updated")
        self.assertEqual(api.call_args_list[1],
                         call("GET", COMMENTS_PATH + "?per_page=100&page=2", TOKEN))
        self.assertEqual(api.call_args_list[2].args[:2],
                         ("PATCH", "/repos/example/project/issues/comments/150"))
        self.assertEqual(api.call_count, 3)

    def test_full_last_page_requires_empty_next_page_before_creating(self):
        first_page = [comment(body="其他评论", identifier=i) for i in range(100)]
        action, api = self.update([first_page, [], {"id": 101}])
        self.assertEqual(action, "created")
        self.assertEqual(api.call_args_list[1],
                         call("GET", COMMENTS_PATH + "?per_page=100&page=2", TOKEN))
        self.assertEqual([c.args[0] for c in api.call_args_list], ["GET", "GET", "POST"])

    def test_read_or_patch_failure_does_not_fall_back_to_create(self):
        for responses, methods in [([URLError("read failed")], ["GET"]),
                                   ([[comment()], URLError("patch failed")], ["GET", "PATCH"])]:
            with self.subTest(methods=methods), patch(
                "src.issue_checker.github_request", side_effect=responses
            ) as api:
                with self.assertRaises(URLError):
                    update_comment(REPOSITORY, 12, MARKER + "\n新结果", TOKEN)
                self.assertEqual([c.args[0] for c in api.call_args_list], methods)


class MainTests(OfflineTests):
    def run_main(self, api, **overrides):
        env = {"GH_TOKEN": TOKEN, "REPOSITORY": REPOSITORY, "ISSUE_NUMBER": "12",
               "TEMPLATE_DIR": str(TEMPLATE_DIR)}
        env.update(overrides)
        with patch.dict(os.environ, env, clear=True), patch(
            "src.issue_checker.github_request", api,
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            main()
        return json.loads(stdout.getvalue())

    def test_default_publishes_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with patch("src.issue_checker.github_request", side_effect=[ISSUE, ISSUE, [], {}]) as api:
                output = self.run_main(api, GITHUB_STEP_SUMMARY=str(summary))
            self.assertEqual(output["result"]["status"], "pass")
            self.assertEqual(output["comment_action"], "created")
            self.assertEqual([c.args[0] for c in api.call_args_list], ["GET", "GET", "GET", "POST"])
            self.assertEqual(summary.read_text(encoding="utf-8"),
                             f"评论操作：created\n\n{output['comment_body']}\n")

    def test_publish_lifecycle_creates_updates_then_skips_identical_comment(self):
        invalid = {"title": "未按模板填写", "body": "启动失败"}
        with patch("src.issue_checker.github_request", side_effect=[invalid, invalid, [], {}]) as api:
            created = self.run_main(api)
        self.assertEqual(created["comment_action"], "created")
        self.assertEqual(created["result"]["status"], "no_match")
        with patch("src.issue_checker.github_request", side_effect=[
            ISSUE, ISSUE, [comment(created["comment_body"])], {},
        ]) as api:
            updated = self.run_main(api)
        self.assertEqual(updated["comment_action"], "updated")
        self.assertEqual(updated["result"]["status"], "pass")
        api.assert_called_with("PATCH", "/repos/example/project/issues/comments/99", TOKEN,
                               {"body": updated["comment_body"]})
        with patch("src.issue_checker.github_request", side_effect=[
            ISSUE, ISSUE, [comment(updated["comment_body"])],
        ]) as api:
            unchanged = self.run_main(api)
        self.assertEqual(unchanged["comment_action"], "unchanged")
        self.assertEqual([c.args[0] for c in api.call_args_list], ["GET", "GET", "GET"])

    def test_title_or_body_change_skips_all_comment_requests(self):
        for changed in [{**ISSUE, "title": "新标题"}, {**ISSUE, "body": "新正文"}]:
            with self.subTest(changed=changed), patch(
                "src.issue_checker.github_request", side_effect=[ISSUE, changed]
            ) as api:
                output = self.run_main(api)
                self.assertEqual(output["comment_action"], "skipped_changed")
                self.assertEqual(api.call_args_list, [call("GET", ISSUE_PATH, TOKEN)] * 2)

    def test_null_or_missing_body_reports_missing_sections(self):
        for issue in [{"title": ISSUE["title"], "body": None}, {"title": ISSUE["title"]}]:
            with self.subTest(issue=issue), patch(
                "src.issue_checker.github_request", side_effect=[issue, issue, [], {}]
            ) as api:
                output = self.run_main(api)
                self.assertEqual(output["result"]["status"], "partial_match")
                self.assertIn("缺少此必填章节", output["comment_body"])
                self.assertEqual(output["comment_action"], "created")
                self.assertEqual(api.call_count, 4)

    def test_invalid_issue_number_is_rejected_before_request(self):
        for number in ["0", "-1", "abc", "12; echo secret"]:
            with self.subTest(number=number), patch("src.issue_checker.github_request") as api:
                with self.assertRaises(ValueError):
                    self.run_main(api, ISSUE_NUMBER=number)
                api.assert_not_called()

    def test_pull_request_is_rejected_without_commenting(self):
        with patch("src.issue_checker.github_request", return_value={**ISSUE, "pull_request": {}}) as api:
            with self.assertRaises(ValueError):
                self.run_main(api)
            api.assert_called_once_with("GET", ISSUE_PATH, TOKEN)

    def test_empty_template_directory_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "src.issue_checker.github_request", return_value=ISSUE
        ) as api:
            with self.assertRaises(ValueError):
                self.run_main(api, TEMPLATE_DIR=directory)
            api.assert_called_once_with("GET", ISSUE_PATH, TOKEN)

    def test_issue_read_or_recheck_error_does_not_publish(self):
        for fail_on_recheck in [False, True]:
            failure = HTTPError("https://api.github.com", 403, "Forbidden", {}, io.BytesIO())
            self.addCleanup(failure.close)
            responses = [ISSUE, failure] if fail_on_recheck else [failure]
            with self.subTest(recheck=fail_on_recheck), patch(
                "src.issue_checker.github_request", side_effect=responses
            ) as api:
                with self.assertRaises(HTTPError):
                    self.run_main(api)
                self.assertEqual(api.call_args_list, [call("GET", ISSUE_PATH, TOKEN)] * len(responses))

    def test_main_uses_real_request_adapter_and_posts_generated_body(self):
        responses = [ISSUE, ISSUE, [], {"id": 99}]
        env = {"GH_TOKEN": TOKEN, "REPOSITORY": REPOSITORY, "ISSUE_NUMBER": "12",
               "TEMPLATE_DIR": str(TEMPLATE_DIR)}
        with patch.dict(os.environ, env, clear=True), patch(
            "src.issue_checker.request.urlopen",
            side_effect=[io.BytesIO(json.dumps(value).encode("utf-8")) for value in responses],
        ) as http, contextlib.redirect_stdout(io.StringIO()) as stdout:
            main()
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["comment_action"], "created")
        requests = [c.args[0] for c in http.call_args_list]
        self.assertEqual([req.get_method() for req in requests], ["GET", "GET", "GET", "POST"])
        self.assertEqual(requests[-1].full_url, "https://api.github.com" + COMMENTS_PATH)
        self.assertEqual(json.loads(requests[-1].data), {"body": output["comment_body"]})
        for req in requests:
            self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
