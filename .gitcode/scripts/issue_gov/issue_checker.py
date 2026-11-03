"""Issue 模板检查：本地校验，以及 AtomGit 工作流读取和评论反馈。"""

import html
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib import request

import yaml
from markdown_it import MarkdownIt

MARKER = "<!-- issue-gov-agent -->" # 评论中的隐藏标记：配合机器人身份，找到本工具之前发布的评论
BOT_LOGIN = "action-bot" # 平台默认工作流令牌的评论作者，已核对公开流水线评论
ATOMGIT_WEB_URL = "https://atomgit.com"
ATOMGIT_API_URL = "https://api.atomgit.com/api/v5"
LLM_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
LLM_TIMEOUT = 120
RECOMMEND_PROMPT = Path(__file__).resolve().with_name("recommend_template.md")


def load_templates(directory: str | Path) -> list[dict[str, Any]]:
    """读取本地 YAML 表单模板，跳过 config.yml。

    Args:
        directory: 模板目录路径。
    Returns:
        模板字典列表；目录中没有模板时返回空列表。
    """
    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(Path(directory).iterdir())
        if path.suffix in {".yml", ".yaml"} and path.stem != "config"
    ]


def atomgit_request(
    method: str, path: str, atomgit_token: str, data: dict[str, Any] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """使用传入的 Token 调用 AtomGit API。

    Args:
        method: HTTP 方法，如 GET、POST、PATCH。
        path: 以 / 开头的 API 路径，可包含查询参数。
        atomgit_token: AtomGit API 访问令牌。
        data: 请求体字典；None 表示不发送请求体。
    Returns:
        JSON 响应解析后的字典或字典列表；请求失败时抛出异常。
    """
    headers = {
        "Authorization": "Bearer " + atomgit_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = None if data is None else json.dumps(data).encode("utf-8")
    req = request.Request(ATOMGIT_API_URL + path, data=payload, headers=headers, method=method)
    with request.urlopen(req, timeout=30) as response:
        content = response.read()
        return json.loads(content) if content.strip() else {}


def _fields(template: dict[str, Any]) -> list[dict[str, Any]]:
    """提取模板中的文本输入字段，忽略 markdown 说明。

    Args:
        template: 一个 YAML 模板解析后的字典。
    Returns:
        input/textarea 字段列表；无文本字段或遇到不支持的字段类型时报错。
    """
    # 提取需要填写的字段，忽略模板中只用于展示说明的 markdown。
    fields = [field for field in template["body"] if field["type"] != "markdown"]
    if not fields or any(field["type"] not in {"input", "textarea"} for field in fields):
        raise ValueError("模板需要文本字段，当前仅支持 input 和 textarea")
    return fields


def _sections(body: str) -> dict[str, str]:
    """提取 Markdown 章节及内容，忽略代码块和引用中的伪标题。

    Args:
        body: Issue 的原始 Markdown 正文。
    Returns:
        章节标题到填写内容的映射；同名章节合并，子节内容计入父节。
    """
    # 将正文解析为「章节标题 → 填写内容」，代码块和引用中的标题不算章节。
    tokens = MarkdownIt("commonmark").parse(body)
    headings = [
        (i, int(token.tag[1:]), tokens[i + 1].content.strip())
        for i, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0
    ]
    sections: dict[str, list[str]] = {}
    for position, (start, level, label) in enumerate(headings):
        # 同级或更高层级的下一个标题是本节终点；子节内容仍归本节。
        end = next(
            (i for i, depth, _ in headings[position + 1:] if depth <= level),
            len(tokens),
        )
        parts = []
        for i in range(start + 3, end):
            token = tokens[i]
            if token.type in {"fence", "code_block"}:
                parts.append(token.content)
            elif token.type in {"inline", "html_block"}:
                # 子标题本身不算填写内容；HTML 注释也不算。
                if tokens[i - 1].type != "heading_open":
                    parts.append(re.sub(r"<!--[\s\S]*?-->", "", token.content))
        sections.setdefault(label, []).append("\n".join(parts).strip())
    return {label: "\n".join(parts).strip() for label, parts in sections.items()}


def check_issue(
    title: str, body: str, templates: list[dict[str, Any]],
) -> dict[str, Any]:
    """检查标题前缀和必填章节，不调用网络或判断内容语义。

    Args:
        title: Issue 标题。
        body: Issue 的原始 Markdown 正文。
        templates: 待匹配的 YAML 模板字典列表，不能为空。
    Returns:
        status 为 pass/partial_match/no_match；matched_templates 为完全/部分匹配模板名称列表；
        checks 为各模板的差异，包含 template 名称及 problems 列表，
        每个问题包含 field、reason 和 message。
    """
    if not templates:
        raise ValueError("至少需要一个模板")

    # 本地检查：逐一对照模板的标题前缀、必填章节及占位内容
    sections = _sections(body)
    checks = [] # 记录各模板的检查结果
    matched = [] # 记录候选模板
    has_title_match = False
    for template in templates:
        fields = _fields(template)
        problems = []
        prefix = template.get("title", "")
        # 检查标题前缀
        if not title.startswith(prefix):
            problems.append({"field": "title", "reason": "title_prefix",
                             "message": f"标题应以 {prefix!r} 开头"})
        # 检查正文
        for field in fields:
            # 非必填不检查
            if not field.get("validations", {}).get("required", False):
                continue
            attributes = field["attributes"]
            label = attributes["label"].strip()
            if label not in sections:
                problems.append({"field": label, "reason": "missing_section",
                                 "message": "缺少此必填章节"})
                continue
            content = sections[label]
            placeholders = {"", "_No response_", "No response",
                            (attributes.get("placeholder") or "").strip()}
            if content in placeholders:
                problems.append({"field": label, "reason": "empty_section",
                                 "message": "此必填章节为空或仅含占位内容"})
        checks.append({"template": template["name"], "problems": problems})
        # 记录候选模板
        if prefix and title.startswith(prefix):
            if not has_title_match:
                matched.clear() # 优先保留本次标题匹配的模板
            has_title_match = True
            matched.append(template["name"])
        elif not has_title_match and any(
            field["attributes"]["label"].strip() in sections for field in fields
        ):
            matched.append(template["name"])

    # 返回匹配模板列表和各模板的检查结果
    complete = [check["template"] for check in checks if not check["problems"]] # 检查是否完全匹配
    status = "pass" if complete else "partial_match" if matched else "no_match"
    return {"status": status, "matched_templates": complete or matched, "checks": checks}


def recommend_template(
    title: str, body: str, templates: list[dict[str, Any]],
    agent_api_key: str, model: str = "glm-5.3",
) -> dict[str, Any]:
    """启动一次 Claude Code 任务，由 GLM 推荐模板，不修改格式检查结果。

    Args:
        title: Issue 原始标题。
        body: Issue 原始 Markdown 正文。
        templates: 仓库候选模板字典列表。
        agent_api_key: 供 Claude Code 使用的 GLM Coding Plan Key；为空时不调用模型。
        model: Agent 使用的模型名称，默认 glm-5.3。
    Returns:
        status 为 recommended / uncertain / unavailable，另含 template、reason。
    """
    if not agent_api_key.strip():
        return {"status": "unavailable", "template": None,
                "reason": "未配置 Agent Key，模板推荐未启用，请手动选择模板。"}
    # 将模板信息整理为json传给模型
    candidates = [{
        "name": item["name"], "description": item.get("description", ""),
        "title": item.get("title", ""),
        "fields": [{"label": field["attributes"]["label"],
                    "description": field["attributes"].get("description", ""),
                    "required": field.get("validations", {}).get("required", False)}
                   for field in _fields(item)],
    } for item in templates]

    # 启动子进程运行 Claude Code
    try:
        # 准备 Claude Code 的环境变量
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
               if key in os.environ}
        env.update({
            "ANTHROPIC_AUTH_TOKEN": agent_api_key,
            "ANTHROPIC_BASE_URL": LLM_BASE_URL,
            "API_TIMEOUT_MS": str(LLM_TIMEOUT * 1000),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        })

        # 准备 Claude Code 命令和数据
        command = [
            "claude", "--bare", "-p", "--model", model,
            "--tools", "", "--no-session-persistence", "--max-turns", "3",
            "--output-format", "json",
            "--system-prompt", RECOMMEND_PROMPT.read_text(encoding="utf-8"),
        ]
        data = json.dumps({"issue": {"title": title, "body": body},
                           "templates": candidates}, ensure_ascii=False)

        # 新建子进程和临时目录，运行 Claude Code
        with tempfile.TemporaryDirectory(prefix="issue-gov-") as directory:
            env["CLAUDE_CONFIG_DIR"] = str(Path(directory) / ".claude")
            with subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", cwd=directory, env=env, start_new_session=True,
            ) as process:
                try:
                    output, stderr = process.communicate(data, timeout=LLM_TIMEOUT)
                finally:
                    # 清理子进程
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()

        # 解析输出
        try:
            envelope = json.loads(output)
        except ValueError:
            raise ValueError(stderr.strip() or output.strip() or "Claude Code 未返回结果") from None
        if not isinstance(envelope, dict):
            raise ValueError("Claude Code 返回格式无效")
        if (process.returncode != 0 or envelope.get("type") != "result"
                or envelope.get("subtype") != "success" or envelope.get("is_error") is not False):
            raise ValueError(envelope.get("result") or envelope.get("errors")
                             or stderr.strip() or "Claude Code 调用失败")
        # 解析 Agent 推荐结果
        suggestion = json.loads(envelope["result"])
        name, reason = (suggestion[key] for key in ("template", "reason")) # name：模板名，reason：推荐理由
        if name is not None and (not isinstance(name, str) or name not in {t["name"] for t in templates}):
            raise ValueError("推荐了候选集合外的模板")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("推荐理由无效")
        return {"status": "recommended" if name is not None else "uncertain",
                "template": name, "reason": reason.strip()}
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        detail = str(exc)
        for secret in (agent_api_key, os.environ.get("ATOMGIT_TOKEN", "")):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        detail = " ".join(detail.split())[:500] or "未知错误"
        return {"status": "unavailable", "template": None,
                "reason": f"模板推荐调用失败：{detail}；基础格式检查结果已保留。"}


