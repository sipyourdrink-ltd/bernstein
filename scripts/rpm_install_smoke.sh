#!/usr/bin/env bash
#
# Install smoke for the RPM channel (issues #3558, #3559).
#
# Builds the RPM the way Copr builds it - from the SRPM this repository
# renders - installs it in a container of the target chroot family, and
# asserts the two things the package promises:
#
#   1. `bernstein --version` prints the version the RPM metadata claims.
#   2. It does so with no network access at all.
#
# Both assertions fail against a package that resolves itself at run time,
# which is the defect #3558 describes.
#
# The Python channels are gated by `Install smoke - pipx` / `Install smoke -
# uv tool` in ci.yml. This is the RPM channel's equivalent, and CI calls this
# same script so a local reproduction and a CI failure are the same run.
#
# Usage:
#   scripts/rpm_install_smoke.sh <container-image> <version>
#
# Example:
#   scripts/rpm_install_smoke.sh fedora:43 3.14.159
#
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <container-image> <version>" >&2
    exit 2
fi

IMAGE="$1"
VERSION="$2"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The version reaches this script from a release tag, a dispatch input, or a
# PyPI query, and is handed to a process running as root inside the build
# container. Constrain it to the release grammar before it goes anywhere: a
# value carrying shell metacharacters must be rejected here, not quoted around
# later.
if ! printf '%s' "${VERSION}" | grep -qE '^[0-9]+(\.[0-9]+)*([-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?[0-9]*)?([-_.]?(post|rev|r)[-_.]?[0-9]*)?([-_.]?dev[-_.]?[0-9]*)?$'; then
    echo "::error::refusing to smoke a version that is not a release version: ${VERSION}" >&2
    exit 2
fi

# Unique per invocation so matrix cells can run in parallel without racing on
# the container name or the committed image tag.
# `printf`, not `echo`: a trailing newline would become a trailing `-`, which
# docker rejects as an invalid tag.
TAG="bernstein-rpm-smoke-$$-$(printf '%s' "${IMAGE}" | tr -C 'a-zA-Z0-9' '-' | tr 'A-Z' 'a-z')"
WORK="$(mktemp -d)"
CONTAINER="${TAG}-install"

cleanup() {
    # Preserve the status the script is exiting with: teardown must never turn
    # a passing smoke into a failure, nor a failing one into a pass.
    local rc=$?
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
    docker rmi -f "${TAG}" >/dev/null 2>&1 || true
    if [ -d "${WORK}" ]; then
        # rpmbuild and dnf run as root inside the container, so the build
        # artefacts in the bind-mounted work dir are root-owned. On a Linux
        # runner the invoking user cannot delete them (on Docker Desktop the
        # VM's uid mapping hides this). Hand ownership back from inside a
        # container before removing the tree.
        docker run --rm -v "${WORK}:/work" "${IMAGE}" \
            chown -R "$(id -u):$(id -g)" /work >/dev/null 2>&1 || true
        rm -rf "${WORK}" || true
    fi
    exit "${rc}"
}
trap cleanup EXIT

echo "::group::[${IMAGE}] build the RPM from the rendered SRPM"
# rpm-build and a python3 for the renderer; the spec pulls its own interpreter
# via BuildRequires. `--rebuild` is what turns the SRPM Copr would receive into
# the binary RPM a user would install, so the smoke tests the real artefact.
# The inner script is a quoted heredoc, so nothing in it is expanded by this
# shell: the version crosses the boundary as an environment variable and is
# never spliced into the source the container executes.
docker run --rm -i \
    -v "${REPO_ROOT}:/src:ro" \
    -v "${WORK}:/out" \
    -e "SMOKE_VERSION=${VERSION}" \
    "${IMAGE}" \
    bash -euo pipefail -s <<'INNER'
dnf -y install rpm-build python3 "dnf-command(builddep)" >/dev/null

# The renderer needs the same Python floor the project targets, but EPEL 9's
# `python3` is 3.9. In the real chain the SRPM is rendered on the runner and
# only rebuilt in the chroot, so pulling a modern interpreter just for that
# step keeps this faithful to Copr.
PY=python3
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    dnf -y install python3.12 >/dev/null
    PY=python3.12
fi

cd /src
SRPM="$("${PY}" scripts/build_copr_srpm.py --version "${SMOKE_VERSION}" --outdir /out/srpm)"
echo "rendered ${SRPM} (using ${PY})"
dnf -y builddep "${SRPM}" >/dev/null
rpmbuild --rebuild \
    --define "_topdir /out/rb" \
    --define "_rpmdir /out/rpms" \
    "${SRPM}"
INNER
echo "::endgroup::"

RPM_PATH="$(find "${WORK}/rpms" -name '*.rpm' -not -name '*.src.rpm' | head -1)"
if [ -z "${RPM_PATH}" ]; then
    echo "::error::[${IMAGE}] no binary RPM was produced"
    exit 1
fi
echo "[${IMAGE}] built $(basename "${RPM_PATH}") ($(du -h "${RPM_PATH}" | cut -f1))"

