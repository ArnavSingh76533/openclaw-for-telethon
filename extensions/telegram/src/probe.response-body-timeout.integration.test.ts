import { createHash } from "node:crypto";
import { createServer, type Server, type ServerResponse } from "node:http";
import type { AddressInfo, Socket } from "node:net";
import path from "node:path";
import { buffer } from "node:stream/consumers";
import { extractErrorCode } from "openclaw/plugin-sdk/error-runtime";
import { createDeferred } from "openclaw/plugin-sdk/extension-shared";
import * as responseLimitRuntime from "openclaw/plugin-sdk/response-limit-runtime";
import * as runtimeEnv from "openclaw/plugin-sdk/runtime-env";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { probeTelegram } from "./probe.js";
import { createTelethonFetch } from "./telethon-transport.js";

const TOKEN = "123456:probe-socket-fixture";

const INITIAL_BODY = '{"ok":true,"result":{"id":123';

describe("probeTelegram cancellation and body deadlines over real sockets", () => {
  let server: Server;
  let apiRoot: string;
  let mode: "ready" | "backoff" | "all" | "webhook" | "stall" | "trickle" | "webhook-body";
  let pendingResponse: ServerResponse;
  let socketClosed = createDeferred<void>();
  let requestStarted = createDeferred<void>();
  let requestCount = 0;
  let connectionCount = 0;
  const liveSockets = new Set<Socket>();

  beforeAll(async () => {
    const proxyNames =
      "HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy OPENCLAW_PROXY_URL OPENCLAW_DEBUG_PROXY_ENABLED OPENCLAW_DEBUG_PROXY_URL";
    for (const name of proxyNames.split(" ")) {
      vi.stubEnv(name, "");
    }

    vi.stubEnv("TELEGRAM_API_ID", "12345");
    vi.stubEnv("TELEGRAM_API_HASH", "synthetic-probe-socket-fixture");
    const digest = (value: string) => createHash("sha256").update(value).digest("hex");
    const stateDir = path.resolve(
      resolveStateDir(),
      "telegram",
      "telethon",
      digest(TOKEN).slice(0, 24),
    );
    const identity = digest(
      `${stateDir}\0${process.env.TELEGRAM_API_ID}\0${process.env.TELEGRAM_API_HASH}\0`,
    );
    const secret = digest(`${identity}\0${TOKEN}\0openclaw-telethon-ipc-v1`);
    // Fault the Python IPC socket itself: diagnostics must retain deadlines
    // and cancellation even when the local worker stalls or truncates JSON.
    server = createServer((request, response) => {
      response.setHeader("connection", "close");
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
        response.writeHead(404).end();
        return;
      }
      requestCount += 1;
      connectionCount += 1;
      void buffer(request)
        .then((body) => {
          const { method } = JSON.parse(body.toString("utf8")) as { method: string };
          if (mode === "backoff") {
            response.socket?.destroy();
            return;
          }
          if (mode === "all" || (mode === "webhook" && method === "getWebhookInfo")) {
            requestStarted.resolve();
            return;
          }
          response.writeHead(200, { "content-type": "application/json" });
          if (
            mode === "ready" ||
            ((mode === "webhook" || mode === "webhook-body") && method === "getMe")
          ) {
            response.end(
              '{"ok":true,"result":{"id":123,"is_bot":true,"first_name":"Test","username":"bot"}}',
            );
            return;
          }
          const closed = socketClosed;
          request.socket.once("close", () => closed.resolve());
          response.write(
            mode === "webhook-body"
              ? '{"ok":true,"result":{"url":"https://example.test/hook"'
              : INITIAL_BODY,
          );
          pendingResponse = response;
        })
        .catch((cause: unknown) =>
          response.destroy(cause instanceof Error ? cause : new Error(String(cause))),
        );
    });
    server.on("connection", (socket) => {
      liveSockets.add(socket);
      socket.once("close", () => liveSockets.delete(socket));
    });
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
        break;
      } catch (cause) {
        if (!["EACCES", "EADDRINUSE"].includes(extractErrorCode(cause) ?? "")) {
          throw cause;
        }
      }
    }
    if (!server.listening) {
      throw new Error("No free probe IPC fixture port");
    }
    apiRoot = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    // Complete service discovery before fake-clock cases; only RPC sockets
    // contribute to the existing request/connection assertions.
    mode = "ready";
    await (await createTelethonFetch()(`${apiRoot}/bot${TOKEN}/getMe`)).json();
    requestCount = 0;
    connectionCount = 0;
  });

  beforeEach(() => {
    // Vitest restores suite-level stubs before each case.
    vi.stubEnv("TELEGRAM_API_ID", "12345");
    vi.stubEnv("TELEGRAM_API_HASH", "synthetic-probe-socket-fixture");
    for (const name of "HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy OPENCLAW_PROXY_URL OPENCLAW_DEBUG_PROXY_ENABLED OPENCLAW_DEBUG_PROXY_URL OPENCLAW_TELETHON_PROXY".split(
      " ",
    )) {
      vi.stubEnv(name, "");
    }
  });

  afterAll(async () => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    vi.unstubAllEnvs();
    for (const socket of liveSockets) {
      socket.destroy();
    }
    await new Promise<void>((resolve) => {
      server.close(() => resolve());
    });
  });

  const beforeProbeEnds = <T>(milestone: Promise<T>, probe: Promise<unknown>) =>
    Promise.race([
      milestone,
      probe.then(() => {
        throw new Error("Probe completed before its socket/body milestone");
      }),
    ]);

  it.each(["backoff", "all", "webhook"] as const)("aborts during %s", async (phase) => {
    const controller = new AbortController();
    const requestsBefore = requestCount;
    const connectionsBefore = connectionCount;
    const backoffStarted = createDeferred<{ sleep: Promise<void> }>();
    requestStarted = createDeferred<void>();
    const realSleepWithAbort = runtimeEnv.sleepWithAbort;
    const sleepSpy =
      phase === "backoff"
        ? vi.spyOn(runtimeEnv, "sleepWithAbort").mockImplementation((...args) => {
            // Register the real abort listener before signaling readiness.
            const sleep = realSleepWithAbort(...args);
            backoffStarted.resolve({ sleep });
            return sleep;
          })
        : undefined;
    mode = phase;
    const startedAt = Date.now();
    const probe = probeTelegram(TOKEN, 10_000, {
      apiRoot,
      includeWebhookInfo: phase === "webhook",
      abortSignal: controller.signal,
      network: { autoSelectFamily: false, dnsResultOrder: "verbatim" },
    });
    try {
      if (phase === "backoff") {
        const { sleep } = await beforeProbeEnds(backoffStarted.promise, probe);
        controller.abort();
        await expect(sleep).rejects.toMatchObject({
          name: "AbortError",
          cause: controller.signal.reason,
        });
      } else {
        await requestStarted.promise;
        controller.abort();
      }
      expect((await probe).ok).toBe(false);
      expect(requestCount).toBe(requestsBefore + (phase === "webhook" ? 2 : 1));
      if (phase !== "webhook") {
        expect(connectionCount).toBe(connectionsBefore + 1);
      }
      if (phase !== "backoff") {
        expect(Date.now() - startedAt).toBeLessThan(1_000);
      }
    } finally {
      controller.abort();
      sleepSpy?.mockRestore();
      await probe;
    }
  });

  it.each([
    ["stall", expect.stringMatching(/response body stalled/i), 100],
    ["trickle", expect.stringMatching(/response body timed out/i), 200],
  ] as const)("times out %s bodies", async (bodyMode, error, elapsedMs) => {
    // Connection setup and socket scheduling must not consume the probe's budget.
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    mode = bodyMode;
    socketClosed = createDeferred<void>();
    const previousRequestCount = requestCount;
    let receivedBytes = 0;
    let consumedBytes = 0;
    let readProgress = createDeferred<void>();
    const waitForConsumedBytes = async (minimum: number) => {
      for (let consumed = consumedBytes; consumed < minimum; consumed = consumedBytes) {
        await readProgress.promise;
      }
    };
    const readResponseWithLimit = responseLimitRuntime.readResponseWithLimit;
    vi.spyOn(responseLimitRuntime, "readResponseWithLimit").mockImplementation(
      (response, maxBytes, options) => {
        const body = response.body;
        if (!body) {
          throw new Error("Expected the real socket response body");
        }
        const getReader = body.getReader.bind(body);
        vi.spyOn(body, "getReader").mockImplementation((readerOptions) => {
          if (readerOptions !== undefined) {
            return getReader(readerOptions);
          }
          const reader = getReader();
          const read = reader.read.bind(reader);
          vi.spyOn(reader, "read").mockImplementation(async () => {
            // A new read means prior bytes were consumed and the idle deadline refreshed.
            consumedBytes = receivedBytes;
            readProgress.resolve();
            readProgress = createDeferred<void>();
            const result = await read();
            if (!result.done) {
              receivedBytes += result.value.byteLength;
            }
            return result;
          });
          return reader;
        });
        return readResponseWithLimit(response, maxBytes, options);
      },
    );
    const abort = new AbortController();
    const probe = probeTelegram(TOKEN, 200, {
      apiRoot,
      includeWebhookInfo: false,
      abortSignal: abort.signal,
    });
    try {
      await beforeProbeEnds(waitForConsumedBytes(INITIAL_BODY.length), probe);
      for (let index = 1; index <= (bodyMode === "trickle" ? 4 : 0); index += 1) {
        await vi.advanceTimersByTimeAsync(40);
        const consumed = waitForConsumedBytes(INITIAL_BODY.length + index);
        pendingResponse.write(" ");
        await beforeProbeEnds(consumed, probe);
      }
      await vi.advanceTimersByTimeAsync(bodyMode === "trickle" ? 40 : 100);
      const result = await probe;
      expect(result).toMatchObject({ ok: false, error, elapsedMs });
      expect(requestCount).toBe(previousRequestCount + 1);
      await socketClosed.promise;
    } finally {
      abort.abort();
      await probe;
      vi.restoreAllMocks();
      vi.useRealTimers();
    }
  });

  it("keeps webhook diagnostics best-effort and closes their stalled socket", async () => {
    mode = "webhook-body";
    socketClosed = createDeferred<void>();
    const previousRequestCount = requestCount;

    const result = await probeTelegram(TOKEN, 200, { apiRoot });

    expect(result).toMatchObject({ ok: true, bot: { id: 123, username: "bot" } });
    expect(result.webhook).toBeUndefined();
    expect(requestCount).toBe(previousRequestCount + 2);
    await socketClosed.promise;
  });
});
