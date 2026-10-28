import { readFile } from "node:fs/promises";
const args = process.argv.slice(2);
if (args[0] === "models") {
  process.stdout.write(
    "MODEL ID                      NAME\ngemini-test-high              Gemini Test High\nclaude-test                   Claude Test\n",
  );
} else {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const request = JSON.parse(JSON.parse(input).message.content);
  const last = request.messages.at(-1);
  const text = last.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("");
  const model = args[args.indexOf("--model") + 1];
  const schema = JSON.parse(await readFile(args[args.indexOf("--json-schema") + 1], "utf8"));
  const config = JSON.parse(
    await readFile(`${process.env.HOME}/.gemini/antigravity-cli/settings.json`, "utf8"),
  );
  if (!config.permissions.deny.includes("command(*)") || !schema.required.includes("tool_calls"))
    process.exit(30);
  if (text === "hang") await new Promise(() => setInterval(() => {}, 1000));
  if (text === "fail") {
    process.stderr.write("SECRET_MUST_NOT_LEAK");
    process.exit(7);
  }
  if (text === "malformed") {
    console.log("not-json");
    process.exit(0);
  }
  if (text === "native") {
    console.log(
      JSON.stringify({
        event: "step_update",
        step_update: { step_type: "tool", tool_name: "run_command" },
      }),
    );
    await new Promise(() => setInterval(() => {}, 1000));
  }
  if (text === "missing") process.exit(0);
  if (model !== "gemini-test-high") process.exit(9);
  const tool_calls = text === "tool" ? [{ name: "lookup", arguments: { query: "weather" } }] : [];
  const response = {
    event: "result",
    result: {
      status: "SUCCESS",
      structured_output: {
        text:
          last.role === "toolResult"
            ? "Tool result received: " + text
            : tool_calls.length
              ? ""
              : "Hello",
        tool_calls,
      },
      usage: {
        input_tokens: 120,
        output_tokens: 8,
        cache_read_tokens: 20,
        thinking_tokens: 2,
        total_tokens: 130,
      },
    },
  };
  const encoded = JSON.stringify(response) + "\n";
  process.stdout.write(encoded.slice(0, 35));
  setTimeout(() => process.stdout.write(encoded.slice(35)), 10);
}
