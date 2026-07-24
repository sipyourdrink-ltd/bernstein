/*
 * krunlaunch - one process, one microVM.
 *
 * `LibkrunMonitor` (src/bernstein/core/sandbox/backends/_libkrun.py) spawns
 * this binary once per `exec()`. It exists as a separate executable for two
 * independent reasons, either of which alone would force it:
 *
 *   1. krun_start_enter() never returns. It hands the calling process to the
 *      VMM, which exit()s with the workload's status. A VM therefore *is* a
 *      process; the orchestrator cannot host one in-process.
 *   2. On macOS, hv_vm_create() fails with EINVAL unless the running
 *      executable image carries com.apple.security.hypervisor. A Homebrew /
 *      python.org CPython re-execs into a framework binary that cannot
 *      usefully carry it, so the entitled image has to be our own.
 *
 * Build + sign: see `bernstein.core.sandbox.backends._libkrun.build_launcher`,
 * or `docs/operations/microvm-libkrun.md`. The link line must carry an rpath
 * to the libkrun prefix: code signing strips DYLD_* from the environment, so a
 * signed binary that relies on DYLD_LIBRARY_PATH fails to resolve libkrunfw.
 *
 * usage: krunlaunch <rootfs_dir> <guest_workdir> <guest_exec> [args...]
 *
 * environment (read from the host process, never forwarded to the guest):
 *   KRUNLAUNCH_VCPUS        guest vCPUs                       (default 1)
 *   KRUNLAUNCH_RAM_MIB      guest RAM in MiB                  (default 512)
 *   KRUNLAUNCH_SHM_BYTES    virtio-fs DAX window, bytes       (default 512 MiB)
 *   KRUNLAUNCH_LOG_LEVEL    libkrun log level                 (default: unset)
 *   KRUNLAUNCH_CONSOLE      file to receive the guest console (default: stdout)
 *   KRUNLAUNCH_SHARE_<N>    extra virtio-fs share, "tag=/host/path", N from 0
 *
 * exit codes: 125 for any libkrun-level failure, mirroring libkrun's own
 * reserved range. The guest's real status is reported out of band by the
 * monitor's wrapper script, because a guest command may legitimately return
 * 125/126/127 itself.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern int32_t krun_set_log_level(uint32_t level);
extern int32_t krun_create_ctx(void);
extern int32_t krun_set_vm_config(uint32_t ctx_id, uint8_t num_vcpus, uint32_t ram_mib);
extern int32_t krun_add_virtiofs3(uint32_t ctx_id, const char *tag, const char *path,
                                  uint64_t shm_size, bool read_only);
extern int32_t krun_add_virtiofs4(uint32_t ctx_id, const char *tag, const char *path,
                                  uint64_t shm_size, bool read_only, uint32_t semantics);
extern int32_t krun_set_workdir(uint32_t ctx_id, const char *workdir);
extern int32_t krun_set_console_output(uint32_t ctx_id, const char *filepath);
extern int32_t krun_set_exec(uint32_t ctx_id, const char *exec_path,
                             const char *const argv[], const char *const envp[]);
extern int32_t krun_start_enter(uint32_t ctx_id);

/* Reserved tag: libkrun mounts this share as the guest root. */
#define KRUN_FS_ROOT_TAG "/dev/root"

/* Store permission bits in the host inode instead of an extended attribute.
 * The workspace share is snapshotted by reading it from the host, so the two
 * views have to agree: under the default semantics a file the guest creates
 * 0644 lands on the host 0600, and freezing it would silently drop the mode
 * (an executable the guest built would restore non-executable). */
#define KRUN_SEMANTICS_LINUX_SIMPLIFIED 1

/* libkrun-level failure. Distinct from the guest's status, which the monitor
 * reads from the status file the wrapper writes into the control share. */
#define RC_VMM_FAILURE 125

#define MAX_EXTRA_SHARES 8

