## DLP gate configurable from the seed

`quality_gates.dlp_scan` and the nine other `dlp_*` keys documented in `docs/architecture/quality-pipeline.md` are now read by the seed parser. Before this they were accepted by the schema and silently dropped, so the DLP gate could not be disabled or tuned from `bernstein.yaml`. A reachability test now pins every `QualityGatesConfig` field against the parser's keyword list, with the fields still unwired named in an explicit allowlist so the next one cannot sit silent (#6615).
