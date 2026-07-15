# Matryca odbioru

Ten dokument definiuje dowody wymagane do odbioru programu. Zielony test
jednostkowy nie zastępuje próby integracyjnej wskazanej w tej samej pozycji.

| Zakres | Dowód w repozytorium | Bramka uruchomieniowa | Stan |
|---|---|---|---|
| Migawki PKW/PRG/TERYT | `tests/test_pipeline.py`, `tests/test_cli_import.py` | `gerry real-smoke ../mapa_obwodow --teryt 020302` | wykonane dla jednej gminy |
| Rekonstrukcja geometrii | `tests/test_reconstruction.py`, regresje parsera i `test_real_data_integration.py` | 4/4 obwody i 100% przypisania dla TERYT 020302 | wykonane dla jednej gminy; przebieg krajowy 2477/2477 wykonany (patrz sekcja krajowa) |
| Graf rook adjacency | `tests/test_graph.py`, testy API/CLI | wersjonowany `processed/snapshots/<id>/graph.json` z pełnym `node_ids` | wykonane; graf krajowy 28351 węzłów / 89823 krawędzie / 1 składowa |
| Import scenariuszy | `tests/test_scenario_import.py` | pełny ZIP PKW Sejm 2023: 12 komitetów bez pól frekwencji | wykonane |
| Arytmetyka wyborcza | `tests/test_elections.py` | porównanie znanych wyników JOW/D’Hondta | wykonane |
| Walidacja prawna | `tests/test_law_conditions.py`, profil YAML z SHA-256 | niezależna kontrola eksperta dla migawki prawa | implementacja głównych reguł wykonana; opinia eksperta oczekuje |
| Archiwum prawa | `tests/test_law_archive.py`, manifest SHA-256 i trzy PDF-y ELI | `gerry law-verify`, kontrola wheel i build obrazu | wykonane |
| Mały solver dokładny | `tests/test_solver.py` | replay certyfikatu enumeracji | wykonane |
| Duży solver dokładny | `tests/test_scip_solver.py` | `gerry solver-smoke`: zweryfikowane `OPTIMAL` i `INFEASIBLE` | manifest v2 odebrany w obrazie |
| Kolejka, geometrie i klucze PostGIS | `tests/test_postgres_integration.py`, `tests/test_postgis_sync.py` i migracje SQL | `/ready`, `gerry postgis-sync`, zdrowy PostGIS, API i worker | odebrane w CI oraz lokalnie na pełnej migawce krajowej: 28351 obwodów, 89823 krawędzie, SRID 2180, 0 niepoprawnych/pustych geometrii, idempotencja (patrz sekcja krajowa) |
| API i CLI | `tests/test_api.py`, `tests/test_cli_import.py` | `/docs`, walidowane stronicowanie zadań i przykładowy przepływ z README | wykonane |
| Interfejs WWW | `tests/test_frontend.py`, `tests/test_api.py` i `node --check frontend/app.js` | lokalna mapa SVG bez CDN, komplet reguł, wybór obszaru, spójne filtrowanie danych, alternatywy, eksport i panel certyfikatu | automatyczne bramki wykonane; pozostaje ręczny przegląd renderowania dużej migawki |
| Eksport | `tests/test_exports.py` | JSON, CSV, GeoJSON, GPKG i HTML, zapis atomowy | wykonane |
| Obraz i zależności exact | Dockerfile oraz job `exact-docker` w CI | `gerry doctor`, `gerry solver-smoke` i `/ready` | najnowszy obraz odebrany |

## Lokalna bramka jakości

```bash
.venv/bin/pytest -q -ra
.venv/bin/ruff check .
docker compose config --quiet
node --check frontend/app.js
git diff --check
```

Testy pominięte lokalnie są dopuszczalne wyłącznie wtedy, gdy komunikat mówi
o braku PostGIS albo SCIP EXACTSOLVE i odpowiadająca próba przechodzi w CI lub
w docelowym obrazie.

## Bramka docelowego obrazu

```bash
docker compose up -d --build
docker compose ps
docker compose exec -T worker gerry doctor
docker compose exec -T worker gerry solver-smoke
curl -fsS http://localhost:8000/ready
```

Odbiór wymaga zdrowych trzech usług, `Certyfikacja dużych zadań: OK`, dwóch
zweryfikowanych wyników smoke oraz manifestu v2 zawierającego dla każdego
etapu `model_sha256` i `proof_sha256`.

