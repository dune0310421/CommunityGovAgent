# Issue Webhook 服务（AtomGit）

## 功能

当前实现 Issue 事件接收、格式检查、模型语义复核与模板推荐、评论反馈功能。

输入：AtomGit 发送的 Issue 新建、标题或正文修改事件。

输出：需要贡献者修改 Issue 时发表评论；首次检查通过保持静默，此前已有检查评论则通知本次通过。沿用相邻两次修改间隔不超过 3 分钟时更新已有评论、超过 3 分钟时新增评论的规则。模板配置故障保持静默，维护者建议单独入库。

## 使用方式

### 部署到服务器

需要已有 Python 3.11+ 的 Linux 机器和可写目录，普通账号即可，无需 sudo。服务默认监听 `0.0.0.0:7000`，可在 `runtime/.env` 中通过 `WEBHOOK_PORT` 修改端口；服务器及网络需允许外部连接所配置的 TCP 端口。

以下路径均相对于 **community-gov-agent 仓库根目录**。

```text
community-gov-agent/
└── webhook/
    ├── receiver.py
    ├── worker.py
    ├── atomgit.py
    ├── analysis.py
    ├── compliance.py
    ├── comment_templates.yml
    ├── errors.py
    ├── recommend_template.md
    ├── manage.py
    ├── requirements.txt
    ├── .env.example
    ├── supervisord.conf
    └── runtime/                 # 实际配置和运行数据，初始化及运行时生成，不提交git
        ├── .env                 # 实际配置、令牌和 Webhook 密码
        ├── repository.yml      # 接入仓库及维护者，初始化生成注释示例后自行填写
        ├── data/                # SQLite 任务数据库
        ├── logs/                # 服务日志
        └── run/                 # 进程编号和本机管理接口
```

1. 将 `webhook/` 目录复制到运行服务的机器。
2. 使用运行服务的同一账号，在 Bash 终端先检查已有环境：

```bash
command -v node npm claude
node --version
npm --version
claude --version

# nvm 是 shell 函数；未加载时再检查它的安装目录
if command -v nvm >/dev/null 2>&1; then
    nvm --version
elif [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
    echo "已找到 nvm，尚未加载到当前终端"
else
    echo "当前终端及默认目录中未找到 nvm"
fi
```

`command not found` 表示当前终端找不到该命令，不一定是整台服务器未安装。按检查结果选择下面的安装步骤：

- **已有可运行的 Claude Code**：先复用，跳过 nvm、Node.js 和 Claude Code 安装段。现有版本与下方指定的 `2.1.316` 不同时，仍需验证模型推荐流程；能输出版本号不代表全部参数和模型调用已验证兼容。
- **没有 Claude Code，但已有 Node.js 22+ 和 npm**：跳过 nvm 和 Node.js 安装段，只安装 Claude Code。
- **没有 Claude Code，且缺少上述 Node.js/npm 环境**：按下方命令准备后再安装；已有 nvm 时跳过其下载安装两行，直接加载并安装 Node.js 22。

4. 在仓库根目录执行以下命令，按上述判断跳过不需要的安装段。下载安装 nvm 需要 `curl`，这套用户目录安装方式无需 sudo。**后续命令均在 `webhook/` 目录执行。**

```bash
cd webhook

# 安装 Python 依赖
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 可选：仅缺少 Claude Code 且需要准备 Node.js/npm 时执行本段
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# 已有 nvm 时跳过下面两行
mkdir -p "$NVM_DIR"
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
. "$NVM_DIR/nvm.sh"
nvm install 22
nvm alias default 22
nvm use 22
node --version

# 可选：仅缺少 Claude Code 时安装，供模型推荐使用
npm install -g @anthropic-ai/claude-code@2.1.316
claude --version

# 初始化服务配置
.venv/bin/python manage.py init
```

初始化会创建 `runtime/` 及其子目录，同时生成 `.env`（含随机 Webhook 密码）和 `repository.yml`（仅含注释示例）。已有 `.env` 只补充 `.env.example` 中新增的配置项，保留已有值及自定义配置；已有 `repository.yml` 不覆盖。

### 配置服务

1. 在本目录的 `runtime/.env` 中配置：

| 配置项 | 类型 | 是否必填 | 说明 |
| --- | --- | --- | --- |
| `WEBHOOK_SECRET` | 密码 | 是 | 初始化时自动生成，各接入仓库的 WebHook 页面填写相同值 |
| `ATOMGIT_TOKEN` | 访问令牌 | 是 | 有权读取接入仓库、Issue 和模板，并创建评论的个人访问令牌 |
| `AGENT_API_KEY` | 模型密钥 | 是 | Claude Code 兼容接口的密钥，使用 Bearer 认证 |
| `LLM_BASE_URL` | 模型接口地址 | 是 | 服务商提供的 Anthropic 兼容接口地址 |
| `LLM_MODEL` | 模型名称 | 是 | 该接口支持的模型名称 |

| `WEBHOOK_DB` | 数据库路径 | 否 | 默认 `./runtime/data/events.sqlite3`，相对于 `webhook/`，需位于可写的本地磁盘 |
| `WEBHOOK_PORT` | 监听端口 | 否 | 默认 `7000`，范围 `1`–`65535`；修改后需 `stop` 再 `start` |


通常只需填写**令牌**和**大模型相关配置**

访问令牌的最小权限需求如下：

