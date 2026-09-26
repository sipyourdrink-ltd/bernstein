# Opting in: the email

Send this from any address. The subject line is what routes the message;
the three lines in the body are what the script reads. Everything else in
the message is read by a person, later.

```
To:      forte@bernstein.run
Subject: BRNSTN-PR-LNKD

GITHUB=your-github-login
OPT_IN=YES
RECOMMENDATION=NO
```

Rules the parser applies, so you know what it will and will not understand:

- The subject must be exactly `BRNSTN-PR-LNKD`. Anything else lands in the
  general queue.
- `GITHUB=` is your GitHub login, the one on your pull requests, without
  the `@`.
- `OPT_IN=YES` to be named in the consolidated LinkedIn post; `OPT_IN=NO`
  to be taken out again later.
- `RECOMMENDATION=YES` if you would like a LinkedIn recommendation from the
  maintainer instead of, or as well as, the post. Leave it at `NO`
  otherwise.
- `NOTE=` (optional, one line) for anything the maintainer should know: for
  instance that you want to see the wording before it goes out, or that
  your work was mostly reviews and you are asking about the soft gate.
- Keys are case-insensitive, one per line, first occurrence wins. Quoted
  text (`> `) is ignored, so replying on top of an earlier mail is fine.

Where it goes: the mail is routed by its subject into the contributors
queue, the three lines are read by a script, and the result is a line in
`community/recognition/opt-ins.toml`, reviewed by the maintainer. You do
not need to open a pull request for that.
