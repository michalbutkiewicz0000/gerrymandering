# Gerrymandering PL

Lokalna aplikacja do automatycznej rekonstrukcji polskich obwodów głosowania,
budowy grafu sąsiedztwa, symulacji wyników, walidacji reguł prawnych oraz
wyznaczania matematycznie optymalnych podziałów.

## Szybki start

```bash
cp .env.example .env
docker compose up --build
```

API: `http://localhost:8000/docs`, aplikacja: `http://localhost:8000/`.
Stan dokładnego solvera można sprawdzić przez `gerry doctor` albo endpoint
`GET /api/system/capabilities`; ten sam stan jest pokazany na stronie głównej.
Pełny test SCIP→VIPR w uruchomionym stosie wykonuje
`docker compose exec worker gerry solver-smoke`.

Jeżeli obok repozytorium znajduje się projekt `mapa_obwodow` z lokalnym cache
PKW/PRG, kompletny rzeczywisty przepływ dla małej gminy wykonuje:

```bash
gerry real-smoke ../mapa_obwodow --teryt 020302
```

Komenda nie modyfikuje projektu źródłowego. Kopiuje wyłącznie potrzebny cache
PRG do własnego katalogu roboczego i wykonuje rekonstrukcję, graf, import
12 komitetów z ZIP PKW Sejm 2023, solver, certyfikat oraz eksport GeoJSON.

Tryb bez Dockera:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev,solver]'
.venv/bin/pytest
.venv/bin/gerry doctor
```

## Jednostka podziału zależy od ordynacji

Obwód jest źródłem głosów, ale nie zawsze jednostką podziału. Węzłem grafu, na
którym pracuje solver, jest jednostka niepodzielna danej ordynacji: **powiat**
dla Sejmu, Senatu, PE i sejmiku (okręgu nie wolno tworzyć dzieląc powiat),
**gmina** dla rady powiatu, a **obwód** dopiero dla rady gminy. Głosy obwodowe
agregujemy w górę po kodzie TERYT, a granice powiatów/gmin bierzemy z warstwy
PRG — dlatego kosztowna rekonstrukcja geometrii obwodów jest potrzebna wyłącznie
dla rady gminy i tylko dla jednej wskazanej gminy.

W interfejsie WWW (i przez `POST /api/districting/assemble`) wybór rodzaju
wyborów sam ustala poziom węzła i agreguje głosy; dla wyborów samorządowych
kaskada województwo→powiat→gmina ogranicza obliczenia do wskazanej jednostki.
Poziom, zakres i kontener każdego profilu są zapisane w `legal_profiles.yaml`.
W CLI graf na powiatach/gminach buduje `gerry graph-build … --unit-level powiat`
(warstwa z kolumną `teryt` jest zlewana do węzłów tego poziomu).

## Przykładowy przepływ (ścieżka obwodowa — rada gminy)

Poniższy przebieg pokazuje rekonstrukcję obwodów potrzebną dla wyborów do rady
gminy. Dla wyborów krajowych pomiń `reconstruct` — graf powiatów i agregację
głosów złoży wizard lub `/api/districting/assemble`.

```bash
gerry snapshot-create sejm2023 2023-10-15
# Skopiuj pole `id` z odpowiedzi jako SNAPSHOT_ID.
gerry import-mapa-obwodow ../mapa_obwodow --election sejm2023
gerry reconstruct data/raw/imports/mapa_obwodow/sejm2023/obwody_glosowania_utf8.xlsx \
  --snapshot-id SNAPSHOT_ID
gerry scenario-import wyniki.xlsx sejm2023 data/artifacts/sejm2023.json --vote-columns "Komitet A,Komitet B"
gerry graph-build \
  data/processed/snapshots/SNAPSHOT_ID/precincts.gpkg \
  data/processed/snapshots/SNAPSHOT_ID/graph.json \
  --snapshot-id SNAPSHOT_ID

# transakcyjny zapis migawki, geometrii EPSG:2180 i grafu do PostGIS
docker compose exec -T worker gerry postgis-sync SNAPSHOT_ID
docker compose exec -T worker gerry law-verify
gerry optimize examples/small_request.json --output data/artifacts/run.json
```

Przebieg rekonstrukcji zapisuje raport po każdej gminie. Po usunięciu przyczyn
błędów można wznowić wyłącznie nieudane jednostki bez utraty wcześniejszego
cache i warstwy krajowej:

```bash
gerry reconstruct data/raw/imports/mapa_obwodow/sejm2023/obwody_glosowania_utf8.xlsx \
  --snapshot-id SNAPSHOT_ID --retry-failed
```

Do pilotażu służy `--teryt 020302,020402` albo `--limit N`. Pełny przebieg
można liczyć równolegle przez `--workers 0` (wszystkie logiczne CPU), ale na
stacji roboczej zalecany jest jawny limit, np. `--workers 4`, aby ograniczyć
temperaturę i pozostawić zasoby dla systemu. Manifest ustawia `complete_country: true`
dopiero po przetworzeniu całego rejestru bez błędów i obecności pliku cache
każdej gminy. CLI najpierw pokazuje liczbę wyników znalezionych w cache, a
następnie raportuje postęp co 25 obszarów oraz każdy błąd osobno.

API rekonstrukcji i grafu również wymaga `snapshot_id`. Ścieżki podawane do
API muszą znajdować się wewnątrz `GERRY_DATA_DIR`; wynik grafu jest zapisywany
atomowo w `processed/snapshots/<snapshot_id>/graph.json`.

Wynik jest końcowy wyłącznie ze statusem `OPTIMAL` i pozytywnie zweryfikowanym
certyfikatem. Checkpoint przerwanego solvera nie jest certyfikowany.

## Ograniczenia metodologiczne

- Automatyczna rekonstrukcja zapewnia wynik dla każdego terytorialnego obwodu,
  lecz bez oficjalnego poligonu nie gwarantuje idealnego przebiegu granicy.
- Obwody odrębne są przypisywane do jednostki zawierającej lokal komisji i nie
  są węzłami grafu.
- Walidator używa zamrożonego stanu prawa z 15 lipca 2026 r. Brak danych
  koniecznych do sprawdzenia reguły daje `UNVERIFIABLE`.
- Nowa mapa może być strukturalnie zgodna, ale formalnie wymaga ustanowienia
  właściwym aktem (`REQUIRES_ENACTMENT`).
- Solver referencyjny jest wyczerpujący i nie ma gwarancji czasu zakończenia.
- Standardowe koła PySCIPOpt mogą zawierać SCIP bez `EXACTSOLVE`; `gerry doctor`
  sprawdza realną kompilację. Taka biblioteka nigdy nie zostanie przedstawiona
  jako certyfikowana. Zadania do 14 węzłów korzystają z niezależnego solvera
  wyczerpującego.

Szczegóły znajdują się w `docs/architecture.md`, a wymagane dowody i komendy
odbiorcze w `docs/acceptance.md`.
