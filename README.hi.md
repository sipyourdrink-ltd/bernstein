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

### डिटर्मिनिस्टिक मल्टी-एजेंट CLI ऑर्केस्ट्रेशन
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

> **स्थिति: beta।** अकेले मेंटेन किया जा रहा है, सक्रिय विकास में। वर्ज़न नंबर रिलीज़ गिनता है, परिपक्वता नहीं — माइनर वर्ज़न में भी इंटरफ़ेस बदल सकते हैं। जिस पर आप निर्भर हैं उसका वर्ज़न पिन कर लें; रिग्रेशन जल्दी ठीक होते हैं, [उन्हें दर्ज करें](https://github.com/sipyourdrink-ltd/bernstein/issues)।

Bernstein, CLI कोडिंग एजेंट्स (Claude Code, Codex, Gemini CLI और 40 से ज़्यादा) के लिए एक डिटर्मिनिस्टिक ऑर्केस्ट्रेटर है। यह उन्हें समानांतर चलाता है, उनके बनाए काम पर गेट लगाता है, और रन का इतना रिकॉर्ड रखता है कि बाद में आप उसे जाँच सकें। एयर-गैप इंस्टॉल प्रोफ़ाइल साथ आती है। Apache-2.0।

### एक नज़र में
<!-- l10n: en="at a glance" hash="sha256:97aa8e70f076" -->

चार चीज़ें इसे अलग करती हैं; उसके बाद सब विवरण है।

- **कोऑर्डिनेशन लूप में कोई LLM नहीं।** शेड्यूलिंग सादा Python है, इसलिए रन शुरू से आख़िर तक दोहराया जा सकता है। कल का प्लान रिप्ले कीजिए, कल वाला ही टास्क ग्राफ़ मिलेगा।
- **बाद में जाँचा जा सकता है।** रिप्ले जर्नल हर रन दर्ज करता है, और हमेशा चालू रहने वाली लीनिएज स्पाइन हर लीनिएज-वाहक स्टेप दर्ज करती है; ऑप्ट-इन HMAC-चेन ऑडिट लॉग (`BERNSTEIN_AUDIT=1`) ऐसी रसीदें जोड़ता है जिन्हें आप ऑफ़लाइन सत्यापित कर सकते हैं। नॉन-डिटर्मिनिज़्म ठीक उसी स्टेप पर हैश मिसमैच बनकर सामने आता है, न कि किसी अस्थिर री-रन के रूप में। ग़ैर-कोड डिलिवरेबल के साथ भी यही बर्ताव है: कोई टास्क आर्टिफ़ैक्ट कॉन्ट्रैक्ट (रिपोर्ट, डेटासेट, ऐक्शन लॉग, ऑप्स नतीजा) घोषित कर सकता है और git कमिट के बजाय हस्ताक्षरित लीनिएज रसीद पर पूरा होता है।
- **बनावट से ही अलग-थलग।** हर कोडिंग टास्क को मर्ज गेट के पीछे अपना git worktree मिलता है; आर्टिफ़ैक्ट-मोड टास्क को `.sdd/workspaces/` के नीचे एक वर्किंग डायरेक्टरी मिलती है। डिफ़ॉल्ट रूप से एजेंट कोई बदलने-योग्य वर्कस्पेस साझा नहीं करते; साझा अवस्था सिर्फ़ टास्क बैकलॉग है, जिसे एटॉमिक ढंग से क्लेम किया जाता है। इससे सख़्त फ़ाइल-सिस्टम पाबंदी ऑप्ट-इन है, [सैंडबॉक्स बैकएंड](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/sandbox.md) से चुनकर। worktree बंद कर दीजिए तो हर टास्क साझा चेकआउट में चलेगा।
- **व्यापक और लोकल।** 40 से ज़्यादा CLI एजेंट अडैप्टर, साथ में एक जेनेरिक `--prompt` रैपर, फ़ाइल-आधारित स्टेट, कोई SaaS हॉप नहीं, कोई थर्ड-पार्टी डेटा प्लेन नहीं।

पूरी सूची [कैपेबिलिटी पेज](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md) पर है; [फ़ीचर मैट्रिक्स](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/FEATURE_MATRIX.md) संपूर्ण अनुक्रमणिका है।

### 30 सेकंड में इंस्टॉल
<!-- l10n: en="install in 30 seconds" hash="sha256:81b04220e0ff" -->

```bash
uv tool install bernstein    # or: pipx install bernstein
bernstein init
bernstein doctor             # checks a CLI agent is installed and authenticated
bernstein -g "fix the failing test in tests/test_foo.py"
```

pipx, pip, brew, dnf, npm और Docker [इंस्टॉल गाइड](https://bernstein.readthedocs.io/en/latest/getting-started/install/) में हैं; एयर-गैप्ड wheelhouse की अपनी अलग [एयर-गैप गाइड](https://bernstein.readthedocs.io/en/latest/installation/air-gap/) है।

<img alt="A real bernstein demo run: mock agents fix four seeded bugs, ending on the run's signed receipt verifying offline" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/demo-run/demo.gif" width="820">

ऊपर की रिकॉर्डिंग एक असली रन है, और वह अपना सबूत साथ लेकर आती है। कास्ट, उस रन के जर्नल से बनी हस्ताक्षरित रन रसीद, और उसे पिन करने वाली पब्लिक की — सब [`docs/assets/demo-run/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/docs/assets/demo-run) में हैं। अभी-अभी आपने जो रन देखा, उसे ऑफ़लाइन सत्यापित कीजिए:

```bash
bernstein verify receipt docs/assets/demo-run/run-receipt.json \
    --public-key docs/assets/demo-run/run-receipt.pub.pem
```

main पर हर push पर CI कमिट की गई रसीद दोबारा सत्यापित करता है — और यह भी साबित करता है कि छेड़छाड़ की गई कॉपी फ़ेल होती है — ताकि प्रकाशित सबूत सड़कर सजावटी फ़ाइल न बन जाए। `scripts/record_demo.sh` रिकॉर्डिंग, रसीद और की को एक नए असली रन से दोबारा बनाता है; टर्मिनल के भीतर कुछ भी बनावटी नहीं है।

चल रहे रन को दोनों में से किसी भी ऑपरेटर सरफ़ेस से देखा जा सकता है। दोनों एक ही टास्क API पढ़ते हैं, इसलिए कोई एक दूसरे का पिछड़ा हुआ मिरर नहीं है।

| ![A two-column terminal dashboard - agents with their live logs on the left, the task board on the right - with a full-width activity feed and a cost line underneath](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/tui-agents.png) | ![A browser dashboard listing sixty-two tasks with eleven running, one of them opened to its working-tree diff](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/webui-agents-diffs.png) |
|:---:|:---:|
| `bernstein live` — टर्मिनल डैशबोर्ड | `bernstein gui serve` — ब्राउज़र डैशबोर्ड |

### रन को साबित कीजिए
<!-- l10n: en="prove a run" hash="sha256:a97100ca1818" -->

यहाँ डिटर्मिनिज़्म भरोसे की चीज़ नहीं, जाँचने की चीज़ है। ऑडिट चालू करके एक बार चलाइए, फिर जो दर्ज हुआ उसे सत्यापित कीजिए:

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

जर्नल हर रन पर लिखा जाता है; लीनिएज स्पाइन हमेशा चालू रहती है और हर लीनिएज-वाहक स्टेप पर एक एंट्री पाती है, इसलिए कोई छोटा रन वैध लेकिन ख़ाली स्पाइन के साथ भी ख़त्म हो सकता है। `bernstein audit verify` के पास जाँचने को चेन तभी होती है जब रन `BERNSTEIN_AUDIT=1`, किसी कम्प्लायंस प्रीसेट, या `bernstein run --audit` के साथ शुरू हुआ हो। `--audit` फ़्लैग `bernstein run` का है; ऊपर वाले `bernstein -g` रूप में एनवायरनमेंट वेरिएबल सेट कीजिए।

एक रन रसीद जर्नल हेड को, और अगर रन ने स्पाइन एंट्रियाँ लिखी हों तो लीनिएज-स्पाइन हेड को, और ऑप्ट-इन पर ऑडिट-चेन की एक रेंज को, एक ही Ed25519-हस्ताक्षरित सब्जेक्ट के नीचे बाँधती है, जिसमें पब्लिक की अंतर्निहित होती है। वह फ़ाइल और ऑपरेटर की पब्लिक की रखने वाला समीक्षक पुष्टि कर सकता है कि उसमें दर्ज ऐक्शन और चेन बदले नहीं गए: न HMAC की चाहिए, न ज़िंदा `.sdd/`, और छेड़छाड़ पर एग्ज़िट `2` पहले भिन्न स्टेप का नाम बता देता है। वह रसीद उसी जर्नल अवस्था की पहचान करती है जिसे वह समेटे हुए है; यह साबित करने के लिए कि वह अवस्था पूरा और समाप्त जर्नल है, अलग से एक स्वतंत्र हेड/काउंट सील चाहिए। सिर्फ़ फ़ाइल के साथ और `--public-key` पिन के बिना जाँच केवल इंटीग्रिटी की होती है — यह साबित करती है कि रसीद अपने भीतर संगत है, यह नहीं कि हस्ताक्षर किसने किया, और नतीजा यही कहता भी है। विवरण [डिटर्मिनिस्टिक रिप्ले](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/deterministic-replay.md#signed-run-receipt-one-file-offline-verification) में।

यही जाँच-योग्यता मूल्यांकन के आँकड़ों पर भी लागू होती है। `bernstein bench run <suite> --reliability k` (जिसे `bernstein eval --reliability k` भी लिखा जाता है) हर टास्क को तय कोऑर्डिनेशन के तहत `k` बार चलाता है, फिर `pass@1` की ऊपरी सीमा के साथ-साथ `pass^k` निचली सीमा (सभी `k` कोशिशें पास होनी चाहिए) बताता है। वह नतीजा एक हस्ताक्षरित रसीद में सील होता है जिसे `bernstein bench reliability-verify` ऑफ़लाइन दोबारा गिनता है, इसलिए गढ़ी हुई निचली सीमा सत्यापन में गिर जाती है। विवरण: [pass^k विश्वसनीयता की निचली सीमा](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/eval/reliability.md)।

### यह काम कैसे करता है
<!-- l10n: en="how it works" hash="sha256:f818df2e6cbb" -->

हर लक्ष्य चार चरणों से गुज़रता है:

1. **विघटन।** मैनेजर आपके लक्ष्य को टास्क में बाँटता है, हर एक के साथ रोल, अपनी फ़ाइलें और पूर्णता-संकेत देता है। एक LLM कॉल, उसके बाद सादा Python।
2. **स्पॉन।** एजेंट अलग-थलग [git worktree](https://git-scm.com/docs/git-worktree) में शुरू होते हैं, प्रति कोडिंग टास्क एक; आर्टिफ़ैक्ट-मोड टास्क को इसके बजाय एक सादी वर्किंग डायरेक्टरी मिलती है। main ब्रांच साफ़ रहती है।
3. **सत्यापन।** जैनिटर ठोस संकेत जाँचता है: टेस्ट पास होते हैं, फ़ाइलें मौजूद हैं, lint साफ़ है, टाइप सही हैं।
4. **मर्ज।** सत्यापित काम main में उतरता है। फ़ेल हुए टास्क दोबारा आज़माए जाते हैं या किसी दूसरे मॉडल को भेज दिए जाते हैं।

शेड्यूलर सादा Python क्यों है, और इसके बदले क्या छोड़ा गया: [क्यों डिटर्मिनिस्टिक](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/WHY_DETERMINISTIC.md)।

### रोज़मर्रा के कमांड
<!-- l10n: en="everyday commands" hash="sha256:b3520027ef7d" -->

```bash
cd your-project
bernstein init                    # creates .sdd/ workspace, bernstein.yaml + templates/
bernstein -g "Add rate limiting"  # agents spawn, work in parallel, verify, exit
bernstein live                    # watch progress in the TUI dashboard
bernstein run plan.yaml           # multi-stage plan: skip LLM planning, execute directly
bernstein stop                    # graceful shutdown with drain
```

पूरा ऑपरेटर सरफ़ेस (PR ऑटोमेशन, शेड्यूल, चैट ब्रिज, autofix डीमन) [ऑपरेटर कमांड](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/commands.md) में है।

रिपॉज़िटरी हाइजीन गेट: `bernstein readme-l10n verify` उस PR को फ़ेल करता है जिसके अनूदित README अंग्रेज़ी स्रोत से भटक गए हों (और बासी सेक्शन का नाम बताता है), `bernstein readme-l10n sync` अंग्रेज़ी में बदलाव के बाद उन्हें दोबारा बाँध देता है। देखिए [readme-l10n](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/playbooks/readme-l10n.md)।

### समर्थित एजेंट
<!-- l10n: en="supported agents" hash="sha256:8c94b4cde068" -->

Claude Code, Codex CLI, Gemini CLI, GitHub Copilot CLI, Cursor, Aider, Goose, Muse Code, OpenAI Agents SDK, Amp, Cody, Continue, Devin Terminal, Junie, Kilo, Kiro, AWS Q Developer, Ollama, OpenCode, OpenHands, Open Interpreter, gptme, Plandex, AIChat, Letta Code, Qwen और भी कई। [अडैप्टर इंडेक्स](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/adapters/index.md) उनमें से 30 के इंस्टॉल कमांड रखता है। `bernstein integrations list` `src/bernstein/adapters/registry.py` से जुड़े हुए सभी 51 इंटीग्रेशन गिनाता है — क्या रिज़ॉल्व होता है, इसका यही इकलौता स्रोत है। उनमें 49 चुनने-योग्य एजेंट अडैप्टर हैं; बाक़ी दो पंक्तियाँ `mock` टेस्ट स्टब और `self-hosted-endpoints` एंडपॉइंट प्रोफ़ाइल हैं। `--prompt` फ़्लैग वाली बाक़ी कोई भी चीज़ जेनेरिक रैपर से चलती है।

एक ही रन में एजेंट मिलाइए: बॉयलरप्लेट के लिए सस्ते लोकल मॉडल, आर्किटेक्चर के लिए भारी क्लाउड मॉडल। `bernstein integrations list --installed` दिखाता है कि आपकी मशीन पर क्या उपलब्ध है।

### पहले पन्ने से आगे
<!-- l10n: en="beyond the front page" hash="sha256:ee01fbaaebd6" -->

गहराई की हर चीज़ [डॉक्स साइट](https://bernstein.readthedocs.io/) पर रहती है:

| पेज | इसमें क्या है |
|---|---|
| [कैपेबिलिटी](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md) | पूरी कैपेबिलिटी सूची: MCP सर्वर मोड, हस्ताक्षरित एजेंट कार्ड, सैंडबॉक्स बैकएंड, आर्टिफ़ैक्ट सिंक, नियामक मैपिंग |
| [यह किसके लिए है](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/use-cases.md) | कहाँ इसका मूल्य बनता है, और कहाँ Bernstein ग़लत औज़ार है |
| [वर्कफ़्लो](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/workflow-manifests.md) | एजेंट / कमांड / लूप नोड्स के घोषणात्मक YAML DAG |
| [वेब UI](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/gui/index.md) | उसी API पर चलने वाला ब्राउज़र डैशबोर्ड जिस पर TUI चलता है |
| [क्लाउड एग्ज़ीक्यूशन](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/cloudflare/cloudflare-overview.md) | प्रयोगात्मक: अपने ही अकाउंट पर Cloudflare Workers में एजेंट चलाइए, R2 वर्कस्पेस सिंक के साथ। होस्टेड `api.bernstein.run` सेवा अभी उपलब्ध नहीं है |
| [डेटासोर्स](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/datasources.md) | केवल-पढ़ने वाली क्वेरी रसीदें, और एक क्वेरी ड्राइवर जो हर नतीजे को उस स्कीमा स्नैपशॉट से बाँधता है जिससे वह निकला |
| [एजेंट कैटलॉग](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/agent-catalogs.md) | भूमिकाओं को बिल्ट-इन टेम्पलेट्स के बाहर की एजेंट परिभाषाओं पर लगाइए — एक सामान्य YAML/SKILL.md डायरेक्टरी, या Claude Code प्लगइन-लेआउट ट्री |
| [सुरक्षा](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/security.md) | स्कोरकार्ड, फ़ज़िंग, हार्डनिंग |
| [आर्किटेक्चर](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/ARCHITECTURE.md) | अंदर यह कैसे काम करता है |

### यह नाम क्यों?
<!-- l10n: en="why the name?" hash="sha256:2444448b9e03" -->

Bernstein का नाम अमेरिकी कंडक्टर और संगीतकार लियोनार्ड बर्नस्टीन पर रखा गया है। यह प्रोजेक्ट CLI कोडिंग एजेंट्स की टोली को ठीक वैसे संचालित करता है जैसे बर्नस्टीन न्यूयॉर्क फ़िलहार्मोनिक को संचालित करते थे: हर वादक अपने संकेत पर, स्कोर डिटर्मिनिस्टिक, और कंडक्टर नतीजे के लिए जवाबदेह।

i wrote bernstein because i was paying $400/month in claude bills running three coding agents in parallel and getting nondeterministic merges. Apache 2.0, अकेले मेंटेन किया जाता है। लाइव आँकड़े: [bernstein.run](https://bernstein.run)।

### जहाँ ज़िक्र हुआ
<!-- l10n: en="mentioned in" hash="sha256:e1dfaf62cb5d" -->

[vinta/awesome-python](https://github.com/vinta/awesome-python) में सूचीबद्ध, Augment Code के [ओपन-सोर्स एजेंट ऑर्केस्ट्रेटर](https://www.augmentcode.com/tools/open-source-agent-orchestrators) राउंडअप में शामिल, और [Python Weekly #742](https://www.pythonweekly.com/p/python-weekly-issue-742-april-23-2026) में सूचीबद्ध। इस तरीक़े को हमने awesome-agentic-patterns में [deterministic zero-LLM orchestration](https://github.com/nibzard/awesome-agentic-patterns/blob/main/patterns/deterministic-zero-llm-orchestration.md) पैटर्न के रूप में भी लिखा है।

<details>
<summary>पूरी सूची: 20 से ज़्यादा awesome लिस्ट, डायरेक्ट्री, न्यूज़लेटर और अन्य परियोजनाओं के उद्धरण</summary>
<br>

हर awesome-list प्रविष्टि, कैटलॉग लिस्टिंग, पूर्व-कार्य के रूप में उद्धरण और न्यूज़लेटर उल्लेख समेत पूरी ट्रैक की गई सूची [docs/mentions.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/mentions.md) में है। जैसे-जैसे मिलती हैं, प्रविष्टियाँ जुड़ती जाती हैं; सुधार issue या PR से भेजिए।

</details>

### योगदान, सहायता, लाइसेंस
<!-- l10n: en="contributing, support, license" hash="sha256:94b6541e4b15" -->

PR का स्वागत है; सेटअप और कोड स्टाइल [CONTRIBUTING.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md) में हैं। सुरक्षा रिपोर्ट [SECURITY.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/SECURITY.md) के रास्ते जाती हैं। अगर Bernstein से आपका समय बचता है: [GitHub Sponsors](https://github.com/sponsors/chernistry)। संपर्क: [forte@bernstein.run](mailto:forte@bernstein.run)।

उद्धरण के लिए मेटाडेटा [CITATION.cff](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CITATION.cff) में है। लाइसेंस: [Apache-2.0](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE); प्रोजेक्ट के नाम को अलग से [TRADEMARKS.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md) में देखा गया है।

---

[Alex Chernysh](https://alexchernysh.com) &middot; [GitHub](https://github.com/chernistry) &middot; [X](https://x.com/alex_chernysh) &middot; [bernstein.run](https://bernstein.run)

<!-- mcp-name: io.github.sipyourdrink-ltd/bernstein -->
