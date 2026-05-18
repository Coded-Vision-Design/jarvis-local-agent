FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1

# OS deps: git for repo ops, curl + ca-certs for gh/Node installers, openssh for git over ssh,
# build-essential only if we later need to compile any python wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        openssh-client \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node 20 (for Claude Code + qwen-code CLIs)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (for `gh pr create`)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Sub-agent CLIs. Pinned-ish; updated by rebuilding the image.
RUN npm install -g \
        @anthropic-ai/claude-code \
        @qwen-code/qwen-code

# Python deps
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Identity for any commits the container creates directly (sub-agents override
# via -c flags per-commit, but the global default belongs to CVD).
RUN git config --global user.email "contact@codedvisiondesign.co.uk" \
    && git config --global user.name "Coded Vision Design" \
    && git config --global init.defaultBranch main \
    && git config --global safe.directory '*'

COPY src ./src

EXPOSE 17920
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "17920", "--log-level", "info"]
