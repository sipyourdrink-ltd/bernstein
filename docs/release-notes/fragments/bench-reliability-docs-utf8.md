## The reliability docs assertion reads its file as UTF-8

`tests/unit/eval/bench/test_reliability.py` read `docs/eval/reliability.md`
with a bare `read_text()`, so the decoding depended on the host's preferred
encoding. That file carries 96 non-ASCII bytes, including U+2500 box drawing:
it raises `UnicodeDecodeError` on a host whose default is cp932, and under
cp1252 it decodes to 64 characters that are not what the file says, so the
assertions run against mojibake rather than failing.

Every other call site in the same directory already passes
`encoding="utf-8"`, including the assertion eleven lines below this one.
