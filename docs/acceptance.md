# Matryca odbioru

Ten dokument definiuje dowody wymagane do odbioru programu. Zielony test
jednostkowy nie zastępuje próby integracyjnej wskazanej w tej samej pozycji.

| Zakres | Dowód w repozytorium | Bramka uruchomieniowa | Stan |
|---|---|---|---|
| Migawki PKW/PRG/TERYT | `tests/test_pipeline.py`, `tests/test_cli_import.py` | `gerry real-smoke ../mapa_obwodow --teryt 020302` | wykonane dla jednej gminy |
| Rekonstrukcja geometrii | `tests/test_reconstruction.py`, regresje parsera i `test_real_data_integration.py` | 4/4 obwody i 100% przypisania dla TERYT 020302 | wykonane dla jednej gminy; przebieg krajowy oczekuje |
| Graf rook adjacency | `tests/test_graph.py`, testy API/CLI | wersjonowany `processed/snapshots/<id>/graph.json` z pełnym `node_ids` | wykonane |
| Import scenariuszy | `tests/test_scenario_import.py` | pełny ZIP PKW Sejm 2023: 12 komitetów bez pól frekwencji | wykonane |
| Arytmetyka wyborcza | `tests/test_elections.py` | porównanie znanych wyników JOW/D’Hondta | wykonane |
| Walidacja prawna | `tests/test_law_conditions.py`, profil YAML z SHA-256 | niezależna kontrola eksperta dla migawki prawa | implementacja głównych reguł wykonana; opinia eksperta oczekuje |
| Archiwum prawa | `tests/test_law_archive.py`, manifest SHA-256 i trzy PDF-y ELI | `gerry law-verify`, kontrola wheel i build obrazu | wykonane |
| Mały solver dokładny | `tests/test_solver.py` | replay certyfikatu enumeracji | wykonane |
| Duży solver dokładny | `tests/test_scip_solver.py` | `gerry solver-smoke`: zweryfikowane `OPTIMAL` i `INFEASIBLE` | manifest v2 odebrany w obrazie |
| Kolejka, geometrie i klucze PostGIS | `tests/test_postgres_integration.py`, `tests/test_postgis_sync.py` i migracje SQL | `/ready`, `gerry postgis-sync`, zdrowy PostGIS, API i worker | odebrane w CI: kolejka, klucze, SRID, poprawność geometrii i idempotencja synchronizacji |
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
jeszcze zachować raport przebiegu wszystkich jednostek TERYT, skontrolować
profile z ekspertem wyborczym i wykonać pomiary pamięci/czasu na reprezentatywnej
dużej instancji. Są to próby danych i skali, których nie dowodzi syntetyczny
test programu.
