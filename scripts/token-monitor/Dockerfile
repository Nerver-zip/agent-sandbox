FROM node:24-bookworm-slim
ARG COLLECTOR_VERSION
ARG COLLECTOR_REVISION
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 socat \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/token-monitor
RUN git init -q \
    && git fetch --depth 1 https://github.com/Javis603/token-monitor.git "refs/tags/${COLLECTOR_VERSION}" \
    && git checkout -q --detach FETCH_HEAD \
    && { test -z "${COLLECTOR_REVISION}" || test "$(git rev-parse HEAD)" = "${COLLECTOR_REVISION}"; }
RUN npm ci --omit=dev --ignore-scripts \
    && npm run ensure:tokscale \
    && rm -rf .git /root/.npm
RUN TOKSCALE_CONFIG_DIR=/opt/tokscale-pricing node_modules/.bin/tokscale pricing refresh --json --no-spinner \
    && test -s /opt/tokscale-pricing/cache/pricing-litellm.json
COPY collect.py restore.cjs /opt/
ENTRYPOINT ["/usr/bin/python3", "-I", "/opt/collect.py"]
