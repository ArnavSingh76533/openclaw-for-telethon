import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { extractErrorCode, toErrorObject } from "openclaw/plugin-sdk/error-runtime";
import { resolveStateDir } from "openclaw/plugin-sdk/state-paths";
import {
  assertTelegramRequestAuthority,
  bindTelegramTransportAuthority,
  getTelegramRequestAuthority,
} from "./request-authority.js";

type Bridge = { url: string; secret: string };
type BridgeEntry = { task: Promise<Bridge> };
const bridges = new Map<string, BridgeEntry>();
const PROTOCOL = "openclaw-telethon";

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function bridgeKey(token: string, proxy?: string): string {
  return digest(
    JSON.stringify([
      path.resolve(resolveStateDir()),
      token,
      proxy ?? "",
      process.env.TELEGRAM_API_ID ?? process.env.API_ID,
      process.env.TELEGRAM_API_HASH ?? process.env.API_HASH,
    ]),
  );
}

function pythonSourceRoot(): string {
  // The directory also ships beside the plugin's compiled src/ directory.
  const candidates = [
    fileURLToPath(new URL("./python", import.meta.url)),
    fileURLToPath(new URL("../python", import.meta.url)),
    fileURLToPath(new URL("../../python", import.meta.url)),
    fileURLToPath(new URL("./extensions/telegram/python", import.meta.url)),
  ];
  const found = candidates.find((candidate) =>
    existsSync(path.join(candidate, "openclaw_telethon", "__main__.py")),
  );
  if (!found) {
    throw new Error("Telegram Telethon Python sources are missing; reinstall the complete plugin.");
  }
  return found;
}

async function health(bridge: Bridge, requireReady = true): Promise<boolean> {
  try {
    const response = await fetch(`${bridge.url}/health`, {
      headers: { "X-OpenClaw-Telethon": bridge.secret },
      signal: AbortSignal.timeout(750),
    });
    if (!response.ok) {
      return false;
    }
    const body = (await response.json()) as {
      ok?: boolean;
      result?: { protocol?: string; version?: number; ready?: boolean };
    };
    return (
      body.ok === true &&
      body.result?.protocol === PROTOCOL &&
      body.result.version === 1 &&
      (!requireReady || body.result.ready === true)
    );
  } catch {
    return false;
  }
}

async function canListen(port: number): Promise<boolean> {
  const server = createServer((socket) => socket.destroy());
  try {
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(port, "127.0.0.1", resolve);
    });
    await new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
    return true;
  } catch (error) {
    const code = extractErrorCode(error);
    if (code === "EADDRINUSE" || code === "EACCES") {
      return false;
    }
    throw error;
  }
}

async function discoverBridge(identity: string, token: string): Promise<Bridge> {
  const secret = digest(`${identity}\0${token}\0openclaw-telethon-ipc-v1`);
  const seed = Number.parseInt(identity.slice(0, 8), 16);
  // Windows reserves port ranges, and another bot can already own a candidate.
  // Every caller probes the same sequence. Check every candidate for an
  // existing owner before claiming a free port: an earlier port can become
  // available after the worker started on a later candidate.
  const candidates = Array.from({ length: 16 }, (_, attempt) => {
    const port = 20_000 + ((seed + attempt * 7_919) % 40_000);
    return { url: `http://127.0.0.1:${port}`, secret };
  });
  for (const bridge of candidates) {
    if (await health(bridge, false)) {
      return bridge;
    }
  }
  for (const bridge of candidates) {
    if (await canListen(Number(new URL(bridge.url).port))) {
      return bridge;
    }
  }
  throw new Error(
    "No local Telethon IPC port is available; check Windows port reservations and local listeners.",
  );
}

