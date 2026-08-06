Now write me a summary report as `report.md` in the workspace root, based on
the analysis results.

Read `results.json` from disk and use the numbers in it. Do not recompute them
from `data.csv`, and do not rely on anything you remember from the previous
step — read the file.

The report must contain:

- A short opening line stating the global mean revenue.
- A `## Above average` section listing every region whose total is **strictly
  greater than** `global_mean`. For each: the region name, its total, and the
  recommendation `INCREASE STOCK`.
- A `## Below average` section listing every other region. For each: the region
  name, its total, and the recommendation `REVIEW PRICING`.

List each region under exactly one of the two sections, and quote every total
exactly as it appears in `results.json`.