## Warunki odbioru pełnej eksploatacji krajowej

Przed traktowaniem instalacji jako zweryfikowanej dla całego kraju trzeba
zachować raport przebiegu wszystkich jednostek TERYT, skontrolować profile
z ekspertem wyborczym i wykonać pomiary pamięci/czasu na reprezentatywnej
dużej instancji. Są to próby danych i skali, których nie dowodzi syntetyczny
test programu.

### Dowody przebiegu i synchronizacji krajowej — 15 lipca 2026 r.

Migawka: `49b45322-b35c-4e6e-8dd7-ccfe498d3e00`.

Rekonstrukcja krajowa (`run_manifest.json`):

- `complete_country = true`, `successful = 2477`, `failed = 0`,
  `cached_municipalities = 2477`, `excluded_nonterritorial_precincts = 424`;
- `precincts.parquet`: 28351 wierszy, CRS EPSG:4326, 21867 Polygon +
  6484 MultiPolygon, 0 geometrii niepoprawnych/pustych, 0 duplikatów klucza.

Graf krajowy (`graph.json`): 28351 węzłów, 89823 krawędzie fizyczne,
1 składowa spójna, 0 izolatów, `errors = []`,
`build_parameters = {key_column: key, metric_crs: 2180, min_shared_border_m: 1.0,
boundary_tolerance_m: 0.01}`.

Bramka runtime obrazu (`docker compose exec -T worker …`):

- `gerry migrate` → `Schemat bazy jest aktualny.`
- `gerry doctor` → `Solver exact: OK (SCIP 10.0.2 EXACTSOLVE)`,
  `VIPR: OK (viprcomp=tak, viprchk=tak)`, `Certyfikacja dużych zadań: OK`,
  `Archiwum prawa: OK (Zweryfikowano 3 akty prawne)`.
- `gerry solver-smoke` → `Status: OPTIMAL; certyfikat: zweryfikowany`,
  `Brak rozwiązania: INFEASIBLE; certyfikat: zweryfikowany`.
- `curl -fsS http://localhost:8000/ready` → `{"status":"ready"}`.

Synchronizacja PostGIS (`gerry postgis-sync 49b45322-…`, ~9 s wall-clock):

- `Zsynchronizowano PostGIS: migawki=1, artefakty=0, obwody=28351, krawędzie=89823`;
- w bazie: `precincts = 28351`, `adjacency_edges = 89823`, SRID `{2180}`,
  `NOT ST_IsValid` = 0, `geometry IS NULL` = 0, wszystkie krawędzie `kind=physical`;
- brak krawędzi między migawkami wymuszony strukturalnie złożonymi kluczami
  obcymi `(snapshot_id, source)` i `(snapshot_id, target)` do `precincts`;
- ponowne wykonanie idempotentne: liczby obwodów/krawędzi/migawek niezmienione.

API na migawce krajowej: `/health` → `{"status":"ok"}`, `/ready` →
`{"status":"ready"}`, `/api/system/capabilities` potwierdza `exact_scip`,
`viprcomp`, `viprchk`, `certified_large_jobs`. Endpoint
`/api/snapshots/<id>/precincts` zwraca kompletną warstwę GeoJSON (28351 obiektów,
~118 MB, ~7 s).

Benchmark reprezentatywnej instancji optymalizacyjnej na rzeczywistych danych,
największa certyfikowana wielkość solvera dokładnego (TERYT 020205, 14 obwodów):
`status OPTIMAL`, `certificate_verified = true`, 2 alternatywy, 21 krawędzi,
12 komitetów; pełny przepływ rekonstrukcja→graf→import PKW→walidacja→solver→
certyfikat→eksport w ~9,8 s wall-clock łącznie ze startem kontenera.

### Pozostaje otwarte

- Niezależna kontrola profili prawnych przez eksperta wyborczego — nie może być
  oznaczona jako wykonana bez rzeczywistej opinii eksperta.
- Ręczny przegląd ergonomii renderowania i wyboru obszaru na pełnej warstwie
  krajowej w docelowej przeglądarce (dane endpointu potwierdzone, ergonomia
  kliknięć na ~118 MB warstwie wymaga oceny człowieka).
- Pomiar czasu i pamięci dla dużej instancji MIP SCIP powyżej 14 węzłów —
  solver dokładny nie ma limitu czasu zgodnie z założeniem projektu, więc taki
  benchmark wymaga świadomego wyboru rozmiaru i limitu zasobów.
