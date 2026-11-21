## `plan validate` reports an unreadable plan file

`bernstein plan validate` read the plan before its error handler, so a file
with an invalid UTF-8 byte printed a Python traceback. The command now reads
the file first and exits 1 with an error that names the file and the reason,
for an encoding error and for an I/O error alike (#7870).
