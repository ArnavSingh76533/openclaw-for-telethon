import { createHash, randomUUID } from "node:crypto";
import { createServer, request as httpRequest, type Server } from "node:http";
import path from "node:path";
import { buffer } from "node:stream/consumers";
import { extractErrorCode } from "openclaw/plugin-sdk/error-runtime";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";
import { captureEnv } from "openclaw/plugin-sdk/test-env";
import { vi } from "vitest";

const fixture = vi.hoisted(() => ({
  beforeRequest: undefined as ((input: RequestInfo | URL) => Promise<void>) | undefined,
}));

// Start the fake Python listener for the current isolated state/token before
// discovery. The real bridge still owns health, auth, serialization and I/O.
vi.mock("./telethon-transport.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./telethon-transport.js")>();
  return {
    ...actual,
    createTelethonFetch: (proxy?: string): typeof fetch => {
      const invoke = actual.createTelethonFetch(proxy);
      return async (input, init) => {
        await fixture.beforeRequest?.(input);
        return invoke(input, init);
      };
    },
  };
});

async function encodeMultipart(form: FormData) {
  const boundary = `openclaw-telethon-fixture-${randomUUID()}`;
  const parts: Buffer[] = [];
  const escape = (value: string) =>
    value.replaceAll("\r", "%0D").replaceAll("\n", "%0A").replaceAll('"', "%22");
  for (const [name, value] of form) {
    const disposition = `Content-Disposition: form-data; name="${escape(name)}"`;
    parts.push(
      Buffer.from(
        `--${boundary}\r\n${disposition}${typeof value === "string" ? "" : `; filename="${escape(value.name)}"`}\r\n${typeof value === "string" ? "" : `Content-Type: ${value.type || "application/octet-stream"}\r\n`}\r\n`,
      ),
    );
    parts.push(
      typeof value === "string" ? Buffer.from(value) : Buffer.from(await value.arrayBuffer()),
    );
    parts.push(Buffer.from("\r\n"));
  }
  parts.push(Buffer.from(`--${boundary}--\r\n`));
  return {
    body: Buffer.concat(parts),
    headers: { "content-type": `multipart/form-data; boundary=${boundary}` },
  };
}

