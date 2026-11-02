# CommunityGovAgent

社区智能化治理智能体

## Issue 模板检查

在 GitHub Issue 新建或修改时，对照仓库模板检查标题与必填内容，没有任何模板匹配时调用 Agent 推荐模板。然后发布或更新一条检查结果评论。

使用流程和实现说明见 [Issue 模板格式检查](issueGovAgent/README.md)。

相关文件：

```text
CommunityGovAgent/
├── .github/
│   ├── ISSUE_TEMPLATE/          # 缺陷、文档、需求和咨询模板
│   └── workflows/
│       └── issue-compliance.yml # 检查工作流
├── issueGovAgent/
│   ├── src/
│   │   └── issue_checker.py    # 模板检查、GitHub 请求及评论处理
│   ├── prompts/                # 模板推荐提示词
│   ├── tests/                  # 本地测试
│   ├── requirements.txt        # Python 依赖
│   └── README.md               # 流程、实现和使用说明
└── README.md
```
