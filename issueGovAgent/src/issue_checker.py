"""Issue 模板检查：本地校验，以及 GitHub 工作流读取和评论反馈。"""

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib import request

import yaml
from markdown_it import MarkdownIt

MARKER = "<!-- issue-gov-agent -->" # 评论中的隐藏标记：配合机器人身份，找到本工具之前发布的评论
BOT_LOGIN = "github-actions[bot]"
GITHUB_WEB_URL = "https://github.com"
GITHUB_API_URL = "https://api.github.com"


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


def github_request(
    method: str, path: str, gh_token: str, data: dict[str, Any] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """使用传入的 Token 调用 GitHub API。
    
    Args:
        method: HTTP 方法，如 GET、POST、PATCH。
        path: 以 / 开头的 API 路径，可包含查询参数。
        gh_token: GitHub API 访问令牌。
        data: 请求体字典；None 表示不发送请求体。
    Returns:
        JSON 响应解析后的字典或字典列表；请求失败时抛出异常。
    """
    headers = {
        "Authorization": "Bearer " + gh_token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    payload = None if data is None else json.dumps(data).encode("utf-8")
    req = request.Request(GITHUB_API_URL + path, data=payload, headers=headers, method=method)
    with request.urlopen(req, timeout=30) as response:
        return json.load(response)


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
        status 为 pass/no_match；matched_templates 为匹配模板名称列表；
        checks 为各模板的差异，包含 template 名称及 problems 列表，
        每个问题包含 field、reason 和 message。
    """
    if not templates:
        raise ValueError("至少需要一个模板")

    # 本地检查：逐一对照模板的标题前缀、必填章节及占位内容
    sections = _sections(body)
    checks = []
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
                                 "message": "缺少此必填章节（不代表正文没有相关信息）"})
                continue
            content = sections[label]
            placeholders = {"", "_No response_", "No response",
                            (attributes.get("placeholder") or "").strip()}
            if content in placeholders:
                problems.append({"field": label, "reason": "empty_section",
                                 "message": "此必填章节为空或仅含占位内容"})
        checks.append({"template": template["name"], "problems": problems})

    # 满足任意一个模板就通过，返回匹配模板列表和各模板的检查结果
    matched = [check["template"] for check in checks if not check["problems"]]
    return {"status": "pass" if matched else "no_match",
            "matched_templates": matched, "checks": checks}


def format_comment(result: dict[str, Any], repository: str) -> str:
    """将检查结果转换成评论草稿。
    
    Args:
        result: check_issue 返回的检查结果。
        repository: 仓库路径，格式为 owner/repo。
    Returns:
        包含隐藏标记、检查结论和格式建议的 Markdown 文本。
    """
    # 撰写检查结果评论
    lines = [MARKER, "### Issue 模板检查", ""]
    if result["status"] == "pass":
        names = "、".join(result["matched_templates"])
        lines.append(f"基础格式检查通过，匹配模板：{names}。")
    else:
        lines.append("未匹配现有模板的完整格式，请按适合的模板整理。")
        # 标题已指定模板时，仅展示对应模板的问题；否则列出各模板供作者选择
        candidates = [
            check for check in result["checks"]
            if not any(p["reason"] == "title_prefix" for p in check["problems"])
        ] or result["checks"]
        for check in candidates:
            lines.extend(["", f"<details><summary>{check['template']}</summary>", ""])
            for problem in check["problems"]:
                lines.append(f"- {problem['field']}：{problem['message']}")
            lines.extend(["", "</details>"])
        url = f"{GITHUB_WEB_URL}/{repository}/issues/new/choose"
        lines.extend(["", f"[查看可用模板]({url})"])
    lines.extend(["", "本检查仅验证格式，不判断分类是否正确、信息是否充分或 bug 是否真实。"])
    return "\n".join(lines)


def update_comment(
    repository: str, number: int | str, body: str, gh_token: str,
) -> str:
    """查找并更新本工具的机器人评论，没有则新建。
    
    Args:
        repository: 仓库路径，格式为 owner/repo。
        number: Issue 编号，整数或数字字符串。
        body: 要发布的完整 Markdown 评论文本。
        gh_token: GitHub API 访问令牌。
    Returns:
        created 表示新建，updated 表示更新，unchanged 表示内容未变。
    """
    # 分页查找本工具的评论；相同则跳过，有变化则更新，没有则新建
    path = f"/repos/{repository}/issues/{number}/comments"
    page = 1
    while True:
        comments = github_request("GET", f"{path}?per_page=100&page={page}", gh_token)
        for comment in comments:
            author = comment.get("user", {})
            if (author.get("login") == BOT_LOGIN and author.get("type") == "Bot"
                    and (comment.get("body") or "").startswith(MARKER)): # 找到本workflow的机器人评论
                if comment["body"] == body:
                    return "unchanged"
                github_request("PATCH", f"/repos/{repository}/issues/comments/{comment['id']}",
                    gh_token, {"body": body}) # 更新评论，直接返回
                return "updated" 
        if len(comments) < 100:
            break
        page += 1
    github_request("POST", path, gh_token, {"body": body}) # 新建评论
    return "created"


def main() -> None:
    """加载配置，执行功能，输出日志和actions摘要。
    
    Args:
        无。通过环境变量读取 Token、仓库、Issue 编号、模板目录及发布开关；
        POST_COMMENT=true 时发布评论，其他值或未设置时仅预览。
    Returns:
        None。标准输出为 JSON；设置 GITHUB_STEP_SUMMARY 时另写 Markdown 摘要。
    """
    
    # 加载配置
    gh_token = os.environ["GH_TOKEN"]
    repository = os.environ["REPOSITORY"] # owner/repo
    number = os.environ["ISSUE_NUMBER"] # issue 编号
    templates = load_templates(os.environ.get("TEMPLATE_DIR", "../.github/ISSUE_TEMPLATE")) # 待匹配的 issue 模板列表
    post_comment = os.environ.get("POST_COMMENT", "false").lower() == "true" # 是否发布评论

    # 运行功能：执行检查和发布评论
    # 当前处理issue
    if not re.fullmatch(r"[1-9][0-9]*", str(number)):
        raise ValueError("Issue 编号必须是正整数")
    path = f"/repos/{repository}/issues/{number}"

    # 通过 API 获取最新标题和正文
    issue = github_request("GET", path, gh_token) # 返回字典，包含 title、body、user 等字段
    if "pull_request" in issue:
        raise ValueError("该编号对应 Pull Request，请输入普通 Issue 编号")

    # 审查issue
    result = check_issue(issue["title"], issue.get("body") or "", templates)

    # 生成评论内容
    body = format_comment(result, repository)

    # 反馈结果：预览或者发布评论
    action = "preview"
    if post_comment:
        # 发布前再读一次：若作者刚补全正文此时会触发新的workflow，本步骤就跳过不用管了
        latest = github_request("GET", path, gh_token)
        if (latest["title"], latest.get("body")) != (issue["title"], issue.get("body")):
            action = "skipped_changed"
        else:
            action = update_comment(repository, number, body, gh_token)
    output = {"result": result, "comment_action": action, "comment_body": body}

    # 打印日志；Actions 运行摘要页展示评论草稿
    print(json.dumps(output, ensure_ascii=False))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(
            f"评论操作：{output['comment_action']}\n\n{output['comment_body']}\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
