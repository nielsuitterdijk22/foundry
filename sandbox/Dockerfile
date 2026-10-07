# foundry-agent: the sandbox OpenCode runs in. Go + Rust + Node + Python tooling, no credentials.
FROM node:lts-trixie-slim AS node

FROM golang:1.27-trixie

# Node (copied from the official image)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Python + uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends python3 ripgrep jq \
 && rm -rf /var/lib/apt/lists/*

# Rust (stable, minimal + fmt/clippy), shared install
ENV RUSTUP_HOME=/usr/local/rustup PATH=/usr/local/cargo/bin:$PATH
RUN curl -fsSL https://sh.rustup.rs | CARGO_HOME=/usr/local/cargo sh -s -- -y --no-modify-path \
      --profile minimal --component rustfmt,clippy \
 && chmod -R a+rwX /usr/local/rustup /usr/local/cargo

# Go linters
RUN GOBIN=/usr/local/bin go install honnef.co/go/tools/cmd/staticcheck@latest \
 && GOBIN=/usr/local/bin go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest \
 && go clean -cache -modcache

# OpenCode (pinned; bump together with the host version)
ARG OPENCODE_VERSION=1.18.20
RUN npm install -g opencode-ai@${OPENCODE_VERSION} && opencode --version

RUN useradd -m -u 1000 agent && mkdir /cache && chown agent /cache
USER agent
# Caches live on a per-repo volume mounted at /cache
ENV HOME=/home/agent \
    GOPATH=/cache/go GOMODCACHE=/cache/go/mod GOCACHE=/cache/go/build \
    GOTOOLCHAIN=local GOPROXY=https://proxy.golang.org GOFLAGS=-buildvcs=false \
    CARGO_HOME=/cache/cargo CARGO_TARGET_DIR=/cache/target \
    npm_config_cache=/cache/npm UV_CACHE_DIR=/cache/uv \
    OPENCODE_DISABLE_AUTOUPDATE=1
ENV PATH=/cache/cargo/bin:/usr/local/cargo/bin:$PATH
WORKDIR /work