def _comment_text(value: str) -> str:
    """将模型文本转为评论中的单行文字，避免产生 HTML、链接或提及。

    Args:
        value: 已验证的模板名或理由。
    Returns:
        转义后的单行评论文字。
    """
    value = html.escape(" ".join(value.split()), quote=False).replace("@", "@\u200b")
    return re.sub(r"([\\`*_{\[\]}()!#>|~])", r"\\\1", value)


def format_comment(result: dict[str, Any], repository: str) -> str:
    """将检查结果转换成评论草稿。

    Args:
        result: 格式检查结果，可包含主流程附加的 recommendation。
        repository: 仓库路径，格式为 owner/repo。
    Returns:
        包含隐藏标记、检查结论和格式建议的 Markdown 文本。
    """
    # 撰写检查结果评论
    lines = [MARKER, "### Issue 模板检查", ""]
    recommendation = result.get("recommendation", {})
    if result["status"] == "pass":
        names = "、".join(result["matched_templates"])
        lines.append(f"基础格式检查通过，匹配模板：{names}。")
    else:
        lines.append("未匹配现有模板的完整格式，请按适合的模板整理。")
        # 部分匹配只展示已选候选的问题；完全无匹配才列出全部模板。
        candidates = [
            check for check in result["checks"]
            if result["status"] == "no_match"
            or check["template"] in result["matched_templates"]
        ]
        if recommendation.get("status") == "recommended":
            lines.extend([
                "", "### 模板推荐",
                f"建议使用：{_comment_text(recommendation['template'])}。",
                f"推荐理由：{_comment_text(recommendation['reason'])}",
            ])
            # 只给出模板建议，不把全部候选模板的缺项一起要求作者补齐。
            candidates = []
        elif recommendation:
            prefix = "暂无法确定推荐模板。" if recommendation["status"] == "uncertain" else ""
            lines.extend(["", prefix + _comment_text(recommendation["reason"])])
        for check in candidates:
            lines.extend(["", f"<details><summary>{check['template']}</summary>", ""])
            for problem in check["problems"]:
                lines.append(f"- {problem['field']}：{problem['message']}")
            lines.extend(["", "</details>"])
        url = f"{ATOMGIT_WEB_URL}/{repository}/issues"
        lines.extend(["", f"[前往仓库 Issue 页面]({url})"])
    if recommendation.get("status") in {"recommended", "uncertain"}:
        lines.extend(["", "模板推荐仅供参考；格式检查与推荐均不判断信息是否充分或 bug 是否真实。"])
    else:
        lines.extend(["", "本检查仅验证格式，不判断分类是否正确、信息是否充分或 bug 是否真实。"])
    return "\n".join(lines)