async function startBridge(token: string, proxy?: string): Promise<Bridge> {
  const apiId = Number(process.env.TELEGRAM_API_ID ?? process.env.API_ID);
  const apiHash = (process.env.TELEGRAM_API_HASH ?? process.env.API_HASH)?.trim();
  if (!Number.isSafeInteger(apiId) || apiId <= 0 || !apiHash) {
    throw new Error(
      "Telethon requires TELEGRAM_API_ID and TELEGRAM_API_HASH in the Gateway environment.",
    );
  }
  const stateDir = path.resolve(
    resolveStateDir(),
    "telegram",
    "telethon",
    digest(token).slice(0, 24),
  );
  // Workers and outbound clients discover the same token-scoped service without
  // putting credentials in command lines, URLs, or a discovery sidecar.
  const identity = digest(`${stateDir}\0${apiId}\0${apiHash}\0${proxy ?? ""}`);
  const bridge = await discoverBridge(identity, token);
  if (await health(bridge)) {
    return bridge;
  }
  if (await health(bridge, false)) {
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => {
        setTimeout(resolve, 200);
      });
      if (await health(bridge)) {
        return bridge;
      }
    }
    throw new Error(
      "The existing Telethon worker is not ready. Check its connection and retained update conversion errors.",
    );
  }
  const python =
    process.env.OPENCLAW_TELETHON_PYTHON?.trim() ||
    (process.platform === "win32" ? "python" : "python3");
  const child = spawn(python, ["-m", "openclaw_telethon"], {
    cwd: pythonSourceRoot(),
    windowsHide: true,
    stdio: ["pipe", "ignore", "pipe"],
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });
  let failure: Error | undefined;
  let diagnostic = "";
  child.once("error", () => {
    failure = new Error(
      "Could not launch Telethon Python; set OPENCLAW_TELETHON_PYTHON to its virtual environment executable.",
    );
  });
  child.stderr?.on("data", (chunk: Buffer) => {
    // Python can print import failures before the service's own safe logger.
    diagnostic = (diagnostic + chunk.toString("utf8")).slice(-2_000);
  });
  child.once("exit", (code) => {
    failure ??= new Error(
      `Telethon Python exited (${String(code)}). Install extensions/telegram/python into its virtual environment.`,
    );
  });
  child.stdin?.on("error", () => undefined);
  child.stdin?.end(
    JSON.stringify({
      token,
      api_id: apiId,
      api_hash: apiHash,
      port: Number(new URL(bridge.url).port),
      secret: bridge.secret,
      state_dir: stateDir,
      parent_pid: process.pid,
      ...(proxy ? { proxy } : {}),
    }) + "\n",
  );
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (await health(bridge)) {
      child.unref();
      child.stderr?.destroy();
      return bridge;
    }
    if (failure) {
      // A simultaneous worker can win the bind race. Its authenticated health
      // response, not our child's exit code, owns readiness.
      await new Promise((resolve) => {
        setTimeout(resolve, 200);
      });
      if (await health(bridge)) {
        return bridge;
      }
      if (await health(bridge, false)) {
        continue;
      }
      const safeDiagnostic = diagnostic
        .replaceAll(token, "[token]")
        .replaceAll(apiHash, "[api-hash]");
      throw new Error(
        `${failure.message}${safeDiagnostic.trim() ? ` ${safeDiagnostic.trim()}` : ""}`,
      );
    }
    await new Promise((resolve) => {
      setTimeout(resolve, 150);
    });
  }
  child.kill();
  throw new Error(
    "Telethon startup timed out. Check Telegram credentials, MTProto network access, and Python dependencies.",
  );
}

async function getBridge(token: string, proxy?: string): Promise<Bridge> {
  const key = bridgeKey(token, proxy);
  let entry = bridges.get(key);
  if (!entry) {
    entry = { task: startBridge(token, proxy) };
    bridges.set(key, entry);
    entry.task.catch(() => {
      if (bridges.get(key) === entry) {
        bridges.delete(key);
      }
    });
  }
  return entry.task;
}

async function waitForBridge(task: Promise<Bridge>, signal?: AbortSignal | null): Promise<Bridge> {
  if (signal?.aborted) {
    throw signal.reason ?? new DOMException("The operation was aborted", "AbortError");
  }
  if (!signal) {
    return task;
  }
  let abort: (() => void) | undefined;
  try {
    return await Promise.race([
      task,
      new Promise<never>((_resolve, reject) => {
        abort = () => reject(toErrorObject(signal.reason, "The operation was aborted"));
        signal.addEventListener("abort", abort, { once: true });
      }),
    ]);
  } finally {
    if (abort) {
      signal.removeEventListener("abort", abort);
    }
  }
}

type TelegramEndpoint = { token: string; method?: string; fileId?: string; query: URLSearchParams };

