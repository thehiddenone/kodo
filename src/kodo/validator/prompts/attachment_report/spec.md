# Regional revenue analysis — specification

Build this in the current workspace, in **Python**, using a standard project
layout for a small Python tool.

## The input

`data.csv` already exists in the workspace root. It has a header row and three
columns:

| Column | Meaning |
|---|---|
| `region` | Region name (several rows per region) |
| `units` | Units sold in that transaction (integer) |
| `unit_price` | Price per unit for that transaction (decimal) |

## What to build

### 1. `analyze.py`

A script that reads `data.csv` from the workspace root and writes
`results.json` to the workspace root.

Revenue for a single row is `units × unit_price`. A region's total is the sum
over all of that region's rows.

`results.json` must be a JSON object with exactly two top-level keys:

- `"regions"` — an object mapping each region name to its total revenue,
  rounded to **2 decimal places**.
- `"global_mean"` — the arithmetic mean of the per-region totals (sum of the
  region totals divided by the number of regions), also rounded to 2 decimal
  places.

Shape:

```json
{
  "regions": { "<region>": 0.00 },
  "global_mean": 0.00
}
```

Do not hardcode the region names or any of the numbers — read them from the
CSV. Running the script must be what produces `results.json`; do not write that
file by hand.

### 2. Unit tests

Automated tests for the calculation logic, using `pytest`. Structure
`analyze.py` so its logic is importable and testable rather than all inline
under `if __name__ == "__main__":`. Cover at least:

- **Per-region totals** — multiple rows for one region are summed correctly.
- **Multiple regions** — rows are grouped by region, not mixed together.
- **The mean** — `global_mean` is the mean of the *region totals*, not the mean
  of the individual rows (these differ; a test should be able to tell them
  apart).
- **Rounding** — a value needing rounding lands on 2 decimal places.
- **An edge case** — an empty input, or a region with a single row.

Tests must use their own small fixture data, not depend on the real `data.csv`
— a test that breaks when the input file changes is testing the wrong thing.

### 3. Build toolchain

Set up the project's toolchain — the standard build/bootstrap, format,
static-analysis, and test scripts for a Python project — so the analysis and
its tests can be run repeatably from the command line rather than only from
inside an editor.

## When you are done

1. Run the toolchain yourself and make sure the tests actually pass.
2. Run `analyze.py` so that `results.json` exists in the workspace root.
3. Tell me the exact commands to run the tests and to run the analysis.

Do not summarise or interpret the results yet — just produce `results.json`.
