from gerry.pkw_parser import (
    NumberRange,
    ObwodRules,
    StreetRule,
    normalize_text,
    parse_city_description,
    parse_opis_granic,
    parse_range_fragment,
    resolve_obwod,
    streets_equal,
)


def test_number_range_contains():
    assert NumberRange(1, 13).contains(1)
    assert NumberRange(1, 13).contains(13)
    assert NumberRange(1, 13).contains(7)
    assert not NumberRange(1, 13).contains(14)
    assert not NumberRange(1, 13).contains(0)


def test_number_range_open_ended():
    rng = NumberRange(10, None)
    assert rng.contains(10)
    assert rng.contains(1000)
    assert not rng.contains(9)


def test_normalize_text_strips_prg_pelne_slowa_warszawa():
    # PRG zapisuje ulice Warszawy pełnymi słowami ("ulica X", "Aleja X"), a nie
    # skrótami jak rejestr PKW ("ul. X", "al. X") — bez normalizacji obu
    # konwencji do tej samej postaci żaden adres Warszawy się nie dopasowywał.
    assert normalize_text("ulica Urokliwa") == normalize_text("ul. Urokliwa")
    assert normalize_text("Aleja Niepodległości") == normalize_text("al. Niepodległości")


def test_streets_equal_exact_match():
    assert streets_equal("Długa", "Długa")
    assert streets_equal("ul. Długa", "długa")  # "ul." usuwane przez normalize_text


def test_streets_equal_rejects_partial_word_match():
    # Dawny luźny substring znakowy dawał fałszywe trafienia typu "Widok" w "Widokowa"
    # (różne słowa/ulice) — obecne dopasowanie działa na całych słowach, nie znakach.
    assert not streets_equal("Widok", "Widokowa")
    assert not streets_equal("Kolej", "Kolejowa")


def test_streets_equal_allows_whole_word_prefix():
    assert streets_equal("Nowa Kolejowa", "Nowa Kolejowa")


def test_normalize_text_strips_prefixes_and_diacritics():
    assert normalize_text("ul. Świętego Jana") == "świetego jana" or normalize_text("ul. Świętego Jana") == "swietego jana"
    assert "ul." not in normalize_text("ul. Krótka")


def test_parse_range_fragment_dash_range():
    ranges, parity, street = parse_range_fragment("Dajwór 2-20")
    assert parity == "all"
    assert any(r.start == 2 and r.end == 20 for r in ranges)


def test_parse_range_fragment_parity():
    ranges, parity, street = parse_range_fragment("Krótka (parzyste)")
    assert parity == "even"
    assert street == "Krótka"


def test_parse_range_fragment_do_konca():
    ranges, parity, street = parse_range_fragment("Długa od 5 do końca")
    assert any(r.end is None for r in ranges)


def test_parse_range_fragment_nieparzyste_not_confused_with_parzyste():
    # "nieparzyste" zawiera "parzyste" jako podciąg — regresja na bug, gdzie
    # sprawdzanie "parzyst" in słowo dawało "even" nawet dla "nieparzyste".
    ranges, parity, street = parse_range_fragment("Krótka (nieparzyste)")
    assert parity == "odd"
    ranges, parity, street = parse_range_fragment("Krótka (parzyste)")
    assert parity == "even"


def test_parse_range_fragment_warszawa_strona_bez_nawiasu():
    # Warszawa zapisuje parzystość jako "strona nieparzysta/parzysta" (bez
    # nawiasu) i zakresy jako "od nr X do Y" (z wtrąconym "nr").
    ranges, parity, street = parse_range_fragment("ul. Radiowa strona nieparzysta od nr 1 do 33")
    assert parity == "odd"
    assert street == "ul. Radiowa"
    assert any(r.start == 1 and r.end == 33 for r in ranges)


def test_parse_city_description_warszawa_multi_parity_continuation():
    # Ta sama ulica z osobnymi zakresami dla nieparzystych i parzystych w
    # kolejnych segmentach po przecinku — musi dać dwie StreetRule (bo jedna
    # reguła ma tylko jedno pole parity), a nie nadpisywać się nawzajem.
    _, _, streets = parse_city_description(
        "ul. Radiowa strona nieparzysta od nr 1 do 33, strona parzysta nr 2, od nr 20 do 26"
    )
    odd_rules = [s for s in streets if s.name == "ul. Radiowa" and s.parity == "odd"]
    even_rules = [s for s in streets if s.name == "ul. Radiowa" and s.parity == "even"]
    assert len(odd_rules) == 1
    assert any(r.start == 1 and r.end == 33 for r in odd_rules[0].ranges)
    assert len(even_rules) == 1
    even_numbers = {r.start for r in even_rules[0].ranges if r.start == r.end}
    even_ranges = [r for r in even_rules[0].ranges if r.start != r.end]
    assert 2 in even_numbers
    assert any(r.start == 20 and r.end == 26 for r in even_ranges)