| 权限项 | 需求 |
| --- | --- |
| 仓库、Issue | 只读 |
| Notes | 读取评论以核对发送结果，写入以新增或更新评论 |
| 其余权限 | 禁止 |

2. 在本目录的 `runtime/repository.yml` 中填写全部接入仓库及其维护者，注释示例可保留，在下方另写实际仓库和账号（生效配置不带 `#`），示例：

```yaml
opencann/community-gov-agent:
  - qiurq1

qiurq1/issue-agent-test:
  - qiurq1

cann/infrastructure: []
```

文件中的仓库均接入服务；无需 @ 维护者时填写 `[]`，未列出的仓库不接入。至少填写一个仓库。


3. 之后**每次修改 `.env` 和 `repository.yml` 后需要 restart 服务才能生效**；`WEBHOOK_PORT` 例外，修改后需 `stop` 再 `start`。

### 运行

1. 启动接收服务：

```bash
.venv/bin/python manage.py start
```

启动命令会等待 Supervisor 管理的接收服务和任务处理程序都显示 `RUNNING`，然后输出“服务已启动”并退出，当前终端可以继续使用；如果未能就绪则报错。后台服务继续运行，子进程异常退出会自动重启。

启动后，用浏览器打开 `http://服务器公网IPv4:7000/health`，显示 `{"status":"ok"}` 表示接收服务可访问。Webhook 接收地址为 `http://服务器公网IPv4:7000/webhook`，它接收带密码的 POST 请求，不是供浏览器打开的网页。以后配置域名和 HTTPS 转发后，将接收地址替换为对应域名即可。

2. 在 `repository.yml` 列出的每个仓库中，进入 **项目设置 → WebHook → 新建 WebHook**，填写以下内容：

| 项目 | 填写内容 |
| --- | --- |
| URL | `http://服务器公网IPv4:7000/webhook`；配置 HTTPS 域名后使用对应域名地址|
| 认证方式 | 选择 **WebHook 密码**，不要选择“签名密钥” |
| 密码 | 填写服务器 `runtime/.env` 中的 `WEBHOOK_SECRET` |
| POST 内容类型 | `application/json` |
| 事件类型 | 仅勾选「Issue 事件」|
| 启用状态 | 勾选「激活」 |

3. 日常管理：

以下命令均在 `webhook/` 目录执行。

**命令列表**

```bash
.venv/bin/python manage.py init             # 首次部署时生成配置
.venv/bin/python manage.py start            # 启动服务
.venv/bin/python manage.py status           # 查看进程状态和任务记录
.venv/bin/python manage.py logs             # 持续查看日志，Ctrl+C 退出查看
.venv/bin/python manage.py restart          # 平滑更新：更新代码及配置，自动补充配置、重启后台并平滑重载接收端
.venv/bin/python manage.py stop             # 停止全部服务
.venv/bin/python manage.py retry-failed     # 将全部失败任务重新排队
```

**场景**

- **首次部署**：执行 `init`，填写 `runtime/.env` 和接入仓库配置 `runtime/repository.yml`，再执行 `start`。
- **查看运行情况或任务结果**：执行 `status`，查看进程状态、待处理、正在检查、失败和已完成的任务数量、最近五条任务、评论处理结果及错误原因。查看完即退出，不修改任务；服务停止后仍可查看已保存的记录。“已完成”不代表格式通过，是否通过及是否新增、更新评论需结合处理结果和日志查看。
- **排查问题或观察处理过程**：执行 `logs`；按 `Ctrl+C` 只退出日志查看，不停止服务。
- **修改配置模板、配置内容、维护者名单或应用代码**：执行 `restart`。若提示配置无效，填写后再执行 `restart`；更新代码时保留 `runtime/`。
- **更换 Python、依赖及 `supervisord.conf`**：执行 `stop`，完成更新后执行 `start`。停机期间无法接收通知；恢复启动后不会主动补查遗漏的 Issue。
- **修复问题后重新处理失败任务**：执行 `retry-failed`；若后台尚未运行，先执行 `start`。任务会重新排队，由后台处理。
- **机器重启后恢复服务**：执行 `start`，后台会恢复已入库的 Webhook 任务。如果机器会在注销时清理用户进程，需要使用该机器允许的常驻任务方式。

## 工作流程

处理一条通知的顺序：

```text
有人新建 Issue，或修改它的标题、正文
  → AtomGit 向我们部署的服务发送通知
  → 服务核对密码，确认这个仓库和这次操作需要检查
  → 记下仓库和 Issue 编号，将同一 Issue 尚未完成的旧请求标为已覆盖；重复通知不再入队
  → 告诉 AtomGit“通知已收到”，随后处理检查任务
  → 读取 Issue 和这个仓库默认分支中的模板
  → 服务器检查格式，完全匹配直接通过；其余情况调用模型选择适用模板并逐项语义复核
  → 3 分钟内连续修改且有检查评论：更新原评论，包括通过结果
  → 没有可更新评论时：首次通过则静默；此前有检查评论则新增通过通知；未通过新增评论
  → 模板配置故障：保持静默，修复建议只入库
  → 现有模板均不适用：维护者建议与已检查标记一起入库
      → 处理成功：记录检查结果和评论操作（静默通过为 suppressed）
      → 暂时失败：等一会再试，多次尝试仍失败就记录原因，等待处理
```
