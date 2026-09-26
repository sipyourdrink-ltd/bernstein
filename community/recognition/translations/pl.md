Przyjaciele, dzień dobry.

Myślę sobie trochę, i wydaje mi się, że nie zabije mnie to, że mniej więcej co dwa tygodnie strona Bernsteina na LinkedIn wymienia, osobiście, każdego współtwórcę, który chce być wymieniony, i dziękuje im wszystkim w jednym wspólnym poście.

Jeden post. Nie jeden na osobę. Nasi obserwujący nie potrzebują dziesięciu postów pod rząd, a ja nie potrzebuję drugiej pracy pisania ich. Ale wewnątrz tego jednego posta chciałbym spróbować powiedzieć, dla każdej osoby, co naprawdę zrobiła, tak aby gdy potencjalny pracodawca to przeczyta, zrozumiał, co zbudowała w prawdziwym projekcie, i aby nie było jej wstyd udostępnić go ponownie.

Zaczęliśmy to już, nawiasem mówiąc. #5524 ma stronę LinkedIn i całą garstkę z was mówiącą "dobrze, wspomnij mnie", a #6229 była pierwszą próbą zbiorowego podziękowania. Więc to nie jest nic nowego. To tylko ja próbuję przestać składać to ręcznie za każdym razem. Ci z was, którzy już powiedzieliście "tak" w #5524, nie musicie mówić tego ponownie; mam was.

## O co chodzi

Cały proces, od waszej strony:

1. Wykonaliście znaczną pracę w Berniecie w ostatnich trzech miesiącach.
2. Śledzicie stronę projektu: https://www.linkedin.com/company/bernstein-run/
3. Od czasu do czasu strona publikuje jeden wspólny post wymieniający osoby, które się zgłosiły (opt in) i co zrobiły.
4. Udostępniacie go ponownie, jeśli chcecie, idealnie oznaczając Bernsteina.
5. Projekt zdobywa zasięg, którego inaczej by nie miał. Wy otrzymujecie publiczny, zawodowy rekord, który nie został napisany przez was o was samych.

To wszystko. Nie ma kroku szóstego.

