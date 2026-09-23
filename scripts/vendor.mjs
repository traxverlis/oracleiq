import { mkdir, readFile, writeFile } from 'node:fs/promises';

await mkdir('static/vendor', { recursive: true });
for (const [source, target] of [
  ['node_modules/marked/lib/marked.umd.js', 'marked.js'],
  ['node_modules/dompurify/dist/purify.min.js', 'purify.js'],
  ['node_modules/lucide/dist/umd/lucide.min.js', 'lucide.js'],
]) {
  // Source maps are not shipped: drop the reference so browser DevTools stop requesting them.
  const code = (await readFile(source, 'utf8')).replace(/\n?\/\/# sourceMappingURL=\S+\s*$/, '\n');
  await writeFile(`static/vendor/${target}`, code);
}