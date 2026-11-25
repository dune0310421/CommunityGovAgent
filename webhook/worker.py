"""后台任务与治理流程：领取任务、组织治理功能、写回评论、保存结果及重试。"""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import signal
import time

from atomgit import AtomGit
from errors import IssueProcessingError
from analysis import analyze_issue
from receiver import Config, connect, initialize

LOG = logging.getLogger("issue-gov-webhook")
MAX_ATTEMPTS = 5  # 每个事件最多完成 5 次处理尝试；进程中断不消耗次数
COMMENT_EDIT_SECONDS = 3 * 60


def _issue_edit_time(issue: dict) -> float | None:
    """使用平台的 Issue 修改时间，避免处理耗时改变三分钟评论更新窗口。

    Args:
        issue: 平台返回的 Issue 对象，updated_at 应为带时区的 ISO 日期时间。
    Returns:
        修改时间的 Unix 时间戳（秒）；时间缺失、格式无效、无时区或溢出时返回 None。
    """
    try:
        value = datetime.fromisoformat(issue.get("updated_at"))
        return value.timestamp() if value.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None  # 缺少可靠时间时不合并评论。


def issue_content_hash(issue: dict) -> str:
    """校验并计算标题、正文的内容标识，不把评论数或更新时间算作正文修改。

    Args:
        issue: AtomGit 返回的 Issue 对象，正文允许为 None。
    Returns:
        标题和正文的 SHA-256；字段无效时抛出可重试的 IssueProcessingError。
    """
    title, body = issue.get("title"), issue.get("body")
    if not isinstance(title, str) or (body is not None and not isinstance(body, str)):
        raise IssueProcessingError("Issue 标题或正文格式不正确")
    return hashlib.sha256(json.dumps([title, body or ""], ensure_ascii=False).encode()).hexdigest()


def _save_maintainer_suggestion(db, repository: str, number: int, suggestion: dict) -> int:
    """在调用方事务内保存维护者建议，不单独提交事务。

    Args:
        db: 当前事务使用的 SQLite 数据库连接。
        repository: 建议关联的仓库路径，格式为 owner/repo。
        number: 建议关联的 Issue 编号。
        suggestion: 包含 reason、suggestion 和 mtr 的字典；前两项为原因和建议文本，
            mtr 为配置中的维护者用户名列表序列化后的 JSON 字符串。
    Returns:
        新增维护者建议记录的整数 ID。
    Raises:
        sqlite3.Error: 数据库写入失败，由调用方回滚事务并安排重试。
    """
    return db.execute("""INSERT INTO maintainer_suggestions
                         (repository, issue_number, created_at, reason, suggestion, mtr) VALUES (?, ?, ?, ?, ?, ?)""",
                      (repository, number, datetime.now(timezone.utc).isoformat(),
                       suggestion["reason"], suggestion["suggestion"], suggestion["mtr"])).lastrowid


