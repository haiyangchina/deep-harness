"""Measure offline coding-workspace primitives against a repeatable generated repository."""

import argparse
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from deep_harness.application.symbols import SymbolArgs, python_symbols, symbols
from deep_harness.domain.models import ProfileScope
from deep_harness.infrastructure.coding_workspace import capture, changes_between


def benchmark(count, runs):
    with TemporaryDirectory(prefix="deep-harness-benchmark-") as directory:
        temporary = Path(directory)
        project = temporary / "project"
        project.mkdir()
        for index in range(count):
            path = project / f"module_{index:05}.py"
            path.write_text(f"def symbol_{index}():\n    return {index}\n", encoding="utf-8")
        name = f"module_{count - 1:05}.py"
        target = project / name
        state = temporary / "state"
        scope = ProfileScope("benchmark", state, project, state)

        def resolve(value):
            return project / value

        def measure(action):
            timings = []
            for _ in range(runs):
                started = perf_counter()
                action()
                timings.append((perf_counter() - started) * 1000)
            return round(median(timings), 1)

        def find(*, cold=False):
            if cold:
                python_symbols.cache_clear()
            request = SymbolArgs(query=f"symbol_{count - 1}")
            pages = 0
            while True:
                page = symbols(request, resolve, resolve)
                pages += 1
                if page["matches"]:
                    assert page["matches"][0]["path"] == name
                    return pages
                if not page["next_cursor"]:
                    raise RuntimeError("Expected fixture symbol is missing")
                request = request.model_copy(update={"cursor": page["next_cursor"]})

        pages = find()
        cold_ms = measure(lambda: find(cold=True))
        warm_ms = measure(find)
        original = capture(scope, include_text=True)
        if not original["complete"]:
            raise RuntimeError("Fixture exceeds workspace observation limits")
        target.write_text("def replaced():\n    return 1\n", encoding="utf-8")

        def observe():
            change = changes_between(original, capture(scope, include_text=True))
            if len(change["files"]) != 1 or change["files"][0]["path"] != name:
                raise RuntimeError("Expected fixture change is missing")

        return {
            "files": count, "runs": runs, "symbol_pages": pages,
            "cold_symbols_ms_median": cold_ms, "warm_symbols_ms_median": warm_ms,
            "workspace_diff_ms_median": measure(observe),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1500)
    parser.add_argument("--runs", type=int, default=3)
    options = parser.parse_args()
    if not 1 <= options.files <= 10000 or not 1 <= options.runs <= 20:
        parser.error("Use 1–10000 files and 1–20 repetitions")
    print(json.dumps(benchmark(options.files, options.runs), indent=2))


if __name__ == "__main__":
    main()
