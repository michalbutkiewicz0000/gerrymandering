# Architektura

Pipeline jest wersjonowany według migawki wyborów. Surowy artefakt ma URL,
lokalną ścieżkę i SHA-256. Geometrie obliczeniowe używają EPSG:2180, a eksport
mapowy EPSG:4326. Publiczne operacje rekonstrukcji i budowy grafu wymagają
`snapshot_id`; ich wyniki trafiają do `processed/snapshots/<snapshot_id>`, więc
cache i grafy różnych wyborów nie mogą się wzajemnie nadpisać.

1. `snapshots` pobiera lub importuje niezmienne artefakty PKW/PRG/TERYT.
2. `reconstruction` parsuje opis granic, przypisuje adresy i tworzy pełny
   podział gminy. Obwody bez terytorium są dołączane audytowalnie.
3. `graph` tworzy rook adjacency przy wspólnej granicy co najmniej 1 m. Granice
   PRG zapisane osobno po obu stronach granicy administracyjnej mogą różnić się
   numerycznie; dlatego domyślna tolerancja topologiczna 1 cm mierzy długość
   równoległych odcinków pozostających w takim pasie. Kontakt wyłącznie w
   punkcie nadal nie tworzy krawędzi. Próg i tolerancja są zapisywane w
   `build_parameters` artefaktu grafu i można je jawnie zmienić w API/CLI.
4. `elections` symuluje większość względną lub D'Hondta na liczbach całkowitych.
5. `law` sprawdza pokrycie, liczbę okręgów, spójność, ludność i ograniczenia
   konfiguracji, oddzielając zgodność strukturalną od obowiązujących granic.
6. `solver` dowodzi optimum przez wyczerpanie dla małych grafów; adapter SCIP
   stosuje ten sam kontrakt dla instancji produkcyjnych.

Publicznym kontraktem są modele Pydantic z `gerry.domain`; API oraz CLI używają
tych samych serwisów i nie duplikują logiki domenowej.

W Dockerze kolejka optymalizacji jest przechowywana w PostgreSQL. Worker
przejmuje pojedyncze zadanie w transakcji przez `FOR UPDATE SKIP LOCKED`, dzięki
czemu wiele workerów nie wykonuje tego samego zadania. Tryb bez Dockera używa
atomowego repozytorium plikowego. Artefakty geometrii i certyfikaty pozostają na
współdzielonym wolumenie danych.

Endpoint `/api/system/capabilities` raportuje realne możliwości podłączonej
biblioteki SCIP oraz dostępność `viprcomp` i `viprchk`. Numer wersji nie jest
traktowany jako dowód włączenia `EXACTSOLVE`.

Konkretne dowody, komendy i rozdzielenie odbioru implementacji od odbioru
eksploatacji krajowej opisuje [matryca odbioru](acceptance.md).