def _process_issue(config: Config, repository: str, number: int, *, client: AtomGit) -> dict:
    """读取 Issue 和模板，按分析决定静默记录或新增、更新评论。

    Args:
        config: 服务配置。
        repository: 目标仓库路径，格式为 owner/repo，必须在接入列表中。
        number: 仓库内的正整数 Issue 编号。
        client: 由 run_worker 创建并复用的客户端，用于保留缓存和请求间隔。
    Returns:
        处理结果字典。status 为 pass、no_match、template_error、skipped_closed
        或 skipped_unchanged；comment_action 为 created、updated、recovered、suppressed、skipped_closed 或 unchanged。
        执行过分析时还包含 recommendation_status，为模型复核状态或 None；
        首次读取即发现 Issue 已关闭时不包含该字段。
    """
    if repository not in config.repositories:
        raise IssueProcessingError("仓库已不在允许接入列表中", retryable=False)
    if not re.fullmatch(r"[1-9][0-9]*", str(number)):
        raise IssueProcessingError("Issue 编号必须是正整数", retryable=False)
    
    # 获取issue
    path = f"/repos/{repository}/issues/{number}"
    issue = client.request("GET", path)
    if not isinstance(issue, dict) or not isinstance(issue.get("title"), str):
        raise IssueProcessingError("Issue 响应格式不正确")
    if "pull_request" in issue:
        raise IssueProcessingError("该编号对应 Pull Request，不能执行 Issue 检查", retryable=False)
    if issue.get("state") == "closed":
        return {"status": "skipped_closed", "comment_action": "skipped_closed"}
    digest = issue_content_hash(issue)
    with connect(config) as db:
        checked = db.execute("SELECT * FROM issue_checks WHERE repository = ? AND issue_number = ?",
                             (repository, number)).fetchone()
        pending = db.execute("SELECT * FROM pending_comments WHERE repository = ? AND issue_number = ?",
                             (repository, number)).fetchone()
    # 有未确认发送时，旧的已检查记录不足以证明当前最后一条评论对应最新内容。
    if checked is not None and checked["content_hash"] == digest and pending is None:
        return {"status": "skipped_unchanged", "comment_action": "unchanged"}
    pending_summary = json.loads(pending["summary"]) if pending is not None else {}
    delivery = pending_summary.pop("delivery", {})
    edit_time = _issue_edit_time(issue)
    comment_id = None
    extra = {}
    suggestion = None
    publish_comment = pending_summary.pop("publish_comment", False)
    resume = (pending is not None and pending["content_hash"] == digest
              and publish_comment)
    if resume:
        # 使用已保存的发布决定；旧记录缺少决定时重新分析。
        summary = pending_summary
        comment_id = delivery.get("comment_id")
        suggestion = delivery.get("maintainer_suggestion")
        edit_time = delivery.get("issue_updated_at", edit_time)
        body, marker = pending["body"], pending["marker"]
        published = client.has_comment(repository, number, marker)
    else:
        # 检查是否有已完成的评论或者待确认发送的评论记录
        has_checked_comment = checked is not None and bool(checked["comment_marker"])
        has_pending_comment = pending is not None and bool(publish_comment)
        previously_commented = has_checked_comment or has_pending_comment

        template_files = client.templates(repository)
        analysis_result = analyze_issue(
            issue["title"], issue.get("body") or "", template_files,
            config.agent_api_key, config.llm_model, config.llm_base_url,
            repository=repository, number=number, maintainers=config.maintainers.get(repository, ()),
            previously_commented=previously_commented)
        summary = analysis_result["summary"]
        publish_comment = analysis_result["publish_comment"]
        suggestion = analysis_result["maintainer_suggestion"]
        if pending is not None and pending["content_hash"] == digest and publish_comment:
            # 旧记录重新分析后仍需评论时，先核对原标记，避免响应丢失导致重复发布。
            marker = pending["marker"]
            published = client.has_comment(repository, number, marker)
        else:
            marker = f"<!-- issue-gov:{secrets.token_hex(24)} -->"
            published = False
        body = analysis_result["comment"] + "\n\n" + marker

    if not resume and publish_comment and not published and edit_time is not None:
        # 未完成的上一版可能已写入平台，先找它，再找最近完成检查的评论。
        candidates = []
        if pending is not None:
            candidates.append((pending["marker"], delivery.get("issue_updated_at")))
        if checked is not None:
            previous_time = (delivery.get("issue_updated_at") if delivery.get("comment_id") is not None
                             else checked["issue_updated_at"])
            candidates.append((checked["comment_marker"], previous_time))
        for previous_marker, previous_time in candidates:
            if (previous_marker and previous_time is not None
                    and 0 <= edit_time - previous_time <= COMMENT_EDIT_SECONDS):
                comment = client.find_comment(repository, number, previous_marker)
                if comment is not None:
                    comment_id = comment.get("id")
                    if type(comment_id) is not int or comment_id <= 0:
                        raise IssueProcessingError("评论 ID 格式不正确")
                    break

    # 写库或发布前复查，避免对已关闭或已变化的 Issue 保存过时结果。
    latest = client.request("GET", path)
    if latest.get("state") == "closed":
        action = "skipped_closed"
    elif issue_content_hash(latest) != digest:
        raise IssueProcessingError("检查期间 Issue 内容已修改，将重新读取并检查")
    else:
        if not publish_comment:
            action = "suppressed"
        elif published:
            action = "recovered"
        else:
            with connect(config) as db:
                db.execute("""INSERT INTO pending_comments VALUES (?, ?, ?, ?, ?, ?)
                              ON CONFLICT(repository, issue_number) DO UPDATE SET
                              content_hash=excluded.content_hash, marker=excluded.marker,
                              body=excluded.body, summary=excluded.summary""",
                           (repository, number, digest, marker, body,
                            json.dumps({**summary, "publish_comment": publish_comment,
                                        "delivery": {"comment_id": comment_id, "issue_updated_at": edit_time,
                                                     "maintainer_suggestion": suggestion}}, ensure_ascii=False)))
            action = client.update_comment(repository, comment_id, body) if comment_id is not None else "missing"
            if action == "missing":
                action = client.create_comment(repository, number, body) if publish_comment else "suppressed"
        saved_marker = marker if action != "suppressed" else checked["comment_marker"] if checked is not None else None
        saved_time = checked["issue_updated_at"] if action == "suppressed" and checked is not None else edit_time
        with connect(config) as db:
            if suggestion is not None:
                extra["maintainer_suggestion_id"] = _save_maintainer_suggestion(
                    db, repository, number, suggestion)
            db.execute("""INSERT INTO issue_checks
                          (repository, issue_number, content_hash, comment_marker, issue_updated_at)
                          VALUES (?, ?, ?, ?, ?) ON CONFLICT(repository, issue_number)
                          DO UPDATE SET content_hash=excluded.content_hash, comment_marker=excluded.comment_marker,
                                        issue_updated_at=excluded.issue_updated_at""",
                       (repository, number, digest, saved_marker, saved_time))
            db.execute("DELETE FROM pending_comments WHERE repository = ? AND issue_number = ?",
                       (repository, number))
    return {**summary, **extra, "comment_action": action}


