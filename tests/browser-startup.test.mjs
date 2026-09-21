import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { launchBrowser } from '../datasets/sessions/tools/browser.mjs';

for (const [name, failures, message, expected] of [
  ['transient startup crash', 1, 'browserType.launch: Received signal 11 SIGSEGV', 2],
  ['persistent startup crash', 5, 'browserType.launch: SIGSEGV', 3],
  ['missing browser installation', 5, 'Executable does not exist', 1],
]) {
  test(name, async () => {
    const out = mkdtempSync(join(tmpdir(), 'bench-browser-'));
    let calls = 0;
    const browser = {};
    const chromium = { launch: async () => {
      calls++;
      if (calls <= failures) throw new Error(message);
      return browser;
    } };
    try {
      if (failures < expected) assert.equal(await launchBrowser(chromium, out), browser);
      else await assert.rejects(launchBrowser(chromium, out), { name: 'BrowserStartupError' });
      assert.equal(calls, expected);
      const saved = JSON.parse(readFileSync(join(out, 'browser-startup.json')));
      assert.equal(saved.attempts, expected);
      assert.equal(saved.status, failures < expected ? 'ready' : 'failed');
      assert.equal(saved.failures.length, Math.min(failures, expected));
    } finally { rmSync(out, { recursive: true, force: true }); }
  });
}
