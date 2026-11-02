# CommunityGovAgent

社区智能化治理智能体

## Issue 模板检查

检查 Issue 的标题与必填内容；没有任何模板匹配时，由 Agent 推荐模板。检查完成后发布或更新评论。

- [GitHub 检查与使用说明](.github/scripts/issue_gov/README.md)
- [AtomGit 检查与使用说明](.gitcode/scripts/issue_gov/README.md)

```text
CommunityGovAgent/
├── .github/
│   ├── ISSUE_TEMPLATE/              # GitHub 模板
│   ├── workflows/issue-compliance.yml
│   └── scripts/issue_gov/           # GitHub 脚本、Prompt、依赖和测试
├── .gitcode/
│   ├── workflows/issue-compliance.yml
│   └── scripts/issue_gov/           # AtomGit 脚本、Prompt、依赖和测试
└── README.md
```

AtomGit 部署时需使用目标仓库实际的 `.gitcode/ISSUE_TEMPLATE/` 模板。
