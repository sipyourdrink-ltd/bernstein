# Reproducibility checklist

- Pin random seeds (`numpy`, `torch`, `random`, `PYTHONHASHSEED`).
- Record library versions (`uv pip freeze > artefact.txt`).
- Capture hardware (GPU model, CUDA, cuDNN) in the run metadata.
- Log the input data checksum, not the data.
- Save optimizer state + scheduler state with every checkpoint.
- Evaluation runs are their own commits — never "re-run to reproduce".

## Experiment tracking
- One run = one config file + one result file.
- Configs live in YAML under `experiments/`.
- Results go to `.sdd/metrics/ml/<run_id>.json`.
- Version configs; do not edit in place.
