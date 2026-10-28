import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const marker = /^space-state-(\d{13})-[a-f0-9-]+\.json$/;
const digest = async file => createHash('sha256').update(await fs.readFile(file)).digest('hex');
const log = message => console.log('[space-state]', message);

export async function openClaw(args) {
  const { stdout } = await execute('openclaw', [...args, '--json'], {
    // Large persistent browser profiles need more than two minutes to archive and verify.
    timeout: 600000, maxBuffer: 8 * 1024 * 1024,
    env: { ...process.env, NO_COLOR: '1' },
  });
  return JSON.parse(stdout);
}

// Keep live SQLite on local disk; bucket files are closed, verified snapshots.
export function stateBackup(state, directory, cli = openClaw) {
  let pending;
  async function requireStorage() {
    if (!directory || !(await fs.stat(directory)).isDirectory()) {
      throw new Error('Private backup bucket is not mounted; refusing ephemeral-only startup.');
    }
  }
  async function manifests() {
    await requireStorage();
    return (await fs.readdir(directory)).filter(name => marker.test(name)).sort().reverse();
  }
  async function restore() {
    const entries = await manifests();
    try { await fs.access(path.join(state, 'openclaw.json')); return; }
    catch (e) { if (e.code !== 'ENOENT') throw e; }
    if (!entries.length) { log('No checkpoint yet; starting first setup.'); return; }
    const saved = JSON.parse(await fs.readFile(path.join(directory, entries[0]), 'utf8'));
    if (saved.version !== 1 || saved.archive !== entries[0].replace(/\.json$/, '.tar.gz') ||
        !/^[a-f0-9]{64}$/.test(saved.sha256) || typeof saved.statePath !== 'string' ||
        saved.statePath.includes('\\') || saved.statePath.startsWith('/') ||
        saved.statePath.split('/').some(part => part === '..' || part === '.')) {
      throw new Error('Invalid checkpoint metadata; refusing to replace state.');
    }
    const temp = await fs.mkdtemp(path.join(path.dirname(state), '.space-restore-'));
    try {
      const archive = path.join(temp, 'state.tar.gz');
      await fs.copyFile(path.join(directory, saved.archive), archive);
      if (await digest(archive) !== saved.sha256) throw new Error('Checkpoint hash mismatch.');
      const staging = path.join(temp, 'extracted');
      await cli(['backup', 'restore', archive, '--target', staging]);
      const source = path.join(staging, saved.statePath);
      await fs.access(path.join(source, 'openclaw.json'));
      // Refuse to overwrite any existing unconfigured state, even on failed restores.
      try { await fs.rmdir(state); } catch (e) { if (e.code !== 'ENOENT') throw e; }
      await fs.rename(source, state);
      log(`Restored verified checkpoint ${entries[0]}.`);
    } finally { await fs.rm(temp, { recursive: true, force: true }); }
  }
  async function save() {
    await requireStorage();
    const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'space-checkpoint-'));
    try {
      const archive = path.join(temp, 'state.tar.gz');
      const result = await cli(['backup', 'create', '--output', archive, '--verify']);
      const asset = result.assets?.find(item => item.kind === 'state' && path.resolve(item.sourcePath) === path.resolve(state));
      if (!result.verified || !asset) throw new Error('Native backup did not verify the state asset.');
      const name = `space-state-${Date.now()}-${randomUUID()}`;
      const hash = await digest(archive);
      const remote = path.join(directory, name + '.tar.gz');
      await fs.copyFile(archive, remote);
      if (await digest(remote) !== hash) throw new Error('Uploaded checkpoint hash mismatch.');
      await fs.writeFile(path.join(directory, name + '.json'), JSON.stringify({
        version: 1, archive: name + '.tar.gz', sha256: hash, statePath: asset.archivePath,
      }), { flag: 'wx', mode: 0o600 });
      log(`Saved verified checkpoint ${name}.`);
      for (const old of (await manifests()).slice(3)) {
        await fs.unlink(path.join(directory, old));
        await fs.unlink(path.join(directory, old.replace(/\.json$/, '.tar.gz')));
      }
    } finally { await fs.rm(temp, { recursive: true, force: true }); }
  }
  return { restore, save() {
    if (!pending) pending = save().finally(() => { pending = undefined; });
    return pending;
  } };
}

export function healthWatchdog(probe, restart, limit = 3) {
  let failures = 0;
  let busy = false;
  return async () => {
    if (busy) return;
    busy = true;
    try {
      if (await probe()) failures = 0;
      else if (++failures >= limit) { failures = 0; await restart(); }
    } finally { busy = false; }
  };
}
