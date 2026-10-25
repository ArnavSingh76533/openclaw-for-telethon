import "./telethon-ipc.test-support.js";
import { Bot } from "grammy";
import { PlatformMessageNotDispatchedError } from "openclaw/plugin-sdk/error-runtime";
import { createDeferred } from "openclaw/plugin-sdk/extension-shared";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getOrCreateAccountThrottler, runReplaceableTelegramRequest } from "./account-throttler.js";
import { asTelegramClientFetch } from "./client-fetch.js";
import { createTelegramDraftStream } from "./draft-stream.js";
import { TelegramRequestNotStartedError } from "./network-errors.js";
import { resetTelegramAccountThrottlersForTest } from "./runtime.test-support.js";
import {
  deleteMessageTelegram,
  editMessageTelegram,
  resetTelegramClientOptionsCacheForTests,
  sendMessageTelegram,
} from "./send.js";
import { useTelegramHttpFixture } from "./send.telegram-http.test-support.js";

describe("Telegram request contracts through real clients", () => {
  const fixture = useTelegramHttpFixture();
  const limitedClient = () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout", "Date"] });
    const { botToken, apiRoot } = fixture.cfg.channels.telegram;
    const bot = new Bot(botToken, { client: { apiRoot } });
    // No send-context authority hook: the send owner must recheck after the limiter.
    bot.api.config.use(getOrCreateAccountThrottler(botToken).transformer);
    return bot;
  };
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    // Flood gates are per-token process state; never leak one test's wait into the next.
    resetTelegramAccountThrottlersForTest();
  });

  it.each(["current", "retired"] as const)(
    "queued unfinished preview retains network authority (%s writer)",
    async (writer) => {
      const bot = limitedClient();
      const entered = createDeferred<void>();
      bot.api.config.use((prev, method, payload, signal) => {
        if (method === "editMessageText") {
          entered.resolve();
        }
        return prev(method, payload, signal);
      });
      let current = true;
      const stream = createTelegramDraftStream({
        api: bot.api,
        chatId: -1001,
        thread: { id: 2, scope: "forum" },
      });
      const authority = () => {
        if (!current) {
          throw new Error("preview writer retired");
        }
      };
      stream.update("seed preview", { assertPlatformSendAuthorized: authority });
      await stream.flush();
      const hold = { arrived: createDeferred<void>(), release: createDeferred<void>() };
      fixture.requestHold = hold;
      // A replaceable blocker lets the preview queue without yielding to a final reply.
      const blocker = runReplaceableTelegramRequest(() =>
        bot.api.sendMessage(-1001, "queue blocker", { message_thread_id: 1 }),
      );
      try {
        await hold.arrived.promise;
        stream.update("queued preview", { assertPlatformSendAuthorized: authority });
        const flushed = stream.flush();
        await entered.promise;
        current = writer === "current";
        hold.release.resolve();
        await blocker;
        await flushed;
        expect(
          fixture.requests
            .filter(({ method }) => method === "editMessageText")
            .map(({ fields }) => fields.text),
        ).toEqual(writer === "current" ? ["queued preview"] : []);
      } finally {
        hold.release.resolve();
        await Promise.allSettled([blocker, stream.discard()]);
      }
    },
  );

  it.each([
    { queued: false, writer: "replaced", expectedSends: 1 },
    { queued: false, writer: "current", expectedSends: 2 },
    { queued: true, writer: "current", expectedSends: 1 },
    { queued: true, writer: "replaced", expectedSends: 0 },
  ])(
    "rechecks $writer writer after flood wait (queued topic: $queued)",
    async ({ queued, writer, expectedSends }) => {
      const bot = limitedClient();
      const entered = createDeferred<void>();
      if (queued) {
        // Installed last: observes topic 2 before it queues in the account limiter.
        bot.api.config.use((prev, method, payload, signal) => {
          if ((payload as { message_thread_id?: unknown }).message_thread_id === 2) {
            entered.resolve();
          }
          return prev(method, payload, signal);
        });
      }
      const startedAt = Date.now();
      const held = { arrived: createDeferred<void>(), release: createDeferred<void>() };
      if (queued) {
        fixture.requestHold = held;
      }
      fixture.rejections.push({
        error_code: 429,
        description: "Too Many Requests: retry after 5",
        parameters: { retry_after: 5 },
      });
      let current = true;
      const errorMessage = queued ? "group session writer replaced" : "session writer replaced";
      const assertCurrent = () => {
        if (!current) {
          throw new Error(errorMessage);
        }
      };
      const send = (topic: number | undefined, authorized: boolean) =>
        sendMessageTelegram(
          queued ? "-1001" : "123",
          queued ? `Topic ${topic} final` : "Final after flood",
          {
            cfg: fixture.cfg,
            api: bot.api,
            messageThreadId: topic,
            assertPlatformSendAuthorized: authorized ? assertCurrent : undefined,
          },
        ).then(
          (result) => ({ result }),
          (error: unknown) => ({ error }),
        );
      const blocker = queued ? send(1, false) : undefined;
      if (queued) {
        await held.arrived.promise;
      }
      const pending = send(queued ? 2 : undefined, true);
      if (queued) {
        await entered.promise;
        await vi.advanceTimersByTimeAsync(0);
      } else {
        await vi.waitFor(() => expect(fixture.requests).toHaveLength(1));
      }
      current = writer === "current";
      held.release.resolve();
      if (queued) {
        await vi.advanceTimersByTimeAsync(4_900);
        expect(fixture.requests).toHaveLength(1);
        await vi.advanceTimersByTimeAsync(10_000);
        expect(await blocker).toMatchObject({ result: { messageId: expect.any(String) } });
        expect(Date.now() - startedAt).toBeGreaterThanOrEqual(5_000);
      } else {
        await vi.advanceTimersByTimeAsync(5_000);
      }
      const settled = await pending;
      const requests = queued
        ? fixture.requests.filter(({ fields }) => fields.message_thread_id === 2)
        : fixture.requests;
      expect(requests).toHaveLength(expectedSends);
      if (writer === "replaced") {
        expect("error" in settled && String(settled.error)).toContain(errorMessage);
      } else {
        expect(settled).toMatchObject({ result: { messageId: expect.any(String) } });
      }
    },
  );

  it.each(["not-started", "connect-timeout", "ambiguous"] as const)(
    "preserves %s custody through grammY's transport error envelope",
    async (kind) => {
      let attempts = 0;
      const error =
        kind === "not-started"
          ? new TelegramRequestNotStartedError()
          : kind === "connect-timeout"
            ? Object.assign(new Error("Connect Timeout"), { code: "UND_ERR_CONNECT_TIMEOUT" })
            : new Error("Network request for 'sendMessage' failed after 1 attempts.");
      const bot = new Bot(fixture.cfg.channels.telegram.botToken, {
        client: {
          apiRoot: fixture.cfg.channels.telegram.apiRoot,
          fetch: asTelegramClientFetch(async () => {
            attempts += 1;
            throw error;
          }),
        },
      });
      const sending = sendMessageTelegram("123", "Never accepted", {
        cfg: fixture.cfg,
        api: bot.api,
        retry: { attempts: 2, minDelayMs: 0, maxDelayMs: 0, jitter: 0 },
      });
      if (kind === "not-started") {
        await expect(sending).rejects.toBeInstanceOf(PlatformMessageNotDispatchedError);
      } else {
        await expect(sending).rejects.not.toBeInstanceOf(PlatformMessageNotDispatchedError);
      }
      expect(attempts).toBe(kind === "ambiguous" ? 1 : 2);
      expect(fixture.requests).toEqual([]);
    },
  );

  it("bounds a direct delete request with the control-call deadline", async () => {
    const held = { arrived: createDeferred<void>(), release: createDeferred<void>() };
    fixture.requestHold = held;
    const timer = global.setTimeout;
    let deadlineObserved = false;
    vi.spyOn(global, "setTimeout").mockImplementation((callback, delay, ...args) => {
      if (delay === 15_000 && !deadlineObserved) {
        deadlineObserved = true;
        return timer(() => {
          void held.arrived.promise.then(() => callback(...args));
        }, 0);
      }
      return timer(callback, delay, ...args);
    });
    const deleting = deleteMessageTelegram("123", 7, { cfg: fixture.cfg, retry: { attempts: 1 } });
    try {
      await expect(deleting).rejects.toMatchObject({
        error: { message: "Telegram deletemessage timed out after 15000ms" },
      });
      expect(deadlineObserved).toBe(true);
      expect(fixture.requests.map(({ method }) => method)).toEqual(["deleteMessage"]);
    } finally {
      held.release.resolve();
      await Promise.allSettled([deleting]);
    }
  });
});