def test_parse_opis_granic_wies():
    rules = parse_opis_granic(1, "wieś", "Nowa Wieś, Stara Wieś")
    assert "Nowa Wieś" in rules.villages
    assert "Stara Wieś" in rules.villages


def test_parse_opis_granic_wies_strips_type_prefix():
    # Rejestr PKW poprzedza nazwy miejscowości słowem opisującym ich typ
    # ("wieś X", "kolonia Y", "Sołectwo: Z") — PRG i granice gmin mają samą
    # nazwę bez prefiksu. Bez usunięcia go 0% adresów dopasowywało się w
    # większości gmin wiejskich (wykryte dopiero przy próbie wygenerowania
    # granic dla całej Polski — Kraków i wcześniej wygenerowane miasta są
    # typu "miasto", nie dotykają tej gałęzi parsera).
    rules = parse_opis_granic(1, "wieś", "wieś Droszków, wieś Jaszkowa Górna, wieś Rogówek, kolonia Gaj")
    assert rules.villages == ["Droszków", "Jaszkowa Górna", "Rogówek", "Gaj"]

    rules_single = parse_opis_granic(2, "wieś", "wieś Boguszyn")
    assert rules_single.villages == ["Boguszyn"]

    rules_solectwo = parse_opis_granic(3, "wieś", "Sołectwo: Podgórze")
    assert rules_solectwo.villages == ["Podgórze"]


def test_parse_opis_granic_wies_miejscowosci_plural_prefix():
    # "miejscowo[śs][ćc]|miejscowo[śs]ci" w alternacji dawało dopasowanie tylko
    # do krótszej l.poj. formy nawet dla tekstu w l.mn. ("Miejscowości: X"),
    # zostawiając nieusunięty ogon "i: X" — regresja na literalny tekst z
    # rejestru (gm. w województwie warmińsko-mazurskim).
    rules = parse_opis_granic(1, "wieś", "Miejscowości: Henrykowo, Kadłubówka, Zasonie.")
    assert rules.villages == ["Henrykowo", "Kadłubówka", "Zasonie"]


def test_parse_opis_granic_wies_solectwo_groups_with_miejscowosci():
    # Rejestr w części regionów (np. woj. kujawsko-pomorskie) grupuje wiele
    # miejscowości pod sołectwem: "Sołectwo X (miejscowości: A, B)" — do
    # dopasowania liczy się TYLKO lista w nawiasie (sołectwo to jednostka
    # administracyjna, nie występuje jako PRG miejscowosc). Znaleziony przy
    # próbie generowania granic dla całej Polski: 0.2% dopasowanych adresów
    # dla gm. Gostycyn zanim to naprawiono.
    rules = parse_opis_granic(
        1,
        "wieś",
        "sołectwa: Wielki Mędromierz; Łyskowo (miejscowości: Łyskowo, Świt, Żółwiniec)",
    )
    assert rules.villages == ["Wielki Mędromierz", "Łyskowo", "Świt", "Żółwiniec"]

    rules_comma_separated = parse_opis_granic(
        2,
        "wieś",
        "sołectwa: Dębowo (miejscowości: Dębina, Dębowo, Sosnówiec), Kołodziejewo (miejscowości: Kołodziejewo, Pałuczyna)",
    )
    assert rules_comma_separated.villages == ["Dębina", "Dębowo", "Sosnówiec", "Kołodziejewo", "Pałuczyna"]


def test_parse_opis_granic_wies_solectwo_with_ulice_is_hybrid():
    # Rzadki hybrydowy format: "Sołectwo X-część, ulice: A, B, C" — sołectwo
    # (prawdopodobnie niedopasowywalne) trafia do villages, a WSZYSTKIE ulice
    # (nie tylko pierwsza po "ulice:") do streets — split_segments już rozbił
    # je na osobne segmenty przecinkami, trzeba pamiętać tryb "jesteśmy w
    # ulicach" między iteracjami.
    rules = parse_opis_granic(1, "wieś", "sołectwo Gostycyn-część, ulice: Budnikowo, Główna, Krótka")
    assert rules.villages == ["Gostycyn-część"]
    names = [s.name for s in rules.streets]
    assert names == ["Budnikowo", "Główna", "Krótka"]


