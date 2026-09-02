## A verified receipt now says what the verification proved

`POST /governance/verify-receipt` takes a run receipt as its request body and
returns the offline verifier's verdict. Nothing under `.sdd` and no key
material is read: the check works from the file's own bytes, so the endpoint
answers about the upload rather than about the installation serving it.

The verdict carries the tier the pass reached. A receipt whose only trust
anchor is itself reaches `integrity-only`, and the document says so in a
`caveat` field beside the result: the signature was checked against the key
embedded in the receipt, which proves the file is internally consistent and not
who produced it. Provenance still requires pinning the operator's key out of
band with `bernstein verify receipt <file> --public-key <pem>`; no key can be
pinned through the endpoint, because a key arriving in the same request as the
receipt is the same channel and not an independent anchor.

A tampered receipt, a file that is not a receipt, and an empty upload each come
back as a verdict with `200` rather than an HTTP error — "this file attests
nothing" is the answer the file was dropped to get.
