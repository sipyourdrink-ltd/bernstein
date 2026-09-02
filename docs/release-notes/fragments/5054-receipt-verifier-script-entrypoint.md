## The standalone receipt verifier works when run as a script

`verify_cli/bernstein_verify_receipt/verify.py` passed its `main` function to `sys.exit` instead of calling it, so anyone verifying a receipt with `python verify.py --receipt <path>` — the path taken by someone who has an evidence bundle but not the wheel — got `<function main at 0x…>` on stderr and exit code 1 without any receipt being checked. The script entry point now calls `main()`, so running it verifies the receipt and exits 0 or 1 on the actual result. (#5054)
