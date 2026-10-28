import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import {
  buildRequest,
  childEnv,
  complete,
  discover,
  parseModels,
  parseResponse,
  prepareHome,
  settings,
  DENY,
} from "../bridge.mjs";
import { findLoginUrl, login, loginFailure } from "../login.mjs";

const tool = {
  name: "lookup",
  description: "Look up information",
  parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
};
const context = (text) => ({
  systemPrompt: "Be helpful.",
  messages: [{ role: "user", content: text }],
  tools: [tool],
});
const fixture = fileURLToPath(new URL("./fake-agy.mjs", import.meta.url));
async function setup(t) {
  const homeDir = await mkdtemp(path.join(tmpdir(), "agy-test-"));
  t.after(() => rm(homeDir, { recursive: true, force: true }));
  // Node's entrypoint through a real executable wrapper, without any shell prompt input.
  const wrapper = path.join(homeDir, process.platform === "win32" ? "fixture.exe" : "fixture");
  if (process.platform === "win32") {
    return { homeDir, command: process.execPath, timeoutMs: 1000 };
  }
  await writeFile(
    wrapper,
    `#!/bin/sh\nexec '${process.execPath.replaceAll("'", "'\\''")}' '${fixture.replaceAll("'", "'\\''")}' "$@"\n`,
    { mode: 0o700 },
  );
  return { homeDir, command: wrapper, timeoutMs: 1000 };
}
test("model discovery preserves exact IDs and rejects an empty catalog", () => {
  const rows = parseModels(
    "MODEL ID  NAME\ngemini-4-high  Gemini 4 High\nclaude-x  Claude X\ngemini-4-high  Duplicate\n",
  );
  assert.deepEqual(
    rows.map((row) => row.id),
    ["gemini-4-high", "claude-x"],
  );
  assert.throws(() => parseModels("Authentication required"), /no models/);
  assert.deepEqual(
    parseModels(
      "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n",
    ).map((m) => m.id),
    ["gemini-3.8-flash-high", "claude-sonnet-4-6"],
  );
});
test("child environment does not inherit API keys or AGY overrides", () => {
  const env = childEnv("/private", {
    PATH: "/bin",
    OPENAI_API_KEY: "SECRET",
    ANTIGRAVITY_CONFIG_DIR: "/wrong",
  });
  assert.equal(env.OPENAI_API_KEY, undefined);
  assert.equal(env.ANTIGRAVITY_CONFIG_DIR, undefined);
  assert.equal(env.HOME, "/private");
});
test("home preparation denies all native action namespaces without touching credentials", async (t) => {
  const opts = await setup(t);
  await prepareHome(opts.homeDir);
  const dir = path.join(opts.homeDir, ".gemini", "antigravity-cli");
  await writeFile(path.join(dir, "oauth-token"), "untouched");
  await Promise.all([prepareHome(opts.homeDir), prepareHome(opts.homeDir)]);
  assert.deepEqual(
    JSON.parse(await readFile(path.join(dir, "settings.json"), "utf8")).permissions.deny,
    DENY,
  );
  assert.equal(await readFile(path.join(dir, "oauth-token"), "utf8"), "untouched");
});
test("request preserves system, tools, tool call IDs, and error results", () => {
  const ctx = context("Hi");
  ctx.messages.push({
    role: "toolResult",
    toolName: "lookup",
    toolCallId: "abc",
    isError: true,
    content: [{ type: "text", text: "failure" }],
  });
  const request = buildRequest(ctx),
    parsed = JSON.parse(request.prompt);
  assert.equal(parsed.systemPrompt, ctx.systemPrompt);
  assert.equal(parsed.messages[1].toolCallId, "abc");
  assert.equal(parsed.messages[1].isError, true);
  assert.deepEqual(
    request.schema.properties.tool_calls.items.anyOf[0].properties.arguments,
    tool.parameters,
  );
});
test("unsupported image content fails instead of silently discarding it", () => {
  assert.throws(
    () => buildRequest({ messages: [{ role: "user", content: [{ type: "image", data: "x" }] }] }),
    /media-capable/,
  );
});
test("responses reject unknown tools, failed status, malformed arguments, and empty replies", () => {
  const result = (value) => ({ status: "SUCCESS", structured_output: value });
  assert.throws(
    () =>
      parseResponse(
        result({ text: "", tool_calls: [{ name: "exec", arguments: {} }] }),
        context(""),
      ),
    /unavailable/,
  );
  assert.throws(() => parseResponse({ status: "ERROR" }, context("")), /successfully/);
  assert.throws(
    () =>
      parseResponse(
        result({ text: "", tool_calls: [{ name: "lookup", arguments: [] }] }),
        context(""),
      ),
    /malformed/,
  );
  assert.throws(() => parseResponse(result({ text: "", tool_calls: [] }), context("")), /empty/);
});
test("tool validation runs before returning a call", () => {
  assert.throws(
    () =>
      parseResponse(
        {
          status: "SUCCESS",
          structured_output: { text: "", tool_calls: [{ name: "lookup", arguments: {} }] },
        },
        context(""),
        () => {
          throw new Error("query required");
        },
      ),
    /query required/,
  );
});
test("Google login URL restricts the official callback", () => {
  assert.equal(findLoginUrl("nothing"), undefined);
  assert.throws(
    () =>
      findLoginUrl(
        "https://accounts.google.com/o/oauth2/auth?redirect_uri=https%3A%2F%2Fevil.test",
      ),
    /callback/,
  );
  assert.match(
    findLoginUrl(
      "https://accounts.google.com/o/oauth2/auth?redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback",
    ),
    /^https:\/\/accounts.google.com/,
  );
});
test("declining login never starts a CLI process", async () => {
  let spawned = false;
  await assert.rejects(
    login({}, { prompter: { note: async () => {}, confirm: async () => false } }, () => {
      spawned = true;
    }),
    /canceled/,
  );
  assert.equal(spawned, false);
});
for (const exitCode of [0, 7])
  test(
    `login delegates code entry and checks session after exit ${exitCode}`,
    { skip: process.platform === "win32" && exitCode !== 0 },
    async (t) => {
      const opts = await setup(t);
      let data,
        exit,
        wrote,
        opened,
        killed = false,
        disposed = 0;
      const codeUrl =
        "https://accounts.google.com/o/oauth2/auth?redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback";
      const ctx = {
        prompter: {
          note: async () => {},
          confirm: async () => true,
          text: async (prompt) => {
            assert.equal(prompt.sensitive, true);
            return "user-code";
          },
        },
        openUrl: async (url) => {
          opened = url;
        },
        assertCurrent: () => {},
      };
      const spawn = async (params) => {
        assert.deepEqual(params.args.slice(0, 2), ["-p", "Reply with exactly: OK"]);
        queueMicrotask(() =>
          setImmediate(() =>
            data(
              "Authentication required. " +
                codeUrl +
                "\nOr, paste the authorization code here and press Enter:\n",
            ),
          ),
        );
        return {
          onData(cb) {
            data = cb;
            return {
              dispose() {
                disposed++;
              },
            };
          },
          onExit(cb) {
            exit = cb;
            return {
              dispose() {
                disposed++;
              },
            };
          },
          write(value) {
            wrote = value;
            setImmediate(() => exit({ exitCode }));
          },
          kill() {
            killed = true;
          },
        };
      };
      await login(opts, ctx, spawn);
      assert.equal(wrote, "user-code\r");
      assert.equal(opened, undefined);
      assert.equal(killed, true);
      assert.equal(disposed, 2);
    },
  );