echo "::group::[${IMAGE}] install the RPM"
# Network is allowed here: installing may pull the interpreter the package
# requires. The offline assertion below is about *run* time.
docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
RPM_IN_CONTAINER="/rpms/$(basename "$(dirname "${RPM_PATH}")")/$(basename "${RPM_PATH}")"
if ! docker run --name "${CONTAINER}" \
    -v "${WORK}/rpms:/rpms:ro" \
    -e "SMOKE_RPM=${RPM_IN_CONTAINER}" \
    "${IMAGE}" \
    bash -euo pipefail -c 'dnf -y install "${SMOKE_RPM}"; rpm -q bernstein'; then
    echo "::error::[${IMAGE}] the RPM does not install; dnf output above is the evidence"
    exit 1
fi
docker commit "${CONTAINER}" "${TAG}" >/dev/null
echo "::endgroup::"

# ── Assertion 1+2: the claimed version runs, with the network removed ────────
#
# `--network none` gives the container no interfaces at all, so anything that
# tries to resolve itself from an index here fails rather than quietly
# succeeding on a well-connected runner.
echo "::group::[${IMAGE}] run offline (--network none)"
set +e
# stdout only: a warning on stderr must not be able to satisfy the version
# assertion below.
OFFLINE_OUT="$(docker run --rm --network none "${TAG}" bernstein --version 2>/dev/null)"
OFFLINE_RC=$?
set -e
echo "exit=${OFFLINE_RC} output=${OFFLINE_OUT:-<empty>}"
echo "::endgroup::"

if [ "${OFFLINE_RC}" -ne 0 ]; then
    echo "::error::[${IMAGE}] 'bernstein --version' failed offline (exit ${OFFLINE_RC}): ${OFFLINE_OUT:-<no output>}"
    exit 1
fi

if [ -z "${OFFLINE_OUT}" ]; then
    # The wrapper this replaced exited 0 while printing nothing, having
    # exec'd into a pip install. An empty success is the failure mode.
    echo "::error::[${IMAGE}] 'bernstein --version' exited 0 but printed nothing; the package ran no program"
    exit 1
fi

if [ "$(printf '%s\n' "${OFFLINE_OUT}" | wc -l)" -ne 1 ]; then
    echo "::error::[${IMAGE}] 'bernstein --version' printed more than the version line: ${OFFLINE_OUT}"
    exit 1
fi

# The version the program reports must equal the version the RPM claims -
# compared as PEP 440, not as strings. A pre-release reaches this script in the
# spelling its tag used (3.15.0-rc1) while the program reports the normalised
# distribution metadata (3.15.0rc1), so a string comparison would reject a
# correctly built pre-release RPM and block the release. The comparison runs on
# the packaged interpreter, which carries `packaging` as one of the
# application's own dependencies, so nothing is needed on the host.
echo "::group::[${IMAGE}] packaged version equals the claimed version (PEP 440)"
set +e
VERIFY_OUT="$(docker run --rm -i --network none \
    -e "SMOKE_VERSION=${VERSION}" \
    "${TAG}" \
    bash -euo pipefail -s <<'VERIFY' 2>&1
VENV_BIN="$(dirname "$(readlink -f /usr/bin/bernstein)")"
# Validate what the symlink resolved to before running anything from it. An
# absent or wrongly pointed /usr/bin/bernstein would otherwise leave a bare
# `./python` to be executed, and the version assertion below could then pass
# against an interpreter that is not the packaged one.
case "${VENV_BIN}" in
    /usr/lib64/bernstein/bin | /usr/lib/bernstein/bin) ;;
    *)
        echo "/usr/bin/bernstein resolves to ${VENV_BIN}, not the packaged venv" >&2
        exit 1
        ;;
esac
if [ ! -x "${VENV_BIN}/python" ]; then
    echo "no executable interpreter at ${VENV_BIN}/python" >&2
    exit 1
fi
"${VENV_BIN}/python" - "${SMOKE_VERSION}" <<'PY'
import importlib.metadata as metadata
import sys

from packaging.version import Version

want = sys.argv[1]
got = metadata.version("bernstein")
if Version(got) != Version(want):
    raise SystemExit(f"packaged {got} but the RPM claims {want}")
print(got)
PY
VERIFY
)"
VERIFY_RC=$?
set -e
echo "exit=${VERIFY_RC} packaged=${VERIFY_OUT:-<empty>}"
echo "::endgroup::"

if [ "${VERIFY_RC}" -ne 0 ]; then
    echo "::error::[${IMAGE}] packaged version does not match the RPM's claim: ${VERIFY_OUT:-<no output>}"
    exit 1
fi

# ── Assertion 3: the CLI is actually loadable, not just version-stamped ──────
echo "::group::[${IMAGE}] bernstein --help offline"
if ! docker run --rm --network none "${TAG}" bernstein --help >/dev/null 2>&1; then
    echo "::error::[${IMAGE}] 'bernstein --help' failed offline; the packaged CLI does not load"
    exit 1
fi
echo "::endgroup::"

echo "[${IMAGE}] OK - installed RPM reports ${VERSION} with no network"
