import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const current = [
  "README.md",
  "README.en.md",
  "README.ja.md",
  "docs/architecture.md",
  "docs/operations.md",
  "docs/codex.md",
  "docs/reminders.md",
  "docs/local-embedding.md",
  "docs/benchmarks/work-memory-v2.md",
  "examples/README.md",
];
const history = [
  "DESIGN.md",
  "SPEC.md",
  "DSH-ADAPTER.md",
  "docs/performance.md",
  "docs/coverage.md",
  "docs/contact-tasks.md",
  "docs/self-knowledge.md",
];
const stale = /\bKin\b|小光|companion|self_knowledge|\/v1\/contact|legacyMode|wechat/i;

for (const name of current) {
  const body = readFileSync(name, "utf8");
  if (stale.test(body)) throw Error(`${name}: retired companion API or wording`);
  for (const match of body.matchAll(/\[[^\]]+\]\(([^)]+)\)/g)) {
    const href = match[1].split("#", 1)[0];
    if (!href || /^https?:\/\//.test(href)) continue;
    const target = resolve(dirname(name), decodeURIComponent(href));
    if (!existsSync(target)) throw Error(`${name}: missing link target ${href}`);
  }
}
for (const name of ["README.md", "README.en.md", "README.ja.md"]) {
  if (!readFileSync(name, "utf8").startsWith("# MemoryPalace 2.0")) {
    throw Error(`${name}: not a 2.0 README`);
  }
}
for (const name of history) {
  const opening = readFileSync(name, "utf8").slice(0, 400);
  if (!/Historical|历史/.test(opening)) throw Error(`${name}: missing history marker`);
}
console.log("Current 2.0 docs and historical labels checked");