/** Fake Python boundary; callers still use the real host transport and HTTP serializer. */
export function createTelethonIpcFixture(apiRoot: string) {
  const env = captureEnv([
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "OPENCLAW_TELETHON_PROXY",
    "OPENCLAW_PROXY_URL",
  ]);
  process.env.TELEGRAM_API_ID = "12345";
  process.env.TELEGRAM_API_HASH = "synthetic-telegram-ipc-fixture";
  delete process.env.OPENCLAW_TELETHON_PROXY;
  delete process.env.OPENCLAW_PROXY_URL;
  const servers = new Map<string, Server>();
  const listeners = new Map<string, Promise<void>>();
  const digest = (value: string) => createHash("sha256").update(value).digest("hex");

  async function listenForToken(token: string) {
    const stateDir = path.resolve(
      resolveStateDir(),
      "telegram",
      "telethon",
      digest(token).slice(0, 24),
    );
    const identity = digest(
      `${stateDir}\0${process.env.TELEGRAM_API_ID}\0${process.env.TELEGRAM_API_HASH}\0`,
    );
    const pending = listeners.get(identity);
    if (pending) {
      return pending;
    }
    const secret = digest(`${identity}\0${token}\0openclaw-telethon-ipc-v1`);
    const server = createServer((request, response) => {
      // Fake clock resets must not leave pooled IPC sockets from a prior case.
      response.setHeader("connection", "close");
      const respond = async () => {
        if (request.headers["x-openclaw-telethon"] !== secret) {
          response.writeHead(401).end();
          return;
        }
        if (request.url === "/health") {
          response.setHeader("content-type", "application/json");
          response.end(
            JSON.stringify({
              ok: true,
              result: { protocol: "openclaw-telethon", version: 1, ready: true },
            }),
          );
          return;
        }
        if (request.url !== "/rpc") {
          throw new Error(`Unexpected Telethon fixture endpoint ${request.url}`);
        }
        const contentType = request.headers["content-type"] ?? "application/json";
        const body = await buffer(request);
        let envelope: { method: string; payload: Record<string, unknown> };
        let upstreamBody: string | Buffer;
        let headers: Record<string, string>;
        if (contentType.includes("multipart/form-data")) {
          const form = await new Response(body, {
            headers: { "content-type": contentType },
          }).formData();
          const payload = form.get("payload");
          if (typeof payload !== "string") {
            throw new Error("Missing Telethon multipart envelope");
          }
          envelope = JSON.parse(payload);
          form.delete("payload");
          for (const [key, value] of Object.entries(envelope.payload)) {
            form.set(key, typeof value === "string" ? value : JSON.stringify(value));
          }
          // Native FormData normalizes scalar LF to CRLF. Python receives the
          // original strings in JSON, so preserve those bytes in this fixture.
          const encodedForm = await encodeMultipart(form);
          upstreamBody = encodedForm.body;
          headers = encodedForm.headers;
        } else {
          envelope = JSON.parse(body.toString("utf8"));
          upstreamBody = JSON.stringify(envelope.payload);
          headers = { "content-type": "application/json" };
        }
        const controller = new AbortController();
        response.once("close", () => {
          if (!response.writableEnded) {
            controller.abort();
          }
        });
        const encoded = typeof upstreamBody === "string" ? Buffer.from(upstreamBody) : upstreamBody;
        // A raw request preserves injected failures exactly. Native fetch can
        // transparently replay HTTP 421, which Python's RPC boundary never does.
        const result = await new Promise<Buffer>((resolve, reject) => {
          const pendingRequest = httpRequest(
            `${apiRoot}/bot${token}/${envelope.method}`,
            {
              method: "POST",
              headers,
              signal: controller.signal,
            },
            (incoming) => {
              void buffer(incoming).then(resolve, reject);
            },
          );
          pendingRequest.once("error", reject);
          pendingRequest.end(encoded);
        });
        // Python returns a Bot API envelope over a successful local RPC exchange.
        response.setHeader("content-type", "application/json");
        response.end(result);
      };
      void respond().catch((cause: unknown) => {
        response.destroy(cause instanceof Error ? cause : new Error(String(cause)));
      });
    });
    const listening = (async () => {
      const seed = Number.parseInt(identity.slice(0, 8), 16);
      for (let attempt = 0; attempt < 16; attempt += 1) {
        try {
          await new Promise<void>((resolve, reject) => {
            server.once("error", reject);
            server.listen(20_000 + ((seed + attempt * 7_919) % 40_000), "127.0.0.1", () => {
              server.removeListener("error", reject);
              resolve();
            });
          });
          return;
        } catch (cause) {
          const code = extractErrorCode(cause);
          if (code !== "EADDRINUSE" && code !== "EACCES") {
            throw cause;
          }
        }
      }
      throw new Error("No free Telethon fixture port");
    })();
    servers.set(identity, server);
    listeners.set(identity, listening);
    await listening;
  }

  fixture.beforeRequest = async (input) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const token = /\/(?:file\/)?bot([^/]+)\//u.exec(url.pathname)?.[1];
    if (!token) {
      throw new Error(`Unexpected logical Telegram fixture URL ${url.pathname}`);
    }
    await listenForToken(decodeURIComponent(token));
  };

  return {
    listenForToken,
    async close() {
      try {
        await Promise.all(
          [...servers.values()].map(async (server) => {
            server.closeAllConnections();
            await new Promise<void>((resolve, reject) => {
              server.close((error) => (error ? reject(error) : resolve()));
            });
          }),
        );
      } finally {
        fixture.beforeRequest = undefined;
        env.restore();
      }
    },
  };
}