def test_match_specificity_checks_both_villages_and_streets():
    # Wcześniejszy kod zwracał None natychmiast, gdy villages było niepuste a
    # przekazana miejscowość się nie zgadzała — NIGDY nie sprawdzając streets,
    # nawet jeśli oba pola były wypełnione (hybrydy sołectwo+ulice). Adres na
    # pasującej ulicy w NIEPASUJĄCEJ miejscowości musi się nadal dopasować.
    rules = ObwodRules(obwod=1, typ_obszaru="wieś", raw="")
    rules.villages = ["Gostycyn-część"]
    rules.streets = [StreetRule(name="Budnikowo")]

    assert rules.match_specificity("Budnikowo", 5, village="Gostycyn") == 0
    assert rules.match_specificity(None, None, village="Gostycyn-część") == 3
    assert rules.match_specificity("Budnikowo", 5, village="Gostycyn-część") == 3


def test_parse_opis_granic_miasto_streets():
    rules = parse_opis_granic(1, "miasto", "Długa, Krótka")
    names = [s.name for s in rules.streets]
    assert "Długa" in names
    assert "Krótka" in names


def test_parse_city_description_semicolon_separated_streets():
    # ~5% wpisów "miasto" w kraju rozdziela ulice średnikiem zamiast
    # przecinkiem (np. m. Skarżysko-Kamienna) — bez normalizacji cała lista
    # trafiała jako jeden, nigdy niepasujący "segment" (8,9% -> 84,5%
    # dopasowanych adresów po naprawie).
    _, _, streets = parse_city_description(
        "ulice: Bazaltowa; Borówkowa; 1 Maja od nr 134 do końca"
    )
    names = [s.name for s in streets]
    assert names == ["Bazaltowa", "Borówkowa", "1 Maja"]
    maja = next(s for s in streets if s.name == "1 Maja")
    assert any(r.end is None for r in maja.ranges)


def test_parse_opis_granic_warszawa_dzielnica_parses_as_streets():
    # Rejestr PKW koduje 18 dzielnic Warszawy z Typ obszaru "dzielnica w m.st.
    # Warszawa" zamiast "miasto" — bez tej gałęzi cały opis trafiał do
    # rules.villages jako jedna (fałszywa) nazwa wsi i nic się nie dopasowywało.
    rules = parse_opis_granic(711, "dzielnica w m.st. Warszawa", "ul. Bawełniana, ul. Grotowska")
    assert rules.villages == []
    names = [s.name for s in rules.streets]
    assert any("Bawełniana" in n for n in names)
    assert rules.matches("Bawełniana", 10)


def test_resolve_obwod_no_match_returns_none():
    rules_list = [parse_opis_granic(1, "miasto", "Długa")]
    winner, count = resolve_obwod(rules_list, "Zupełnie Inna", 5)
    assert winner is None
    assert count == 0


def test_resolve_obwod_single_match():
    rules_list = [parse_opis_granic(1, "miasto", "Długa")]
    winner, count = resolve_obwod(rules_list, "Długa", 5)
    assert winner == 1
    assert count == 1


def test_resolve_obwod_prefers_more_specific_rule():
    # Obwód 1: cała ulica Długa. Obwód 2: Długa tylko parzyste 1-50.
    rules1 = StreetRule(name="Długa")
    rules2 = StreetRule(name="Długa", parity="even", ranges=[NumberRange(1, 50)])
    obwod1 = parse_opis_granic(1, "miasto", "Długa")
    obwod1.streets = [rules1]
    obwod2 = parse_opis_granic(2, "miasto", "Długa parzyste 1-50")
    obwod2.streets = [rules2]

    winner, count = resolve_obwod([obwod1, obwod2], "Długa", 20)
    assert count == 2  # oba pasują surowo
    assert winner == 2  # ale obwod2 ma bardziej specyficzną regułę (zakres+parzystość)


def test_resolve_obwod_true_ambiguity_returns_none():
    # Dwa obwody z identyczną specyficznością dla tej samej ulicy — prawdziwy konflikt.
    obwod1 = parse_opis_granic(1, "miasto", "Wspólna")
    obwod2 = parse_opis_granic(2, "miasto", "Wspólna")
    winner, count = resolve_obwod([obwod1, obwod2], "Wspólna", 5)
    assert winner is None
    assert count == 2
