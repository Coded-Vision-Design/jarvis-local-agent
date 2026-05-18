FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1

# OS deps: git for repo ops, curl + ca-certs for gh/Node installers, openssh for git over ssh,
# rsync for workspace mirroring, bubblewrap for Claude Code sandbox mode.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        openssh-client \
        gnupg \
        rsync \
        bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# Node LTS via n (gives Node 24 LTS + npm 11)
# Install a minimal node from NodeSource first, then use n to get the real LTS.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g n \
    && n lts \
    && npm install -g npm@latest \
    && hash -r

# GitHub CLI (for `gh pr create`)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Sub-agent CLIs + quality tools
RUN npm install -g \
        @anthropic-ai/claude-code \
        @qwen-code/qwen-code \
        @openai/codex \
        cspell \
        @cspell/dict-en-gb

# Qwen skill packs — baked in at build time so qwen-code discovers them via
# QWEN_HOME=/root/.qwen without any network call at runtime.
# We cap antigravity to 50 files to keep image size sane (1400+ skills total).
RUN mkdir -p /root/.qwen/skills \
    && git clone --depth=1 https://github.com/gsd-build/get-shit-done /tmp/gsd \
    && find /tmp/gsd -name "SKILL.md" -exec sh -c \
       'dir=$(basename $(dirname "$1")); mkdir -p "/root/.qwen/skills/$dir" && cp "$1" "/root/.qwen/skills/$dir/"' _ {} \; \
    && rm -rf /tmp/gsd \
    && git clone --depth=1 https://github.com/sickn33/antigravity-awesome-skills /tmp/antigravity \
    && find /tmp/antigravity -name "SKILL.md" | head -50 | while read f; do \
       dir="antigravity-$(basename $(dirname $f))"; \
       mkdir -p "/root/.qwen/skills/$dir" && cp "$f" "/root/.qwen/skills/$dir/"; \
       done \
    && rm -rf /tmp/antigravity \
    && git clone --depth=1 https://github.com/obra/superpowers /tmp/superpowers \
    && find /tmp/superpowers -name "SKILL.md" -exec sh -c \
       'dir="superpowers-$(basename $(dirname "$1"))"; mkdir -p "/root/.qwen/skills/$dir" && cp "$1" "/root/.qwen/skills/$dir/"' _ {} \; \
    && rm -rf /tmp/superpowers

# Python deps (includes playwright for visual/browser inspection tasks)
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt playwright \
    && playwright install chromium --with-deps

# Identity for any commits the container creates directly (sub-agents override
# via -c flags per-commit, but the global default belongs to CVD).
RUN git config --global user.email "contact@codedvisiondesign.co.uk" \
    && git config --global user.name "Coded Vision Design" \
    && git config --global init.defaultBranch main \
    && git config --global safe.directory '*'

COPY src ./src

# Shared Claude config (CLAUDE.md, skills/, commands/) baked in so a new
# host with an empty volume gets the baseline from the image rather than
# starting blank. On local dev the C:/Users/djohn/.claude bind-mount
# replaces this dir entirely (host config wins). On VPS the named volume
# is seeded from this content on first creation, then `claude login`
# writes the OAuth token into the same volume — persists across image
# upgrades.
COPY claude-config /root/.claude/

EXPOSE 17920
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "17920", "--log-level", "info"]
