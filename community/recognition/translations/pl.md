Cześć wszystkim.

Trochę nad tym myślałem i doszedłem do wniosku, że nic mi się nie stanie, jeśli mniej więcej co dwa tygodnie strona Bernsteina na LinkedIn będzie imiennie wymieniać każdego kontrybutora, który chce być wymieniony, i dziękować wszystkim w jednym wspólnym poście.

Jeden post. Nie jeden na osobę. Nasi obserwujący nie potrzebują dziesięciu postów pod rząd, a ja nie potrzebuję drugiego etatu na ich pisanie. Ale w tym jednym poście chcę spróbować napisać, dla każdej osoby, co naprawdę zrobiła, żeby potencjalny pracodawca, czytając to, rozumiał, co zbudowałeś w prawdziwym projekcie, i żebyś nie wstydził się tego udostępnić.

Zresztą już to zaczęliśmy. #5524 to strona na LinkedIn i sporo osób mówiących "okej, wymień mnie", a #6229 było pierwszą próbą zbiorczego podziękowania. Więc to nic nowego. To ja po prostu próbuję przestać składać to ręcznie za każdym razem. Ci, którzy już powiedzieli tak w #5524, nie muszą mówić tego ponownie — mam was zapisanych.

## O co chodzi

Cały przebieg, z twojej strony:

1. Zrobiłeś znaczącą pracę w Bernsteinie w ciągu ostatnich trzech miesięcy.
2. Obserwujesz stronę projektu: https://www.linkedin.com/company/bernstein-run/
3. Co jakiś czas strona publikuje jeden zbiorczy post z nazwiskami osób, które się zgodziły, i tym, co zrobiły.
4. Udostępniasz go, jeśli chcesz, najlepiej oznaczając Bernsteina.
5. Projekt zyskuje zasięg, którego inaczej by nie miał. Ty zyskujesz publiczny, profesjonalny zapis, którego nie napisałeś sam o sobie.

To wszystko. Nie ma punktu szóstego.

