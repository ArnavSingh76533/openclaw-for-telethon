import { buildJsonPluginConfigSchema, definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import manifest from "./openclaw.plugin.json" with { type: "json" };
import { createAssistantMessageEventStream, validateToolArguments } from "openclaw/plugin-sdk/llm";
import { spawnTerminalPty } from "openclaw/plugin-sdk/process-runtime";
import { MARKER, PROVIDER, complete, discover, settings } from "./bridge.mjs";
import { login } from "./login.mjs";

const baseUrl = "http://127.0.0.1/agy-cli-owned-transport";
const providerConfig = (models) => ({ baseUrl, api: "openai-completions", apiKey: MARKER, models });

export function streamFor(options) {
  return (model, context, callOptions = {}) => {
    const stream = createAssistantMessageEventStream();
    const message = {
      role: "assistant",
      api: model.api,
      provider: model.provider,
      model: model.id,
      content: [],
      stopReason: "stop",
      timestamp: Date.now(),
      usage: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      },
    };
    stream.push({ type: "start", partial: { ...message } });
    void (async () => {
      try {
        const result = await complete(options, model, context, {
          signal: callOptions.signal,
          validate: validateToolArguments,
        });
        message.content = result.content;
        message.stopReason = result.stopReason;
        const count = (value) => (Number.isSafeInteger(value) && value >= 0 ? value : 0);
        const u = result.usage;
        message.usage.cacheRead = count(u.cache_read_tokens);
        message.usage.input = Math.max(0, count(u.input_tokens) - message.usage.cacheRead);
        message.usage.output = count(u.output_tokens);
        message.usage.totalTokens =
          count(u.total_tokens) ||
          message.usage.input + message.usage.cacheRead + message.usage.output;
        // shortcut: structured responses publish complete content, not token deltas.
        for (let contentIndex = 0; contentIndex < message.content.length; contentIndex++) {
          const block = message.content[contentIndex];
          if (block.type === "text") {
            stream.push({ type: "text_start", contentIndex, partial: message });
            stream.push({ type: "text_delta", contentIndex, delta: block.text, partial: message });
            stream.push({ type: "text_end", contentIndex, content: block.text, partial: message });
          } else {
            stream.push({ type: "toolcall_start", contentIndex, partial: message });
            stream.push({
              type: "toolcall_delta",
              contentIndex,
              delta: JSON.stringify(block.arguments),
              partial: message,
            });
            stream.push({ type: "toolcall_end", contentIndex, toolCall: block, partial: message });
          }
        }
        stream.push({ type: "done", reason: message.stopReason, message });
      } catch (error) {
        message.stopReason = callOptions.signal?.aborted ? "aborted" : "error";
        message.errorMessage = error instanceof Error ? error.message : "AGY request failed";
        stream.push({ type: "error", reason: message.stopReason, error: message });
      } finally {
        stream.end();
      }
    })();
    return stream;
  };
}

export default definePluginEntry({
  id: PROVIDER,
  name: "Antigravity CLI (AGY)",
  description: "Opt-in, CLI-owned Google authentication and models",
  configSchema: buildJsonPluginConfigSchema(manifest.configSchema),
  register(api) {
    const options = settings(api.pluginConfig);
    const loadModels = (signal) => discover(options, signal);
    api.registerProvider({
      id: PROVIDER,
      label: "Antigravity CLI (AGY)",
      authScope: "plugin",
      auth: [
        {
          id: "google-cli",
          label: "Google sign-in through AGY",
          kind: "oauth",
          async run(ctx) {
            await login(options, ctx, spawnTerminalPty);
            const models = await loadModels(ctx.signal);
            return {
              profiles: [],
              defaultModel: `${PROVIDER}/${models[0].id}`,
              configPatch: { models: { providers: { [PROVIDER]: providerConfig(models) } } },
              notes: [
                "AGY owns the Google session. Select an antigravity-cli model under Models. Your existing default model was preserved.",
              ],
            };
          },
        },
      ],
      catalog: {
        order: "late",
        async run(ctx) {
          if (!ctx.config.models?.providers?.[PROVIDER]) return null;
          return { provider: providerConfig(await loadModels(ctx.signal)) };
        },
      },
      resolveSyntheticAuth: ({ config }) =>
        config?.models?.providers?.[PROVIDER]
          ? {
              apiKey: MARKER,
              source: "AGY CLI-owned transport (non-secret eligibility marker)",
              mode: "api-key",
            }
          : undefined,
      createStreamFn: () => streamFor(options),
    });
    api.registerModelCatalogProvider({
      provider: PROVIDER,
      kinds: ["text"],
      async liveCatalog(ctx) {
        if (!ctx.config.models?.providers?.[PROVIDER]) return [];
        return (await loadModels(ctx.signal)).map((model) => ({
          kind: "text",
          provider: PROVIDER,
          model: model.id,
          label: model.name,
          source: "live",
          capabilities: { input: ["text"], tools: true },
        }));
      },
    });
  },
});
