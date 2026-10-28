FROM node:24-bookworm-slim

# 1. Install system dependencies + C++ build tools for native modules
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    build-essential \
    python3 \
    python3-pip \
    python3-venv \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# 2. Force Git to use HTTPS instead of SSH for npm sub-dependencies
RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \
    git config --global --add url."https://github.com/".insteadOf "git@github.com:"

# 3. Build the pinned OpenClaw Telethon migration instead of the npm release.
ARG OPENCLAW_REF=f0b4a9f7ce9e869f43998d75e5fc8cb7856ccb24
COPY --from=oven/bun:1.4.3 /usr/local/bin/bun /usr/local/bin/bun
RUN git init /opt/openclaw && \
    git -C /opt/openclaw remote add origin https://github.com/ArnavSingh76533/openclaw-for-telethon.git && \
    git -C /opt/openclaw fetch --depth 1 origin "$OPENCLAW_REF" && \
    git -C /opt/openclaw checkout --detach FETCH_HEAD
RUN corepack enable && \
    cd /opt/openclaw && \
    NODE_OPTIONS=--max-old-space-size=2048 pnpm install --frozen-lockfile && \
    ln -s /opt/openclaw/openclaw.mjs /usr/local/bin/openclaw && \
    chown -R 1000:1000 /opt/openclaw
RUN python3 -m venv /opt/telethon && \
    /opt/telethon/bin/python -m pip install --no-cache-dir /opt/openclaw/extensions/telegram/python && \
    /opt/telethon/bin/python -m unittest discover -s /opt/openclaw/extensions/telegram/python/tests -v

# 4. Prepare directories & permissions for Hugging Face UID 1000
RUN chown -R 1000:1000 /home/node

USER 1000
WORKDIR /home/node

# Apply Git HTTPS rule for the non-root user as well
RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/"

ENV HOME=/home/node \
    OPENCLAW_HOME=/home/node \
    OPENCLAW_STATE_DIR=/home/node/.openclaw \
    OPENCLAW_WORKSPACE_DIR=/home/node/.openclaw/workspace \
    OPENCLAW_GATEWAY_PORT=18789 \
    OPENCLAW_TELETHON_PYTHON=/opt/telethon/bin/python \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    NODE_ENV=production

EXPOSE 7860

# 5. Strip all proxy/forwarded headers so OpenClaw sees pure 127.0.0.1 loopback and auto-approves token holders
RUN node -e 'const fs = require("fs"); \
const code = `const http = require("http"); \
const net = require("net"); \
function cleanHeaders(headers) { \
  const out = {}; \
  for (const [k, v] of Object.entries(headers)) { \
    const lk = k.toLowerCase(); \
    if (lk.startsWith("x-forwarded") || lk === "forwarded" || lk === "x-real-ip" || lk === "via" || lk.startsWith("cf-") || lk === "true-client-ip" || lk === "x-client-ip") continue; \
    out[k] = v; \
  } \
  out["host"] = "127.0.0.1:18789"; \
  if (out["origin"]) out["origin"] = "http://127.0.0.1:18789"; \
  return out; \
} \
const server = http.createServer((req, res) => { \
  const headers = cleanHeaders(req.headers); \
  const proxyReq = http.request({ hostname: "127.0.0.1", port: 18789, path: req.url, method: req.method, headers }, (proxyRes) => { \
    res.writeHead(proxyRes.statusCode, proxyRes.headers); \
    proxyRes.pipe(res); \
  }); \
  proxyReq.on("error", () => { if (!res.headersSent) res.writeHead(502); res.end("OpenClaw Gateway starting..."); }); \
  req.pipe(proxyReq); \
}); \
server.on("upgrade", (req, socket, head) => { \
  const headers = cleanHeaders(req.headers); \
  const upstream = net.connect(18789, "127.0.0.1", () => { \
    let raw = req.method + " " + req.url + " HTTP/" + req.httpVersion + "\\r\\n"; \
    for (const [k, v] of Object.entries(headers)) { \
      if (Array.isArray(v)) v.forEach(i => raw += k + ": " + i + "\\r\\n"); \
      else if (v !== undefined) raw += k + ": " + v + "\\r\\n"; \
    } \
    raw += "\\r\\n"; \
    upstream.write(raw); \
    if (head && head.length) upstream.write(head); \
    socket.pipe(upstream).pipe(socket); \
  }); \
  upstream.on("error", () => socket.destroy()); \
  socket.on("error", () => upstream.destroy()); \
}); \
server.listen(7860, "0.0.0.0", () => console.log("[hf-proxy] listening on 0.0.0.0:7860 -> 127.0.0.1:18789 (loopback mode)"));`; \
fs.writeFileSync("/home/node/proxy.js", code);'

