## A test file that crashes now says why

`scripts/run_tests.py` could only quote a failure pytest had reported: it
collected the `FAILURES` section and the short test summary, and when neither
was present it fell back to the last thirty lines of the file's output. A file
whose process dies before pytest prints either - a segfault, an OOM kill, a
hard exit from inside a test - produced neither section, so the report line
lost its counts and the fallback printed whichever test happened to print most
recently under `-s`. The exit code appeared nowhere in the job log.

Such a file is now classified as a crash, distinct from a reported failure and
just as fatal to the run. It reports as `CRASH <file> (<duration>) exit code
134`, or `killed by signal 11 (SIGSEGV)` where the process was signalled,
followed by the last lines of that file's own stderr - or a note that it wrote
none. A file that fails an assertion is reported exactly as before (#6039).
