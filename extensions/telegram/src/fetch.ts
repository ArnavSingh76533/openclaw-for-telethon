import type { TelegramNetworkConfig } from "openclaw/plugin-sdk/config-contracts";
import { collectErrorGraphCandidates, extractErrorCode } from "openclaw/plugin-sdk/error-runtime";
import {
  getProxyUrlFromFetch,
  type PinnedDispatcherPolicy,
} from "openclaw/plugin-sdk/fetch-runtime";
import { findTelegramRequestAuthorityError } from "./request-authority.js";
import { createTelethonFetch } from "./telethon-transport.js";

export { normalizeTelegramApiRoot as resolveTelegramApiBase } from "./api-root.js";

/** Public transport contract retained for host/media integrations. */
export type TelegramTransport = {
  fetch: typeof fetch;
  sourceFetch: typeof fetch;
  dispatcherAttempts?: { dispatcherPolicy?: PinnedDispatcherPolicy }[];
  forceFallback?: (reason: string, err?: unknown) => boolean;
  close(): Promise<void>;
};

export function shouldRetryTelegramTransportFallback(error: unknown): boolean {
  if (findTelegramRequestAuthorityError(error)) {
    return false;
  }
  const retryable = new Set([
    "ECONNRESET",
    "ECONNREFUSED",
    "ETIMEDOUT",
    "ENETDOWN",
    "ENETUNREACH",
    "EHOSTUNREACH",
    "UND_ERR_CONNECT_TIMEOUT",
    "UND_ERR_SOCKET",
  ]);
  return collectErrorGraphCandidates(error, (candidate) => [candidate.cause]).some((candidate) =>
    retryable.has(extractErrorCode(candidate) ?? ""),
  );
}

export function resolveTelegramTransport(
  proxyFetch?: typeof fetch,
  _options?: { network?: TelegramNetworkConfig },
): TelegramTransport {
  const proxy =
    (proxyFetch ? getProxyUrlFromFetch(proxyFetch) : undefined) ??
    process.env.OPENCLAW_TELETHON_PROXY?.trim() ??
    process.env.OPENCLAW_PROXY_URL?.trim();
  const invoke = createTelethonFetch(proxy);
  const lifetime = new AbortController();
  const transportFetch: typeof fetch = (input, init) => {
    const callerSignal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
    return invoke(input, {
      ...init,
      signal: callerSignal ? AbortSignal.any([callerSignal, lifetime.signal]) : lifetime.signal,
    });
  };
  return {
    fetch: transportFetch,
    // Equal fetch/sourceFetch identities mark a raw HTTP client in client-fetch.
    // This adapter owns admission after IPC preparation, so retain its private
    // authority callback until that boundary instead of stripping it as raw I/O.
    sourceFetch: (input, init) => transportFetch(input, init),
    // MTProto reconnects inside Telethon. Replaying an uncertain outbound call
    // at the old HTTP fallback layer could send a duplicate message.
    forceFallback: () => false,
    close: async () => {
      lifetime.abort(new Error("Telegram transport closed"));
    },
  };
}

export function resolveTelegramFetch(
  proxyFetch?: typeof fetch,
  options?: { network?: TelegramNetworkConfig },
): typeof fetch {
  return resolveTelegramTransport(proxyFetch, options).fetch;
}