def update_comment(
    repository: str, number: int | str, body: str, atomgit_token: str,
) -> str:
    """查找并更新本工具的机器人评论，没有则新建。

    Args:
        repository: 仓库路径，格式为 owner/repo。
        number: Issue 编号，整数或数字字符串。
        body: 要发布的完整 Markdown 评论文本。
        atomgit_token: AtomGit API 访问令牌。
    Returns:
        created 表示新建，updated 表示更新，unchanged 表示内容未变。
    """
    # 分页查找本工具的评论；相同则跳过，有变化则更新，没有则新建
    path = f"/repos/{repository}/issues/{number}/comments"
    page = 1
    while True:
        comments = atomgit_request(
            "GET", f"{path}?per_page=100&page={page}&direction=asc", atomgit_token,
        )
        for comment in comments:
            author = comment.get("user", {})
            if (author.get("login") == BOT_LOGIN
                    and (comment.get("body") or "").startswith(MARKER)): # 找到本workflow的机器人评论
                if comment["body"] == body:
                    return "unchanged"
                atomgit_request("PATCH", f"/repos/{repository}/issues/comments/{comment['id']}",
                    atomgit_token, {"body": body}) # 更新评论，直接返回
                return "updated"
        if len(comments) < 100:
            break
        page += 1
    atomgit_request("POST", path, atomgit_token, {"body": body}) # 新建评论
    return "created"


