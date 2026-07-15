FROM python:3.12-slim AS solver-build

ARG SCIP_VERSION=v10.0.2
ARG SOPLEX_VERSION=v8.0.2
ARG PYSCIPOPT_VERSION=6.2.1
ARG VIPR_COMMIT=30f2951d1e90e47afa821bdd1b12b82246656c42

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git ninja-build python3-dev \
    libboost-dev libgmp-dev libmpfr-dev libtbb-dev zlib1g-dev \
    libreadline-dev libncurses-dev && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${SOPLEX_VERSION} https://github.com/scipopt/soplex.git /src/soplex && \
    cmake -S /src/soplex -B /src/soplex/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/scip \
      -DBUILD_SHARED_LIBS=ON -DGMP=ON -DMPFR=ON && \
    cmake --build /src/soplex/build --parallel && \
    cmake --install /src/soplex/build

RUN git clone https://github.com/scipopt/vipr.git /src/vipr && \
    git -C /src/vipr checkout ${VIPR_COMMIT} && \
    cmake -S /src/vipr/code -B /src/vipr/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/opt/scip \
      -DSOPLEX_DIR=/opt/scip/lib/cmake/soplex -DVIPRCOMP=ON && \
    cmake --build /src/vipr/build --parallel --target viprcomp viprchk && \
    cp /src/vipr/build/viprcomp /src/vipr/build/viprchk /opt/scip/bin/

RUN git clone --depth 1 --branch ${SCIP_VERSION} https://github.com/scipopt/scip.git /src/scip && \
    cmake -S /src/scip -B /src/scip/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/scip \
      -DCMAKE_PREFIX_PATH=/opt/scip -DSOPLEX_DIR=/opt/scip/lib/cmake/soplex \
      -DEXACTSOLVE=ON -DGMP=ON -DMPFR=ON -DBOOST=ON \
      -DZIMPL=OFF -DPAPILO=OFF -DSYM=none -DIPOPT=OFF && \
    cmake --build /src/scip/build --parallel && \
    cmake --install /src/scip/build

ENV SCIPOPTDIR=/opt/scip
ENV LD_LIBRARY_PATH=/opt/scip/lib
ENV PATH=/opt/scip/bin:${PATH}
RUN pip wheel --no-cache-dir --no-deps --no-binary=pyscipopt \
    "pyscipopt==${PYSCIPOPT_VERSION}" -w /wheels

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgeos-dev libgdal-dev gdal-bin curl \
    libgmp10 libmpfr6 libtbb12 zlib1g libreadline8 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=solver-build /opt/scip /opt/scip
COPY --from=solver-build /wheels /wheels
ENV SCIPOPTDIR=/opt/scip
ENV LD_LIBRARY_PATH=/opt/scip/lib
ENV PATH=/opt/scip/bin:${PATH}

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
COPY frontend ./frontend
RUN pip install --no-cache-dir /wheels/pyscipopt-*.whl . && \
    python -c "from gerry.scip_solver import exact_scip_available; ok, detail = exact_scip_available(); assert ok, detail" && \
    command -v viprcomp && command -v viprchk && gerry law-verify
EXPOSE 8000
CMD ["sh", "-c", "gerry migrate && uvicorn gerry.api:app --host 0.0.0.0 --port 8000"]
