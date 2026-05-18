FROM codercom/code-server:latest
USER root

# Install Node LTS (24) via n + npm 11 + CLI tools.
# Bootstrap from NodeSource 22, then upgrade in-place with n.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates socat bubblewrap \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g n \
    && n lts \
    && npm install -g npm@latest \
    && hash -r \
    && npm install -g \
        @anthropic-ai/claude-code \
        @qwen-code/qwen-code \
        @openai/codex \
        cspell \
        @cspell/dict-en-gb \
        cline \
        pnpm \
    && npm cache clean --force

# pip3 + Python packages for MCP server and terminal scripting
RUN curl -fsSL https://bootstrap.pypa.io/get-pip.py \
        | python3 - --break-system-packages \
    && pip3 install --break-system-packages --no-cache-dir \
        httpx pydantic pyyaml mcp

# The base image already has a 'coder' user (uid 1000). We reuse it as the
# non-root terminal user — claude --dangerously-skip-permissions is blocked for
# root in recent Claude Code versions.
RUN echo 'coder ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers \
    && usermod -s /bin/bash coder

# Playwright + headless Chromium for visual/browser inspection tasks
RUN npm install -g @playwright/test \
    && npx playwright install chromium --with-deps \
    && npm cache clean --force

# ── Android development ──────────────────────────────────────────────────────
# Build environment for native Android apps. The agent can edit Kotlin/Java,
# run gradle builds, install APKs to a connected device/emulator via ADB.
# The actual emulator + Android Studio IDE stay on the Windows host - this
# container builds and tests, the host runs the emulator (KVM/HAXM-bound).
#
# Footprint: ~2 GB layer (JDK 17 + Android SDK platform tools + build tools
# + gradle 8.x). Reasonable for a dev image.
ENV ANDROID_HOME=/opt/android-sdk \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    GRADLE_HOME=/opt/gradle

RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jdk-headless \
        unzip \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Android command-line tools (sdkmanager + adb)
RUN mkdir -p $ANDROID_HOME/cmdline-tools \
    && cd /tmp \
    && wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O cmdtools.zip \
    && unzip -q cmdtools.zip -d $ANDROID_HOME/cmdline-tools \
    && mv $ANDROID_HOME/cmdline-tools/cmdline-tools $ANDROID_HOME/cmdline-tools/latest \
    && rm cmdtools.zip \
    && yes | $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager --licenses >/dev/null \
    && $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager \
        "platform-tools" \
        "platforms;android-35" \
        "build-tools;35.0.0" \
        >/dev/null

# Gradle 8.10 - matches modern AGP requirements
RUN cd /opt \
    && wget -q https://services.gradle.org/distributions/gradle-8.10-bin.zip -O gradle.zip \
    && unzip -q gradle.zip \
    && mv gradle-8.10 gradle \
    && rm gradle.zip

ENV PATH=$PATH:$JAVA_HOME/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$GRADLE_HOME/bin

# Configure ADB to reach the host emulator (Android Studio running on Windows).
# The agent connects via host.docker.internal:5037 and from there the host
# adb-server reaches the emulator. User must run `adb -a server nodaemon`
# on the host once (or via the host setup script) for this to work.
ENV ADB_SERVER_SOCKET=tcp:host.docker.internal:5037

# Helper scripts on PATH
COPY scripts/claude-delegate.sh /usr/local/bin/claude-delegate
COPY scripts/code-server-init.sh /usr/local/bin/code-server-init.sh
RUN sed -i 's/\r$//' /usr/local/bin/claude-delegate /usr/local/bin/code-server-init.sh \
    && chmod +x /usr/local/bin/claude-delegate /usr/local/bin/code-server-init.sh

# Shared Claude config baked in for hosts without a host bind-mount of
# ~/.claude. See claude-config/README.md for what belongs here vs not.
COPY claude-config /root/.claude/

# Override entrypoint so we can set up developer user env before code-server starts
ENTRYPOINT ["/usr/local/bin/code-server-init.sh"]
