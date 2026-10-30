#!/usr/bin/env node
// Decompress dsh session archives (session.v4.jsonl.zstd) into plain JSONL.
// Usage: node sessions_dump.js --root ~/.dsh/sessions --out ~/.dsh/memory/cache [--force]
// Prints one output path per line.
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const args = process.argv.slice(2);
function arg(name, dflt) {
  const i = args.indexOf('--' + name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
}
const root = arg('root', path.join(process.env.HOME || '.', '.dsh/sessions'));
const out = arg('out', path.join(process.env.HOME || '.', '.dsh/memory/cache'));
const force = args.includes('--force');
fs.mkdirSync(out, { recursive: true });

function walk(dir, acc) {
  let ents;
  try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch { return acc; }
  for (const e of ents) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, acc);
    else if (e.name === 'session.v4.jsonl.zstd') acc.push(p);
  }
  return acc;
}

const files = walk(root, []);
const outPaths = [];
for (const f of files) {
  const name = path.basename(path.dirname(f));
  const dest = path.join(out, name + '.jsonl');
  try {
    const src = fs.statSync(f);
    if (!force && fs.existsSync(dest) && fs.statSync(dest).mtimeMs >= src.mtimeMs) {
      outPaths.push(dest);
      continue;
    }
    const buf = zlib.zstdDecompressSync(fs.readFileSync(f));
    fs.writeFileSync(dest, buf);
    outPaths.push(dest);
  } catch (err) {
    process.stderr.write('skip ' + f + ': ' + err.message + '\n');
  }
}
process.stdout.write(outPaths.join('\n') + (outPaths.length ? '\n' : ''));
