<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/logo-light.svg">
  <img alt="Bernstein" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/logo-light.svg" width="340">
</picture>

<br>

<img alt="Bernstein - deterministic multi-agent CLI orchestration" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/banner-readme.png" width="820">

<br>

> *"To achieve great things, two things are needed: a plan and not quite enough time."* - Leonard Bernstein

### 确定性多智能体 CLI 编排
<!-- l10n: en="deterministic multi-agent CLI orchestration" hash="sha256:71266dcc2820" -->

[![CI](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/ci.yml/badge.svg)](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bernstein)](https://pypi.org/project/bernstein/)
[![GHCR](https://img.shields.io/badge/ghcr.io-bernstein-2496ed?logo=docker&logoColor=white)](https://ghcr.io/sipyourdrink-ltd/bernstein)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/github/license/sipyourdrink-ltd/bernstein)](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/sipyourdrink-ltd/bernstein/badge)](https://scorecard.dev/viewer/?uri=github.com/sipyourdrink-ltd/bernstein)
[![CodeQL](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/codeql.yml)
[![Open in Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/sipyourdrink-ltd/bernstein?quickstart=1)
[![MCP Toplist](https://mcptoplist.com/badge/io.github.sipyourdrink-ltd%2Fbernstein.svg)](https://mcptoplist.com/server/io.github.sipyourdrink-ltd%2Fbernstein)

[website](https://bernstein.run) &middot; [docs](https://bernstein.readthedocs.io/) &middot; [install](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/getting-started/install.md) &middot; [first run](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/getting-started/first-run.md) &middot; [glossary](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/GLOSSARY.md) &middot; [limitations](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/KNOWN_LIMITATIONS.md) &middot; [name policy](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md) &middot; [sponsor](https://github.com/sponsors/chernistry)

[简体中文](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.zh-Hans.md) &middot; [繁體中文](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.zh-TW.md)

</div>

---

> **状态：beta。** 由单人维护，正在积极开发中。版本号计的是发布次数，而非成熟度——次版本（minor）可能改变接口。凡有依赖请锁定版本；回归问题会被尽快修复，[欢迎提交](https://github.com/sipyourdrink-ltd/bernstein/issues)。

Bernstein 是一个面向 CLI 编码智能体（Claude Code、Codex、Gemini CLI 以及 40 多个其他智能体）的确定性编排器。调度是纯 Python——协调循环中没有 LLM——因此运行可以端到端复现。每个编码任务都在自己的 git worktree 中运行，背后有 lint/type/test 门禁；产物模式（artifact-mode）任务以签署的血统收据（lineage receipt）而非提交来宣告完成，获得一个普通的工作目录。结果事后仍可核查：常驻的血统脊柱（lineage spine）和回放日志（replay journal），外加可选的 HMAC 链式审计日志（`BERNSTEIN_AUDIT=1`），其收据可离线验证。包含离线安装（air-gap）配置。Apache-2.0 许可。

### 一览
<!-- l10n: en="at a glance" hash="sha256:bfd131192bf6" -->

有四件事让它与众不同；其余都是细节。

- **协调循环中没有 LLM。** 调度是纯 Python，因此运行可以端到端复现。重放昨天的计划，得到昨天的任务图。
- **事后可核查。** 血统脊柱和回放日志记录每一次运行；可选的审计链增加了可离线验证的收据。不确定性会在精确步骤处以哈希失配的形式浮出水面，而不是一次偶发的重跑。非代码交付物也享受同样待遇：任务可以在计划步骤、积压条目或任务 CLI 上声明产物契约（报告、数据集、动作日志、运维结果），并以签署的血统收据而非 git 提交来宣告完成。
- **构造上即隔离。** 每个编码任务在合并门禁之后获得自己的 git worktree；产物模式任务在 `.sdd/workspaces/` 下获得工作目录隔离。在这种默认隔离下，智能体之间没有共享的可变状态；超出该隔离的文件系统强制是可选的，来自[沙箱后端](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/sandbox.md)（禁用 worktree 会在共享检出中运行每个任务）。
- **广泛且本地。** 40 多个 CLI 智能体适配器，外加通用的 `--prompt` 包装器、基于文件的状态、无 SaaS 跳转、无第三方数据平面。

完整列表见[能力页面](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md)；[功能矩阵](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/FEATURE_MATRIX.md)是详尽的索引。

### 30 秒安装
<!-- l10n: en="install in 30 seconds" hash="sha256:30f872dea647" -->

```bash
pipx install bernstein
bernstein init
bernstein -g "fix the failing test in tests/test_foo.py"
```

pip、uv、brew、dnf、npm、Docker 以及离线 wheelhouse 都涵盖在[安装指南](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/getting-started/install.md)中。

<img alt="A real bernstein demo run: mock agents fix four seeded bugs in parallel worktrees, ending on the run's signed receipt verifying offline" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/demo-run/demo.gif" width="820">

上面的录像是真实运行，并且自带证明：录制文件、那次精确运行产生的签署运行收据，以及将其钉死的公钥，一起存放在 [`docs/assets/demo-run/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/docs/assets/demo-run) 中。离线验证你刚看到的运行：

```bash
bernstein verify receipt docs/assets/demo-run/run-receipt.json \
    --public-key docs/assets/demo-run/run-receipt.pub.pem
```

CI 在每次推送时重新验证已提交的收据——并证明被篡改的副本会失败——因此已发布的证据不会腐烂成装饰性文件。`scripts/record_demo.sh` 从一次全新的真实运行重新生成录制、收据和密钥；终端里没有任何内容是合成的。

运行中的任务可从任一操作界面观看。两者读取同一个任务 API，因此彼此都不是对方的滞后镜像。

| ![A three-column terminal dashboard: agents with their live logs on the left, the task board on the right, an activity feed and a cost line underneath](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/tui-agents.png) | ![A browser dashboard listing sixty-two tasks with eleven running, one of them opened to its working-tree diff](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/webui-agents-diffs.png) |
|:---:|:---:|
| `bernstein live` — 终端仪表盘 | `bernstein gui serve` — 浏览器中查看同一运行 |

### 证明一次运行
<!-- l10n: en="prove a run" hash="sha256:9472ce86e140" -->

这里的确定性是需要你去核查的东西，而不是凭空相信。启用审计运行一次，然后验证记录的内容：

```bash
BERNSTEIN_AUDIT=1 bernstein -g "fix the failing test in tests/test_foo.py"
bernstein replay list                 # run ids recorded on disk
bernstein replay latest --verify      # recompute the journal head, name the first divergent step
bernstein lineage verify <run_id>     # recompute the always-on lineage spine
bernstein audit verify                # HMAC chain + Merkle seal (written because audit was enabled)
bernstein audit diagnose <run_id> --signal gate --sign-key KEY
                                      # name the exact step a failure entered the run, as a signed receipt
bernstein verify run <run_id> --signing-key-path key.pem   # sign one portable run receipt
bernstein verify receipt .sdd/runs/<run_id>/run-receipt.json  # verify it offline: file only
```

日志和血统脊柱在每次运行时写入。`bernstein audit verify` 只有在运行以 `BERNSTEIN_AUDIT=1`、合规预设或 `bernstein run --audit` 启动时才有链可查。`--audit` 旗标属于 `bernstein run`；在上面的 `bernstein -g` 形式中，请设置环境变量。

运行收据在一个 Ed25519 签署主体下绑定日志头部和血统脊柱头部（外加可选的审计链范围），并内嵌公钥，因此持有文件与操作者公钥的审查者可以确认记录的动作正是实际执行的动作——无需 HMAC 密钥，无需活跃的 `.sdd/`，篡改时以退出码 `2` 命名第一个分歧步骤。仅凭文件（不钉 `--public-key`）时，检查只是完整性检查：它证明收据内部自洽，而非谁签署的，结论也会如实说明。详见[确定性回放](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/deterministic-replay.md#signed-run-receipt-one-file-offline-verification)。

同样的可核查性适用于评估数字：`bernstein bench run <suite> --reliability k`（也写作 `bernstein eval --reliability k`）在固定协调下把每个任务运行 `k` 次，并在签署的收据中报告 `pass^k` 下限（所有 `k` 次尝试都必须通过）以及 `pass@1` 上限，`bernstein bench reliability-verify` 可离线重算该收据——伪造的下限会验证失败。详情：[pass^k 可靠性下限](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/eval/reliability.md)。

### 工作原理
<!-- l10n: en="how it works" hash="sha256:f818df2e6cbb" -->

每个目标经历四个阶段：

1. **分解（Decompose）**。管理者把你的目标分解为带角色、归属文件和完成信号的任务。一次 LLM 调用，然后全是纯 Python。
2. **孵化（Spawn）**。智能体在隔离的 [git worktrees](https://git-scm.com/docs/git-worktree) 中启动，每个编码任务一个；产物模式任务获得普通工作目录。主分支保持干净。
3. **验证（Verify）**。janitor 检查具体信号：测试通过、文件存在、lint 干净、类型正确。
4. **合并（Merge）**。验证过的工作落入 main。失败的任务被重试或路由到不同模型。

调度器为什么是纯 Python，以及这换来什么代价：[为什么确定性](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/WHY_DETERMINISTIC.md)。

### 日常命令
<!-- l10n: en="everyday commands" hash="sha256:097c6c2e0ddf" -->

```bash
cd your-project
bernstein init                    # creates .sdd/ workspace + bernstein.yaml
bernstein -g "Add rate limiting"  # agents spawn, work in parallel, verify, exit
bernstein live                    # watch progress in the TUI dashboard
bernstein run plan.yaml           # multi-stage plan: skip LLM planning, execute directly
bernstein stop                    # graceful shutdown with drain
```

完整的操作界面（PR 自动化、定时任务、聊天桥接、autofix 守护进程）见[操作命令](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/commands.md)。

### 支持的智能体
<!-- l10n: en="supported agents" hash="sha256:e8c85ea6fd82" -->

Claude Code、Codex CLI、Gemini CLI、GitHub Copilot CLI、Cursor、Aider、Goose、OpenAI Agents SDK、Amp、Cody、Continue、Devin Terminal、Junie、Kilo、Kiro、AWS Q Developer、Ollama、OpenCode、OpenHands、Open Interpreter、gptme、Plandex、AIChat、Letta Code、Qwen 等等。[适配器索引](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/adapters/index.md)为其中 29 个提供安装命令；`bernstein integrations list` 从 `src/bernstein/adapters/registry.py` 中的注册表枚举全部 50 个已接线适配器，该文件是"什么能解析"的唯一事实来源；`src/bernstein/adapters/use_cases.py` 为每个适配器提供面向终端用户的文案。任何带 `--prompt` 旗标的其他工具都可以通过通用包装器工作。

在同一运行中混用智能体：用便宜的本地模型处理样板，用更重的云模型处理架构。`bernstein integrations list --installed` 显示你的机器上可用的内容。

### 首页之外
<!-- l10n: en="beyond the front page" hash="sha256:0420eb016a43" -->

所有深入内容都在[文档站点](https://bernstein.readthedocs.io/)上：

| | |
|---|---|
| [capabilities](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md) | 完整能力列表：MCP 服务器模式、签署的智能体卡片、沙箱后端、产物存储、监管映射 |
| [who this is for](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/use-cases.md) | 价值落在哪里，以及 Bernstein 何时是错误工具 |
| [workflows](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/workflow-manifests.md) | 智能体/命令/循环节点的声明式 YAML DAG |
| [web UI](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/gui/index.md) | 与 TUI 使用同一 API 的浏览器仪表盘 |
| [cloud execution](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/cloudflare/cloudflare-overview.md) | 实验性：在你的账户上通过 R2 workspace 同步在 Cloudflare Workers 上运行智能体。托管的 `api.bernstein.run` 服务尚不可用 |
| [datasources](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/datasources.md) | 只读查询收据，外加把每个结果绑定到其推导时所依据的 schema 快照的查询驱动 |
| [security](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/security.md) | scorecard、模糊测试、加固 |
| [architecture](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/ARCHITECTURE.md) | 底层工作原理 |

### 为什么叫这个名字？
<!-- l10n: en="why the name?" hash="sha256:3c98a35004a1" -->

Bernstein 得名于美国指挥家、作曲家 Leonard Bernstein。这个项目像 Bernstein 指挥纽约爱乐乐团那样编排一支 CLI 编码智能体队伍：每个乐手准时到位，乐谱确定，指挥对结果负责。他就是这个项目名字所源自的那位原初编排者。

我写 bernstein 是因为我每月为并行运行三个编码智能体支付 400 美元的 claude 账单，却得到不确定性的合并。Apache 2.0，单人维护。实时数据：[bernstein.run](https://bernstein.run)。

### 被提及的地方
<!-- l10n: en="mentioned in" hash="sha256:e79981346792" -->

收录于 [vinta/awesome-python](https://github.com/vinta/awesome-python)，被 Augment Code 的[开源智能体编排器](https://www.augmentcode.com/tools/open-source-agent-orchestrators)综述提及，被 [awesome-agentic-patterns](https://github.com/nibzard/awesome-agentic-patterns/blob/main/patterns/deterministic-zero-llm-orchestration.md) 引用为确定性零 LLM 编排的生产实现，登上 [Python Weekly #742](https://www.pythonweekly.com/p/python-weekly-issue-742-april-23-2026)，并在一个十仓库的 [Claude Code 智能体系统剖析](https://x.com/Granite0x/status/2080665298609328201)中被列为编排层。

<details>
<summary>全部覆盖：20 多个 awesome 列表、目录、通讯和同行引用</summary>
<br>

完整跟踪列表，包括每一条 awesome-list 条目、目录收录、先前引用和通讯提及，都在 [docs/mentions.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/mentions.md) 中。条目出现即添加；欢迎通过 issue 或 PR 更正。

</details>

### 贡献、支持与许可
<!-- l10n: en="contributing, support, license" hash="sha256:94b6541e4b15" -->

欢迎 PR；[CONTRIBUTING.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md) 有环境搭建和代码风格。安全报告通过 [SECURITY.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/SECURITY.md) 提交。如果 Bernstein 为你节省了时间：[GitHub Sponsors](https://github.com/sponsors/chernistry)。联系：[forte@bernstein.run](mailto:forte@bernstein.run)。

引用元数据位于 [CITATION.cff](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CITATION.cff)。许可：[Apache-2.0](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE)；项目名在 [TRADEMARKS.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md) 中单独说明。

---

[Alex Chernysh](https://alexchernysh.com) &middot; [GitHub](https://github.com/chernistry) &middot; [X](https://x.com/alex_chernysh) &middot; [bernstein.run](https://bernstein.run)

<!-- mcp-name: io.github.sipyourdrink-ltd/bernstein -->
