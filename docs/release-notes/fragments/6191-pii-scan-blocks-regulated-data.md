## The `pii_scan` gate blocks a Social Security number or a card number

The gate decided its verdict on `severity == "high"`, and the SSN and
payment-card rules are graded `"medium"`. So agent output holding either one
passed, with the finding listed in the gate's own detail, while a private key
blocked. Each finding now carries `block_merge`, set for every high-severity
secret and for the regulated-data rules in `BLOCKING_PII_RULES`, and the gate
blocks on that. Email addresses and phone numbers remain warnings (#6191).
