import { registerHooks } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
if (!process.env.OPENCLAW_SOURCE_ROOT)
  throw new Error("Set OPENCLAW_SOURCE_ROOT to the OpenClaw source checkout.");
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("openclaw/plugin-sdk/")) {
      const name = specifier.slice("openclaw/plugin-sdk/".length);
      return nextResolve(
        pathToFileURL(
          path.join(process.env.OPENCLAW_SOURCE_ROOT, "src", "plugin-sdk", `${name}.ts`),
        ).href,
        context,
      );
    }
    return nextResolve(specifier, context);
  },
});
