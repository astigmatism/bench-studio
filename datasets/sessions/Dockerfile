FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS node
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN apt-get update && apt-get install -y --no-install-recommends git tmux ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt test-requirements.txt /opt/requirements/
RUN pip install --no-cache-dir -r /opt/requirements/requirements.txt -r /opt/requirements/test-requirements.txt ruff==0.11.13
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN npm install --prefix /opt/browser --ignore-scripts --no-audit --no-fund playwright@1.58.2 && /opt/browser/node_modules/.bin/playwright install --with-deps chromium
COPY datasets/sessions/base/frontend/package*.json /opt/frontend/
RUN npm ci --prefix /opt/frontend --ignore-scripts --no-audit --no-fund
COPY datasets/sessions/base /opt/base
COPY datasets/sessions/tools /opt/tools
RUN useradd --uid 1000 --create-home bench && mkdir -p /workspace /logs/agent /logs/verifier && chown -R bench:bench /workspace /logs && chmod -R a+rX /opt
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER bench
CMD ["sleep","infinity"]
