import { copyFile, mkdir } from 'node:fs/promises';

await mkdir('static/vendor', { recursive: true });
for (const [source, target] of [
  ['node_modules/marked/lib/marked.umd.js', 'marked.js'],
  ['node_modules/dompurify/dist/purify.min.js', 'purify.js'],
  ['node_modules/lucide/dist/umd/lucide.min.js', 'lucide.js'],
]) {
  await copyFile(source, `static/vendor/${target}`);
}