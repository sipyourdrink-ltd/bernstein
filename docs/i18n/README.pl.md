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

### deterministyczna orkiestracja agentów CLI w systemach wieloagentowych
<!-- l10n: en="deterministic multi-agent CLI orchestration" hash="sha256:2cb1281992f1" -->

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

[简体中文](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.zh-Hans.md) &middot; [繁體中文](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.zh-TW.md) &middot; [日本語](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.ja.md) &middot; [한국어](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.ko.md) &middot; [हिन्दी](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.hi.md) &middot; [বাংলা](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.bn.md) &middot; [Русский](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.ru.md) &middot; [Español](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.es.md) &middot; [Português](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.pt.md) &middot; [Deutsch](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.de.md) &middot; [Français](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.fr.md) &middot; [Italiano](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.it.md) &middot; [Nederlands](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.nl.md) &middot; [Polski](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.pl.md) &middot; [Svenska](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.sv.md) &middot; [Suomi](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.fi.md) &middot; [Українська](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.uk.md) &middot; [Türkçe](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.tr.md) &middot; [العربية](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.ar.md) &middot; [עברית](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.he.md) &middot; [Bahasa Indonesia](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.id.md) &middot; [Tiếng Việt](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.vi.md) &middot; [ไทย](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/i18n/README.th.md)

</div>

---

