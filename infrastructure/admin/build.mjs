import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const dir = dirname(fileURLToPath(import.meta.url));
const api = readFileSync(join(dir, 'worker.js'), 'utf8');
const html = readFileSync(join(dir, 'ui.html'), 'utf8');
const marker = '"__INLINE_HTML__"';
if (!api.includes(marker) || api.indexOf(marker) !== api.lastIndexOf(marker)) {
  throw new Error('Expected exactly one HTML substitution marker');
}
const result = api.replace(marker, JSON.stringify(html));
mkdirSync(join(dir, 'dist'), { recursive: true });
writeFileSync(join(dir, 'dist/worker.mjs'), result, 'utf8');
console.log('Panel compiled. No secrets or OSEP login used.');
