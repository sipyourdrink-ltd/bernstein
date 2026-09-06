## ASI detectors fold obfuscated spellings before matching

ASI01 and ASI06 match English goal-hijack keywords, and both matched the raw string. Two standard prompt-injection obfuscations therefore walked straight past them: a Cyrillic capital I in place of the ASCII one, and a zero-width space inside the keyword. Both render identically to a reader and neither matched a pattern.

The text is now folded before matching: invisible codepoints are dropped, NFKC is applied, and the confusables NFKC deliberately leaves alone are mapped to ASCII. The folded form is used for matching only, and a finding that came from it says so in its evidence, since the bytes an operator has to inspect are the ones that arrived.

ASI06's `source` field is also compared as a trust label rather than as bytes. `Untrusted` and `UNTRUSTED` used to read as trusted, which is the wrong direction to be wrong in when an integration partner's JSON envelope case-normalises on the way through.
