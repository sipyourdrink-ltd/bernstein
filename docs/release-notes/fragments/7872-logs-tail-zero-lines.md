## `logs tail --lines 0` prints no stored lines

`bernstein logs tail` sliced the log with `[-lines:]`, so `--lines 0` printed
every stored line (also before `--follow` started) and `--lines -1` printed all
but the first. Zero now prints no stored line, a negative count is a usage
error, and positive counts are unchanged (#7872).
