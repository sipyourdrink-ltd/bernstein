## Nmap scans now produce transcript-anchored findings

Bernstein now includes an Nmap `TRANSCRIPT_ANCHORED` scanner adapter. It
normalizes volatile timestamps from Nmap XML while preserving meaningful scan
details, including service version and banner information, in the recorded
transcript. (#3618)
