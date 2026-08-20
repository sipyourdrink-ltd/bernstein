<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/logo-light.svg">
  <img alt="Bernstein" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/logo-light.svg" width="340">
</picture>

<br>

<img alt="Bernstein - deterministic multi-agent CLI orchestration" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/banner-readme.webp" width="820">

<br>

> *"To achieve great things, two things are needed: a plan and not quite enough time."* - [attributed to](https://quoteinvestigator.com/2020/08/19/plan-time/) Leonard Bernstein

### ডিটারমিনিস্টিক মাল্টি-এজেন্ট CLI অর্কেস্ট্রেশন
<!-- l10n: en="deterministic multi-agent CLI orchestration" hash="sha256:0971da0d25ac" -->

[![CI](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/ci.yml/badge.svg)](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bernstein)](https://pypi.org/project/bernstein/)
[![GHCR](https://img.shields.io/badge/ghcr.io-bernstein-2496ed?logo=docker&logoColor=white)](https://ghcr.io/sipyourdrink-ltd/bernstein)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/github/license/sipyourdrink-ltd/bernstein)](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/sipyourdrink-ltd/bernstein/badge)](https://scorecard.dev/viewer/?uri=github.com/sipyourdrink-ltd/bernstein)
[![CodeQL](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/codeql.yml)
[![Open in Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/sipyourdrink-ltd/bernstein?quickstart=1)
[![MCP Toplist](https://mcptoplist.com/badge/io.github.sipyourdrink-ltd%2Fbernstein.svg)](https://mcptoplist.com/server/io.github.sipyourdrink-ltd%2Fbernstein)
<a href="https://deepwiki.com/sipyourdrink-ltd/bernstein"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a>

[website](https://bernstein.run) &middot; [docs](https://bernstein.readthedocs.io/) &middot; [install](https://bernstein.readthedocs.io/en/latest/getting-started/install/) &middot; [first run](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/getting-started/first-run.md) &middot; [glossary](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/GLOSSARY.md) &middot; [limitations](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/KNOWN_LIMITATIONS.md) &middot; [name policy](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md) &middot; [sponsor](https://github.com/sponsors/chernistry)

[简体中文](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.zh-Hans.md) &middot; [繁體中文](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.zh-TW.md) &middot; [日本語](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.ja.md) &middot; [한국어](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.ko.md) &middot; [हिन्दी](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.hi.md) &middot; [বাংলা](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.bn.md) &middot; [Русский](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.ru.md)

</div>

---

> **স্ট্যাটাস: beta।** একজনই মেনটেইন করেন, সক্রিয় ডেভেলপমেন্টে আছে। ভার্সন নম্বর রিলিজ গোনে, পরিপক্বতা নয় — মাইনর ভার্সনেও ইন্টারফেস বদলাতে পারে। যার উপর আপনি নির্ভর করছেন তার ভার্সন পিন করে নিন; রিগ্রেশন দ্রুত সারানো হয়, [সেগুলো জানান](https://github.com/sipyourdrink-ltd/bernstein/issues)।

Bernstein হলো CLI কোডিং এজেন্টের (Claude Code, Codex, Gemini CLI এবং আরও 40টির বেশি) জন্য একটি ডিটারমিনিস্টিক অর্কেস্ট্রেটর। এটি তাদের সমান্তরালে চালায়, তাদের বানানো কাজে গেট বসায়, এবং রানের এতটা রেকর্ড রাখে যে পরে আপনি সেটি যাচাই করতে পারেন। এয়ার-গ্যাপ ইনস্টল প্রোফাইল সঙ্গেই আছে। Apache-2.0।

### এক নজরে
<!-- l10n: en="at a glance" hash="sha256:97aa8e70f076" -->

চারটি জিনিস একে আলাদা করে; এরপরের সবই বিস্তারিত।

- **কোঅর্ডিনেশন লুপে কোনো LLM নেই।** শিডিউলিং সাদামাটা Python, তাই একটি রান শুরু থেকে শেষ পর্যন্ত পুনরুৎপাদনযোগ্য। গতকালের প্ল্যান রিপ্লে করুন, গতকালের টাস্ক গ্রাফই পাবেন।
- **পরে যাচাই করা যায়।** রিপ্লে জার্নাল প্রতিটি রান লিখে রাখে, আর সবসময় চালু লিনিয়েজ স্পাইন প্রতিটি লিনিয়েজ-বাহী ধাপ লিখে রাখে; অপ্ট-ইন HMAC-চেইন অডিট লগ (`BERNSTEIN_AUDIT=1`) এমন রসিদ যোগ করে যা আপনি অফলাইনে যাচাই করতে পারেন। নন-ডিটারমিনিজম ঠিক যে ধাপে ঘটেছে সেখানেই হ্যাশ অমিল হয়ে সামনে আসে, অস্থির রি-রান হিসেবে নয়। কোড নয় এমন ডেলিভারেবলও একই আচরণ পায়: একটি টাস্ক আর্টিফ্যাক্ট কনট্র্যাক্ট (রিপোর্ট, ডেটাসেট, অ্যাকশন লগ, অপ্‌স ফলাফল) ঘোষণা করতে পারে এবং git কমিটের বদলে স্বাক্ষরিত লিনিয়েজ রসিদে সম্পূর্ণ হয়।
- **গঠনগতভাবেই বিচ্ছিন্ন।** প্রতিটি কোডিং টাস্ক মার্জ গেটের পিছনে নিজস্ব git worktree পায়; আর্টিফ্যাক্ট-মোডের টাস্ক `.sdd/workspaces/` এর নিচে একটি ওয়ার্কিং ডিরেক্টরি পায়। ডিফল্টে এজেন্টরা পরিবর্তনযোগ্য কোনো ওয়ার্কস্পেস ভাগ করে না; ভাগ করা অবস্থা কেবল টাস্ক ব্যাকলগ, যা অ্যাটমিকভাবে দাবি করা হয়। এর চেয়ে কড়া ফাইল-সিস্টেম নিয়ন্ত্রণ অপ্ট-ইন, [স্যান্ডবক্স ব্যাকএন্ড](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/sandbox.md) থেকে বেছে নিয়ে। worktree বন্ধ করে দিলে প্রতিটি টাস্ক ভাগ করা চেকআউটেই চলবে।
- **বিস্তৃত এবং লোকাল।** 40টির বেশি CLI এজেন্ট অ্যাডাপ্টার, সঙ্গে একটি জেনেরিক `--prompt` র‍্যাপার, ফাইল-ভিত্তিক স্টেট, কোনো SaaS হপ নেই, কোনো থার্ড-পার্টি ডেটা প্লেন নেই।

পুরো তালিকা আছে [ক্যাপাবিলিটি পাতায়](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md); [ফিচার ম্যাট্রিক্স](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/FEATURE_MATRIX.md) হলো সম্পূর্ণ সূচি।

### 30 সেকেন্ডে ইনস্টল
<!-- l10n: en="install in 30 seconds" hash="sha256:81b04220e0ff" -->

```bash
uv tool install bernstein    # or: pipx install bernstein
bernstein init
bernstein doctor             # checks a CLI agent is installed and authenticated
bernstein -g "fix the failing test in tests/test_foo.py"
```

pipx, pip, brew, dnf, npm এবং Docker আছে [ইনস্টল গাইডে](https://bernstein.readthedocs.io/en/latest/getting-started/install/); এয়ার-গ্যাপড wheelhouse-এর জন্য আলাদা [এয়ার-গ্যাপ গাইড](https://bernstein.readthedocs.io/en/latest/installation/air-gap/) আছে।

<img alt="A real bernstein demo run: mock agents fix four seeded bugs, ending on the run's signed receipt verifying offline" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/demo-run/demo.gif" width="820">

উপরের রেকর্ডিংটি একটি সত্যিকারের রান, এবং সেটি নিজের প্রমাণ সঙ্গে নিয়ে আসে। কাস্ট, ওই রানের জার্নাল থেকে তৈরি স্বাক্ষরিত রান রসিদ, এবং সেটিকে পিন করা পাবলিক কি — সবই [`docs/assets/demo-run/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/docs/assets/demo-run) এ আছে। এইমাত্র যে রানটি দেখলেন, সেটি অফলাইনে যাচাই করুন:

```bash
bernstein verify receipt docs/assets/demo-run/run-receipt.json \
    --public-key docs/assets/demo-run/run-receipt.pub.pem
```

main-এ প্রতিটি push-এ CI কমিট করা রসিদটি আবার যাচাই করে — এবং প্রমাণ করে যে হাত-লাগানো কপি ব্যর্থ হয় — যাতে প্রকাশিত প্রমাণ পচে গিয়ে সাজসজ্জার ফাইল হয়ে না যায়। `scripts/record_demo.sh` রেকর্ডিং, রসিদ ও কি একটি নতুন সত্যিকারের রান থেকে আবার বানায়; টার্মিনালের ভিতরে কিছুই বানানো নয়।

চলমান রান দুটি অপারেটর সারফেসের যেকোনোটি থেকেই দেখা যায়। দুটোই একই টাস্ক API পড়ে, তাই একটি অন্যটির পিছিয়ে থাকা মিরর নয়।

| ![A two-column terminal dashboard - agents with their live logs on the left, the task board on the right - with a full-width activity feed and a cost line underneath](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/tui-agents.png) | ![A browser dashboard listing sixty-two tasks with eleven running, one of them opened to its working-tree diff](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/webui-agents-diffs.png) |
|:---:|:---:|
| `bernstein live` — টার্মিনাল ড্যাশবোর্ড | `bernstein gui serve` — ব্রাউজার ড্যাশবোর্ড |

### একটি রান প্রমাণ করুন
<!-- l10n: en="prove a run" hash="sha256:a97100ca1818" -->

এখানে ডিটারমিনিজম বিশ্বাস করার জিনিস নয়, যাচাই করার জিনিস। অডিট চালু রেখে একবার চালান, তারপর যা লেখা হলো তা যাচাই করুন:

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

জার্নাল প্রতিটি রানেই লেখা হয়; লিনিয়েজ স্পাইন সবসময় চালু থাকে এবং প্রতিটি লিনিয়েজ-বাহী ধাপে একটি এন্ট্রি পায়, তাই ছোট একটি রান বৈধ কিন্তু খালি স্পাইন নিয়েও শেষ হতে পারে। `bernstein audit verify` এর কাছে যাচাই করার মতো চেইন তখনই থাকে যখন রানটি `BERNSTEIN_AUDIT=1`, কোনো কমপ্লায়েন্স প্রিসেট, বা `bernstein run --audit` দিয়ে শুরু হয়েছে। `--audit` ফ্ল্যাগটি `bernstein run` এর; উপরের `bernstein -g` রূপে এনভায়রনমেন্ট ভেরিয়েবলটি সেট করুন।

একটি রান রসিদ জার্নাল হেড, রানটি স্পাইন এন্ট্রি লিখে থাকলে লিনিয়েজ-স্পাইন হেড, এবং অপ্ট-ইনে অডিট-চেইনের একটি রেঞ্জ — সবকিছু একটিমাত্র Ed25519-স্বাক্ষরিত সাবজেক্টের নিচে বাঁধে, পাবলিক কি ভিতরেই থাকে। ওই ফাইল আর অপারেটরের পাবলিক কি হাতে থাকা একজন রিভিউয়ার নিশ্চিত করতে পারেন যে ভিতরে থাকা অ্যাকশন ও চেইন বদলানো হয়নি: HMAC কি লাগে না, জীবন্ত `.sdd/` লাগে না, আর কারচুপি থাকলে এক্সিট `2` প্রথম অমিল ধাপটির নাম বলে দেয়। ওই রসিদ যে জার্নাল অবস্থাকে ধারণ করে সেটিকেই শনাক্ত করে; ওই অবস্থাই যে সম্পূর্ণ ও সমাপ্ত জার্নাল তা প্রমাণ করতে আলাদা একটি স্বাধীন হেড/কাউন্ট সিল লাগে। শুধু ফাইল নিয়ে, `--public-key` পিন ছাড়া, যাচাইটি কেবল ইন্টিগ্রিটির — এটি প্রমাণ করে রসিদটি নিজের ভিতরে সঙ্গতিপূর্ণ, কে স্বাক্ষর করেছে তা নয়, এবং রায়েও সেটাই লেখা থাকে। বিস্তারিত [ডিটারমিনিস্টিক রিপ্লে](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/deterministic-replay.md#signed-run-receipt-one-file-offline-verification) এ।

একই যাচাইযোগ্যতা মূল্যায়নের সংখ্যাগুলোর ক্ষেত্রেও খাটে। `bernstein bench run <suite> --reliability k` (একে `bernstein eval --reliability k` ও লেখা যায়) প্রতিটি টাস্ক নির্দিষ্ট কোঅর্ডিনেশনের অধীনে `k` বার চালায়, তারপর `pass@1` সিলিং-এর পাশাপাশি একটি `pass^k` মেঝে (সবগুলো `k` চেষ্টাই পাস করতে হবে) জানায়। ওই ফলাফল একটি স্বাক্ষরিত রসিদে সিল করা হয় যা `bernstein bench reliability-verify` অফলাইনে আবার হিসাব করে, তাই বানানো মেঝে যাচাইয়ে ধরা পড়ে। বিস্তারিত: [pass^k নির্ভরযোগ্যতার মেঝে](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/eval/reliability.md)।

### এটি কীভাবে কাজ করে
<!-- l10n: en="how it works" hash="sha256:f818df2e6cbb" -->

প্রতিটি লক্ষ্য চারটি ধাপ পেরোয়:

1. **ভাগ করা।** ম্যানেজার আপনার লক্ষ্যকে টাস্কে ভাগ করে, প্রতিটির সঙ্গে রোল, নিজস্ব ফাইল আর সমাপ্তির সংকেত দেয়। একটি LLM কল, তারপর থেকে সাদামাটা Python।
2. **স্পন করা।** এজেন্টরা বিচ্ছিন্ন [git worktree](https://git-scm.com/docs/git-worktree) এ চালু হয়, প্রতি কোডিং টাস্কে একটি; আর্টিফ্যাক্ট-মোডের টাস্ক এর বদলে একটি সাধারণ ওয়ার্কিং ডিরেক্টরি পায়। main ব্রাঞ্চ পরিষ্কার থাকে।
3. **যাচাই করা।** জ্যানিটর মূর্ত সংকেত পরীক্ষা করে: টেস্ট পাস হয়, ফাইল আছে, lint পরিষ্কার, টাইপ ঠিক।
4. **মার্জ করা।** যাচাই হওয়া কাজ main-এ নামে। ব্যর্থ টাস্ক আবার চেষ্টা করা হয় বা অন্য মডেলের কাছে পাঠানো হয়।

শিডিউলার কেন সাদামাটা Python, আর তার বিনিময়ে কী ছাড়া হলো: [কেন ডিটারমিনিস্টিক](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/WHY_DETERMINISTIC.md)।

### রোজকার কমান্ড
<!-- l10n: en="everyday commands" hash="sha256:b3520027ef7d" -->

```bash
cd your-project
bernstein init                    # creates .sdd/ workspace, bernstein.yaml + templates/
bernstein -g "Add rate limiting"  # agents spawn, work in parallel, verify, exit
bernstein live                    # watch progress in the TUI dashboard
bernstein run plan.yaml           # multi-stage plan: skip LLM planning, execute directly
bernstein stop                    # graceful shutdown with drain
```

পুরো অপারেটর সারফেস (PR অটোমেশন, শিডিউল, চ্যাট ব্রিজ, autofix ডিমন) আছে [অপারেটর কমান্ডে](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/commands.md)।

রিপোজিটরি হাইজিন গেট: `bernstein readme-l10n verify` এমন PR ব্যর্থ করে যার অনূদিত README ইংরেজি উৎস থেকে সরে গেছে (এবং বাসি সেকশনটির নাম বলে দেয়), `bernstein readme-l10n sync` ইংরেজিতে সম্পাদনার পরে সেগুলো আবার বেঁধে দেয়। দেখুন [readme-l10n](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/playbooks/readme-l10n.md)।

### সমর্থিত এজেন্ট
<!-- l10n: en="supported agents" hash="sha256:8c94b4cde068" -->

Claude Code, Codex CLI, Gemini CLI, GitHub Copilot CLI, Cursor, Aider, Goose, Muse Code, OpenAI Agents SDK, Amp, Cody, Continue, Devin Terminal, Junie, Kilo, Kiro, AWS Q Developer, Ollama, OpenCode, OpenHands, Open Interpreter, gptme, Plandex, AIChat, Letta Code, Qwen এবং আরও অনেক। [অ্যাডাপ্টার সূচি](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/adapters/index.md) তাদের মধ্যে 30টির ইনস্টল কমান্ড রাখে। `bernstein integrations list` `src/bernstein/adapters/registry.py` থেকে জুড়ে দেওয়া 51টি ইন্টিগ্রেশনই তালিকাভুক্ত করে — কী রিজলভ হয় তার একমাত্র উৎস ওই ফাইলটিই। এর মধ্যে 49টি বেছে নেওয়ার মতো এজেন্ট অ্যাডাপ্টার; বাকি দুটি সারি হলো `mock` টেস্ট স্টাব আর `self-hosted-endpoints` এন্ডপয়েন্ট প্রোফাইল। `--prompt` ফ্ল্যাগ আছে এমন বাকি যেকোনো কিছু জেনেরিক র‍্যাপারের মধ্য দিয়ে চলে।

একই রানে এজেন্ট মেশান: বয়লারপ্লেটের জন্য সস্তা লোকাল মডেল, আর্কিটেকচারের জন্য ভারী ক্লাউড মডেল। `bernstein integrations list --installed` দেখায় আপনার মেশিনে কী কী আছে।

### প্রথম পাতার বাইরে
<!-- l10n: en="beyond the front page" hash="sha256:ee01fbaaebd6" -->

গভীরের সবকিছু থাকে [ডকুমেন্টেশন সাইটে](https://bernstein.readthedocs.io/):

| পাতা | কী আছে |
|---|---|
| [ক্যাপাবিলিটি](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md) | পূর্ণ ক্যাপাবিলিটি তালিকা: MCP সার্ভার মোড, স্বাক্ষরিত এজেন্ট কার্ড, স্যান্ডবক্স ব্যাকএন্ড, আর্টিফ্যাক্ট সিংক, নিয়ন্ত্রক ম্যাপিং |
| [এটি কাদের জন্য](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/use-cases.md) | কোথায় এর মূল্য দাঁড়ায়, আর কোথায় Bernstein ভুল হাতিয়ার |
| [ওয়ার্কফ্লো](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/workflow-manifests.md) | এজেন্ট / কমান্ড / লুপ নোডের ডিক্লারেটিভ YAML DAG |
| [ওয়েব UI](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/gui/index.md) | TUI যে API ব্যবহার করে, সেই একই API-র উপর ব্রাউজার ড্যাশবোর্ড |
| [ক্লাউড এক্সিকিউশন](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/cloudflare/cloudflare-overview.md) | পরীক্ষামূলক: নিজের অ্যাকাউন্টে Cloudflare Workers-এ এজেন্ট চালান, R2 ওয়ার্কস্পেস সিংক সহ। হোস্ট করা `api.bernstein.run` সার্ভিসটি এখনও পাওয়া যাচ্ছে না |
| [ডেটাসোর্স](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/datasources.md) | শুধু-পড়ার কোয়েরি রসিদ, সঙ্গে এমন একটি কোয়েরি ড্রাইভার যা প্রতিটি ফলাফলকে যে স্কিমা স্ন্যাপশট থেকে সেটি এসেছে তার সঙ্গে বেঁধে দেয় |
| [এজেন্ট ক্যাটালগ](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/agent-catalogs.md) | ভূমিকাগুলিকে বিল্ট-ইন টেমপ্লেটের বাইরের এজেন্ট সংজ্ঞার দিকে নির্দেশ করুন — একটি সাধারণ YAML/SKILL.md ডিরেক্টরি, বা Claude Code প্লাগইন-লেআউট ট্রি |
| [নিরাপত্তা](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/security.md) | স্কোরকার্ড, ফাজিং, হার্ডেনিং |
| [আর্কিটেকচার](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/ARCHITECTURE.md) | ভিতরে এটি কীভাবে কাজ করে |

### নামটি কেন?
<!-- l10n: en="why the name?" hash="sha256:2444448b9e03" -->

Bernstein নামটি এসেছে মার্কিন কন্ডাক্টর ও সুরকার লিওনার্ড বার্নস্টাইন থেকে। এই প্রকল্প CLI কোডিং এজেন্টদের দলটিকে ঠিক সেভাবেই পরিচালনা করে যেভাবে বার্নস্টাইন নিউ ইয়র্ক ফিলহারমোনিক পরিচালনা করতেন: প্রত্যেক বাদক নিজের সংকেতে, স্কোর ডিটারমিনিস্টিক, আর ফলাফলের দায় কন্ডাক্টরের।

i wrote bernstein because i was paying $400/month in claude bills running three coding agents in parallel and getting nondeterministic merges. Apache 2.0, একজনই মেনটেইন করেন। লাইভ পরিসংখ্যান: [bernstein.run](https://bernstein.run)।

### যেখানে উল্লেখ হয়েছে
<!-- l10n: en="mentioned in" hash="sha256:e1dfaf62cb5d" -->

[vinta/awesome-python](https://github.com/vinta/awesome-python) এ তালিকাভুক্ত, Augment Code-এর [ওপেন-সোর্স এজেন্ট অর্কেস্ট্রেটর](https://www.augmentcode.com/tools/open-source-agent-orchestrators) সংকলনে আলোচিত, এবং [Python Weekly #742](https://www.pythonweekly.com/p/python-weekly-issue-742-april-23-2026) এ তালিকাভুক্ত। এই পদ্ধতিটি আমরা awesome-agentic-patterns-এ [deterministic zero-LLM orchestration](https://github.com/nibzard/awesome-agentic-patterns/blob/main/patterns/deterministic-zero-llm-orchestration.md) প্যাটার্ন হিসেবেও লিখেছি।

<details>
<summary>সব উল্লেখ: 20টির বেশি awesome তালিকা, ডিরেক্টরি, নিউজলেটার ও অন্য প্রকল্পের উদ্ধৃতি</summary>
<br>

প্রতিটি awesome-list এন্ট্রি, ক্যাটালগ তালিকাভুক্তি, পূর্বকাজ হিসেবে উদ্ধৃতি ও নিউজলেটার উল্লেখ সহ পুরো ট্র্যাক করা তালিকাটি আছে [docs/mentions.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/mentions.md) এ। যেমন যেমন আসে, এন্ট্রি যোগ হতে থাকে; সংশোধন issue বা PR-এ স্বাগত।

</details>

### অবদান, সহায়তা, লাইসেন্স
<!-- l10n: en="contributing, support, license" hash="sha256:94b6541e4b15" -->

PR স্বাগত; সেটআপ ও কোড স্টাইল আছে [CONTRIBUTING.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md) এ। নিরাপত্তা রিপোর্ট যায় [SECURITY.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/SECURITY.md) এর পথে। Bernstein আপনার সময় বাঁচালে: [GitHub Sponsors](https://github.com/sponsors/chernistry)। যোগাযোগ: [forte@bernstein.run](mailto:forte@bernstein.run)।

উদ্ধৃতির মেটাডেটা আছে [CITATION.cff](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CITATION.cff) এ। লাইসেন্স: [Apache-2.0](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE); প্রকল্পের নামটি আলাদাভাবে [TRADEMARKS.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md) এ বলা আছে।

---

[Alex Chernysh](https://alexchernysh.com) &middot; [GitHub](https://github.com/chernistry) &middot; [X](https://x.com/alex_chernysh) &middot; [bernstein.run](https://bernstein.run)

<!-- mcp-name: io.github.sipyourdrink-ltd/bernstein -->