> **Status: beta.** Projekt rozwijany i utrzymywany przez jedną osobę. Numer wersji oznacza kolejne wydania, a nie dojrzałość — wersje minor mogą zmieniać interfejsy. Przypnij wersję dla istotnych zależności; regresje są naprawiane na bieżąco, [zgłoś problem](https://github.com/sipyourdrink-ltd/bernstein/issues).

Bernstein to deterministyczny orkiestrator dla agentów kodujących CLI (Claude Code, Codex, Gemini CLI i ponad 40 innych). Uruchamia ich równolegle, weryfikuje ich wyniki za pomocą bramek jakościowych (gates) i rejestruje wystarczająco dużo danych z przebiegu, aby umożliwić późniejszy audyt. Zawiera profil instalacji w środowiskach odizolowanych (air-gap). Licencja Apache-2.0.

### w skrócie
<!-- l10n: en="at a glance" hash="sha256:97aa8e70f076" -->

Cztery cechy wyróżniają ten projekt; reszta to szczegóły.

- **Brak LLM w pętli koordynacyjnej.** Harmonogramowanie jest napisane w czystym Pythonie, dzięki czemu każdy przebieg jest w pełni powtarzalny. Odtwórz wczorajszy plan i uzyskaj identyczny graf zadań.
- **Weryfikowalność po fakcie.** Dziennik powtórzeń (replay journal) rejestruje każdy przebieg, a stale aktywny kręgosłup pochodzenia (lineage spine) zapisuje każdy krok tworzący historię pochodzenia; opcjonalny dziennik audytu powiązany łańcuchem HMAC (`BERNSTEIN_AUDIT=1`) dodaje pokwitowania (receipts), które można zweryfikować w trybie offline. Niedeterminizm ujawnia się jako niezgodność skrótu w konkretnym kroku, a nie jako losowy błąd ponownego uruchomienia. Rezultaty inne niż kod podlegają tym samym regułom: zadanie może zadeklarować kontrakt artefaktu (raport, zbiór danych, dziennik działań, wynik operacyjny) i kończy się podpisanym pokwitowaniem pochodzenia zamiast commita git.
- **Izolacja na poziomie architektury.** Każde zadanie programistyczne otrzymuje własny git worktree za bramkami scalania (merge gates); zadania w trybie artefaktów otrzymują katalog roboczy w `.sdd/workspaces/`. Domyślnie agenci nie współdzielą modyfikowalnej przestrzeni roboczej; jedynym współdzielonym stanem jest rejestr zadań (backlog), rezerwowany atomowo. Bardziej rygorystyczna ochrona systemu plików jest opcjonalna dzięki [backendom sandbox](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/sandbox.md). Po wyłączeniu worktrees każde zadanie wykonuje się we wspólnym katalogu roboczym.
- **Szeroki wachlarz i lokalne działanie.** Ponad 40 adapterów agentów CLI oraz ogólny wrapper `--prompt`, stan oparty na plikach, brak zależności od chmury SaaS, brak zewnętrznych platform przetwarzania danych.

Pełna lista znajduje się na [stronie możliwości](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md); [macierz funkcji](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/FEATURE_MATRIX.md) stanowi wyczerpujący spis.

### instalacja w 30 sekund
<!-- l10n: en="install in 30 seconds" hash="sha256:81b04220e0ff" -->

```bash
uv tool install bernstein    # or: pipx install bernstein
bernstein init
bernstein doctor             # checks a CLI agent is installed and authenticated
bernstein -g "fix the failing test in tests/test_foo.py"
```

pipx, pip, brew, dnf, npm oraz Docker zostały opisane w [przewodniku instalacji](https://bernstein.readthedocs.io/en/latest/getting-started/install/); izolowany pakiet instalacyjny posiada dedykowany [przewodnik air-gap](https://bernstein.readthedocs.io/en/latest/installation/air-gap/).

<img alt="A real bernstein demo run: mock agents fix four seeded bugs, ending on the run's signed receipt verifying offline" src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/demo-run/demo.gif" width="820">

Powyższe nagranie przedstawia rzeczywiste wykonanie i zawiera własny dowód poprawności. Zapis sesji, podpisane pokwitowanie wygenerowane z dziennika tego przebiegu oraz klucz publiczny znajdują się w [`docs/assets/demo-run/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/docs/assets/demo-run). Zweryfikuj obejrzany przebieg w trybie offline:

```bash
bernstein verify receipt docs/assets/demo-run/run-receipt.json \
    --public-key docs/assets/demo-run/run-receipt.pub.pem
```

System CI weryfikuje zatwierdzone pokwitowanie przy każdym wypchnięciu do gałęzi main — wykazując, że zmodyfikowana kopia nie przechodzi testu — dzięki czemu opublikowany dowód nie staje się bezużytecznym plikiem. Skrypt `scripts/record_demo.sh` generuje nagranie, pokwitowanie i klucz na nowo z rzeczywistego przebiegu; nic w terminalu nie jest symulowane.

Trwający przebieg można obserwować w dowolnym interfejsie operatora. Oba korzystają z tego samego API zadań, dzięki czemu żaden nie prezentuje opóźnionego stanu. W `bernstein live` lewa i prawa kolumna przewijają się niezależnie jako całe panele, co zapewnia dostęp do widżetów na mniejszych terminalach.

| ![A two-column terminal dashboard - agents with their live logs on the left, the task board on the right - with a full-width activity feed and a cost line underneath](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/tui-agents.png) | ![A browser dashboard listing sixty-two tasks with eleven running, one of them opened to its working-tree diff](https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/assets/webui-agents-diffs.png) |
|:---:|:---:|
| `bernstein live` — panel w terminalu | `bernstein gui serve` — panel w przeglądarce |

### udowodnij poprawność przebiegu
<!-- l10n: en="prove a run" hash="sha256:a97100ca1818" -->

Determinizm w tym projekcie można sprawdzić, zamiast przyjmować go na wiarę. Uruchom zadanie z włączonym audytem, a następnie zweryfikuj zarejestrowane dane:

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

Dziennik jest zapisywany przy każdym uruchomieniu; kręgosłup pochodzenia jest stale aktywny i dodaje wpis dla każdego kroku tworzącego historię, dzięki czemu krótki przebieg może zakończyć się poprawnym, pustym kręgosłupem. Komenda `bernstein audit verify` sprawdza łańcuch tylko wtedy, gdy uruchomienie nastąpiło z flagą `BERNSTEIN_AUDIT=1`, profilem zgodności lub `bernstein run --audit`. Flaga `--audit` dotyczy polecenia `bernstein run`; w przypadku formy `bernstein -g` należy ustawić zmienną środowiskową.

Pokwitowanie przebiegu łączy nagłówek dziennika, nagłówek pochodzenia (jeśli zapisano wpisy) oraz opcjonalnie zakres łańcucha audytu w ramach jednego rekordu podpisanego kluczem Ed25519 z osadzonym kluczem publicznym. Weryfikator dysponujący tym plikiem oraz kluczem publicznym operatora może potwierdzić brak modyfikacji: bez konieczności posiadania klucza HMAC, bez aktywnego katalogu `.sdd/`, z kodem wyjścia `2` wskazującym pierwszy niezgodny krok w razie manipulacji. Pokwitowanie to identyfikuje stan dziennika; udowodnienie, że stan ten stanowi pełny, ukończony dziennik, wymaga dodatkowo niezależnej pieczęci nagłówka/liczby kroków. Bez wskazania klucza `--public-key` następuje jedynie weryfikacja spójności wewnętrznej. Szczegóły w [deterministycznym odtwarzaniu](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/deterministic-replay.md#signed-run-receipt-one-file-offline-verification).

Taka sama weryfikowalność dotyczy wyników benchmarków. Polecenie `bernstein bench run <suite> --reliability k` (dostępne także jako `bernstein eval --reliability k`) uruchamia każde zadanie `k` razy przy stałej koordynacji, raportując dolny próg `pass^k` (wszystkie `k` prób musi zakończyć się sukcesem) obok górnego `pass@1`. Wynik zostaje przypieczętowany w podpisanym pokwitowaniu przeliczanym offline przez `bernstein bench reliability-verify`, co uniemożliwia sfałszowanie wskaźników. Szczegóły: [próg niezawodności pass^k](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/eval/reliability.md).

### jak to działa
<!-- l10n: en="how it works" hash="sha256:f818df2e6cbb" -->

Każdy cel realizowany jest w czterech etapach:

1. **Dekompozycja (Decompose)**. Menedżer dzieli cel na zadania z przypisanymi rolami, plikami i sygnałami ukończenia. Jedno wywołanie LLM, a dalej wyłącznie czysty Python.
2. **Uruchomienie (Spawn)**. Agenci rozpoczynają pracę w izolowanych [git worktrees](https://git-scm.com/docs/git-worktree), po jednym na zadanie programistyczne; zadania w trybie artefaktów otrzymują standardowy katalog roboczy. Główna gałąź pozostaje nienaruszona.
3. **Weryfikacja (Verify)**. Moduł weryfikacji (janitor) sprawdza twarde kryteria: powodzenie testów, obecność plików, poprawność lintera i zgodność typów.
4. **Scalenie (Merge)**. Zweryfikowane zmiany trafiają do gałęzi main. Nieudane zadania są ponawiane lub przekazywane do innego modelu.

Dlaczego harmonogram został zaimplementowany w czystym Pythonie i jakie niesie to kompromisy: [dlaczego determinizm](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/WHY_DETERMINISTIC.md).

### codzienne polecenia
<!-- l10n: en="everyday commands" hash="sha256:7d149b09b9bc" -->

```bash
cd your-project
bernstein init                    # creates .sdd/ workspace, bernstein.yaml + templates/
bernstein -g "Add rate limiting"  # agents spawn, work in parallel, verify, exit
bernstein live                    # watch progress in the TUI dashboard
bernstein run plan.yaml           # multi-stage plan: skip LLM planning, execute directly
bernstein stop                    # graceful shutdown with drain
```

Pełny zestaw funkcji operatora (automatyzacja PR, harmonogramy, integracje czatu, demon autofix) znajduje się w [poleceniach operatora](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/commands.md).

`bernstein workflow` uruchamia deklaratywne grafy DAG w formacie YAML złożone z węzłów agent / command / loop — ze wsparciem dla wznawiania przerwanych przebiegów:

```bash
bernstein workflow run idea-to-pr -g "Add JWT auth"   # prints run_id
bernstein workflow resume <run_id>                    # picks up at the first non-completed node
```

Punkty kontrolne stanu przebiegu trafiają do `.sdd/runs/<run_id>/` przy każdym węźle. Wznowienie weryfikuje skrót manifestu na starcie przebiegu, więc zmieniona specyfikacja zostaje odrzucona, zamiast po cichu wykonać inny manifest. Zobacz [manifesty workflow](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/workflows.md).

Bramki jakości repozytorium: `bernstein readme-l10n verify` odrzuca PR, w którym przetłumaczone pliki README odbiegają od wersji angielskiej (wskazując zdezaktualizowaną sekcję), natomiast `bernstein readme-l10n sync` aktualizuje powiązania po zmianach w tekście źródłowym. Zobacz [readme-l10n](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/playbooks/readme-l10n.md).

### obsługiwani agenci
<!-- l10n: en="supported agents" hash="sha256:8c94b4cde068" -->

Claude Code, Codex CLI, Gemini CLI, GitHub Copilot CLI, Cursor, Aider, Goose, Muse Code, OpenAI Agents SDK, Amp, Cody, Continue, Devin Terminal, Junie, Kilo, Kiro, AWS Q Developer, Ollama, OpenCode, OpenHands, Open Interpreter, gptme, Plandex, AIChat, Letta Code, Qwen i inni. [Indeks adapterów](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/adapters/index.md) zawiera instrukcje instalacji dla 30 z nich. Polecenie `bernstein integrations list` wyświetla wszystkie 51 wbudowanych integracji z pliku `src/bernstein/adapters/registry.py`, będącego jedynym źródłem prawdy. 49 z nich to adaptery agentów; pozostałe dwie pozycje to moduł testowy `mock` oraz profil punktów końcowych `self-hosted-endpoints`. Wszystkie inne narzędzia obsługujące flagę `--prompt` działają poprzez uniwersalny wrapper.

Możesz łączyć różnych agentów w ramach jednego przebiegu: tańsze modele lokalne do kodu powtarzalnego, bardziej zaawansowane modele chmurowe do architektury. Polecenie `bernstein integrations list --installed` wyświetla narzędzia dostępne w systemie.

### wolontariacka moc obliczeniowa
<!-- l10n: en="volunteer compute" hash="sha256:f0bd4a22affd" -->

Projekt może oznaczyć zgłoszenia jako otwarte dla wolontariuszy, a każdy może uruchomić jedno z nich na własnej maszynie, bez konta i bez koordynatora. To, co zadaniu wolno robić, projekt deklaruje w manifeście `volunteer.json` - backend piaskownicy, lista dozwolonych adresów sieciowych, limity czasu i pamięci - a własne limity darczyńcy mogą to tylko zawęzić, nigdy poszerzyć. Pokwitowanie ukończonego zadania wiąże wynik z decyzją o izolacji, pod którą powstał, więc opiekun projektu jeszcze po miesiącach może sprawdzić, do czego praca faktycznie miała dostęp.

```bash
bernstein volunteer verify .
bernstein volunteer browse --budget 60
```

[Przewodnik darczyńcy](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/volunteer/donor-guide.md) opisuje uruchamianie workera i budżet, który ustawiasz, [przewodnik projektu](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/volunteer/project-guide.md) - deklarowanie manifestu, a [model zagrożeń](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/volunteer/threat-model.md) mówi, przed czym każda granica chroni, a przed czym nie. Uruchamianie jedną komendą nie zostało jeszcze wydane: dziś działają podkomendy `verify`, `browse` i `hub`.

### poza stroną główną
<!-- l10n: en="beyond the front page" hash="sha256:ee01fbaaebd6" -->

Szczegółowa dokumentacja znajduje się w [serwisie dokumentacji](https://bernstein.readthedocs.io/):

| strona | zakres tematyczny |
|---|---|
| [capabilities](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/capabilities.md) | pełna lista możliwości: tryb serwera MCP, podpisane karty agentów, backendy sandbox, miejsca zapisu artefaktów, zgodność regulacyjna |
| [who this is for](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/use-cases.md) | gdzie tkwi wartość i w jakich sytuacjach Bernstein nie jest odpowiednim narzędziem |
| [workflows](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/workflow-manifests.md) | deklaratywne grafy DAG w formacie YAML złożone z węzłów agent / command / loop |
| [web UI](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/gui/index.md) | panel przeglądarkowy korzystający z tego samego API co TUI |
| [cloud execution](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/cloudflare/cloudflare-overview.md) | funkcja eksperymentalna: uruchamianie agentów w Cloudflare Workers z synchronizacją przestrzeni roboczej w R2 na własnym koncie. Usługa hostowana `api.bernstein.run` nie jest jeszcze publicznie dostępna |
| [datasources](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/datasources.md) | pokwitowania zapytań tylko do odczytu oraz sterownik wiążący każdy wynik ze zrzutem schematu |
| [agent catalogs](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/agent-catalogs.md) | przypisywanie ról do zewnętrznych definicji agentów — uniwersalne katalogi YAML/SKILL.md lub struktury wtyczek Claude Code |
| [security](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/operations/security.md) | scorecard, fuzzing, utwardzanie |
| [architecture](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/architecture/ARCHITECTURE.md) | zasada działania od strony technicznej |

### skąd taka nazwa?
<!-- l10n: en="why the name?" hash="sha256:2444448b9e03" -->

Projekt nazwano na cześć Leonarda Bernsteina, amerykańskiego dyrygenta i kompozytora. Koordynuje on zespół agentów CLI niczym Bernstein orkiestrę New York Philharmonic: każdy muzyk wchodzi we właściwym momencie, partytura jest deterministyczna, a dyrygent odpowiada za efekt końcowy.

stworzyłem bernsteina, ponieważ płaciłem 400 dolarów miesięcznie za rachunki w claude, uruchamiając trzy agenty równolegle i uzyskując niedeterministyczne scalenia. Licencja Apache 2.0, projekt rozwijany jednoosobowo. Statystyki na żywo: [bernstein.run](https://bernstein.run).

### wzmianki
<!-- l10n: en="mentioned in" hash="sha256:e1dfaf62cb5d" -->

Projekt wymieniony w [vinta/awesome-python](https://github.com/vinta/awesome-python), omówiony w zestawieniu [orkiestratorów agentów open source](https://www.augmentcode.com/tools/open-source-agent-orchestrators) przygotowanym przez Augment Code oraz wskazany w [Python Weekly #742](https://www.pythonweekly.com/p/python-weekly-issue-742-april-23-2026). Opisaliśmy to podejście również jako wzorzec [deterministycznej orkiestracji bez LLM](https://github.com/nibzard/awesome-agentic-patterns/blob/main/patterns/deterministic-zero-llm-orchestration.md) w repozytorium awesome-agentic-patterns.

<details>
<summary>Wszystkie wzmianki: ponad 20 zestawień awesome, katalogów, newsletterów i cytowań</summary>
<br>

Pełna lista publikacji, w tym wpisy w spisach awesome, katalogach oraz wzmianki w biuletynach, znajduje się w pliku [docs/mentions.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/mentions.md). Nowe wpisy są dodawane na bieżąco; poprawki można zgłaszać poprzez issue lub PR.

</details>

### współpraca, wsparcie, licencja
<!-- l10n: en="contributing, support, license" hash="sha256:94b6541e4b15" -->

Propozycje zmian (PR) są mile widziane; plik [CONTRIBUTING.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md) zawiera zasady konfiguracji i stylu kodu. Zgłoszenia dotyczące bezpieczeństwa przyjmujemy poprzez [SECURITY.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/SECURITY.md). Jeśli Bernstein oszczędza Twój czas: [GitHub Sponsors](https://github.com/sponsors/chernistry). Kontakt: [forte@bernstein.run](mailto:forte@bernstein.run).

Metadane cytowania znajdują się w [CITATION.cff](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CITATION.cff). Licencja: [Apache-2.0](https://github.com/sipyourdrink-ltd/bernstein/blob/main/LICENSE); nazwa projektu podlega odrębnym zasadom w [TRADEMARKS.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/TRADEMARKS.md).

---

[Alex Chernysh](https://alexchernysh.com) &middot; [GitHub](https://github.com/chernistry) &middot; [X](https://x.com/alex_chernysh) &middot; [bernstein.run](https://bernstein.run)

<!-- mcp-name: io.github.sipyourdrink-ltd/bernstein -->
