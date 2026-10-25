import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { createServer as createProbeServer } from "node:net";
import path from "node:path";
import { Bot, InputFile } from "grammy";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("openclaw/plugin-sdk/state-paths", () => ({
  resolveStateDir: () => "/synthetic-telethon-state",
}));
vi.mock("node:child_process", () => ({
  spawn: () => {
    throw new Error("The authenticated IPC fixture must prevent Python startup");
  },
}));
vi.mock("node:net", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:net")>();
  return { ...actual, createServer: vi.fn(actual.createServer) };
});
// These tests exercise serialization/admission. HTTP fallback classification
// has separate existing tests and is disabled for the MTProto transport.
vi.mock("./network-errors.js", () => ({
  TelegramRequestNotStartedError: class extends Error {},
  isTelegramMisdirectedRequestError: () => false,
}));

import { asTelegramClientFetch, createTelegramClientFetch } from "./client-fetch.js";
import { resolveTelegramTransport } from "./fetch.js";
import { bindTelegramRequestAuthority } from "./request-authority.js";
import { createTelethonFetch } from "./telethon-transport.js";

describe("Telethon transport at the existing Telegram client boundary", () => {
  const requests: Request[] = [];
  let healthHook: (() => void) | undefined;
  let unavailablePort: number | undefined;
  beforeEach(() => {
    requests.length = 0;
    vi.mocked(createProbeServer).mockClear();
    healthHook = undefined;
    unavailablePort = undefined;
    vi.stubEnv("TELEGRAM_API_ID", "12345");
    vi.stubEnv("TELEGRAM_API_HASH", "synthetic-api-hash");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new Request(input, init);
        expect(new URL(request.url).hostname).toBe("127.0.0.1");
        expect(request.headers.get("X-OpenClaw-Telethon")).toMatch(/^[a-f0-9]{64}$/u);
        if (new URL(request.url).pathname === "/health") {
          healthHook?.();
          if (Number(new URL(request.url).port) === unavailablePort) {
            return new Response(null, { status: 401 });
          }
          return Response.json({
            ok: true,
            result: { protocol: "openclaw-telethon", version: 1, ready: true },
          });
        }
        requests.push(request);
        return Response.json({
          ok: true,
          result: { message_id: 77, chat: { id: 123, type: "private" }, date: 1 },
        });
      }),
    );
  });

  it("discovers the same authenticated worker after an occupied or reserved first port", async () => {
    const digest = (value: string) => createHash("sha256").update(value).digest("hex");
    const token = "10005:synthetic";
    const stateDir = path.resolve(
      "/synthetic-telethon-state",
      "telegram",
      "telethon",
      digest(token).slice(0, 24),
    );
    const identity = digest([stateDir, "12345", "synthetic-api-hash", ""].join("\0"));
    const seed = Number.parseInt(identity.slice(0, 8), 16);
    unavailablePort = 20_000 + (seed % 40_000);
    const occupied = createServer();
    try {
      await new Promise<void>((resolve, reject) => {
        occupied.once("error", reject);
        occupied.listen(unavailablePort, "127.0.0.1", resolve);
      }).catch((cause: unknown) => {
        // An OS reservation or existing listener also establishes the required
        // unavailable first candidate without taking ownership of that socket.
        if (
          !(cause instanceof Error) ||
          !("code" in cause) ||
          !["EACCES", "EADDRINUSE"].includes(String(cause.code))
        ) {
          throw cause;
        }
      });
      await createTelethonFetch()(`https://api.telegram.org/bot${token}/getMe`);
      expect(createProbeServer).not.toHaveBeenCalled();
      expect(new URL(requests[0]!.url).port).toBe(String(20_000 + ((seed + 7_919) % 40_000)));
      expect(await requests[0]!.json()).toEqual({ method: "getMe", payload: {} });
    } finally {
      if (occupied.listening) {
        await new Promise<void>((resolve, reject) => {
          occupied.close((error) => (error ? reject(error) : resolve()));
        });
      }
    }
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("preserves native reply quotes and exact callback values through grammY serialization", async () => {
    const bot = new Bot("10001:synthetic", {
      client: {
        fetch: asTelegramClientFetch(
          createTelegramClientFetch({ fetchImpl: asTelegramClientFetch(createTelethonFetch()) }),
        ),
      },
    });
    await bot.api.sendMessage(123, "42", {
      reply_parameters: { message_id: 9, quote: "😀 quote", quote_position: 2 },
      reply_markup: { inline_keyboard: [[{ text: "Apply", callback_data: "env|prod" }]] },
    });
    expect(await requests[0]?.json()).toEqual({
      method: "sendMessage",
      payload: {
        chat_id: 123,
        text: "42",
        reply_parameters: { message_id: 9, quote: "😀 quote", quote_position: 2 },
        reply_markup: { inline_keyboard: [[{ text: "Apply", callback_data: "env|prod" }]] },
      },
    });
  });

  it("translates real multipart albums without changing numeric-looking captions or bytes", async () => {
    const bot = new Bot("10002:synthetic", {
      client: {
        fetch: asTelegramClientFetch(
          createTelegramClientFetch({ fetchImpl: asTelegramClientFetch(createTelethonFetch()) }),
        ),
      },
    });
    await bot.api.sendMediaGroup(123, [
      {
        type: "photo",
        media: new InputFile(new Uint8Array([0, 255, 7]), "test.jpg"),
        caption: "123",
      },
    ]);
    const form = await requests[0]?.formData();
    const envelopeField = form?.get("payload");
    if (typeof envelopeField !== "string") {
      throw new Error("Expected the multipart JSON envelope");
    }
    const envelope = JSON.parse(envelopeField);
    expect(envelope.method).toBe("sendMediaGroup");
    expect(envelope.payload.chat_id).toBe(123);
    expect(envelope.payload.media[0].caption).toBe("123");
    expect(Object.keys(envelope.payload).toSorted()).toEqual(["chat_id", "media"]);
    const attachment = envelope.payload.media[0].media.replace("attach://", "");
    const upload = form?.get(attachment);
    expect(upload).toBeInstanceOf(File);
    if (upload instanceof File) {
      expect(upload.name).toBe("test.jpg");
      expect([...new Uint8Array(await upload.arrayBuffer())]).toEqual([0, 255, 7]);
    }
    await bot.api.sendDocument(123, new InputFile(new Uint8Array([9]), "scalar.txt"), {
      caption: "123",
    });
    const documentForm = await requests[1]?.formData();
    const documentField = documentForm?.get("payload");
    if (typeof documentField !== "string") {
      throw new Error("Expected the document JSON envelope");
    }
    const documentPayload = JSON.parse(documentField).payload;
    expect(Object.keys(documentPayload).toSorted()).toEqual(["caption", "chat_id", "document"]);
    expect(documentPayload.caption).toBe("123");
  });

  it("routes direct lookup and media paths to Python without a Telegram HTTP call", async () => {
    const invoke = createTelethonFetch();
    await invoke("https://api.telegram.org/bot10003:synthetic/getChat?chat_id=-100123");
    expect(await requests[0]?.json()).toEqual({ method: "getChat", payload: { chat_id: -100123 } });
    await invoke("https://api.telegram.org/file/bot10003:synthetic/telethon/opaque-file-id");
    expect(new URL(requests[1]?.url ?? "").pathname).toBe("/download");
    expect(new URL(requests[1]?.url ?? "").searchParams.get("file_id")).toBe("opaque-file-id");
  });

  it.each(["direct", "host"] as const)(
    "checks current authority after startup through the %s client",
    async (layer) => {
      let authorized = true;
      healthHook = () => {
        authorized = false;
      };
      const transport = layer === "host" ? resolveTelegramTransport() : undefined;
      const invoke = transport
        ? createTelegramClientFetch({
            fetchImpl: asTelegramClientFetch(transport.fetch),
            transport,
          })
        : createTelethonFetch();
      const token = layer === "host" ? "10006:synthetic" : "10004:synthetic";
      const guarded = bindTelegramRequestAuthority(asTelegramClientFetch(invoke), () => {
        if (!authorized) {
          throw new Error("synthetic authority revoked");
        }
      });
      try {
        await expect(
          guarded(`https://api.telegram.org/bot${token}/sendMessage`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ chat_id: 123, text: "must not send" }),
          }),
        ).rejects.toThrow("Telegram request authority rejected");
        expect(authorized).toBe(false);
        expect(requests).toHaveLength(0);
      } finally {
        await transport?.close();
      }
    },
  );
});
