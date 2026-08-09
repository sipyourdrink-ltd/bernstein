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

### 確定性多代理 CLI 編排
<!-- l10n: en="deterministic multi-agent CLI orchestration" hash="sha256:d0cc91d44434" -->

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

Bernstein 是面向 CLI 編碼代理（Claude Code、Codex、Gemini CLI 以及 40 多個其他代理）的確定性編排器。排程是純 Python——協調迴圈中沒有 LLM——因此執行可以端到端重現。每個編碼任務都在自己的 git worktree 中執行，背後有 lint/type/test 門禁；產物模式（artifact-mode）任務以簽署的血統收據（lineage receipt）而非提交來宣告完成，獲得一個普通的工作目錄。結果事後仍可核查：常駐的血統脊柱（lineage spine）和重播日誌（replay journal），外加選用的 HMAC 鏈式稽核日誌（`BERNSTEIN_AUDIT=1`），其收據可離線驗證。包含離線安裝（air-gap）設定。Apache-2.0 授權。

### 一覽
<!-- l10n: en="at a glance" hash="sha256:bfd131192bf6" -->

有四件事讓它與眾不同；其餘都是細節。

- **協調迴圈中沒有 LLM。** 排程是純 Python，因此執行可以端到端重現。重播昨天的計畫，得到昨天的任務圖。
- **事後可核查。** 血統脊柱和重播日誌記錄每一次執行；選用的稽核鏈增加了可離線驗證的收據。不確定性會在精確步驟處以雜湊失配的形式浮出水面，而不是一次偶發的重跑。非程式碼交付物也享有同樣待遇：任務可以在計畫步驟、積壓條目或任務 CLI 上宣告產物契約（報告、資料集、動作日誌、維運結果），並以簽署的血統收據而非 git 提交來宣告完成。
- **構造上即隔離。** 每個編碼任務在合併門禁之後獲得自己的 git worktree；產物模式任務在 `.sdd/workspaces/` 下獲得工作目錄隔離。在這種預設隔離下，代理之間沒有共享的可變狀態；超出該隔離的檔案系統強制是選用的，來自[沙箱後端](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/sandbox.md)（停用 worktree 會在共享檢出中執行每個任務）。
- **廣泛且本地。** 40 多個 CLI 代理介面卡，外加通用的 `--prompt` 包裝器、基於檔案的狀態、無 SaaS 跳轉、無第三方資料平面。

完整清單見[能力頁面](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md)；[功能矩陣](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/FEATURE_MATRIX.md)是詳盡的索引。

### 30 秒安裝
<!-- l10n: en="install in 30 seconds" hash="sha256:30f872dea647" -->

```bash
pipx install bernstein
bernstein init
bernstein -g "fix the failing test in tests/test_foo.py"
```

pip、uv、brew、dnf、npm、Docker 以及離線 wheelhouse 都涵蓋在[安裝指南](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/getting-started/install.md)中。

<img alt="A real bernstein demo run: mock agents fix four seeded bugs in parallel worktrees, ending on the run's signed receipt verifying offline" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/demo-run/demo.gif" width="820">

