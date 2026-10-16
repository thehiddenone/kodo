Work only from this text and the files in the current directory. Do not ask questions; nobody will answer them.

`t1/orders.csv` is a CSV file: a header row, then one order per row. Create the file `t1/summary.txt` holding exactly these two lines:

```text
rows=N
max_id=ID
```

N is the number of orders in `t1/orders.csv`; the header row is not an order. ID is the `id` of the order with the largest `amount_cents`. End each of the two lines with one newline character (LF). Put nothing else in the file: no spaces, no blank lines, no other text.

Do not create, change or delete any other file.
