## `report commits` writes plain text when redirected

`bernstein report commits` rendered its table as a 120-column terminal string
and printed that string again, so redirected output held terminal escape bytes
and the header and totals broke onto extra lines at 80 columns. The table is
now printed by the console itself, which follows the output width and writes no
colour when the output is not a terminal (#7947).