上面的錄影是真實執行，並且自帶證明：錄製檔、那次精確執行產生的簽署執行收據，以及將其釘死的公開金鑰，一起存放在 [`docs/assets/demo-run/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/docs/assets/demo-run) 中。離線驗證你剛看到的執行：

```bash
bernstein verify receipt docs/assets/demo-run/run-receipt.json \
    --public-key docs/assets/demo-run/run-receipt.pub.pem
```

CI 在每次推送時重新驗證已提交的收據——並證明被竄改的副本會失敗——因此已發布的證據不會腐化成裝飾性檔案。`scripts/record_demo.sh` 從一次全新的真實執行重新產生錄影、收據和金鑰；終端機裡沒有任何內容是合成的。

執行中的任務可從任一操作介面觀看。兩者讀取同一個任務 API，因此彼此都不是對方的落後鏡像。

| ![A three-column terminal dashboard: agents with their live logs on the left, the task board on the right, an activity feed and a cost line underneath](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/tui-agents.png) | ![A browser dashboard listing sixty-two tasks with eleven running, one of them opened to its working-tree diff](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/webui-agents-diffs.png) |
|:---:|:---:|
| `bernstein live` — 終端機儀表板 | `bernstein gui serve` — 在瀏覽器中查看同一執行 |

### 證明一次執行
<!-- l10n: en="prove a run" hash="sha256:9472ce86e140" -->

這裡的確定性是你要去核查的東西，而不是憑空相信。啟用稽核執行一次，然後驗證記錄的內容：

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

日誌和血統脊柱在每次執行時寫入。`bernstein audit verify` 只有在執行以 `BERNSTEIN_AUDIT=1`、合規預設或 `bernstein run --audit` 啟動時才有鏈可查。`--audit` 旗標屬於 `bernstein run`；在上面的 `bernstein -g` 形式中，請設定環境變數。

執行收據在一個 Ed25519 簽署主體下綁定日誌頭部和血統脊柱頭部（外加選用的稽核鏈範圍），並內嵌公開金鑰，因此持有檔案與操作者公開金鑰的審查者可以確認記錄的動作正是實際執行的動作——無需 HMAC 金鑰，無需活躍的 `.sdd/`，竄改時以退出碼 `2` 命名第一個分歧步驟。僅憑檔案（不釘 `--public-key`）時，檢查只是完整性檢查：它證明收據內部自洽，而非誰簽署的，結論也會如實說明。詳見[確定性重播](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/deterministic-replay.md#signed-run-receipt-one-file-offline-verification)。

同樣的可核查性適用於評估數字：`bernstein bench run <suite> --reliability k`（也寫作 `bernstein eval --reliability k`）在固定協調下把每個任務執行 `k` 次，並在簽署的收據中報告 `pass^k` 下限（所有 `k` 次嘗試都必須通過）以及 `pass@1` 上限，`bernstein bench reliability-verify` 可離線重算該收據——偽造的下限會驗證失敗。詳情：[pass^k 可靠性下限](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/eval/reliability.md)。

### 運作原理
<!-- l10n: en="how it works" hash="sha256:f818df2e6cbb" -->

每個目標經歷四個階段：

1. **分解（Decompose）**。管理者把你的目標分解為帶角色、歸屬檔案和完成訊號的任務。一次 LLM 呼叫，然後全是純 Python。
2. **孵化（Spawn）**。代理在隔離的 [git worktrees](https://git-scm.com/docs/git-worktree) 中啟動，每個編碼任務一個；產物模式任務獲得普通工作目錄。主分支保持乾淨。
3. **驗證（Verify）**。janitor 檢查具體訊號：測試通過、檔案存在、lint 乾淨、型別正確。
4. **合併（Merge）**。驗證過的工作落入 main。失敗的任務被重試或路由到不同模型。

排程器為什麼是純 Python，以及這換來什麼代價：[為什麼確定性](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/WHY_DETERMINISTIC.md)。

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

完整的操作介面（PR 自動化、定時任務、聊天橋接、autofix 守護行程）見[操作命令](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/commands.md)。

### 支援的代理
<!-- l10n: en="supported agents" hash="sha256:e8c85ea6fd82" -->

Claude Code、Codex CLI、Gemini CLI、GitHub Copilot CLI、Cursor、Aider、Goose、OpenAI Agents SDK、Amp、Cody、Continue、Devin Terminal、Junie、Kilo、Kiro、AWS Q Developer、Ollama、OpenCode、OpenHands、Open Interpreter、gptme、Plandex、AIChat、Letta Code、Qwen 等等。[介面卡索引](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/adapters/index.md)為其中 29 個提供安裝命令；`bernstein integrations list` 從 `src/bernstein/adapters/registry.py` 中的登錄檔列舉全部 50 個已接線介面卡，該檔案是「什麼能解析」的唯一事實來源；`src/bernstein/adapters/use_cases.py` 為每個介面卡提供面向終端使用者的文案。任何帶 `--prompt` 旗標的其他工具都可以透過通用包裝器運作。

在同一執行中混用代理：用便宜的本地模型處理樣板，用更重的雲端模型處理架構。`bernstein integrations list --installed` 顯示你的機器上可用的內容。

### 首頁之外
<!-- l10n: en="beyond the front page" hash="sha256:0420eb016a43" -->

所有深入內容都在[文件網站](https://bernstein.readthedocs.io/)上：

| | |
|---|---|
| [capabilities](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md) | 完整能力清單：MCP 伺服器模式、簽署的代理卡片、沙箱後端、產物儲存、監管對映 |
| [who this is for](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/use-cases.md) | 價值落在哪裡，以及 Bernstein 何時是錯誤工具 |
| [workflows](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/workflow-manifests.md) | 代理/命令/迴圈節點的宣告式 YAML DAG |
| [web UI](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/gui/index.md) | 與 TUI 使用同一 API 的瀏覽器儀表板 |
| [cloud execution](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/cloudflare/cloudflare-overview.md) | 實驗性：在你的帳戶上透過 R2 workspace 同步在 Cloudflare Workers 上執行代理。託管的 `api.bernstein.run` 服務尚不可用 |
| [datasources](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/datasources.md) | 唯讀查詢收據，外加把每個結果綁定到其推導時所依據的 schema 快照的查詢驅動 |
| [security](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/security.md) | scorecard、模糊測試、強化 |
| [architecture](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/ARCHITECTURE.md) | 底層運作原理 |

### 為什麼叫這個名字？
<!-- l10n: en="why the name?" hash="sha256:3c98a35004a1" -->

Bernstein 得名於美國指揮家、作曲家 Leonard Bernstein。這個專案像 Bernstein 指揮紐約愛樂樂團那樣編排一支 CLI 編碼代理隊伍：每個樂手準時到位，樂譜確定，指揮對結果負責。他就是這個專案名字所源自的那位原初編排者。

我寫 bernstein 是因為我每月為並行執行三個編碼代理支付 400 美元的 claude 帳單，卻得到不確定性的合併。Apache 2.0，單人維護。即時資料：[bernstein.run](https://bernstein.run)。

### 被提及的地方
<!-- l10n: en="mentioned in" hash="sha256:e79981346792" -->

收錄於 [vinta/awesome-python](https://github.com/vinta/awesome-python)，被 Augment Code 的[開源代理編排器](https://www.augmentcode.com/tools/open-source-agent-orchestrators)綜述提及，被 [awesome-agentic-patterns](https://github.com/nibzard/awesome-agentic-patterns/blob/main/patterns/deterministic-zero-llm-orchestration.md) 引用為確定性零 LLM 編排的生產實作，登上 [Python Weekly #742](https://www.pythonweekly.com/p/python-weekly-issue-742-april-23-2026)，並在一個十倉庫的 [Claude Code 代理系統剖析](https://x.com/Granite0x/status/2080665298609328201)中被列為編排層。

<details>
<summary>全部涵蓋：20 多個 awesome 清單、目錄、通訊和同儕引用</summary>
<br>

完整追蹤清單，包括每一條 awesome-list 條目、目錄收錄、先前引用和通訊提及，都在 [docs/mentions.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/mentions.md) 中。條目出現即添加；歡迎透過 issue 或 PR 更正。

</details>

### 貢獻、支援與授權
<!-- l10n: en="contributing, support, license" hash="sha256:94b6541e4b15" -->

歡迎 PR；[CONTRIBUTING.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md) 有環境搭建和程式碼風格。安全報告透過 [SECURITY.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/SECURITY.md) 提交。如果 Bernstein 為你節省了時間：[GitHub Sponsors](https://github.com/sponsors/chernistry)。聯絡：[forte@bernstein.run](mailto:forte@bernstein.run)。

引用中繼資料位於 [CITATION.cff](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CITATION.cff)。授權：[Apache-2.0](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE)；專案名稱在 [TRADEMARKS.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md) 中單獨說明。

---

[Alex Chernysh](https://alexchernysh.com) &middot; [GitHub](https://github.com/chernistry) &middot; [X](https://x.com/alex_chernysh) &middot; [bernstein.run](https://bernstein.run)

<!-- mcp-name: io.github.sipyourdrink-ltd/bernstein -->