def process_one(
    config: Config, now: float | None = None, *, client: AtomGit,
) -> bool:
    """领取并处理一个到期事件，保存成功结果或安排失败重试。

    文件锁覆盖整个处理过程，同一数据库只允许一个治理任务执行，防止长任务被重复领取。
    锁文件与数据库一起放在本地磁盘，文件关闭或进程退出时由系统释放锁。

    Args:
        config: 接收端与后台进程共用的服务配置。
        now: 本轮处理使用的 Unix 时间戳；为 None 时读取当前时间，可注入以验证重试行为。
        client: 由 run_worker 创建的共用客户端，原样传给 _process_issue。
    Returns:
        本轮领取并尝试处理了一个事件时返回 True；无到期任务、仍在限流等待期
        或未取得文件锁时返回 False。
        返回 True 不保证处理成功，具体结果以队列状态和日志为准。
    """

    # 领取事件前先尝试获取文件锁，防止多个进程同时处理同一数据库
    fd = os.open(str(Path(config.database).resolve()) + ".worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fixed_now = now

        now = time.time() if now is None else now

        # 从数据库领取一条任务，并标记为“正在处理”
        with connect(config) as db:
            db.execute("BEGIN IMMEDIATE")
            # 已取得覆盖整个处理过程的文件锁，遗留 running 任务不可能仍由其他进程执行。
            # 退回未完成的这一次尝试；同时恢复旧版本因中断次数耗尽而标记失败的任务。
            db.execute("""UPDATE events SET state = 'queued', attempts = MAX(attempts - 1, 0),
                          next_attempt = 0, last_error = '后台处理中断，已重新排队'
                          WHERE state = 'running'
                             OR (state = 'failed' AND last_error = '后台处理超时或中断')""")
            cooldown = db.execute("SELECT until FROM api_cooldown WHERE name = 'atomgit'").fetchone()
            if cooldown is not None and cooldown["until"] > now:
                return False
            # 领取一条待处理事件
            row = db.execute("""SELECT e.* FROM events e WHERE e.state = 'queued' AND e.next_attempt <= ?
                                AND NOT EXISTS (SELECT 1 FROM events waiting
                                    WHERE waiting.repository = e.repository AND waiting.issue_number = e.issue_number
                                      AND waiting.state = 'queued' AND waiting.next_attempt > ?)
                                ORDER BY e.updated_at LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return False
            # 领取尚需处理的请求，沿用已有重试预算。
            attempt = db.execute("""SELECT MAX(attempts) FROM events WHERE repository = ?
                                    AND issue_number = ? AND state = 'queued'""",
                                 (row["repository"], row["issue_number"])).fetchone()[0] + 1
            db.execute("""UPDATE events SET state = 'running', attempts = ?, updated_at = ?
                          WHERE repository = ? AND issue_number = ? AND state = 'queued'""",
                       (attempt, now, row["repository"], row["issue_number"]))

        # 执行处理和评论
        try:
            result = _process_issue(config, row["repository"], row["issue_number"], client=client)
            if not isinstance(result, dict) or not result.get("comment_action"):
                raise IssueProcessingError("治理程序未返回有效处理结果")
        except Exception as error:
            # 未预期错误也需要有限重试，不能因反复崩溃跳过失败上限；不记录异常原文。
            exc = error if isinstance(error, IssueProcessingError) else IssueProcessingError("治理程序发生内部错误，请检查代码和配置")
            now = time.time() if fixed_now is None else fixed_now
            # 记录失败原因，安排重试或标记为失败
            state = "queued" if exc.retryable and attempt < MAX_ATTEMPTS else "failed"
            # 指数退避最多 5 分钟；429 同时遵守服务端要求的更长等待时间。
            retry_delay = min(30 * 2**(attempt - 1), 300)
            if exc.status_code == 429:
                retry_delay = max(retry_delay, math.ceil(exc.retry_after))
            with connect(config) as db:
                if exc.status_code == 429:
                    db.execute("""INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)
                                  ON CONFLICT(name) DO UPDATE SET until = MAX(until, excluded.until)""",
                               (now + retry_delay,))
                db.execute("""UPDATE events SET state = CASE WHEN state='superseded' THEN state ELSE ? END,
                              next_attempt = ?, last_error = ?, updated_at = ?
                              WHERE event_id = ? AND attempts = ? AND state IN ('running', 'superseded')""",
                           (state, now + retry_delay, str(exc), now, row["event_id"], attempt))
                state = db.execute("SELECT state FROM events WHERE event_id=?", (row["event_id"],)).fetchone()[0]
            if exc.status_code == 429:
                LOG.warning("AtomGit API 已限流，全部治理任务暂停请求 %s 秒；Webhook 继续接收并保存通知", retry_delay)
            if state == "superseded":
                LOG.info("旧请求已被新请求覆盖：仓库=%s，Issue=#%s，停止本次处理，由新请求接替，原因=%s，事件=%s",
                         row["repository"], row["issue_number"], exc, row["event_id"][:12])
            else:
                outcome = (f"将在 {retry_delay} 秒后自动重试" if state == "queued"
                           else "任务已失败，停止自动重试，请排查原因")
                LOG.warning("治理处理失败：仓库=%s，Issue=#%s，第 %s/%s 次尝试，%s，原因=%s，事件=%s",
                            row["repository"], row["issue_number"], attempt, MAX_ATTEMPTS, outcome, exc,
                            row["event_id"][:12])
        else:
            now = time.time() if fixed_now is None else fixed_now
            # 只更新本次请求的结果；若已经被覆盖则保留该标记，不影响新请求。
            with connect(config) as db:
                db.execute("""UPDATE events SET state = CASE WHEN state='superseded' THEN state ELSE 'completed' END,
                              result = ?, last_error = NULL, updated_at = ?
                              WHERE event_id = ? AND attempts = ? AND state IN ('running', 'superseded')""",
                           (json.dumps(result, ensure_ascii=False), now, row["event_id"], attempt))
            statuses = {"pass": "规范检查通过", "no_match": "未匹配模板",
                        "template_error": "模板配置异常，维护者建议已入库", "skipped_closed": "Issue 已关闭",
                        "skipped_unchanged": "标题和正文已检查，跳过重复处理"}
            actions = {"created": "已发布评论", "recovered": "已找到先前发布的评论，恢复完成状态", "unchanged": "未重复发布评论",
                       "updated": "已更新检查评论",
                       "suppressed": "静默处理，未发表评论",
                       "skipped_closed": "跳过已关闭的 Issue"}
            if result.get("maintainer_suggestion_id") is not None:
                statuses["no_match"] = "检查通过，模板覆盖建议已入库"
            LOG.info("治理处理完成：仓库=%s，Issue=#%s，检查=%s，评论=%s，事件=%s",
                     row["repository"], row["issue_number"], statuses.get(result.get("status"), "已处理"),
                     actions.get(result["comment_action"], "已处理"), row["event_id"][:12])
        return True


def run_worker(config: Config) -> None:
    """持续处理已保存的任务，由 Supervisor 在独立后台进程中调用。

    Args:
        config: 接收进程与任务处理进程共用的配置。
    Returns:
        正常情况下持续循环、不返回；收到进程终止信号或发生未处理异常时结束运行。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def stop_worker(signum, frame) -> None:
        """响应停止信号，退出处理并执行模型子进程清理。

        Args:
            signum: 操作系统信号编号。
            frame: 收到信号时的执行栈帧。
        Returns:
            不返回；抛出 SystemExit，遗留任务由下一进程取得锁后恢复。
        """
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)

    # 确保数据库和表已创建
    initialize(config)
    # 创建客户端，复用缓存和请求间隔记录
    client = AtomGit(config.token)
    LOG.info("后台处理服务已启动，正在等待任务：接入仓库=%s", ",".join(config.repositories))

    # 只处理已收到的 Webhook 通知，不主动扫描仓库历史 Issue。
    while True:
        if not process_one(config, client=client):
            time.sleep(1)


def retry_failed(config: Config) -> int:
    """将所有失败任务重新安排为待处理；由 manage.py 在修复问题后调用。

    Args:
        config: 指向服务任务数据库的配置。
    Returns:
        重新安排的任务数量；不会自行执行检查，需要后台进程继续处理。
    """
    initialize(config)
    with connect(config) as db:
        return db.execute("""UPDATE events SET state = 'queued', attempts = 0,
                             next_attempt = 0, updated_at = ? WHERE state = 'failed'""",
                          (time.time(),)).rowcount
