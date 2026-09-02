## ``bernstein audit receipt conform`` checks receipts against numbered format requirements

Added ``bernstein audit receipt conform`` to validate audit receipts against a
numbered set of format requirements with an executable corpus of positive and
negative test cases. Running the command without arguments checks a verifier
implementation (default: the standalone verifier) against every corpus case;
passing a receipt path evaluates that specific receipt and reports which
requirement(s) it violates. Exit codes: 0 conformant, 1 violation, 2 verifier
not found. (#4987)
