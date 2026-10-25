import { afterEach, describe, expect, it, vi } from "vitest";

const boundary = vi.hoisted(() => ({
  invoke: vi.fn<typeof fetch>(),
  proxy: vi.fn<(fetch?: typeof globalThis.fetch) => string | undefined>(),
  create: vi.fn(),
}));
vi.mock("./telethon-transport.js", () => ({
  createTelethonFetch: (proxy?: string) => {
    boundary.create(proxy);
    return boundary.invoke;
  },
}));
vi.mock("openclaw/plugin-sdk/fetch-runtime", () => ({ getProxyUrlFromFetch: boundary.proxy }));
import { resolveTelegramTransport } from "./fetch.js";

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

describe("Telegram transport ownership", () => {
  it("uses the configured MTProto proxy without starting a separate HTTP transport", async () => {
    boundary.proxy.mockReturnValue("socks5://127.0.0.1:1080");
    boundary.invoke.mockResolvedValue(Response.json({ ok: true, result: true }));
    const transport = resolveTelegramTransport(vi.fn());
    await transport.fetch("https://api.telegram.org/bot10000:synthetic/getMe");
    await transport.sourceFetch(
      "https://api.telegram.org/file/bot10000:synthetic/telethon/file-id",
    );
    expect(boundary.create).toHaveBeenCalledWith("socks5://127.0.0.1:1080");
    expect(boundary.invoke).toHaveBeenCalledTimes(2);
    expect(boundary.invoke.mock.calls.map(([input]) => input)).toEqual([
      "https://api.telegram.org/bot10000:synthetic/getMe",
      "https://api.telegram.org/file/bot10000:synthetic/telethon/file-id",
    ]);
    await transport.close();
    expect(boundary.invoke.mock.calls.every(([, init]) => init?.signal?.aborted)).toBe(true);
  });

  it("cancels this transport's in-flight IPC without retiring another caller", async () => {
    boundary.proxy.mockReturnValue(undefined);
    boundary.invoke.mockImplementation(
      async (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          const signal = init?.signal;
          if (signal?.aborted) {
            reject(signal.reason instanceof Error ? signal.reason : new Error("Request cancelled"));
            return;
          }
          signal?.addEventListener(
            "abort",
            () => {
              reject(
                signal.reason instanceof Error ? signal.reason : new Error("Request cancelled"),
              );
            },
            { once: true },
          );
        }),
    );
    const first = resolveTelegramTransport();
    const second = resolveTelegramTransport();
    const pending = first.fetch("https://api.telegram.org/bot10000:synthetic/getMe");
    const rejected = expect(pending).rejects.toThrow("Telegram transport closed");
    const otherAbort = new AbortController();
    const other = second.fetch("https://api.telegram.org/bot10000:synthetic/getMe", {
      signal: otherAbort.signal,
    });
    const otherRejected = expect(other).rejects.toThrow("caller ended");
    await first.close();
    await rejected;
    expect(otherAbort.signal.aborted).toBe(false);
    otherAbort.abort(new Error("caller ended"));
    await otherRejected;
    await second.close();
  });

  it("never replays an uncertain MTProto send through an HTTP fallback", async () => {
    const failure = new Error("synthetic IPC disconnected after admission");
    boundary.invoke.mockRejectedValue(failure);
    const transport = resolveTelegramTransport();
    await expect(
      transport.fetch("https://api.telegram.org/bot10000:synthetic/sendMessage"),
    ).rejects.toBe(failure);
    expect(transport.forceFallback?.("connection lost", failure)).toBe(false);
    expect(boundary.invoke).toHaveBeenCalledOnce();
    await transport.close();
  });
});
