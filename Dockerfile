FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM docker:cli@sha256:9f36dfce2d1fd053d700a4eca00c358df79bf7d8cb69d4a9e8d9981af18834ea AS dockercli

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS datasets
WORKDIR /src
RUN pip install --no-cache-dir pyarrow==25.0.1
COPY scripts/prepare-coding-data.py /src/scripts/prepare-coding-data.py
COPY datasets/coding-manifest.json /src/datasets/coding-manifest.json
RUN python scripts/prepare-coding-data.py

FROM scratch AS source
COPY common.py invoke.py worker.py /app/
COPY vendor /app/vendor
COPY profiles /app/profiles
COPY studio /app/studio

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 BETTERBENCH_NO_UPDATE_CHECK=1 HOME=/tmp PYTHONPATH=/app:/app/vendor
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY datasets/coding-manifest.json /app/datasets/coding-manifest.json
COPY --from=datasets /src/datasets/cache /app/datasets/cache

FROM base AS app
COPY --from=ui /ui/dist /app/frontend/dist
COPY --from=source /app /app
ARG SOURCE_REVISION=development
ENV SOURCE_REVISION=$SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/astigmatism/bench-studio" org.opencontainers.image.revision=$SOURCE_REVISION
USER 1000:1000
CMD ["uvicorn","studio.api:app","--host","0.0.0.0","--port","8080"]

FROM base AS worker
COPY --from=source /app /app
ARG SOURCE_REVISION=development
ENV SOURCE_REVISION=$SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/astigmatism/bench-studio" org.opencontainers.image.revision=$SOURCE_REVISION
USER 1000:1000
CMD ["python","/app/worker.py"]

FROM base AS runner
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates util-linux && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins /usr/local/lib/docker/cli-plugins
COPY requirements-runner.txt ./
RUN pip install --no-cache-dir -r requirements-runner.txt
COPY --from=source /app /app
ARG SOURCE_REVISION=development
ENV SOURCE_REVISION=$SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/astigmatism/bench-studio" org.opencontainers.image.revision=$SOURCE_REVISION
USER 1000:1000
CMD ["python","-m","studio.runner"]

FROM runner AS updater
USER root
COPY scripts /app/scripts
USER 1000:1000
CMD ["sh"]

FROM base AS verifier
USER root
COPY --from=ui /usr/local/bin/node /usr/local/bin/node
COPY --from=ui /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && npm install -g typescript@5.9.3 --no-audit --no-fund
COPY requirements-verifier.txt ./
RUN pip install --no-cache-dir -r requirements-verifier.txt
COPY scripts/fetch-multiple-verifier.py /tmp/fetch-multiple-verifier.py
RUN python /tmp/fetch-multiple-verifier.py
ENV PYTHONPATH=/app:/app/vendor:/opt/multiple
COPY --from=source /app /app
ARG SOURCE_REVISION=development
ENV SOURCE_REVISION=$SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/astigmatism/bench-studio" org.opencontainers.image.revision=$SOURCE_REVISION
USER 1000:1000
CMD ["python","/app/studio/verify.py","/task"]
