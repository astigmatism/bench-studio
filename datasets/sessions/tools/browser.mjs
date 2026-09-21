import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

// Only browser startup is retried. No page or candidate action has run yet.
// Keep the pinned browser and launch settings identical across attempts.
export async function launchBrowser(chromium, output) {
  const evidence = { status: 'starting', attempts: 0, failures: [] };
  mkdirSync(output, { recursive: true });
  const save = () => writeFileSync(join(output, 'browser-startup.json'), JSON.stringify(evidence, null, 2));
  for (let attempt = 1; attempt <= 3; attempt++) {
    evidence.attempts = attempt;
    try {
      const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], timeout: 30000 });
      evidence.status = 'ready';
      save();
      return browser;
    } catch (error) {
      const detail = String(error);
      evidence.status = 'failed';
      evidence.failures.push(detail.slice(-16000));
      save();
      if (attempt === 3 || !/SIGSEGV|Received signal 11/.test(detail)) {
        const failure = new Error(`Chromium failed to start after ${attempt} launch attempt${attempt === 1 ? '' : 's'}. No browser checks ran. See browser-startup.json.`, { cause: error });
        failure.name = 'BrowserStartupError';
        throw failure;
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
}