Jeśli aktualnie szukacie pracy, istnieje alternatywa: mogę napisać wam zalecenie LinkedIn zamiast tego, z mojego profilu (https://www.linkedin.com/in/alex-chernysh/). To nie jest żadna kłopotliwość. Ale chcę być szczery co do wymiany: zalecenie pozostaje na moim profilu i pomaga wam; odświeranie wspólnego posta pomaga wam i projektowi jednocześnie. I szczerze mówiąc, prawie nikt nie przewija aż do sekcji Rekomendacje w ogóle. Dlatego proszę używać zalecenia głównie, gdy aktywnie szukacie pracy, a odświeranie w pozostałych przypadkach. Jeśli chcecie oboje, dajcie znać.

O wartości tego wszystkiego. Jeśli naprawdę wykonałeś tę pracę, nie ukrywaj jej. Umieść Bernsteina w swoim CV, w swoim portfolio, gdzie pasuje. Ja tak robię, niektórzy z was już to robicie, i uważam, że jest to dobry sygnał: scalona wkładka do poważnego projektu open-source, którą możesz wyjaśnić linia po linii, jest warta więcej niż linią w CV mówiącą "zna Pythona". To jest moja opinia, nie badanie. To także nie jest obietnica niczego: to jest projekt wolontariacki, nikt nie zatrudnia, i wolę powiedzieć to teraz wyraźnie, niż pozwolić komukolwiek później coś do tego doszukać się.

## Jak powiedzieć tak

Wyślij jeden e-mail. Adres i temat dokładnie taki:

> **Do:** forte@bernstein.run
> **Temat:** `BRNSTN-PR-LNKD`

W treści, kilka linii, które skrypt może odczytać:

```
GITHUB=twój-login-na-githubie
OPT_IN=TAK
REKOMENDACJA=NIE
```

Ustaw `REKOMENDACJA=TAK`, jeśli chcesz zalecenie LinkedIn zamiast posta, lub oprócz niego. Dodaj linię `UWAGA=`, jeśli jest coś, czego powinienem się dowiedzieć (na przykład, że chcesz zobaczyć sformułowanie przed jego opublikowaniem; kilka osób o to pytało i jest to w porządku). Aby później zrezygnować, wyślij e-mail na ten sam adres, z tym samym tematem i `OPT_IN=NIE`.

Nie zmieniaj tematu linii. Parser lubi dokładnie ten ciąg. Wolumen przychodzącej poczty jest taki, że to nie jest kwestia gustu; wiadomość z innym tematem trafia do ogólnej kolejki i nie mogę obiecać, że kiedykolwiek ją zobaczę.

{{REPLY_BY}}

## Miękka brama, i dlaczego jest miękka

Aby zdecydować, kto trafia do danego posta bez czytania każdego PR ręcznie, istnieje mechaniczny filtr: około 1000 dodanych linii w scalanych pull requestach z ostatnich 30 dni, z wykluczeniem plików wygenerowanych. To nie jest KPI. To nie jest metryka jakości. To nie jest ocena kogokolwiek. To tani sposób dla skryptu, aby zebrać kohortę co dwa tygodnie bez udziału człowieka w pętli.

I nie jest to betonowy płot. Ludzie tutaj wykonują pracę związana z bezpieczeństwem, recenzjami, dokumentacją, benchmarkami, przygotowaniem wydań i debugowaniem, które może być enormnie wartościowe przy czterdziestu linijkach dyfu. Jeśli wykonałeś znaczącą pracę i nie osiągnąłeś tysiąca z jakiegokolwiek powodu, napisz na ten sam adres z tym samym tematem i powiedz tak. Nie jesteśmy potworami; znajdziemy rozwiązanie. Zwykła odpowiedź polega na tym, że znajdziemy dla ciebie następne zadanie i prześcigniesz tę niezręczną tysięczną granicę przy następnym wkładzie.

Imiona w okresowej liście są uporządkowane alfabetycznie. Nie ze względu na liczbę PRów, nie ze względu na to, kogo lubię bardziej. Alfabetycznie.

## GitHub Discussions i strona Linkedina nie konkurują

Wykonują różne zadania. Dla szybkich wiadomości (wydanie, zmiana, aktualizacja społeczności) strona Linkedin jest szybszym kanałem. GitHub Discussions jest miejscem, gdzie właściwie uczestniczysz w projekcie: widzisz kontekst, dyskutujesz, proponujesz, znajdujesz odpowiedź, i pozostaje ona zapisana, gdzie następna osoba może ją znaleźć. Jeśli chcesz zarówno wiadomości, jak i głębię, śledź oba. Decyzje nadal zapadają w zgłoszeniach i pull requestach; to się nie zmieniło.

## Dlaczego to robię

Wielu z was jest młodych. Niektórzy z was są znacznie starsi. Ale dla osoby zaczynającej, prawdziwy projekt open-source jest dość rzadką szansą na spróbowanie nie tylko kodowania, ale także recenzji, architektury, testowania, dokumentacji, bezpieczeństwa, wydań, koordynacji, nawet trochę publicznego pisania, i podejmowania decyzji, które mają konsekwencje. Gdybym mógł pracować nad czymś takim podczas studiów na uczelni, moje życie prawdopodobnie potoczyłoby się inaczej. Chcę więc wspierać początkujących twórców tak bardzo, jak mogę.

Mój model jest prosty: dziel kamienie milowe na kawałki, które osoba może objąć, daj komuś prawdziwe zadanie, pozostaw decyzję im tam, gdzie może należeć, nie odbieraj zadania po pierwszym błędzie, i pozwól ludziom przejmować coraz większą odpowiedzialność w miarę postępów.

Do tych z was, którzy aktualnie studiuje coś kształtem informatycznym, lub właśnie zaczynacie: widzę was. Jestem z wami. Radzicie sobie dobrze.

Jeszcze jedna opinia, wyraźnie oznaczona jako taka. Umiejętność kodowania staje się podstawą, podobnie jak umiejętność pisania na maszynie. Umiejętność myślenia, dostrzegania kompromisu, podejmowania decyzji, którą możesz obronić, jest nadal rzadka, i podejrzewam, że w erze AI staje się jeszcze rzadsza i cenna, a nie mniej. To jest miejsce, aby ćwiczyć dokładnie to.

## Jak prowadzony jest projekt, na razie

Do końca tego roku kalendarzowego ja pozostanę jedynym utrzymującym. To jest celowe. Nie oznacza to, że utrzymujący decyduje wszystko. Własność należy do mnie; nie sądzę, że powinienem osobisty wybierać każdy przecinek w architekturze. Jeśli przyszedłeś, aby zrobić PR, chcę, abyś pomyślał, nie zgadywał, czego chce utrzymujący.

Gdy mam możliwość, a teraz jestem nieco zajęty innymi sprawami, chciałbym spróbować uczynić pracę nieco bardziej ukształtowaną, z wirtualnymi "departamentami" w sposób, jaki ma zwykła firma. Wszystko dobrowolne, bez zobowiązań, bez biurokracji dla samej biurokracji. Na początek widzę tylko dwa: B+R, które już istnieje w zgłoszeniach i pull requestach i wymaga niewiele ode mnie, oraz PR / działania zewnętrzne, które prawdopodobnie będę nadzorował nieco dokładniej, ponieważ komunikacja bez struktury prędzej czy później przechodzi w "powinniśmy to kiedyś zrobić" szybciej niż wszystko inne, co znam.

Później, jeśli będzie wystarczająco dużo aktywnych osób, mogą pojawić się role: dyrektor ds. rozwoju, jeden ds. operacji, jeden ds. bezpieczeństwa, jeden ds. QA i dokumentacji. Nie tworzę tych tytułów z wyprzedzeniem. Liczba osób określa strukturę, a nie odwrotnie. Nikt nie potrzebuje korporacji z sześcioma osobami.

## Pieniądze, skoro ktoś zawsze pyta

Gdy ktoś pyta, czy istnieje płatna możliwość pracy nad Bernsteinem, czasem śmieję się małym nerwowym śmiechem. Bernstein nie zarabia pieniędzy. Wydaje moje. Nie liczę nawet swojego czasu; po prostu obserwuję, co opuszcza konto bankowe, w dosyć drogim kraju, podczas gdy jestem między pracami i okoliczności prawdopodobnie zaraz dadzą mi jeszcze przynajmniej kolejne kilka tygodni na tym projekcie. To, co robię, więc nie jest pracą niewypłacaną. To praca ze stratą. Lubię tę pracę. To w dużej mierze dlatego wszyscy tutaj jesteśmy.

Darmowe dla użytkownika nie oznacza darmowego dla utrzymującego. Infrastruktura za tym kosztuje rzędu kilku tysięcy dolarów miesięcznie, a ten rachunek przybywa niezależnie od tego, czy ktoś publikuje cokolwiek na LinkedInie.

Co do licencji, dokładnie, bo sam się kiedyś pomyliłem: Bernstein jest na licencji Apache-2.0. Apache-2.0 zezwala na użycie komercyjne; nie zobowiązuje nikogo do bycia niekomercyjnym. Że Bernstein był, jest, i moim zdaniem pozostaje projektem wolnego oprogramowania open-source, jest to stanowisko i intencja, a nie warunek licencji.

Projekt darmowy może nadal mieć sponsory, reklamy, umiejscowienia partnerstw, istotne integracje. Jeśli znasz laboratorium AI lub firmę w tej dziedzinie, dla których to mogłoby naprawdę mieć sens, skieruj je do mnie. Spróbuję przygotować właściwy pakiet partnerski w kolejnych tygodniach, i wtedy zobaczymy, co można z tym zrobić. Do tego czasu istnieje GitHub Sponsors, który istnieje i jest mały.

Jedna zasada bezwzględna. Żadna płatna integracja nigdy nie kupuje scalania. Jeśli firma chce zapłacić za konkretny kawałek pracy integracyjnej, praca przechodzi przez zwykły proces techniczny, utrzymujący i społeczność zachowują prawo do odmowy, a decyzja techniczna nie zmienia się z powodu pojawienia się budżetu. Jeśli kiedykolwiek taka praca zostanie rzeczywiście zatwierdzona, dochody zostaną przeznaczone na hosting, koszty utrzymania oraz osoby prowadzące projekt. To nie jest polityka wynagrodzeń; nie ma procentów; to właśnie tam pieniądze pójdą.

I jeszcze jedna rzecz ludzka. Gdy kiedykolwiek pojawi się prawdziwa możliwość płatnej pracy wokół Bernsteina, osoby, które już wykonały pracę i których już znam, oczywiście będą wśród pierwszych, na których patrzeć. Nie jest to obietnica, nie jest to program, i nie jest to "pracuj za darmo teraz, zostaniesz zatrudniony później". To jedynie zwykła logika, że jeśli już lubię, jak ktoś myśli i pracuje, zapamiętam ich przed obcym.

## Kto patrzy, i dlaczego to ma znaczenie

Oczywiście nie możemy opublikować całego naszego pipeline'a analitycznego, ale z pozorów nie jesteśmy jedynymi czytającymi. Osoby z dużych firm IT i laboratoriów AI pojawiają się na stronie dosyć regularnie i zadają bardzo konkretne pytania dotyczące dokumentacji. Konkurenci, zakładając, że śpią.

A przy okazji: według naszej pasywnej analityki, z wyraźnym hałasem botów usuniętym, Bernstein działa regularnie na przynajmniej około 3000 maszyn na całym świecie. Uważam, że jest to całkiem przyzame. Bernstein nie wysyła domyślnie żadnej telemetrii produktu, a ta liczba nie pochodzi z żadnego źródła; jest to sygnał pasywny, i tyle powiem o metodzie.

Sytuacja jest już ciekawa. Budujemy darmowy produkt, którego ludzie rzeczywiście używają, przyciąga uwagę inżynierów, laboratoriów AI i dużych firm, a każdy użytkownik może wziąć kod źródłowy i nim władać. Moja opinia, wyraźnie opinią: gdy możesz uzyskać poważne narzędzie za darmo i posiadać kod, staje się trudne wytłumaczyć, dlaczego ktokolwiek miałby płacić dziesiątki tysięcy dolarów miesięcznie za coś podobnego.

Chcę, aby ten projekt wydawał się duży. Nie po to, abyśmy mogli mówić ludziom, że jesteśmy świetni, ale po to, aby każdy pracujący tu rozumiał, że wykonuje coś, co może mieć znaczenie znacznie poza jednym żądaniem ściągnięcia. Belka w mojej głowie jest mniej więcej na poziomie Ansible, Kubernetes, Terraform. Nie "Bernstein to następny Kubernetes". Raczej: jeśli Bernstein kiedykolwiek stanie się odniesieniem w swojej kategorii, nie chcę, abyśmy patrzyli wstecz i myśleli, że zrobiliśmy to pośpiesznie. Powinniśmy to zrobić dobrze.

## Szczere zastrzeżenia

Nie mogę obiecać regularności. "Mniej więcej co dwa tygodnie" to cel, nie poziom usługi. Jest całkowicie możliwe, że pierwszy post będzie za dwa tygodnie, potem miesiąc minie, potem okaże się, że chcieliśmy najpierw zautomatyzować trzy inne rzeczy, a wszystko, co miało zająć tygodnie, cicho ciągnie się aż do końca roku kalendarzowego. Spróbujemy. Nie obiecuję tego, czego nie jestem pewien, że mogę dostarczyć.

W okolicach połowy listopada, jeśli Bóg pozwoli i pozwoli na to pojemność, chciałbym zorganizować krótkie połączenie Zoom. Tylko się spotkać, porozmawiać o tym, dokąd każde z was zmierza. Brak obowiązku uczestnictwa, brak dokładnej daty jeszcze.

Sz wypowiadamy różnymi językami, więc oryginał pozostaje po angielsku, a kilka tłumaczeń następuje poniżej w komentarzach. To jest mała próba uprzyjemnienia wam rzeczy, nic więcej.

Po mojej stronie, to głównie ja i kot. Po waszej stronie, to wszystko pozostałe.

Dzielenie się jest troską. Staram się dać wam tyle z powrotem, ile mogę za waszą pracę i po prostu za bycie tutaj. Uważajcie, że jestem bardzo przywiązany do wszystkich was. Spośród innych rzeczy, to właśnie to sprawia, że wstaje rano i siadam przed komputerem, po czym odkrywam, że minęło piętnaście godzin. Zobaczmy, co zbudujemy z tego wszystkiego.

Alex