# Keep the tested Telegram receipt fix pinned without reinstalling the full dependency layer.
ARG TELEGRAM_FIX_REF=4fd651f5c82b98c06edd82f81655deb692a72376
RUN git -C /opt/openclaw fetch --depth 1 origin "$TELEGRAM_FIX_REF" && \
    git -C /opt/openclaw checkout "$TELEGRAM_FIX_REF" -- extensions/telegram/python/openclaw_telethon/codec.py extensions/telegram/python/tests/test_codec.py && \
    PYTHONPATH=/opt/openclaw/extensions/telegram/python /opt/telethon/bin/python -m unittest discover -s /opt/openclaw/extensions/telegram/python/tests -v

# Install the reviewed Fortress release alongside the existing Chromium browser.
# Keep the activation file under OpenClaw state so verified checkpoints retain it.
RUN mkdir -p /home/node/fortress && \
    curl -fL --connect-timeout 15 --max-time 240 --retry 2 \
      https://github.com/tiliondev/fortress/releases/download/v153.0.8010.36/fortress-v153-linux-x64.tar.gz \
      -o /tmp/fortress.tar.gz && \
    printf '%s  %s\n' '27a2661d531bbbab14875992ba44fda61622d1aaf3ee0ee5fa5d69d0d02863b6' /tmp/fortress.tar.gz | sha256sum -c - && \
    tar -xzf /tmp/fortress.tar.gz -C /home/node/fortress --strip-components=1 && \
    rm /tmp/fortress.tar.gz && \
    ln -s /home/node/.openclaw/fortress-license /home/node/.tilion && \
    TILION_NO_ACTIVATE=1 /home/node/fortress/tilion --version

# Keep the optional AGY provider in the image; native backups omit managed npm packages.
RUN mkdir -p /home/node/agy-bin && \
    curl -fsSL --connect-timeout 15 --max-time 180 \
      https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.8-5447777392656384/linux-x64/cli_linux_x64.tar.gz \
      -o /tmp/agy-cli.tar.gz && \
    printf '%s  %s\n' 'b23683771438e72bffe0a2231168f65a16a0d73ed2c7a8460b311dc5fe0c3c9d9dfc6cbfb7d8755173340071aa4cc5c7a552fb5d1be3fdd31c2b7559d67e404a' '/tmp/agy-cli.tar.gz' | sha512sum -c - && \
    tar -xOf /tmp/agy-cli.tar.gz antigravity > /home/node/agy-bin/agy && \
    chmod 700 /home/node/agy-bin/agy && rm /tmp/agy-cli.tar.gz && \
    /home/node/agy-bin/agy --help
COPY --chown=1000:1000 openclaw-agy-0.1.2.tgz /tmp/openclaw-agy-0.1.2.tgz
RUN printf '%s  %s\n' '32f4768613a8a2bdbd37b7cde94ea5136899426a3e85b80dd22c6c578ab5957e' '/tmp/openclaw-agy-0.1.2.tgz' | sha256sum -c - && \
    mkdir -p /home/node/agy-plugin && \
    tar -xzf /tmp/openclaw-agy-0.1.2.tgz -C /home/node/agy-plugin --strip-components=1 && \
    mkdir -p /home/node/agy-plugin/node_modules && \
    ln -s /opt/openclaw /home/node/agy-plugin/node_modules/openclaw && \
    node --test /home/node/agy-plugin/test/bridge.test.mjs

# 6. Load Telegram credentials from private Space secrets.
COPY --chown=1000:1000 start-telegram.mjs space-state.mjs space-state.test.mjs /home/node/
RUN node --test /home/node/space-state.test.mjs
# shortcut: compile in the 16 GB runtime on cold starts; use a prebuilt image to remove this delay.
CMD ["sh", "-c", "cd /opt/openclaw && OPENCLAW_TSDOWN_MAX_OLD_SPACE_MB=6144 NODE_OPTIONS=--max-old-space-size=6144 ROLLDOWN_WORKER_THREADS=2 RAYON_NUM_THREADS=2 node --import ./scripts/tsx.mjs scripts/build-all.mts runtime && test -f dist/extensions/telegram/python/openclaw_telethon/service.py && openclaw --version && cd /home/node && exec node /home/node/start-telegram.mjs"]