describe("Telegram transport cache request retirement through IPC", () => {
  const fixture = useTelegramHttpFixture();

  it("cancels evicted and reset callers while their shared service remains usable", async () => {
    resetTelegramClientOptionsCacheForTests();
    const ACCOUNTS = 65;
    const accounts = Object.fromEntries(
      Array.from({ length: ACCOUNTS }, (_, i) => [
        `acct-${i}`,
        { botToken: `10${i}:e2e-token-${i}`, apiRoot: fixture.cfg.channels.telegram.apiRoot },
      ]),
    );
    const cfg = { channels: { telegram: { accounts } } };
    const edit = (account: number, text = "preview") =>
      editMessageTelegram("123", 1, text, { cfg, accountId: `acct-${account}` });
    const observe = (pending: ReturnType<typeof edit>) =>
      pending.then(
        () => ({ ok: true }),
        (error: unknown) => ({ error }),
      );
    fixture.handleRequest = (method, fields) =>
      method === "editMessageText" && String(fields.text).startsWith("held-");
    const evicted = observe(edit(0, "held-eviction"));
    let reset: ReturnType<typeof observe> | undefined;
    try {
      await vi.waitFor(() =>
        expect(fixture.requests.some(({ fields }) => fields.text === "held-eviction")).toBe(true),
      );
      const evictedSocket = fixture.requestSockets.get("/bot100:e2e-token-0");
      expect(evictedSocket).toBeDefined();
      for (let i = 1; i < ACCOUNTS; i += 1) {
        await edit(i);
      }
      await expect(evicted).resolves.toMatchObject({
        error: { error: { message: "Telegram transport closed" } },
      });
      await vi.waitFor(() => expect(evictedSocket?.destroyed).toBe(true));
      await edit(0);

      reset = observe(edit(64, "held-reset"));
      await vi.waitFor(() =>
        expect(fixture.requests.some(({ fields }) => fields.text === "held-reset")).toBe(true),
      );
      const resetSocket = fixture.requestSockets.get("/bot1064:e2e-token-64");
      expect(resetSocket).toBeDefined();
      resetTelegramClientOptionsCacheForTests();
      await expect(reset).resolves.toMatchObject({
        error: { error: { message: "Telegram transport closed" } },
      });
      await vi.waitFor(() => expect(resetSocket?.destroyed).toBe(true));
      await edit(64);
      expect(
        fixture.endpoints.filter(
          (endpoint) => endpoint === "/bot1064:e2e-token-64/editMessageText",
        ),
      ).toHaveLength(3);
    } finally {
      resetTelegramClientOptionsCacheForTests();
      await Promise.allSettled([evicted, reset]);
    }
  });
});
