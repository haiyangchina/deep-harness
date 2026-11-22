import { createInterface } from "node:readline";
import { search } from "pi-web-access/gemini-search.ts";

// stdout is exclusively the wire protocol. Third-party diagnostics go to stderr.
console.log = console.info = console.debug = console.error;
const active = new Map<number, AbortController>();

interface Request {
  id: number;
  query: string;
  limit: number;
  freshness?: "day" | "week" | "month" | "year";
}

async function run(request: Request, controller: AbortController) {
  try {
    const batch = await search(request.query, {
      numResults: request.limit,
      recencyFilter: request.freshness,
      signal: controller.signal,
    });
    const hits = batch.results.slice(0, request.limit).map((row: {
      title: string; url: string; snippet: string; publishedDate?: unknown;
    }) => ({
      title: row.title.slice(0, 250),
      url: row.url.slice(0, 4096),
      snippet: row.snippet.slice(0, 1200),
      provider: batch.provider,
      published_at: typeof row.publishedDate === "string" ? row.publishedDate.slice(0, 40) : "",
    }));
    if (!controller.signal.aborted) reply({ id: request.id, hits });
  } catch (error) {
    if (!controller.signal.aborted) {
      // Upstream error messages can include request URLs or credentials.
      const kind = error instanceof Error && "kind" in error ? error.kind : "backend";
      reply({ id: request.id, error: String(kind) });
    }
  } finally {
    active.delete(request.id);
  }
}

function reply(message: unknown) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", line => {
  const request = JSON.parse(line);
  if (request.cancel) {
    active.get(request.id)?.abort();
    return;
  }
  const controller = new AbortController();
  active.set(request.id, controller);
  void run(request, controller);
});
input.on("close", () => {
  for (const controller of active.values()) controller.abort();
  process.exit(0);
});
