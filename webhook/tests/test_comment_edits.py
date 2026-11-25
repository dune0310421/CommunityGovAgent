"""三分钟窗口、首次静默及修改后通过通知的事务恢复测试；不访问网络。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis
import compliance
import atomgit
import receiver
import worker
from errors import IssueProcessingError


class CommentEditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = receiver.Config('secret', 'token', ('example/repo',), str(Path(temp.name) / 'events.db'))
        receiver.initialize(self.config)
        self.client = atomgit.AtomGit('token', request_interval=0)
        self.issue = {'title': '问题', 'body': '正文', 'state': 'open', 'created_at': '2026-11-17T00:00:00Z'}
        self.comments = {}
        self.writes = []
        self.next_id = 1
        self.write_error = None
        self.accept_before_error = False
        self.lookup_error = None
        self.delete_on_patch = False
        self.enterContext(patch.object(self.client, 'request', side_effect=self.request))
        self.enterContext(patch.object(self.client, 'templates', return_value=[]))
        self.analyze = self.enterContext(patch.object(worker, 'analyze_issue', side_effect=self.analyze_with_history))
        self.enterContext(patch.object(atomgit, 'build_opener', side_effect=AssertionError('No live network')))
        self.edit(0)

    def analyze_with_history(self, *args, **kwargs):
        """为预设的分析结果应用真实发布策略。

        Args:
            *args: Worker 传入的分析参数；本测试使用 edit 预设结果。
            **kwargs: 分析上下文，读取此前是否有评论的标记。
        Returns:
            包含当前发布决定的分析结果副本。
        """
        result = deepcopy(self.analyze.return_value)
        result['publish_comment'] = analysis.should_publish_comment(
            result['summary'], previously_commented=kwargs['previously_commented'])
        return result

    def edit(self, seconds, outcome='fail'):
        self.issue['body'] = f'正文 {seconds} {outcome}'
        self.issue['updated_at'] = (datetime(2026, 11, 17, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()
        self.analyze.return_value = {
            'summary': {'status': 'pass' if outcome == 'pass' else 'no_match',
                        'recommendation_status': 'not_applicable' if outcome == 'gap' else None},
            'publish_comment': outcome == 'fail', 'comment': f'检查结果 {seconds} {outcome}',
            'maintainer_suggestion': {'reason': '现有模板不足', 'suggestion': '建议补充适用模板', 'mtr': '["alice"]'}
                if outcome == 'gap' else None,
        }

    def request(self, method, path, data=None):
        if method == 'GET':
            if '/comments?' in path:
                if self.lookup_error:
                    raise self.lookup_error
                return deepcopy(list(self.comments.values()))
            return deepcopy(self.issue)
        self.writes.append((method, path, deepcopy(data)))
        if method == 'PATCH':
            comment_id = int(path.rsplit('/', 1)[1])
            if self.delete_on_patch:
                self.comments.pop(comment_id, None)
            if comment_id not in self.comments:
                raise IssueProcessingError('评论已删除', retryable=False, status_code=404)
        else:
            self.assertEqual(method, 'POST')
            comment_id = self.next_id
            self.next_id += 1
        if self.write_error is None or self.accept_before_error:
            self.comments[comment_id] = {'id': comment_id, 'body': data['body']}
        if self.write_error:
            raise self.write_error
        return {'id': comment_id}

    def process(self):
        return worker._process_issue(self.config, 'example/repo', 12, client=self.client)

    def rows(self, table):
        with receiver.connect(self.config) as db:
            return [dict(row) for row in db.execute(f'SELECT * FROM {table}')]

    def test_rolling_window_includes_180_seconds_and_new_comment_after_181(self):
        """验证相邻修改间隔为 180 秒时更新评论，超过窗口时新增评论。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.assertEqual(self.process()['comment_action'], 'created')
        for second in (179, 359):
            self.edit(second)
            self.assertEqual(self.process()['comment_action'], 'updated')
        self.edit(540)
        self.assertEqual(self.process()['comment_action'], 'created')
        self.assertEqual([w[0] for w in self.writes], ['POST', 'PATCH', 'PATCH', 'POST'])
        self.assertEqual(len(self.comments), 2)

    def test_pass_and_template_gap_update_previous_comment(self):
        """验证两种通过结果在窗口内更新评论，维护者建议只入库。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.process()
        for seconds, outcome in ((100, 'pass'), (200, 'gap')):
            self.edit(seconds, outcome)
            result = self.process()
            self.assertEqual(result['comment_action'], 'updated')
            self.assertIn(f'{seconds} {outcome}', self.comments[1]['body'])
            self.assertNotIn('建议补充适用模板', self.comments[1]['body'])
        self.assertEqual(result['maintainer_suggestion_id'], self.rows('maintainer_suggestions')[0]['id'])
        self.assertEqual([w[0] for w in self.writes], ['POST', 'PATCH', 'PATCH'])
        self.edit(300)
        self.assertEqual(self.process()['comment_action'], 'updated')
        self.assertEqual(len(self.comments), 1)

    def test_real_analysis_notifies_both_pass_results(self):
        """通过真实分析流程验证完全匹配和模板不适用均可通知通过。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        files = [{"filename": "bug.md", "source": "bug.md",
                  "content": '---\nname: 缺陷反馈\ntitle: "[Bug] "\n---\n### 问题（必填）\n'}]
        with patch.object(worker, 'analyze_issue', wraps=analysis.analyze_issue), patch.object(
                self.client, 'templates', return_value=files):
            self.issue.update(title='[Bug] 失败', body='缺少问题章节')
            with patch.object(compliance, 'recommend_template', return_value={
                    'status': 'needs_changes', 'template': '缺陷反馈', 'reason': '缺陷模板适用',
                    'problems': [{'field': '问题', 'reason': 'body', 'message': '请补充问题章节'}]}):
                self.assertEqual(self.process()['comment_action'], 'created')
            self.assertIn('提交内容规范性检查：未通过', self.comments[1]['body'])
            self.edit(100, 'pass')
            self.issue['body'] = '### 问题\n启动失败'
            self.assertEqual(self.process()['comment_action'], 'updated')
            self.assertIn('提交内容规范性检查：通过', self.comments[1]['body'])
            self.edit(200, 'gap')
            self.issue['title'] = '权限申请'
            with patch.object(compliance, 'recommend_template', return_value={
                    'status': 'not_applicable', 'reason': '模板未覆盖权限申请',
                    'maintainer': '建议新增权限申请模板'}):
                self.assertEqual(self.process()['comment_action'], 'updated')
            self.assertIn('提交内容规范性检查：通过', self.comments[1]['body'])
            self.assertNotIn('建议新增权限申请模板', self.comments[1]['body'])
            self.assertEqual(self.rows('maintainer_suggestions')[0]['suggestion'], '建议新增权限申请模板')
            self.assertEqual([w[0] for w in self.writes], ['POST', 'PATCH', 'PATCH'])
            self.assertEqual(self.process()['comment_action'], 'unchanged')
            self.assertEqual(len(self.writes), 3)

    def test_template_error_does_not_update_recent_comment(self):
        """验证模板故障不更新近期评论，并且重复处理不会重复保存建议。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.process()
        original = deepcopy(self.comments)
        self.edit(100)
        with patch.object(worker, 'analyze_issue', wraps=analysis.analyze_issue):
            result = self.process()
        self.assertEqual(result['status'], 'template_error')
        self.assertEqual(result['comment_action'], 'suppressed')
        self.assertEqual(self.comments, original)
        self.assertEqual([w[0] for w in self.writes], ['POST'])
        self.assertEqual(len(self.rows('maintainer_suggestions')), 1)
        self.assertIn('未找到可用的', self.rows('maintainer_suggestions')[0]['reason'])
        self.assertEqual(self.process()['comment_action'], 'unchanged')
        self.assertEqual(len(self.rows('maintainer_suggestions')), 1)
        # 配置故障不能清除历史评论，也不能延长原评论的更新窗口。
        self.edit(200, 'pass')
        self.assertEqual(self.process()['comment_action'], 'created')
        self.assertEqual(len(self.comments), 2)

    def test_pass_without_previous_comment_remains_silent_then_failure_creates(self):
        for seconds, outcome in ((0, 'pass'), (100, 'gap')):
            self.edit(seconds, outcome)
            self.assertEqual(self.process()['comment_action'], 'suppressed')
        self.assertEqual(self.writes, [])
        self.assertEqual(len(self.rows('maintainer_suggestions')), 1)
        self.edit(200)
        self.assertEqual(self.process()['comment_action'], 'created')

    def test_pass_outside_window_creates_notification(self):
        """验证窗口外通过时新增通知，窗口内再次通过则更新该通知。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.process()
        original = deepcopy(self.comments[1])
        self.edit(181, 'pass')
        self.assertEqual(self.process()['comment_action'], 'created')
        self.assertEqual(self.comments[1], original)
        self.assertIn('181 pass', self.comments[2]['body'])
        self.edit(361, 'pass')
        self.assertEqual(self.process()['comment_action'], 'updated')
        self.assertIn('361 pass', self.comments[2]['body'])
        self.edit(542, 'pass')
        self.assertEqual(self.process()['comment_action'], 'created')
        self.assertEqual(len(self.comments), 3)

    def test_deleted_comment_falls_back_to_create(self):
        """旧评论删除后新增通过通知，后续修改可更新新评论。

        Args:
            无。
        Returns:
            None；评论发送方式不符合预期时测试失败。
        """
        self.process()
        self.comments.clear()
        self.edit(100, 'pass')
        self.assertEqual(self.process()['comment_action'], 'created')
        self.edit(200)
        self.assertEqual(self.process()['comment_action'], 'updated')
        self.comments.clear()
        self.edit(300)
        self.assertEqual(self.process()['comment_action'], 'created')

    def test_comment_deleted_between_lookup_and_patch(self):
        """验证更新前评论被删除时，未通过和通过结果均回退到新增评论。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.process()
        self.delete_on_patch = True
        self.edit(100)
        self.assertEqual(self.process()['comment_action'], 'created')
        self.assertEqual([w[0] for w in self.writes], ['POST', 'PATCH', 'POST'])
        self.edit(200, 'pass')
        self.assertEqual(self.process()['comment_action'], 'created')
        self.assertIn('200 pass', self.comments[3]['body'])
        self.assertEqual([w[0] for w in self.writes], ['POST', 'PATCH', 'POST', 'PATCH', 'POST'])

    def test_lost_patch_response_recovers_without_reanalysis_or_duplicate(self):
        """验证评论更新成功但响应丢失时恢复状态，不重新分析或重复更新。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.process()
        self.edit(100, 'gap')
        self.write_error = IssueProcessingError('响应丢失')
        self.accept_before_error = True
        with self.assertRaises(IssueProcessingError):
            self.process()
        self.assertEqual(self.rows('maintainer_suggestions'), [])
        self.assertEqual(len(self.rows('pending_comments')), 1)
        self.write_error = None
        self.client = atomgit.AtomGit('token', request_interval=0)
        self.enterContext(patch.object(self.client, 'request', side_effect=self.request))
        receiver.initialize(self.config)
        result = self.process()
        self.assertEqual(result['comment_action'], 'recovered')
        self.assertEqual(self.analyze.call_count, 2)
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(len(self.rows('maintainer_suggestions')), 1)
        self.assertEqual(self.rows('pending_comments'), [])
        self.assertEqual(self.process()['comment_action'], 'unchanged')
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(len(self.rows('maintainer_suggestions')), 1)

    def test_pending_pass_from_old_version_is_reanalyzed(self):
        """验证旧版本静默通过的待处理记录重新分析后更新已有评论。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.process()
        self.edit(100)
        self.write_error = IssueProcessingError('响应丢失')
        with self.assertRaises(IssueProcessingError):
            self.process()
        with receiver.connect(self.config) as db:
            row = db.execute('SELECT summary FROM pending_comments').fetchone()
            summary = json.loads(row['summary'])
            summary.update(status='pass', publish_comment=False)
            db.execute('UPDATE pending_comments SET summary = ?', (json.dumps(summary),))
        self.analyze.return_value.update(summary={'status': 'pass', 'recommendation_status': None},
                                         publish_comment=False, comment='检查通过')
        self.write_error = None
        self.assertEqual(self.process()['comment_action'], 'updated')
        self.assertIn('检查通过', self.comments[1]['body'])
        self.assertEqual(len(self.writes), 3)
        self.assertEqual(self.rows('pending_comments'), [])

    def test_retry_keeps_update_target_after_wall_clock_window(self):
        self.process()
        self.edit(100)
        self.write_error = IssueProcessingError('限流', status_code=429, retry_after=600)
        with self.assertRaises(IssueProcessingError):
            self.process()
        self.write_error = None
        with patch.object(worker.time, 'time', return_value=9999999999):
            self.assertEqual(self.process()['comment_action'], 'updated')
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.analyze.call_count, 2)

    def test_pass_advice_database_failure_rolls_back_without_duplicate_notification(self):
        """验证通过通知后的建议写库失败可恢复，不重复通知或保存建议。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.process()
        checked = self.rows('issue_checks')
        self.edit(100, 'gap')
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
            raise sqlite3.OperationalError('模拟写库失败')
        with patch.object(worker, '_save_maintainer_suggestion', side_effect=fail_after_insert):
            with self.assertRaises(sqlite3.OperationalError):
                self.process()
        self.assertEqual(self.rows('maintainer_suggestions'), [])
        self.assertEqual(self.rows('issue_checks'), checked)
        self.assertEqual(self.process()['comment_action'], 'recovered')
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(len(self.rows('maintainer_suggestions')), 1)

    def test_issue_changed_or_closed_before_patch_does_not_update_comment(self):
        self.process()
        original = deepcopy(self.comments)
        self.edit(100)
        for change in ({'body': '更新的正文'}, {'state': 'closed'}):
            reads = 0
            def latest(method, path, data=None):
                nonlocal reads
                if method == 'GET' and '/comments?' not in path:
                    reads += 1
                    if reads == 2:
                        return {**self.issue, **change}
                return self.request(method, path, data)
            with self.subTest(change=change), patch.object(self.client, 'request', side_effect=latest):
                if 'body' in change:
                    with self.assertRaises(IssueProcessingError):
                        self.process()
                else:
                    self.assertEqual(self.process()['comment_action'], 'skipped_closed')
            self.assertEqual(self.comments, original)
            self.assertEqual(len(self.writes), 1)

    def test_new_edit_after_uncertain_post_updates_the_comment_that_was_created(self):
        """首次未通过通知的响应丢失后，修改通过仍能更新已发布的评论。

        Args:
            无。
        Returns:
            None；丢失历史评论或重复新增时测试失败。
        """
        self.write_error = IssueProcessingError('响应丢失')
        self.accept_before_error = True
        with self.assertRaises(IssueProcessingError):
            self.process()
        self.edit(100, 'pass')
        self.write_error = None
        self.assertEqual(self.process()['comment_action'], 'updated')
        self.assertEqual(len(self.comments), 1)
        self.assertIn('100', self.comments[1]['body'])

    def test_new_edit_after_failed_patch_uses_previous_edit_time(self):
        """验证更新失败后的新修改沿用上次修改时间判断评论更新窗口。

            Args:
                无。
            Returns:
                None；断言不满足时测试失败。
            """
        self.process()
        self.edit(150)
        self.write_error = IssueProcessingError('网络错误')
        with self.assertRaises(IssueProcessingError):
            self.process()
        self.edit(300)
        self.write_error = None
        self.assertEqual(self.process()['comment_action'], 'updated')
        self.assertEqual(len(self.comments), 1)
        self.assertIn('300', self.comments[1]['body'])

    def test_lookup_failure_and_permission_denied_do_not_create_new_comment(self):
        self.process()
        self.edit(100)
        self.lookup_error = IssueProcessingError('查询失败')
        with self.assertRaises(IssueProcessingError):
            self.process()
        self.assertEqual(len(self.writes), 1)
        self.lookup_error = None
        self.write_error = IssueProcessingError('无修改权限', retryable=False, status_code=403)
        with self.assertRaises(IssueProcessingError):
            self.process()
        self.assertEqual([w[0] for w in self.writes], ['POST', 'PATCH'])

    def test_missing_invalid_or_backwards_timestamp_does_not_edit_history(self):
        self.process()
        for stamp in (None, 'invalid', '2026-11-17T00:00:00', '2026-11-16T00:00:00Z'):
            self.issue['body'] += '修改'
            self.issue['updated_at'] = stamp
            self.assertEqual(self.process()['comment_action'], 'created')
        self.assertTrue(all(w[0] == 'POST' for w in self.writes))

    def test_unrelated_comments_are_not_updated(self):
        self.comments[99] = {'id': 99, 'body': '贡献者的评论'}
        self.process()
        self.edit(100)
        self.process()
        self.assertEqual(self.comments[99]['body'], '贡献者的评论')
        self.assertEqual(self.writes[-1][1], '/repos/example/repo/issues/comments/1')

    def test_migration_preserves_old_completed_checks(self):
        with receiver.connect(self.config) as db:
            db.execute('DROP TABLE issue_checks')
            db.execute('CREATE TABLE issue_checks (repository TEXT, issue_number INTEGER, content_hash TEXT, PRIMARY KEY(repository, issue_number))')
            db.execute("INSERT INTO issue_checks VALUES ('example/repo', 12, 'old')")
        receiver.initialize(self.config)
        receiver.initialize(self.config)
        row = self.rows('issue_checks')[0]
        self.assertEqual(row['content_hash'], 'old')
        self.assertIsNone(row['comment_marker'])
        self.assertIsNone(row['issue_updated_at'])
        self.assertEqual(self.process()['comment_action'], 'created')


if __name__ == '__main__':
    unittest.main()