def main() -> None:
    """加载配置，执行功能，输出日志和actions摘要。

    Args:
        无。通过环境变量读取平台及模型凭据、模型名、仓库、Issue 编号、模板目录；
        检查后复查 Issue 内容，再发布或更新评论。
    Returns:
        None。标准输出为 JSON；设置 ATOMGIT_STEP_SUMMARY 时另写 Markdown 摘要。
    """

    # 加载配置
    atomgit_token = os.environ["ATOMGIT_TOKEN"]
    agent_api_key = os.environ.get("AGENT_API_KEY", "")
    llm_model = os.environ.get("LLM_MODEL") or "glm-5.3"
    repository = os.environ["REPOSITORY"] # owner/repo
    number = os.environ["ISSUE_NUMBER"] # issue 编号
    templates = load_templates(os.environ.get("TEMPLATE_DIR") or Path(__file__).resolve().parents[2] / "ISSUE_TEMPLATE") # 待匹配的 issue 模板列表

    # 运行功能：执行检查和发布评论
    # 当前处理issue
    if not re.fullmatch(r"[1-9][0-9]*", str(number)):
        raise ValueError("Issue 编号必须是正整数")
    path = f"/repos/{repository}/issues/{number}"

    # 通过 API 获取最新标题和正文
    issue = atomgit_request("GET", path, atomgit_token) # 返回字典，包含 title、body、user 等字段
    if "pull_request" in issue:
        raise ValueError("该编号对应 Pull Request，请输入普通 Issue 编号")

    # 审查issue
    result = check_issue(issue["title"], issue.get("body") or "", templates)

    # 无法匹配模板时，根据内容推荐模板
    if result["status"] == "no_match":
        result["recommendation"] = recommend_template(
            issue["title"], issue.get("body") or "", templates, agent_api_key, llm_model,
        )
    recommendation = result.get("recommendation", {})
    # agent 调用出错时在 Actions 日志中输出警告
    if recommendation.get("status") == "unavailable":
        prefix = "::warning title=Issue Agent::" if os.environ.get("ATOMGIT_ACTIONS") == "true" else ""
        print(prefix + recommendation["reason"].replace("%", "%25"), file=sys.stderr)

    # 生成评论内容
    body = format_comment(result, repository)

    # 发布前复查，内容未变化时发布或更新评论。
    # 发布前再读一次：若作者刚补全正文此时会触发新的workflow，本步骤就跳过不用管了
    latest = atomgit_request("GET", path, atomgit_token)
    if (latest["title"], latest.get("body")) != (issue["title"], issue.get("body")):
        action = "skipped_changed"
    else:
        action = update_comment(repository, number, body, atomgit_token)
    output = {"result": result, "comment_action": action, "comment_body": body}

    # 输出日志；Actions 运行摘要页展示评论草稿
    print(json.dumps(output, ensure_ascii=False))
    summary = os.environ.get("ATOMGIT_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(
            f"评论操作：{output['comment_action']}\n\n{output['comment_body']}\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
