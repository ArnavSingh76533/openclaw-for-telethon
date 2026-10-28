import { childEnv, discover, prepareHome } from "./bridge.mjs";

export function loginFailure(output, submitted) {
  if (/timed out|deadline exceeded|authentication timeout|timeout waiting/i.test(output))
    return new Error(
      "The AGY sign-in timed out. Start again and submit the new Google code within 60 seconds.",
    );
  if (/invalid_grant|invalid.*authorization code/i.test(output))
    return new Error(
      "Google rejected this authorization code. Start a fresh sign-in; codes cannot be reused.",
    );
  if (/keyring|secret service|credential.*stor|dbus/i.test(output))
    return new Error(
      "AGY could not save its Google session. Check the CLI credential store and provider HOME permissions.",
    );
  return new Error(
    submitted
      ? "AGY could not complete Google sign-in or account setup. Check account access and retry."
      : "AGY closed before receiving the Google code. Start a fresh sign-in and submit the code in OpenClaw.",
  );
}

export function findLoginUrl(text) {
  const match = text.match(/https:\/\/accounts\.google\.com\/o\/oauth2\/auth\?[^\s\x1b]+/);
  if (!match) return undefined;
  const url = new URL(match[0]);
  if (url.searchParams.get("redirect_uri") !== "https://antigravity.google/oauth-callback")
    throw new Error("Unexpected AGY OAuth callback.");
  return url.href;
}

export async function login(options, ctx, spawnPty) {
  await ctx.prompter.note(
    "AGY owns Google sign-in and credentials. Google currently prohibits third-party Antigravity OAuth use, including OpenClaw, and may suspend accounts. This optional adapter is not endorsed by Google or Hermes. Review https://antigravity.google/terms before continuing.",
    "Antigravity CLI",
  );
  if (
    !(await ctx.prompter.confirm({
      message: "Start the official AGY Google sign-in?",
      initialValue: false,
    }))
  )
    throw new Error("AGY sign-in canceled.");
  ctx.signal?.throwIfAborted();
  ctx.assertCurrent?.();
  await prepareHome(options.homeDir);
  const terminal = await spawnPty(
    {
      file: options.command,
      args: ["-p", "Reply with exactly: OK", "--print-timeout", "2m"],
      cwd: options.homeDir,
      env: childEnv(options.homeDir, ctx.env || process.env),
      cols: 240,
      rows: 40,
    },
    { abortSignal: ctx.signal, assertCurrent: ctx.assertCurrent },
  );
  let output = "",
    settled = false,
    urlStarted = false,
    submitted = false;
  let dataSubscription, exitSubscription, timer;
  const abort = new AbortController();
  const cancel = () => abort.abort(ctx.signal?.reason);
  ctx.signal?.addEventListener("abort", cancel, { once: true });
  if (ctx.signal?.aborted) cancel();
  try {
    await new Promise((resolve, reject) => {
      const finish = (error) => {
        if (settled) return;
        settled = true;
        error ? reject(error) : resolve();
      };
      abort.signal.addEventListener(
        "abort",
        () => finish(new Error("AGY login canceled or timed out.")),
        { once: true },
      );
      if (abort.signal.aborted) return finish(new Error("AGY login canceled."));
      timer = setTimeout(() => abort.abort(), 180000);
      exitSubscription = terminal.onExit(({ exitCode }) => {
        if (exitCode === 0) return finish();
        if (!submitted || abort.signal.aborted) return finish(loginFailure(output, submitted));
        // Login may succeed even when the CLI's initial default-model prompt has no quota.
        void (async () => {
          try {
            ctx.assertCurrent?.();
            await discover(options, abort.signal);
            ctx.assertCurrent?.();
            finish();
          } catch {
            finish(loginFailure(output, submitted));
          }
        })();
      });
      dataSubscription = terminal.onData((chunk) => {
        if (settled) return;
        output = (output + chunk).slice(-65536).replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, "");
        if (urlStarted) return;
        try {
          const url = findLoginUrl(output);
          if (!url || !output.includes("paste the authorization code")) return;
          urlStarted = true;
          void (async () => {
            ctx.assertCurrent?.();
            await ctx.prompter.note(
              `Copy this official Google URL into a new browser tab:\n${url}\n\nComplete Google sign-in, then return to this OpenClaw tab and paste the code below within 60 seconds. Only continue a sign-in you started.`,
              "Google sign-in",
            );
            // AGY uses an out-of-band code, not OpenClaw's automatic browser callback.
            const code = await ctx.prompter.text({
              message: "Paste the authorization code shown by Google",
              sensitive: true,
              signal: abort.signal,
              validate: (value) =>
                value.trim() && !/[\r\n\x00-\x1f]/.test(value)
                  ? undefined
                  : "Enter a single authorization code.",
            });
            if (settled) return;
            abort.signal.throwIfAborted();
            ctx.assertCurrent?.();
            submitted = true;
            terminal.write(`${code.trim()}\r`);
          })().catch(finish);
        } catch (error) {
          finish(error);
        }
      });
    });
  } finally {
    clearTimeout(timer);
    abort.abort();
    ctx.signal?.removeEventListener("abort", cancel);
    dataSubscription?.dispose();
    exitSubscription?.dispose();
    terminal.kill();
    output = "";
  }
}
