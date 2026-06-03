// tsc only emits JS; runtime assets read via fs must be copied into dist so the
// built binary can find them at the same __dirname-relative paths used in dev.
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";

const root = process.cwd();
const assets = [
  ["config/default.toml", "dist/config/default.toml"],
  ["src/pricing/table.json", "dist/src/pricing/table.json"],
  ["src/store/schema.sql", "dist/src/store/schema.sql"],
];

for (const [from, to] of assets) {
  const dest = join(root, to);
  mkdirSync(dirname(dest), { recursive: true });
  copyFileSync(join(root, from), dest);
}

console.log(`Copied ${assets.length} runtime assets into dist/`);
