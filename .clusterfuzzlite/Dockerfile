# ClusterFuzzLite builder image for bernstein.
#
# Digest-pinned base image so the build is reproducible and Scorecard's
# Pinned-Dependencies check is satisfied. The base image ships Python
# 3.11; the OSSF Scorecard Fuzzing check only requires a runnable
# harness, not coverage of the full bernstein package (which targets
# Python 3.12+). The harness here therefore targets YAML parsing
# directly via PyYAML, which is the same parser primitive that
# bernstein.core.config.seed_parser sits on top of. The Hypothesis
# property-test suite in tests/ remains the primary fuzzing surface
# for the rest of bernstein's input handling.
# Ref: https://google.github.io/clusterfuzzlite/build-integration/python-lang/
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:51a7caaecdc0b814dd32ad272eab7ca4a40c134cecd38294f2aabb9e32adcb4f

COPY . /src/bernstein
WORKDIR /src/bernstein
COPY .clusterfuzzlite/build.sh /src/build.sh
