Work only from this text and the files in the current directory. Do not ask questions; nobody will answer them.

In `t7/report.py`, change the function `format_total(cents)` so that it returns the amount in dollars: a dollar sign, the whole dollars with a comma between each group of three digits, a period, and exactly two digits of cents. `cents` is an int, 0 or greater. Examples:

- `format_total(0)` returns `"$0.00"`
- `format_total(5)` returns `"$0.05"`
- `format_total(123456)` returns `"$1,234.56"`

Change nothing else. Every other function in `t7/report.py` and every other file in `t7/` stays exactly as it is, even where it looks wrong or carries a TODO comment.

Do not create, change or delete any file other than `t7/report.py`.