test("login errors explain expiry without exposing raw diagnostics", () => {
  assert.match(loginFailure("Authentication timed out SECRET", false).message, /timed out/);
  assert.match(loginFailure("invalid_grant SECRET", true).message, /cannot be reused/);
  assert.match(loginFailure("dbus SECRET", true).message, /credential store/);
  assert.equal(loginFailure("SECRET", true).message.includes("SECRET"), false);
});
test("canceling a running login closes its PTY", async (t) => {
  const opts = await setup(t),
    controller = new AbortController();
  let killed = false;
  const ctx = {
    signal: controller.signal,
    prompter: { note: async () => {}, confirm: async () => true },
  };
  const spawn = async () => {
    setTimeout(() => controller.abort(), 10);
    return {
      onData() {},
      onExit() {},
      kill() {
        killed = true;
      },
    };
  };
  await assert.rejects(login(opts, ctx, spawn), /canceled/);
  assert.equal(killed, true);
});
test("default home is within persistent OpenClaw state", () => {
  assert.match(settings().homeDir, /agy-home$/);
});

// Real subprocess coverage runs on the Linux deployment platform.
for (const name of [
  "discovery",
  "text",
  "tools",
  "failure",
  "malformed",
  "native",
  "missing",
  "cancel",
  "timeout",
  "bad-model",
]) {
  test(`subprocess: ${name}`, { skip: process.platform === "win32" }, async (t) => {
    const opts = await setup(t);
    if (name === "discovery")
      return assert.deepEqual(
        (await discover(opts)).map((m) => m.id),
        ["gemini-test-high", "claude-test"],
      );
    if (name === "cancel" || name === "timeout") {
      const controller = new AbortController();
      if (name === "cancel") setTimeout(() => controller.abort(), 100);
      await assert.rejects(
        complete({ ...opts, timeoutMs: 150 }, { id: "gemini-test-high" }, context("hang"), {
          signal: controller.signal,
        }),
        name === "cancel" ? /abort|cancel/i : /timed out/,
      );
      return;
    }
    const prompts = {
      failure: "fail",
      malformed: "malformed",
      native: "native",
      missing: "missing",
      tools: "tool",
    };
    const run = () =>
      complete(
        opts,
        { id: name === "bad-model" ? "not-an-alias" : "gemini-test-high" },
        context(prompts[name] || "hello"),
      );
    if (["failure", "malformed", "native", "missing", "bad-model"].includes(name)) {
      await assert.rejects(run(), (error) => !error.message.includes("SECRET_MUST_NOT_LEAK"));
      return;
    }
    const result = await run();
    assert.equal(result.stopReason, name === "tools" ? "toolUse" : "stop");
    if (name === "tools") {
      const ctx = context("tool");
      ctx.messages.push({ role: "assistant", content: result.content });
      ctx.messages.push({
        role: "toolResult",
        toolName: "lookup",
        toolCallId: result.content[0].id,
        isError: false,
        content: [{ type: "text", text: "sunny" }],
      });
      assert.equal(
        (await complete(opts, { id: "gemini-test-high" }, ctx)).content[0].text,
        "Tool result received: sunny",
      );
    } else assert.equal(result.content[0].text, "Hello");
  });
}