static int fail(const char *what, int32_t rc) {
    fprintf(stderr, "krunlaunch: %s failed rc=%d\n", what, rc);
    return RC_VMM_FAILURE;
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <rootfs> <workdir> <exec> [args...]\n", argv[0]);
        return RC_VMM_FAILURE;
    }
    const char *rootfs = argv[1];
    const char *workdir = argv[2];
    const char *exec_path = argv[3];

    const char *vcpus_s = getenv("KRUNLAUNCH_VCPUS");
    const char *ram_s = getenv("KRUNLAUNCH_RAM_MIB");
    const char *shm_s = getenv("KRUNLAUNCH_SHM_BYTES");
    const char *log_s = getenv("KRUNLAUNCH_LOG_LEVEL");
    const char *console = getenv("KRUNLAUNCH_CONSOLE");
    uint8_t vcpus = vcpus_s ? (uint8_t)atoi(vcpus_s) : 1;
    uint32_t ram = ram_s ? (uint32_t)strtoul(ram_s, NULL, 10) : 512;
    uint64_t shm = shm_s ? strtoull(shm_s, NULL, 10) : (1ULL << 29);

    if (log_s) krun_set_log_level((uint32_t)atoi(log_s));

    int32_t ctx = krun_create_ctx();
    if (ctx < 0) return fail("krun_create_ctx", ctx);

    int32_t rc;
    if ((rc = krun_set_vm_config((uint32_t)ctx, vcpus, ram)) < 0)
        return fail("krun_set_vm_config", rc);

    /* The root share. krun_set_root() would also work, but virtiofs3 is what
     * exposes the DAX window size, and an oversized window is rejected by
     * Hypervisor.framework. */
    if ((rc = krun_add_virtiofs3((uint32_t)ctx, KRUN_FS_ROOT_TAG, rootfs, shm, false)) < 0)
        return fail("krun_add_virtiofs3(root)", rc);

    /* Extra shares. The workspace and the control directory arrive this way so
     * neither has to live inside the guest root filesystem: the workspace is
     * exactly the tree freeze_image() canonicalises, and the control directory
     * (stdio + status) must never appear in a snapshot. */
    for (int n = 0; n < MAX_EXTRA_SHARES; n++) {
        char key[32];
        snprintf(key, sizeof(key), "KRUNLAUNCH_SHARE_%d", n);
        const char *spec = getenv(key);
        if (!spec) break;
        const char *sep = strchr(spec, '=');
        if (!sep || sep == spec || sep[1] == '\0') {
            fprintf(stderr, "krunlaunch: %s must be \"tag=/host/path\"\n", key);
            return RC_VMM_FAILURE;
        }
        size_t taglen = (size_t)(sep - spec);
        char *tag = strndup(spec, taglen);
        if (!tag) return RC_VMM_FAILURE;
        rc = krun_add_virtiofs4((uint32_t)ctx, tag, sep + 1, shm, false,
                                KRUN_SEMANTICS_LINUX_SIMPLIFIED);
        free(tag);
        if (rc < 0) return fail("krun_add_virtiofs4(share)", rc);
    }

    if ((rc = krun_set_workdir((uint32_t)ctx, workdir)) < 0)
        return fail("krun_set_workdir", rc);

    /* Kernel boot chatter shares the console with the guest's own writes to
     * /dev/console. Diverting it to a file keeps this process's stdout clean
     * and gives the monitor something to quote when a boot fails. */
    if (console && (rc = krun_set_console_output((uint32_t)ctx, console)) < 0)
        return fail("krun_set_console_output", rc);

    /* Guest argv, excluding argv[0]: libkrun derives that from exec_path. */
    int guest_argc = argc - 4;
    const char **gargv = calloc((size_t)guest_argc + 1, sizeof(char *));
    if (!gargv) return RC_VMM_FAILURE;
    for (int i = 0; i < guest_argc; i++) gargv[i] = argv[4 + i];
    gargv[guest_argc] = NULL;

    /* An explicit, minimal environment is REQUIRED. Passing NULL makes libkrun
     * collect the *host* environment and fold it into the guest kernel command
     * line, which overruns CMDLINE_MAX_SIZE and aborts the VMM (SIGABRT, not an
     * error return). The session's real environment is not passed here either,
     * for the same size reason: the monitor writes it into the control share
     * and the wrapper script sources it. */
    const char *genvp[] = {
        "PATH=/bin:/sbin:/usr/bin:/usr/sbin",
        "HOME=/root",
        "TERM=dumb",
        NULL,
    };

    if ((rc = krun_set_exec((uint32_t)ctx, exec_path, gargv, genvp)) < 0)
        return fail("krun_set_exec", rc);

    krun_start_enter((uint32_t)ctx);
    /* Unreachable unless the VMM refused to start. */
    fprintf(stderr, "krunlaunch: krun_start_enter returned\n");
    return RC_VMM_FAILURE;
}
