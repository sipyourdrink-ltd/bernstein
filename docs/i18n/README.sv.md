<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/readme-banner-dark.webp">
  <img alt="Bernstein: An open-source governance layer for AI agents. Chords: Gm9, C13(♭9), Fmaj9/A, F6/9." src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/readme-banner-light.webp" width="820">
</picture>

<br>

> *"To achieve great things, two things are needed: a plan and not quite enough time."* - [attributed to](https://quoteinvestigator.com/2020/08/19/plan-time/) Leonard Bernstein

### governance-lagret med öppen källkod för AI-agenter
<!-- l10n: en="an open-source governance layer for AI agents" hash="sha256:f988e1e29b9e" -->

[![CI](https://img.shields.io/github/actions/workflow/status/sipyourdrink-ltd/bernstein/ci.yml?branch=main&label=CI)](https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bernstein)](https://pypi.org/project/bernstein/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab)](https://python.org)
[![License](https://img.shields.io/github/license/sipyourdrink-ltd/bernstein)](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE)
[![OpenSSF Scorecard](https://img.shields.io/ossf-scorecard/github.com/sipyourdrink-ltd/bernstein?label=openssf%20scorecard)](https://scorecard.dev/viewer/?uri=github.com/sipyourdrink-ltd/bernstein)
![built with Bernstein](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/built-with-bernstein.svg)

[website](https://bernstein.run) &middot; [docs](https://docs.bernstein.run/) &middot; [deepwiki](https://deepwiki.com/sipyourdrink-ltd/bernstein) &middot; [install](https://docs.bernstein.run/en/latest/getting-started/install/) &middot; [first run](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/getting-started/first-run.md) &middot; [plugins](https://github.com/sipyourdrink-ltd/bernstein-addons) &middot; [org-kit](https://github.com/sipyourdrink-ltd/bernstein-addons/tree/main/plugins/org-kit) &middot; [glossary](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/GLOSSARY.md) &middot; [limitations](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/KNOWN_LIMITATIONS.md) &middot; [name policy](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md)

[简体中文](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.zh-Hans.md) &middot; [繁體中文](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.zh-TW.md) &middot; [日本語](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.ja.md) &middot; [한국어](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.ko.md) &middot; [हिन्दी](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.hi.md) &middot; [বাংলা](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.bn.md) &middot; [Русский](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.ru.md) &middot; [Español](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.es.md) &middot; [Português](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.pt.md) &middot; [Deutsch](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.de.md) &middot; [Français](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.fr.md) &middot; [Italiano](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.it.md) &middot; [Nederlands](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.nl.md) &middot; [Polski](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.pl.md) &middot; [Svenska](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.sv.md) &middot; [Suomi](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.fi.md) &middot; [Українська](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.uk.md) &middot; [Türkçe](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.tr.md) &middot; [العربية](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.ar.md) &middot; [עברית](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.he.md) &middot; [Bahasa Indonesia](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.id.md) &middot; [Tiếng Việt](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.vi.md) &middot; [ไทย](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.th.md)

</div>

---

> **Status: beta.** Underhålls av en person, under aktiv utveckling. Versionsnumret räknar utgåvor, inte mognadsgrad — delversioner kan ändra gränssnitt. Lås versionen för kritiska beroenden; regressioner åtgärdas snabbt, [rapportera dem](https://github.com/sipyourdrink-ltd/bernstein/issues).

Bernstein är governance-lagret med öppen källkod för AI-agenter. Det körs på policy as code: du skriver policyn - vem som får göra vad, vad som kräver godkännande, vad som måste loggas - och Bernstein tillämpar den och tar fram det verifierbara beviset. En deterministisk schemaläggare - ingen modell i koordinationsloopen - kör agenter parallellt, grindar det de producerar och loggar varje steg, så att en körning kan verifieras i efterhand, offline, enbart från artefakterna. CLI-kodagenter fungerar direkt (Claude Code, Codex, Gemini CLI), och samma lager governar vilken agentlast som helst: leveransen kan vara en diff, en forskningsrapport, ett dataset eller ett paket med revisionsbevis. Air-gap-installationsprofil ingår. Apache-2.0.

### i korthet
<!-- l10n: en="at a glance" hash="sha256:3c95f239d9cf" -->

Fem saker skiljer det från andra; resten är detaljer.

- **Ingen LLM i koordineringsloopen.** Schemaläggningen sker i ren Python, vilket gör en körning reproducerbar från början till slut. Återuppspela gårdagens plan och få exakt samma uppgiftsgraf.
- **Bevis för hårdvarunycklar.** Offlineverifierare för PIV-förvaring och FIDO2-godkännanden delar kvittoprotokollet, med fastlagd tillit och explicit tid. Tokenförvaring höjer inte TRACE:s tillitsnivåer. [Bevis för hårdvarunycklar](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/hardware-key-evidence.md).
- **Verifierbar i efterhand.** Replay-journalen registrerar varje körning, och den ständigt aktiva lineage-ryggraden loggar varje spårbarhetspunkt; den valfria HMAC-kedjade granskningsloggen (`BERNSTEIN_AUDIT=1`) lägger till kvitton (receipts) som du kan verifiera offline. Icke-determinism visar sig som en hash-avvikelse vid det exakta steget, inte som ett instabilt testfel vid omkörning. Leverabler som inte är kod behandlas på samma sätt: en uppgift kan deklarera ett artefaktkontrakt (rapport, dataset, åtgärdslogg, ops-resultat) och slutförs med ett signerat lineage-kvitto istället för en git-commit.
- **Isolerad genom konstruktion.** Varje kodningsuppgift får ett eget git worktree bakom merge gates; uppgifter i artefaktläge får en arbetskatalog under `.sdd/workspaces/`. Agenter delar som standard ingen föränderlig arbetsyta; det enda delade tillståndet är uppgiftskön, som reserveras atomärt. Striktare filsystemsbegränsningar är valfria via [sandbox-backends](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/sandbox.md). Inaktivera worktrees så körs varje uppgift i den delade arbetskatalogen.
- **Bred och lokal.** 55 valbara namn på CLI-agentadaptrar, inklusive det generiska `--prompt`-omslaget. filbaserat tillstånd, inga SaaS-mellanled, inget externt dataplan.

Hela listan finns på [funktionssidan](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md); [funktionsmatrisen](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/FEATURE_MATRIX.md) är det fullständiga indexet.

Uppgiftsservern stöder en beständig, operatörsstyrd spärr för att ta nya uppgifter; se [guiden](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/claim-admission.md).

Adapterpolicyn för roller stöder en valfri standardlista över tillåtna adaptrar för roller som inte anges separat; se [guiden](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/role-adapter-policy.md).

### hur en körning ser ut
<!-- l10n: en="what a run looks like" hash="sha256:8279488abf5a" -->

En enda YAML-fil deklarerar körningen: faser, roller, beroenden och villkoren under vilka en nod alls körs. Schemaläggaren exekverar den som ren Python - inget i filen är en prompt, och ingen modell bestämmer vad som händer härnäst. Den här grafen bygger ett paket med revisionsbevis; hela filen finns i [`.bernstein/workflows/audit-evidence-pack.yaml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/.bernstein/workflows/audit-evidence-pack.yaml).

```yaml
name: audit-evidence-pack
version: "1.0.0"

phases:
  - name: scope
    allowed_roles: [manager, architect]
  - name: collect
  - name: validate
    allowed_roles: [qa, security]
  - name: deliver
    allowed_roles: [security, manager]

nodes:
  define-control-inventory:
    phase: scope
    role: architect

  collect-audit-logs:
    phase: collect
    role: security
    depends_on: [define-control-inventory]

  # three more evidence streams collect in parallel:
  # collect-sboms-and-attestations, collect-runbooks-and-policies,
  # collect-eval-results

  assemble-pack:
    phase: validate
    role: docs
    depends_on:
      - collect-audit-logs
      - collect-sboms-and-attestations
      - collect-runbooks-and-policies
      - collect-eval-results

  mock-auditor-pass:
    phase: validate
    role: qa
    depends_on: [assemble-pack]

  remediate-findings:
    phase: collect
    role: docs
    depends_on:
      - source: mock-auditor-pass
        condition: "status == 'failed'"
    retry:
      max_attempts: 3
      until: "status == 'done'"

  sign-and-deliver:
    phase: deliver
    role: security
    depends_on:
      - source: mock-auditor-pass
        condition: "status == 'done'"
```

```mermaid
flowchart LR
    inv[define-control-inventory] --> logs[collect-audit-logs]
    inv --> sbom[collect-sboms-and-attestations]
    inv --> rb[collect-runbooks-and-policies]
    inv --> ev[collect-eval-results]
    logs --> pack[assemble-pack]
    sbom --> pack
    rb --> pack
    ev --> pack
    pack --> gate{mock-auditor-pass}
    gate -->|failed| fix["remediate-findings (retry x3)"]
    gate -->|done| sign[sign-and-deliver]
```

Varje nod tas av en agent vars roll fasen tillåter; rollstängsel och godkännandegrindar håller oavsett vad agenten gör i uppgiften. En kodnod avslutas bakom merge gates i sitt eget git worktree. Noderna ovan avslutas annorlunda: ett [artefaktkontrakt](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/artifacts.md) namnger leveransen (rapport, dataset, skanning, åtgärdslogg) och noden stängs med ett signerat lineage-kvitto i stället för en commit. Samma schemaläggare, samma journal, samma offlineverifiering - oavsett om grafen levererar kod, forskning, en ops-ändring eller en blandning av alla tre. Färdiga grafer för mjukvara, forskning, dokumentation, enterprise och bidragsflöden finns i [`.bernstein/scenarios/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/.bernstein/scenarios).

Nya försök behåller `dispatch_tier`; när en uppgift öppnas igen kan nivån väljas uttryckligen. Se [kontraktet för körningsnivåer](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/task-tier-classification.md#retry-and-reopen).

### installera på 30 sekunder
<!-- l10n: en="install in 30 seconds" hash="sha256:b24c255f61fb" -->

```bash
uv tool install bernstein    # or: pipx install bernstein
bernstein demo               # zero-key local run (4 mock tasks); prints project path & run_id
# Inside the printed demo directory, sign and verify your own receipt offline:
openssl genpkey -algorithm ED25519 -out demo.key && openssl pkey -in demo.key -pubout -out demo.pub
bernstein verify run <run_id> --signing-key-path demo.key
bernstein verify receipt .sdd/runs/<run_id>/run-receipt.json --public-key demo.pub
```

Starta körningen från en arbetsgren. Bernstein slår ihop verifierade ändringar med den utcheckade grenen och stoppar körningar som kan slå ihop ändringar med standardgrenen. Granska ändringarna i arbetsgrenen innan du öppnar en pull request.

pipx, pip, brew, dnf, npm och Docker beskrivs i [installationsguiden](https://docs.bernstein.run/en/latest/getting-started/install/); det isolerade paketet har en egen [air-gap-guide](https://docs.bernstein.run/en/latest/installation/air-gap/).

[GHCR](https://ghcr.io/sipyourdrink-ltd/bernstein) &middot; [GitHub Codespaces](https://codespaces.new/sipyourdrink-ltd/bernstein?quickstart=1)

<img alt="A real bernstein demo run: mock agents fix four seeded bugs, ending on the run's signed receipt verifying offline" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/demo-run/demo.gif" width="820">

Inspelningen ovan visar en verklig körning och innehåller sitt eget bevis. Sessionen, det signerade körkvittot genererat från journalen och den offentliga nyckeln finns i [`docs/assets/demo-run/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/docs/assets/demo-run). Verifiera körningen offline:

```bash
bernstein verify receipt docs/assets/demo-run/run-receipt.json \
    --public-key docs/assets/demo-run/run-receipt.pub.pem
```

Det incheckade kvittot kan kontrolleras igen när som helst med kommandot ovan; en manipulerad kopia klarar inte samma kontroll. `scripts/record_demo.sh` återskapar inspelning, kvitto och nyckel från en ny verklig körning; ingenting i terminalen är syntetiserat.

En pågående körning kan följas från båda operatörsgränssnitten. Båda läser samma uppgifts-API, vilket gör att ingen av dem är en fördröjd spegling. I `bernstein live` rullar vänster och höger kolumn oberoende som hela paneler, så att kontroller förblir åtkomliga på mindre terminaler.

| ![A two-column terminal dashboard - agents with their live logs on the left, the task board on the right - with a full-width activity feed and a cost line underneath](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/tui-agents.png) | ![A browser dashboard listing sixty-two tasks with eleven running, one of them opened to its working-tree diff](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/webui-agents-diffs.png) |
|:---:|:---:|
| `BERNSTEIN_SERVER_URL="<task-server-url>"` `bernstein live` — terminalpanelen (fjärranslutna arbetsnoder) | `<task-server-url>/ui/` — webbpanelen för den körande uppgiftsservern när GUI-filerna är installerade. `bernstein gui serve` startar en fristående server i den aktuella arbetsytan. |

Om servern kräver autentisering, ange `BERNSTEIN_AUTH_TOKEN` för terminalen och mata in en auktoriserad token på webbläsarens inloggningssida. [GUI authentication](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/gui/index.md#quickstart).

### bevisa en körning
<!-- l10n: en="prove a run" hash="sha256:d4095258a0e7" -->

Determinism är här något du verifierar, inte något du accepterar på god tro. Kör en gång med granskning aktiverad och kontrollera sedan vad som loggades:

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

Journalen skrivs vid varje körning; lineage-ryggraden är alltid aktiv och lägger till en post för varje spårbart steg, så att en kort körning kan avslutas med en giltig, tom ryggrad. `bernstein audit verify` har bara en kedja att kontrollera när körningen startades med `BERNSTEIN_AUDIT=1`, en regelefterlevnadsprofil eller `bernstein run --audit`. Flaggan `--audit` tillhör `bernstein run`; i `bernstein -g`-formen ovan anger du miljövariabeln.

Ett körkvitto binder samman journalens topp, lineage-ryggradens topp (om poster sparats) och valfritt ett granskningskedjeintervall under ett enda Ed25519-signerat objekt med inbäddad offentlig nyckel. En granskare med den filen och operatörens offentliga nyckel kan bekräfta att åtgärder och kedjor är oförvanskade: ingen HMAC-nyckel krävs, ingen aktiv `.sdd/`, och slutkod `2` med angivelse av det första avvikande steget vid manipulation. Kvittot identifierar det inbäddade journaltillståndet; att bevisa att tillståndet utgör hela den färdiga journalen kräver dessutom ett oberoende kontrollsigill. Utan `--public-key` kontrolleras endast intern integritet. Detaljer i [deterministisk återuppspelning](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/deterministic-replay.md#signed-run-receipt-one-file-offline-verification).

Öppna **Governance** i webbgränssnittet och använd **Verify a receipt** för att kontrollera ett körkvitto på servern. Webbläsaren visar resultatet av enbart integritetskontrollen och nyckelns källa. Se [Governance operator guide](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/governance.md#verifying-a-dropped-receipt).

Samma verifierbarhet gäller för utvärderingsresultat. `bernstein bench run <suite> --reliability k` (även `bernstein eval --reliability k`) kör varje uppgift `k` gånger under fast koordinering och rapporterar ett `pass^k`-golv (alla `k` försök måste lyckas) tillsammans med `pass@1`-taket. Resultatet förseglas i ett signerat kvitto som `bernstein bench reliability-verify` räknar om offline, så att ett manipulerat golv underkänns. Detaljer: [pass^k tillförlitlighetsgolv](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/eval/reliability.md).

Använd `bernstein govern audit --format json` eller `--format sarif` för maskinläsbara kontrollresultat och `--strict --quiet` som godkännandekrav i CI. Se [govern audit](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/governance.md#govern-audit-machine-readable-output-and-ci-gates). Evidenspaketet `cosai` innehåller MCP-hotklasserna från den låsta versionen av dokumentet daterat 2026-01-08 och anger de återstående luckorna i täckningen.

`bernstein delegation verify RUN` jämför kvittokedjan med det slutliga kedjehuvudet och antalet delegeringssteg, som förseglats i körningsmanifestet med en HMAC baserad på granskningsnyckeln. Det upptäcker borttagning av kedjans slut, utbyte och förlängning efter avslut, inklusive omskrivning av deklarationen utan nyckeln. Om deklarationen tas bort förblir körningen ofullständig (avslutskod `3`) och betraktas aldrig som giltig. Detta upptäcker inte återanvändning av en äldre autentisk ögonblicksbild och skyddar inte mot en skribent som har granskningsnyckeln. Se [arbetsflödet för delegeringsverifiering](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/delegation-verify.md).

### hur det fungerar
<!-- l10n: en="how it works" hash="sha256:b734c935f998" -->

Varje mål rör sig genom fyra steg:

1. **Dela upp (Decompose)**. Managern bryter ned målet i uppgifter med roller, tilldelade filer och slutförandesignaler. Ett LLM-anrop, därefter ren Python.
2. **Starta (Spawn)**. Agenter startar i isolerade [git worktrees](https://git-scm.com/docs/git-worktree), en per kodningsuppgift; uppgifter i artefaktläge får en vanlig arbetskatalog. Huvudgrenen förblir ren.
3. **Verifiera (Verify)**. Janitorn kontrollerar konkreta signaler: tester passerar, filer finns, lint är godkänd, typer stämmer.
4. **Slå ihop (Merge)**. Verifierat arbete slås samman till main. Misslyckade uppgifter prövas igen eller styrs om till en annan modell.

Varför schemaläggaren är i ren Python och vilka avvägningar det innebär: [varför deterministisk](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/WHY_DETERMINISTIC.md).

Kontrollen `can_spawn()` i kostnadsuppföljningen returnerar `False` när de korrigerade sammanlagda utgifterna når eller överskrider budgetens inställda stoppgräns, även om inga nya token har rapporterats. Efter en omstart räcker de sparade tokenantalen inte för att återskapa den exakta sammanlagda kostnad som tidigare rapporterats för en viss kombination av agent, uppgift och modell.

### vardagliga kommandon
<!-- l10n: en="everyday commands" hash="sha256:db85124a3e75" -->

```bash
cd your-project
bernstein init                    # creates .sdd/ workspace, bernstein.yaml + templates/
bernstein -g "Add rate limiting"  # agents spawn, work in parallel, verify, exit
bernstein live                    # watch progress in the TUI dashboard
bernstein run plan.yaml           # multi-stage plan: skip LLM planning, execute directly
bernstein stop                    # graceful shutdown with drain
```

Om en uppgift i en frikopplad SSH-körning avslutas med en annan slutkod än noll misslyckas både uppgiften och körningen. Uppgiftens kvitto innehåller slutkoden och efterföljande uppgifter får vänta.

Hela operatörsytan (PR-automatisering, scheman, chattbryggor, autofix-demonen) finns i [operatörskommandon](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/commands.md).

`bernstein workflow` kör deklarativa YAML-DAG:ar av agent-, kommando- och loop-noder - med stöd för att återuppta avbrutna körningar:

```bash
bernstein workflow run idea-to-pr -g "Add JWT auth"   # prints run_id
bernstein workflow resume <run_id> -g "Add JWT auth" # picks up at the first non-completed node
```

Körningens tillstånd sparas som en checkpoint under `.sdd/runs/<run_id>/` vid varje nod. Återupptagning validerar manifest-digesten vid körningens start, så en ändring av specifikationen avvisas i stället för att tyst köra ett annat manifest. Se [workflow-manifest](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/workflows.md). Ange samma `-g`-värde igen vid återupptagning så att `{goal}` ersätts i promptarna för ofärdiga noder.

`bernstein compliance controls` visar testsviterna för varje kontroll och den senaste fullständiga mätningen om den finns. [Testtäckning](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/compliance/regulator-mapped-packs.md#cli-inspection)

Grindar för kodhygien: `bernstein readme-l10n verify` underkänner en PR vars översatta README-filer avviker från den engelska källan (med angivande av inaktuellt avsnitt), `bernstein readme-l10n sync` uppdaterar kopplingarna efter engelska ändringar. Se [readme-l10n](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/playbooks/readme-l10n.md).

### agenter som stöds
<!-- l10n: en="supported agents" hash="sha256:d674cd57d15a" -->

Claude Code, Codex CLI, Gemini CLI, GitHub Copilot CLI, Cursor, Aider, Goose, Muse Code, OpenAI Agents SDK, Amp, Cody, Continue, Devin Terminal, Junie, Kilo, Kiro, AWS Q Developer, Ollama, OpenCode, OpenHands, Open Interpreter, gptme, Plandex, AIChat, Letta Code, Qwen och fler. [Adapterindexet](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/adapters/index.md) innehåller installationskommandon för 30 av dem. `bernstein integrations list` listar alla 57 anslutna integrationer från `src/bernstein/adapters/registry.py`, källan till sanning. 55 av dem är valbara agentadaptrar; de övriga två raderna är teststubben `mock` och slutpunktsprofilen `self-hosted-endpoints`. Allt annat med en `--prompt`-flagga fungerar via den generiska wrappern. `bernstein adapters list` → `Selectable CLI agent adapter names: 55` (`generic`; `agy` = `antigravity`).

Kombinera agenter i samma körning: billiga lokala modeller för standardkod, tyngre molnmodeller för arkitektur. `bernstein integrations list --installed` visar vad som finns tillgängligt på din maskin.

### bortom förstasidan
<!-- l10n: en="beyond the front page" hash="sha256:63d7eefff374" -->

All fördjupad information finns på [dokumentationswebbplatsen](https://docs.bernstein.run/):

| sida | vad den täcker |
|---|---|
| [capabilities](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md) | hela funktionslistan: MCP-serverläge, signerade agentkort, sandbox-backends, artefaktsänkor, regulatoriska mappningar |
| [Agent Manifest export](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/agent-manifest.md) | `bernstein manifest export-agent` · `composition-only` · `agent-manifest==0.12.0` |
| [`bernstein_create_task`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/bare-tasks.md) | `POST /api/v1/tasks` → `bernstein_claim` → `bernstein_complete` |
| [who this is for](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/use-cases.md) | var värdet finns och när Bernstein är fel verktyg |
| [workflows](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/workflow-manifests.md) | deklarativa YAML-DAG:ar av agent- / kommando- / loop-noder |
| [web UI](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/gui/index.md) | webbläsardashboard baserad på samma API som TUI använder (skapa och granska uppgifter) |
| [klusterarbetarnas status](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/cluster-mode.md#worker-health-telemetry) (`bernstein worker`) | Arbetarna hämtar inga nya uppgifter så länge ledigt diskutrymme, minnesanvändning eller nätverksfördröjning ligger utanför de konfigurerade gränserna. |
| [datasources](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/datasources.md) | skrivskyddade frågekvitton plus en frågedrivrutin som binder varje resultat till den schemaögonblicksbild det härleddes mot |
| [agent catalogs](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/agent-catalogs.md) | peka roller mot agentdefinitioner utanför de inbyggda mallarna — en generisk YAML/SKILL.md-katalog eller Claude Code-plugin-struktur |
| [security](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/security.md) | scorecard, fuzzing, härdning |
| [architecture](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/ARCHITECTURE.md) | hur det fungerar under huven |

### varför namnet?
<!-- l10n: en="why the name?" hash="sha256:e9839a1c718d" -->

Bernstein är uppkallat efter Leonard Bernstein, den amerikanske dirigenten och kompositören. Projektets orkestrerare styr en ensemble av AI-agenter på samma sätt som Bernstein ledde New York Philharmonic: varje musiker på rätt insats, partituret deterministiskt, dirigenten ansvarig för slutresultatet. Dirigentmetaforen beskriver Bernsteins förmåga att orkestrera agentuppgifter som producerar kod, forskningsrapporter eller datamängder.

Bernstein startade som en deterministisk ram för att förhindra icke-deterministiska sammanslagningar och overifierat tillstånd mellan parallella kodagenter. Apache-2.0. Utgåvor och dokumentation: [bernstein.run](https://bernstein.run).

### bidra, support, licens
<!-- l10n: en="contributing, support, license" hash="sha256:cb937af4a6cc" -->

Pull requests välkomnas; [CONTRIBUTING.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md) beskriver installation och kodstil. Säkerhetsrapporter lämnas via [SECURITY.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/SECURITY.md). Kontakt: [forte@bernstein.run](mailto:forte@bernstein.run).

Citeringsmetadata finns i [CITATION.cff](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CITATION.cff). Licens: [Apache-2.0](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE); projektnamnet omfattas separat i [TRADEMARKS.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md).

---

[Alex Chernysh](https://alexchernysh.com) &middot; [GitHub](https://github.com/chernistry) &middot; [X](https://x.com/alex_chernysh) &middot; [bernstein.run](https://bernstein.run) &middot; [task notifications](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/notifications.md#role-task-subscriptions-sse)

<!-- mcp-name: io.github.sipyourdrink-ltd/bernstein -->
