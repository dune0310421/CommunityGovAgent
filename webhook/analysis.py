"""组织 Issue 的各项分析，并汇总检查评论。"""

import json
from typing import Any

import yaml

import compliance
from errors import IssueProcessingError


def should_publish_comment(summary: dict, *, previously_commented: bool = False) -> bool:
    """根据分析摘要决定是否允许新增或更新评论。

    Args:
        summary: 分析摘要，包含 status 和 recommendation_status。
        previously_commented: 此前是否已有检查评论或待确认的评论发送记录。
    Returns:
        模板配置故障始终为 False；通过检查时仅在此前有评论时为 True；未通过时为 True。
    """
    if summary.get("status") == "template_error":
        return False
    # 通过检查时，若此前已有评论，则通知“检查通过”，否则不新增评论
    if summary.get("status") == "pass" or (
            summary.get("status") == "no_match" and summary.get("recommendation_status") == "not_applicable"):
        return previously_commented
    # 检查未通过，需要发评论
    return True


def analyze_issue(
    title: str, body: str, template_files: list[dict[str, str]],
    agent_api_key: str, model: str = "", base_url: str = "", *,
    repository: str, number: int, maintainers: tuple[str, ...] = (),
    previously_commented: bool = False,
) -> dict[str, Any]:
    """执行 Issue 分析，生成结果摘要与可直接发布的完整评论。

    Args:
        title: Issue 原始标题。
        body: Issue 原始 Markdown 正文。
        template_files: 模板原文列表，每项含 filename（文件名）、source（来源路径）、
            content（UTF-8 文本）；来源可以是当前仓库或组织公共仓库。
        agent_api_key: 分析使用的模型认证密钥；需要模型参与但密钥为空时抛出可重试错误。
        model: 服务商提供的模型名称。
        base_url: 服务商提供的 Anthropic 兼容接口地址。
        repository: 仓库路径，格式为 owner/repo，用于标识模板缺失的来源。
        number: Issue 编号。
        maintainers: 当前仓库的维护者账号，用于维护者建议的接收人字段。
        previously_commented: 此前是否已有检查评论或待确认的评论发送记录，用于决定通过后是否通知。
    Returns:
        包含 summary 和 comment 的字典：summary 是可保存的结果摘要，
        当前包含 status（最终检查状态）和 recommendation_status（模型复核状态或 None）；
        publish_comment 决定是否新增或更新评论；maintainer_suggestion 为待保存的维护者建议，
        无建议时为 None；
        comment 是汇总各项检查的 Markdown 评论，静默时为空；模板缺失或配置错误时
        status 为 template_error，修复建议只入库。
    Raises:
        IssueProcessingError: 推荐配置、调用或输出异常时重试，不生成评论。
    """

    # -----执行分析-----
    # 规范性检查
    # 1）模板解析
    templates = []
    template_problems = []
    if not template_files:
        template_problems.append({"source": repository,
                                  "message": "未找到可用的 YAML 或 Markdown Issue 模板，请添加模板或检查组织公共模板配置。"})
    for file in template_files:
        try:
            templates.append(compliance.parse_template(file["content"], file["filename"]))
        except yaml.YAMLError:
            template_problems.append({"source": file["source"],
                                      "message": "YAML 语法无效，请检查缩进、引号、括号及标签。"})
        except ValueError as exc:
            template_problems.append({"source": file["source"], "message": str(exc)})
    # 2）根据模板检查issue
    if template_problems:
        # 模板解析失败时记录故障，修复建议只入库。
        result = {"status": "template_error", "template_problems": template_problems}
    else:
        result = compliance.check_issue(title, body, templates)
    # 3）完全匹配直接通过；其余统一由模型选定模板并逐项复核。
    if result["status"] == "no_match":
        result["recommendation"] = compliance.recommend_template(
            title, body, templates, agent_api_key, model, base_url)
        # 模型符合发现是pass时需要改变现有状态
        if result["recommendation"]["status"] == "pass":
            result["status"] = "pass"
    recommendation = result.get("recommendation", {})
    # 4）模板配置故障或现有模板无法覆盖时，记录给维护者的建议
    maintainer_suggestion = None
    if result["status"] == "template_error":
        maintainer_suggestion = {
            "reason": "\n".join(f"{p['source']}：{p['message']}" for p in template_problems),
            "suggestion": "请修复上述仓库模板配置问题。",
            "mtr": json.dumps(maintainers, ensure_ascii=False),
        }
    elif result["status"] == "no_match" and recommendation.get("status") == "not_applicable":
        maintainer_suggestion = {
            "reason": recommendation["reason"],
            "suggestion": recommendation["maintainer"],
            "mtr": json.dumps(maintainers, ensure_ascii=False), # 名单来自当前仓库配置，不能由模型指定；当前只有用户名。
        }

    # -----汇总所有分析结果，生成md评论-----
    # 分析结果摘要
    summary = {"status": result["status"], "recommendation_status": recommendation.get("status")}
    # 是否发布评论
    publish_comment = should_publish_comment(summary, previously_commented=previously_commented)
    # 具体发布内容
    # 规范性检查
    comment = ""
    # 需要新增或更新评论时，才生成内容
    if publish_comment:
        # 统一由规范性检查模块根据结果生成通过通知或未通过说明。
        comment = compliance.format_compliance_check(result, templates=templates)

    return {
        "summary": summary,
        "comment": comment,
        "publish_comment": publish_comment,
        "maintainer_suggestion": maintainer_suggestion
    }
