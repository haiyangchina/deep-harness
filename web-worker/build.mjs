// Run via scripts/build_pi_worker.py, which installs the locked build inputs in Conda.
import { createRequire } from "node:module";
import { readFile, writeFile, mkdir, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const deps = path.resolve(process.argv[2], "node_modules");
const require = createRequire(path.join(deps, "../package.json"));
const { build } = require("esbuild");
const out = path.join(here, "../src/deep_harness/infrastructure/web/pi_dist");
await mkdir(out, { recursive: true });

// Keep the provider's explicit metadata before Pi flattens it into an answer string.
// Fail on upstream drift instead of silently publishing empty snippets or dates.
const patches = {
  "exa.ts": [[
    'snippet: "",',
    'snippet: item.highlights?.join(" ") || item.content || item.text || "", publishedDate: item.publishedDate,',
  ]],
  "bocha.ts": [[
    "results.push({ title, url, snippet });",
    "results.push({ title, url, snippet, publishedDate: entry.datePublished });",
  ]],
  "brave.ts": [[
    "snippet: stripHtml(item.description",
    "publishedDate: item.page_age, snippet: stripHtml(item.description",
  ]],
  "tavily.ts": [[
    "snippet: typeof item.content",
    "publishedDate: item.published_date, snippet: typeof item.content",
  ]],
  "parallel-mcp.ts": [[
    "snippet: excerpts(result)[0]",
    "publishedDate: result.publish_date, snippet: excerpts(result)[0]",
  ]],
  "gemini-search.ts": [[
    "const diagnostics: string[] = [];\n\tlet firstWeak:",
    "const started = performance.now();\n\tconst diagnostics: string[] = [];\n\tlet firstWeak:",
  ], [
    "const response = await untilDeadline(searchWithResolvedProvider(provider, query, options, useCurrentModel));",
    `const slots = routing.providers.length - routing.providers.indexOf(provider);
      const remaining = routing.timeoutMs - (performance.now() - started);
      const attempt = routing.timeoutMs && slots > 1
        ? AbortSignal.timeout(Math.max(1, Math.floor(remaining / slots)))
        : undefined;
      const attemptSignal = attempt ? AbortSignal.any([signal, attempt]) : signal;
      const response = await awaitWithAbort(searchWithResolvedProvider(
        provider, query, { ...options, signal: attemptSignal }, useCurrentModel,
      ), attemptSignal).catch(error => {
        if (attempt?.aborted && !signal?.aborted) {
          throw new SearchProviderError(provider, "transient", "Provider timed out", undefined, error);
        }
        throw error;
      });`,
  ]],
};
const result = await build({
  entryPoints: [path.join(here, "search.ts")], outfile: path.join(out, "search.cjs"),
  platform: "node", target: "node24", format: "cjs", bundle: true, minify: true,
  legalComments: "eof", metafile: true, nodePaths: [deps],
  external: ["@earendil-works/*"],
  plugins: [{ name: "source-metadata", setup(builder) {
    builder.onLoad({ filter: /pi-web-access[\\/](exa|bocha|brave|tavily|parallel-mcp|gemini-search)\.ts$/ }, async args => {
      let source = (await readFile(args.path, "utf8")).replaceAll("\r\n", "\n");
      for (const [original, replacement] of patches[path.basename(args.path)]) {
        if (source.split(original).length !== 2) throw new Error("Update Pi integration for " + args.path);
        source = source.replace(original, replacement);
      }
      return { contents: source, loader: "ts" };
    });
  } }],
});

// Distribute the licenses of the libraries actually incorporated into the bundle.
const packages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const full = path.resolve(input).split(path.sep);
  const i = full.lastIndexOf("node_modules");
  if (i >= 0) packages.add(full.slice(0, i + (full[i + 1].startsWith("@") ? 3 : 2)).join(path.sep));
}
const notices = [];
for (const dir of [...packages].sort()) {
  const pkg = JSON.parse(await readFile(path.join(dir, "package.json"), "utf8"));
  const files = (await readdir(dir)).filter(name => /^(licen[sc]e|copying|notice)(\.|$)/i.test(name)).sort();
  notices.push([pkg.name + " " + pkg.version, ...(await Promise.all(files.map(name =>
    readFile(path.join(dir, name), "utf8"))))].join("\n"));
}
await writeFile(path.join(out, "LICENSES.txt"), notices.join("\n\n"));