Jeśli akurat szukasz teraz pracy, jest alternatywa: mogę zamiast tego napisać ci rekomendację na LinkedIn, z mojego profilu (https://www.linkedin.com/in/alex-chernysh/). Żaden kłopot. Ale chcę być szczery co do kompromisu: rekomendacja zostaje na moim profilu i pomaga tobie; udostępnienie wspólnego posta pomaga tobie i projektowi jednocześnie. I szczerze mówiąc, prawie nikt i tak nie przewija do sekcji Rekomendacje. Więc proszę, korzystaj z rekomendacji głównie wtedy, gdy aktywnie szukasz pracy, a z udostępnienia w pozostałych przypadkach. Jeśli chcesz obu, powiedz tak.

O wartości tego wszystkiego. Jeśli naprawdę wykonałeś tę pracę, nie ukrywaj tego. Wpisz Bernsteina do CV, do portfolio, gdziekolwiek pasuje. Ja tak robię, niektórzy z was już tak robią, i uważam, że to dobry sygnał: scalony (merged) wkład w poważny projekt open source, który potrafisz wyjaśnić linijka po linijce, jest wart więcej niż linijka w CV mówiąca "zna Pythona". To moja opinia, nie badanie naukowe. To też nie jest żadna obietnica: to projekt wolontariacki, nikt nikogo nie zatrudnia, i wolę powiedzieć to wprost teraz, niż pozwolić komuś wyczytać z tego coś innego później.

## Jak powiedzieć tak

Wyślij jeden e-mail. Adres i temat dokładnie takie:

> **To:** forte@bernstein.run
> **Subject:** `BRNSTN-PR-LNKD`

W treści kilka linijek, które skrypt potrafi odczytać:

```
GITHUB=your-github-login
OPT_IN=YES
RECOMMENDATION=NO
```

Ustaw `RECOMMENDATION=YES`, jeśli chcesz rekomendacji na LinkedIn zamiast posta, albo oprócz niego. Dodaj linijkę `NOTE=`, jeśli jest coś, o czym powinienem wiedzieć (na przykład że chcesz zobaczyć treść przed publikacją; kilka osób o to prosiło i to żaden problem). Żeby się wypisać później, ten sam adres, ten sam temat, `OPT_IN=NO`.

Proszę, nie zmieniaj tematu wiadomości. Parser potrzebuje dokładnie tego ciągu znaków. Ilość przychodzącej poczty jest taka, że to nie jest kwestia gustu; wiadomość z innym tematem trafia do ogólnej kolejki i nie mogę obiecać, że kiedykolwiek ją zobaczę.

## Miękki próg i dlaczego jest miękki

Żeby zdecydować, kto trafia do danego posta, bez czytania każdego PR ręcznie, istnieje mechaniczny filtr: mniej więcej 1000 dodanych linii w scalonych pull requestach w ciągu ostatnich 30 dni, z wyłączeniem plików generowanych. To nie jest KPI. To nie jest metryka jakości. To nie jest ocena nikogo. To po prostu tani sposób, żeby skrypt co dwa tygodnie zebrał grupę bez udziału człowieka.

I to nie jest sztywna granica. Są tu ludzie robiący pracę związaną z bezpieczeństwem, recenzje, dokumentację, benchmarki, prace przy wydaniach i debugowanie, które mogą być ogromnie wartościowe przy czterdziestu liniach diffa. Jeśli wykonałeś znaczącą pracę i z jakiegoś powodu nie dobiłeś do tysiąca, napisz na ten sam adres z tym samym tematem i powiedz o tym. Nie jesteśmy potworami; coś wymyślimy. Zwykle chodzi o to, że znajdziemy ci kolejne zadanie i przekroczysz ten nieszczęsny tysiąc przy następnym wkładzie.

Nazwiska na okresowej liście idą w kolejności alfabetycznej. Nie według liczby PR-ów, nie według tego, kogo bardziej lubię. Alfabetycznie.

## GitHub Discussions i strona na LinkedIn nie konkurują ze sobą

Robią różne rzeczy. Dla szybkich wiadomości (wydanie, zmiana, aktualizacja społeczności) strona na LinkedIn jest szybszym kanałem. GitHub Discussions to miejsce, gdzie naprawdę uczestniczysz w projekcie: widzisz kontekst, dyskutujesz, proponujesz, znajdujesz odpowiedź, i zostaje to zapisane tam, gdzie kolejna osoba może to znaleźć. Jeśli chcesz zarówno wiadomości, jak i głębi, śledź oba. Decyzje nadal zapadają w issues i pull requestach; to się nie zmieniło.

## Dlaczego w ogóle to robię

Wielu z was jest młodych. Niektórzy niekoniecznie. Ale dla kogoś, kto zaczyna, prawdziwy projekt open source to dość rzadka okazja, żeby wypróbować nie tylko kod, ale recenzje, architekturę, testy, dokumentację, bezpieczeństwo, wydania, koordynację, nawet trochę publicznego pisania, i podejmować decyzje, które mają konsekwencje. Gdybym mógł pracować nad czymś takim, kiedy byłem na studiach, moje życie prawdopodobnie potoczyłoby się zupełnie inaczej. Więc chcę wspierać początkujących, jak tylko potrafię.

Mój model jest prosty: tnij kamienie milowe na kawałki, które jedna osoba może udźwignąć, daj komuś prawdziwe zadanie, zostaw decyzję jej, gdziekolwiek może być jej, nie odbieraj zadania po pierwszym błędzie, i pozwól ludziom brać na siebie coraz więcej odpowiedzialności w miarę postępów.

Do tych z was, którzy studiują teraz coś związanego z informatyką, albo dopiero zaczynają: widzę was. Jestem z wami. Dobrze wam idzie.

Jeszcze jedna opinia, wyraźnie oznaczona jako opinia. Umiejętność programowania staje się czymś podstawowym, jak umiejętność pisania na klawiaturze. Umiejętność myślenia, dostrzegania kompromisu (trade-off), podejmowania decyzji, której potrafisz bronić — to wciąż rzadkość, a w erze AI podejrzewam, że będzie coraz rzadsza i coraz cenniejsza, nie mniej. To miejsce, żeby trenować właśnie to.

## Jak jest prowadzony projekt, na razie

Do końca tego roku kalendarzowego zostaję jedynym maintainerem. To celowe. Nie oznacza to, że maintainer decyduje o wszystkim. Własność jest moja; nie uważam, że powinienem osobiście wybierać każdy przecinek architektury. Jeśli przyszedłeś zrobić PR, chcę, żebyś myślał, a nie zgadywał, czego chce maintainer.

Gdy będę miał więcej czasu, a akurat teraz jestem trochę zasypany innymi sprawami, chcę spróbować nadać pracy nieco więcej kształtu, z wirtualnymi "działami", tak jak ma je zwykła firma. Wszystko dobrowolne, bez zobowiązań, bez biurokracji dla samej biurokracji. Na początek widzę tylko dwa: R&D, który już funkcjonuje w issues i pull requestach i niewiele ode mnie potrzebuje, oraz PR / outreach, który chyba będę nadzorował trochę bardziej z bliska, bo komunikacja bez struktury szybciej niż cokolwiek innego zamienia się w "powinniśmy to kiedyś zrobić".

Później, jeśli będzie wystarczająco dużo aktywnych osób, mogą pojawić się role: dyrektor ds. rozwoju, jeden ds. operacji, jeden ds. bezpieczeństwa, jeden ds. QA i dokumentacji. Nie tworzę tych tytułów z wyprzedzeniem. Liczba ludzi określa strukturę, a nie odwrotnie. Nikomu nie jest potrzebna korporacja składająca się z sześciu osób.

## Pieniądze, bo ktoś zawsze pyta

Kiedy ktoś pyta, czy jest płatna możliwość pracy przy Bernsteinie, czasem wyrywa mi się nerwowy śmiech. Bernstein nie zarabia pieniędzy. Wydaje moje. Nawet nie liczę swojego czasu; po prostu patrzę, co znika z konta bankowego, w dość drogim kraju, podczas gdy jestem między pracami i okoliczności zapewne dadzą mi jeszcze przynajmniej parę tygodni na ten projekt. Więc to, co robię, nie jest pracą niepłatną. To praca na stratę. Lubię tę pracę. I to jest w sumie powód, dla którego tu wszyscy jesteśmy.

Darmowe dla użytkownika nie znaczy darmowe dla maintainera. Infrastruktura stojąca za tym kosztuje rzędu paru tysięcy dolarów miesięcznie, i ten rachunek przychodzi niezależnie od tego, czy ktoś coś opublikuje na LinkedIn, czy nie.

O licencjach, precyzyjnie, bo sam kiedyś źle to sobie poukładałem w głowie: Bernstein jest na licencji Apache-2.0. Apache-2.0 pozwala na użycie komercyjne; nie zobowiązuje nikogo do bycia niekomercyjnym. To, że Bernstein był, jest i, o ile zależy to ode mnie, pozostanie darmowym projektem open source, to stanowisko i intencja, a nie warunek licencji.

Darmowy projekt wciąż może mieć sponsorów, reklamy, umieszczenia partnerskie, odpowiednie integracje. Jeśli znasz laboratorium AI albo firmę z tej branży, dla której mogłoby to mieć sens, skieruj ich do mnie. Spróbuję w najbliższych tygodniach przygotować porządny pakiet partnerski, a potem zobaczymy, co da się z nim zrobić. Do tego czasu jest GitHub Sponsors, który istnieje i jest niewielki.

Jedna twarda zasada. Żadna płatna integracja nigdy nie kupuje merge'a. Jeśli firma chce zapłacić za konkretną pracę integracyjną, ta praca przechodzi przez zwykły proces techniczny, maintainer i społeczność zachowują prawo odmowy, a decyzja techniczna nie zmienia się dlatego, że pojawił się budżet. Gdyby jakaś płatna praca została kiedykolwiek faktycznie zatwierdzona, wpływy poszłyby na hosting, koszty utrzymania i osoby, które niosą projekt. To nie jest polityka wynagrodzeń; nie ma żadnych procentów; to tylko informacja, dokąd poszłyby pieniądze.

I jeszcze jedna ludzka sprawa. Jeśli kiedykolwiek pojawi się prawdziwa płatna okazja wokół Bernsteina, osoby, które już włożyły pracę i które już znam, będą oczywiście wśród pierwszych, na które spojrzę. To nie jest obietnica, ani program, ani "pracuj teraz za darmo, zatrudnimy cię później". To tylko zwykła logika, że skoro już podoba mi się, jak ktoś myśli i pracuje, zapamiętam tę osobę wcześniej niż obcego.

## Kto patrzy i dlaczego to ważne

Oczywiście nie możemy opublikować całego naszego pipeline'u analitycznego, ale wszystko wskazuje na to, że nie tylko my czytamy. Ludzie z dużych firm IT i laboratoriów AI pojawiają się na stronie dość regularnie i zadają dość konkretne pytania o dokumentację. Konkurencja, załóżmy, też nie śpi.

I przy okazji: według naszej pasywnej analityki, po usunięciu oczywistego szumu botów, Bernstein działa teraz regularnie na co najmniej około 3000 maszynach na całym świecie. Myślę, że to całkiem szanowany wynik. Bernstein domyślnie nie wysyła żadnej telemetrii produktowej i ta liczba nie pochodzi z żadnej; to sygnał pasywny, i to wszystko, co powiem o metodzie.

Więc sytuacja sama w sobie jest już ciekawa. Budujemy darmowy produkt, którego ludzie naprawdę używają, przyciągający uwagę inżynierów, laboratoriów AI i dużych firm, a każdy użytkownik może wziąć kod źródłowy i być jego właścicielem. Moja opinia, wyraźnie opinia: kiedy można dostać poważne narzędzie za darmo i być właścicielem kodu, trudno wytłumaczyć, dlaczego ktokolwiek miałby płacić dziesiątki tysięcy dolarów miesięcznie za coś podobnego.

Chcę, żeby ten projekt był odczuwany jako duży. Nie po to, żebyśmy mogli mówić ludziom, że jesteśmy wspaniali, ale żeby każdy, kto tu pracuje, rozumiał, że robi coś, co może mieć znaczenie znacznie wykraczające poza jeden pull request. Poprzeczka, którą mam w głowie, to mniej więcej Ansible, Kubernetes, Terraform. Nie "Bernstein to następne Kubernetes". Raczej: jeśli Bernstein kiedykolwiek stanie się referencyjną implementacją w swojej kategorii, nie chcę, żebyśmy patrzyli wstecz i myśleli, że zrobiliśmy to w pośpiechu. Powinniśmy zrobić to dobrze.

## Kilka szczerych zastrzeżeń

Nie mogę obiecać częstotliwości. "Mniej więcej co dwa tygodnie" to cel, a nie poziom usługi (SLA). Całkiem możliwe, że pierwszy post wypadnie za dwa tygodnie, potem minie miesiąc, potem okaże się, że najpierw chcieliśmy zautomatyzować jeszcze trzy rzeczy, i wszystko, co miało zająć tygodnie, po cichu zajmie do końca roku kalendarzowego. Będziemy się starać. Nie obiecam wam czegoś, czego nie jestem pewien, że mogę dostarczyć.

Gdzieś w połowie listopada, jeśli Bóg pozwoli i czas starczy, chciałbym zrobić krótkie spotkanie na Zoomie. Po prostu żeby się poznać i porozmawiać o tym, dokąd każdy z was chce zmierzać. Bez obowiązku uczestnictwa, na razie bez dokładnej daty.

Szanujemy, że ludzie w tym projekcie mówią różnymi językami, więc oryginał zostaje po angielsku, a poniżej, w komentarzach, znajdziesz kilka tłumaczeń. To mała próba, żeby było wam trochę przyjemniej, nic więcej.

Z mojej strony to głównie ja i kot. Z waszej strony to cała reszta.

Dzielenie się to troska (sharing is caring). Staram się oddać wam jak najwięcej za waszą pracę i po prostu za to, że tu jesteście. Uwierzcie mi, bardzo was wszystkich lubię. Między innymi właśnie to sprawia, że rano wstaję i siadam przed komputerem, po czym odkrywam, że minęło piętnaście godzin. Zobaczmy, co z tego wszystkiego zbudujemy.

Alex
