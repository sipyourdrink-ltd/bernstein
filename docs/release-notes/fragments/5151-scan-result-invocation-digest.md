## Scan results record the invocation that produced them

`ScanResult` now carries the invocation digest its adapter already computed,
populated by the Nmap, Trivy and Gitleaks adapters. The digest previously
lived only on the adapter instance, so a stored result could not say which
scan produced it -- and Nmap builds its transcript from the hosts and ports it
found, never the ones requested, so two empty scans of different targets were
byte-identical. The scanner conformance harness checks the digest separately
from the transcript, so a replay no longer passes on a transcript that matches
for the wrong reason. (#5151)
