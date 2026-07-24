# Running the microVM backend on libkrun

The `microvm` sandbox backend isolates agent work behind a real hardware
boundary — its own kernel, process tree and network stack. The libkrun adapter
implements that boundary on ordinary developer hardware: KVM on Linux,
Hypervisor.framework on macOS/Apple Silicon. No nested virtualisation is
involved, so it works on machines where Firecracker cannot run at all.

Architecture and design rationale live in
[`docs/architecture/sandbox.md`](../architecture/sandbox.md). This page is the
host setup.

## TL;DR

| Step | Command | Status when done |
|---|---|---|
| 1. Install libkrun + libkrunfw | `brew tap slp/krun && brew install libkrun` (macOS) / distro package (Linux) | ✅ hypervisor + guest kernel present |
| 2. Build the launcher | `uv run bernstein sandbox microvm-launcher` | ✅ signed `krunlaunch` in the build cache |
| 3. Provide a guest rootfs | export `BERNSTEIN_MICROVM_LIBKRUN_ROOTFS=/path/to/rootfs` | ✅ guest can run `/bin/sh` |
| 4. Select the adapter | export `BERNSTEIN_MICROVM_MONITOR=libkrun` | ✅ `microvm` backend uses libkrun |

Check the result at any point — it lists everything still missing and creates
nothing:

```bash
uv run python -c "
from bernstein.core.sandbox.backends._libkrun import LibkrunMonitor
print(LibkrunMonitor().preflight() or 'host ready')"
```

## 1. Install libkrun and libkrunfw

`libkrunfw` carries the guest kernel and is `dlopen`ed by libkrun when the VM
starts, so both must be present. A host with libkrun but no libkrunfw is
reported as a missing precondition rather than failing later with a loader
error.

**macOS (Apple Silicon only):**

```bash
brew tap slp/krun
brew install libkrun
```

**Linux:** install your distribution's `libkrun` and `libkrunfw` packages, and
make sure `/dev/kvm` is readable and writable by the user running Bernstein
(usually membership of the `kvm` group).

Override discovery with `BERNSTEIN_MICROVM_LIBKRUN_LIB` if the library is not in
a standard location.

## 2. Build the launcher

```bash
uv run bernstein sandbox microvm-launcher
```

This compiles a small C program, `krunlaunch`, against the locally installed
libkrun and caches it (by default under `~/.cache/bernstein/libkrun/`). Pass
`--output` to put it elsewhere, and point
`BERNSTEIN_MICROVM_LIBKRUN_LAUNCHER` at it.

The launcher is a separate process for two independent reasons, either of which
alone would force it:

- `krun_start_enter()` never returns — it hands the calling process to the VMM,
  which exits with the workload's status. A VM *is* a process, so the
  orchestrator cannot host one in-process.
- On macOS, `hv_vm_create()` fails unless the running executable image carries
  `com.apple.security.hypervisor`. A framework CPython re-execs into a binary
  that cannot usefully carry it, so the entitled image has to be one the project
  builds and signs itself.

On macOS the build ad-hoc code-signs the binary with
`com.apple.security.hypervisor` and
`com.apple.security.cs.disable-library-validation`. Rebuild it after upgrading
libkrun. Until it exists, `preflight()` reports it as a missing host
precondition — exactly like a missing hypervisor. It is never built implicitly.

## 3. Provide a guest root filesystem

Point `BERNSTEIN_MICROVM_LIBKRUN_ROOTFS` at a **host directory** holding a Linux
root filesystem for the guest architecture. libkrun exposes it to the guest as
`/` over virtio-fs.

It must provide:

- `/bin/sh` — a POSIX shell. The monitor runs each command through a small
  wrapper script.
- `mount` supporting `-t virtiofs` — the wrapper mounts the workspace and
  control shares itself.

Any minimal userland with those two (busybox-based images are the usual choice)
is enough. Extract an OCI image for your guest architecture into a directory, or
build a rootfs with your distribution's tooling.

Use a directory dedicated to sandboxing: the guest root share is writable, so a
guest can modify the rootfs directory it was given.

Optional sizing knobs:

| Variable | Default | Meaning |
|---|---|---|
| `BERNSTEIN_MICROVM_LIBKRUN_VCPUS` | `1` | guest vCPUs |
| `BERNSTEIN_MICROVM_LIBKRUN_RAM_MIB` | `512` | guest RAM, MiB |
| `BERNSTEIN_MICROVM_LIBKRUN_SHM_BYTES` | 512 MiB | virtio-fs DAX window per share |

## 4. Select the adapter

Selection is opt-in at two levels, and neither changes the backend heuristic:

```bash
export BERNSTEIN_MICROVM_MONITOR=libkrun   # microvm backend -> libkrun adapter
```

then request the `microvm` backend explicitly, either in `plan.yaml`:

```yaml
sandbox:
  backend: microvm
```

or through the sandbox CLI. Without `BERNSTEIN_MICROVM_MONITOR=libkrun` the
`microvm` backend keeps its historical Firecracker adapter, which still refuses
to boot.

## Verifying the setup

```bash
BERNSTEIN_MICROVM_LIBKRUN_INTEGRATION=1 \
  uv run pytest tests/integration/sandbox/test_microvm_libkrun.py -v
```

The opt-in tests boot real guests and exercise exec, stdin, file I/O and the
canonical freeze. They skip themselves when `preflight()` still reports
anything missing. The refusal-invariant assertions in the same file run
everywhere, with or without a hypervisor.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `libkrun shared library not found` | libkrun not installed, or in a non-standard prefix | install it, or set `BERNSTEIN_MICROVM_LIBKRUN_LIB` |
| `libkrunfw shared library not found` | guest kernel package missing | install `libkrunfw` |
| `launcher binary missing at ...` | step 2 not run | `uv run bernstein sandbox microvm-launcher` |
| `launcher ... is not code-signed with com.apple.security.hypervisor` | signing failed, or the binary was copied without its signature | rebuild it in place |
| `Building the microVM failed: Internal(Vm(VmSetup(VmCreate)))` | the process calling libkrun is not entitled | you are not going through `krunlaunch`; rebuild it |
| `libkrun "init" could not find the guest entrypoint` (127) | the rootfs has no `/bin/sh` | fix the rootfs |
| `libkrun "init" found the guest entrypoint but could not execute it` (126) | `/bin/sh` is not executable, or is built for the wrong architecture | fix the rootfs |
| `guest produced no status report` | the VM died before the command finished | check the quoted diagnostics in the error; they carry the VMM's own message |

A guest command that *itself* exits 125, 126 or 127 is reported as an ordinary
result with that exit code, never as a VM failure. The two cases are
distinguished by a status file the guest wrapper writes only after the command
completes, so the process exit code is never used to guess.

## What the boundary does and does not cover

**Does:** a separate guest kernel, a separate process tree, a separate network
stack. Guest code cannot see host processes, or host files outside the shared
directories.

**Does not:** the guest and the VMM share a security context, so a hypervisor
escape lands with the privileges of the process that spawned the launcher.
virtio-fs is a passthrough, not a sandbox — it does not constrain access within
a shared directory beyond ordinary filesystem permissions, and the guest root
share is writable. Confining the VMM process itself (user namespaces, seccomp,
or a dedicated uid on Linux) remains an operator responsibility, as it is for
any hypervisor.
