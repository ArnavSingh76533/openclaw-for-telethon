import assert from "node:assert/strict";
import { test } from "node:test";
import plugin, { streamFor } from "../index.mjs";
import { parseResponse } from "../bridge.mjs";
import { validateToolArguments } from "openclaw/plugin-sdk/llm";

test("real SDK: plugin registers Models auth, catalog and stream without changing defaults", () => {
  let provider, catalog;
  plugin.register({
    pluginConfig: {},
    registerProvider(value) {
      provider = value;
    },
    registerModelCatalogProvider(value) {
      catalog = value;
    },
  });
  assert.equal(provider.id, "antigravity-cli");
  assert.equal(provider.auth[0].kind, "oauth");
  assert.equal(catalog.provider, provider.id);
  assert.equal(typeof provider.createStreamFn(), "function");
  assert.equal(provider.resolveSyntheticAuth({}), undefined);
  assert.equal(
    provider.resolveSyntheticAuth({
      config: { models: { providers: { "antigravity-cli": { models: [] } } } },
    }).apiKey,
    "agy-cli-owned-auth",
  );
  assert.equal(
    plugin.configSchema.safeParse({ command: "/bin/agy", timeoutSeconds: 60 }).success,
    true,
  );
  assert.equal(plugin.configSchema.safeParse({ timeoutSeconds: 0 }).success, false);
});
test("real SDK: invalid tool arguments fail before OpenClaw execution", () => {
  const context = {
    tools: [
      {
        name: "lookup",
        description: "",
        parameters: {
          type: "object",
          properties: { query: { type: "string" } },
          required: ["query"],
        },
      },
    ],
  };
  const result = (args) => ({
    status: "SUCCESS",
    structured_output: { text: "", tool_calls: [{ name: "lookup", arguments: args }] },
  });
  assert.throws(() => parseResponse(result({}), context, validateToolArguments), /query/);
  assert.equal(
    parseResponse(result({ query: "hi" }), context, validateToolArguments).content[0].arguments
      .query,
    "hi",
  );
});
test("real SDK: unsupported media produces a settled error event", async () => {
  const stream = streamFor({})(
    { api: "openai-completions", provider: "antigravity-cli", id: "test" },
    { messages: [{ role: "user", content: [{ type: "image", data: "x" }] }] },
  );
  const events = [];
  for await (const event of stream) events.push(event.type);
  assert.deepEqual(events, ["start", "error"]);
  assert.equal((await stream.result()).stopReason, "error");
});