function endpoint(input: RequestInfo | URL): TelegramEndpoint {
  const url = new URL(input instanceof Request ? input.url : String(input));
  const match = /\/(file\/)?bot([^/]+)\/(.+)$/u.exec(url.pathname);
  if (!match?.[2] || !match[3]) {
    throw new Error("Telethon transport accepts only Telegram bot API/file request paths.");
  }
  return {
    token: decodeURIComponent(match[2]),
    ...(match[1]
      ? { fileId: decodeURIComponent(match[3].replace(/^telethon\//u, "")) }
      : { method: match[3] }),
    query: url.searchParams,
  };
}

const JSON_FIELDS = new Set([
  "entities",
  "caption_entities",
  "reply_parameters",
  "link_preview_options",
  "reply_markup",
  "media",
  "options",
  "question_entities",
  "explanation_entities",
  "commands",
  "scope",
  "reaction",
  "rich_message",
  "rich_text",
  "blocks",
]);
const NUMBER_FIELDS = new Set([
  "chat_id",
  "user_id",
  "message_id",
  "message_thread_id",
  "direct_messages_topic_id",
  "offset",
  "timeout",
  "limit",
  "duration",
  "length",
  "width",
  "height",
  "latitude",
  "longitude",
  "heading",
  "live_period",
  "horizontal_accuracy",
  "proximity_alert_radius",
  "cache_time",
  "icon_color",
  "correct_option_id",
  "open_period",
  "close_date",
]);
const FILE_FIELDS = new Set([
  "photo",
  "animation",
  "video",
  "video_note",
  "voice",
  "audio",
  "document",
  "sticker",
  "thumbnail",
  "thumb",
  "cover",
  "certificate",
]);

function parseValue(key: string, value: string): unknown {
  const booleanField =
    /^(?:is_|has_|disable_|allow_|show_|supports_)/u.test(key) || key === "protect_content";
  if (!JSON_FIELDS.has(key) && !NUMBER_FIELDS.has(key) && !booleanField) {
    return value;
  }
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}

async function rpcBody(
  request: Request,
  method: string,
  query: URLSearchParams,
): Promise<{ body: BodyInit; headers?: HeadersInit }> {
  const payload: Record<string, unknown> = {};
  query.forEach((value, key) => {
    payload[key] = parseValue(key, value);
  });
  if (
    request.body &&
    /multipart\/form-data|application\/x-www-form-urlencoded/iu.test(
      request.headers.get("content-type") ?? "",
    )
  ) {
    const form = await request.formData();
    const uploads = new FormData();
    let hasUploads = false;
    for (const [key, value] of form.entries()) {
      if (typeof value === "string") {
        payload[key] = parseValue(key, value);
      } else {
        hasUploads = true;
        uploads.append(key, value, value.name);
        // grammY already writes attach:// references for nested media groups.
        if (FILE_FIELDS.has(key) && !(key in payload)) {
          payload[key] = `attach://${key}`;
        }
      }
    }
    if (hasUploads) {
      uploads.append("payload", JSON.stringify({ method, payload }));
      return { body: uploads };
    }
  } else if (request.body) {
    const json: unknown = await request.json();
    if (!json || typeof json !== "object" || Array.isArray(json)) {
      throw new Error("Telegram RPC payload must be a JSON object.");
    }
    Object.assign(payload, json);
  }
  return {
    body: JSON.stringify({ method, payload }),
    headers: { "content-type": "application/json" },
  };
}

export function createTelethonFetch(proxy?: string): typeof fetch {
  return async (input, init) => {
    const target = endpoint(input);
    const key = bridgeKey(target.token, proxy);
    const authority = getTelegramRequestAuthority(init);
    const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
    if (signal?.aborted) {
      throw signal.reason ?? new DOMException("The operation was aborted", "AbortError");
    }
    const bridge = await waitForBridge(getBridge(target.token, proxy), signal);
    if (signal?.aborted) {
      throw signal.reason ?? new DOMException("The operation was aborted", "AbortError");
    }
    const requestInit = { ...init, duplex: "half" as const };
    const request = new Request(input, requestInit);
    const rpc = target.method ? await rpcBody(request, target.method, target.query) : undefined;
    if (signal?.aborted) {
      throw signal.reason ?? new DOMException("The operation was aborted", "AbortError");
    }
    assertTelegramRequestAuthority(authority);
    // Capture effect authority after startup/upload preparation and immediately
    // before the local request that admits Telegram I/O. Never retry a send on
    // uncertain IPC settlement; the existing send funnel owns that decision.
    const send = bindTelegramTransportAuthority(fetch, authority);
    const headers = new Headers(rpc?.headers);
    headers.set("X-OpenClaw-Telethon", bridge.secret);
    try {
      return await send(
        target.method
          ? `${bridge.url}/rpc`
          : `${bridge.url}/download?file_id=${encodeURIComponent(target.fileId ?? "")}`,
        {
          method: target.method ? "POST" : "GET",
          headers,
          ...(rpc ? { body: rpc.body } : {}),
          signal,
        },
      );
    } catch (error) {
      // The next caller may restart a dead service. This call is never replayed.
      if (!signal?.aborted && !(await health(bridge))) {
        bridges.delete(key);
      }
      throw error;
    }
  };
}
