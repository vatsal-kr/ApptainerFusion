# Apptainer migration

SandboxFusion was originally built to run as a Docker container, using Docker
both to **host the server** and (in `full` isolation mode) to **spawn a fresh
sibling container per code execution**. On HPC systems Docker is typically
unavailable: there is no Docker daemon, users are unprivileged, and the only
supported container runtime is [Apptainer](https://apptainer.org/) (formerly
Singularity) running rootless via `--fakeroot`.

This document describes the changes made to run SandboxFusion under Apptainer:
the new **`bindroot` isolation mode** that replaces overlay-based `lite` mode,
why it was necessary, what it does, and the fixes required to make the full
test suite pass under Apptainer at parity with Docker.

---

## 1. Background: the three isolation modes

`RunConfig.sandbox.isolation` (`sandbox/configs/run_config.py`) now accepts
three values:

| Mode | Mechanism | Per-exec isolation | Runtime requirement |
|------|-----------|--------------------|---------------------|
| `full` | `docker run --rm` of a fresh container per execution | container (memory/cpu cgroups, `--network none`, `--pids-limit`) | Docker daemon + `/var/run/docker.sock` |
| `lite` | overlayfs (host `/` as lower, tmpfs upper) + `chroot` + cgroup + netns + `unshare --pid` | overlay CoW root, cgroup limits, network namespace | privileged container, overlay-on-rootfs allowed |
| `bindroot` | recursive bind-mount of `/` + per-exec tmpfs scratch + `chroot`, all inside a nested `unshare -U -m -r` | unshared mount + user namespace, unique cwd | **Apptainer-compatible** — no Docker, no host root |

`full` and `lite` are the original Docker modes. `bindroot` is the new mode for
Apptainer. The test harness selects them via `--sandbox-backend
{docker,apptainer}` and `--sandbox-mode {full,lite,bindroot}`
(`conftest.py`); the matching server config files are `docker_full.yaml`,
`docker_lite.yaml`, and the new `docker_bindroot.yaml`.

---

## 2. Why `bindroot` was necessary

`lite` mode builds the sandbox filesystem with **overlayfs**, using the host
root `/` as the read-only lower layer and a tmpfs upper layer so all writes are
ephemeral:

```
mount -t overlay overlay -o lowerdir=/,upperdir=...,workdir=... merged/
```

This **fails inside Apptainer**. Apptainer's own `/` is *already* an overlay
(the SIF's squashfs with a session tmpfs on top) whose child mounts are locked
(`/etc/hosts`, `/etc/resolv.conf`, `/.singularity.d/*`, `/dev/*`). The kernel
refuses to stack a second overlay whose lower layer is such a tree:

```
overlayfs: failed to clone lowerpath
```

So a different filesystem-isolation strategy was required for Apptainer.
`bindroot` provides the same *lite-equivalent* semantics (ephemeral, isolated
root; fast per-exec setup) without overlayfs.

A second Apptainer constraint shaped the design: on HPC hosts the user is
usually **not listed in `/etc/subuid`**, so Apptainer's `--fakeroot` falls back
to an `LD_PRELOAD`-based fakeroot rather than a real uid mapping. Under that
fallback the kernel sees the *real* uid for `mount(2)` syscalls and rejects them
with `EPERM`, regardless of fakeroot's `uid=0` illusion. The workaround is to
do the mounts inside a **nested `unshare -U -m -r`**, which gives the process
real `CAP_SYS_ADMIN` in a fresh user + mount namespace.

---

## 3. What `bindroot` does

Implementation lives in `sandbox/runners/isolation.py`
(`build_bindroot_wrapper`, `tmp_bindroot`) and `sandbox/runners/base.py`
(`run_commands`, the `isolation == 'bindroot'` branch).

For each code execution the server:

1. Allocates an empty scratch base dir on the host
   (`/tmp/bindroot_<rand>/merged`) via `tmp_bindroot()`.
2. Builds a bash "wrapper" snippet and runs the whole thing as:

   ```
   unshare -U -m -r bash -c '<wrapper>' _ bash -c '<inner cmd>'
   ```

   `unshare -U -m -r` creates a fresh **user + mount namespace** with the caller
   mapped to `uid=0` (real `CAP_SYS_ADMIN` inside), so the mounts below are
   permitted even without `/etc/subuid` entries.

3. The wrapper (`build_bindroot_wrapper`) sets up the chroot:

   ```
   mount --make-rprivate /                 # detach from host shared propagation
   mount --rbind / merged/                 # recursive bind of '/' as the new root
   mount --make-rprivate merged/
   mount -t tmpfs tmpfs merged/tmp         # ephemeral, isolated /tmp
   mount -t tmpfs tmpfs merged/var/tmp     # (+ /run, /root/.cache)
   ...
   mount --rbind <cwd> merged/<cwd>        # re-expose the host cwd through the tmpfs
   exec chroot merged "$@"                 # run the user command
   ```

   Because all of this lives in the unshared mount namespace, the entire mount
   tree **vanishes automatically when the namespace dies** — there is nothing to
   unmount on the happy path. `tmp_bindroot`'s cleanup only sweeps stray mounts
   left by an aborted setup, then `rmtree`s the base dir.

4. `restore_files` stages input files into the host cwd before exec; because the
   cwd is `--rbind`'d into the chroot at the same absolute path, writes made
   inside the chroot land directly on the host path, so `fetch_files` works
   without copying back out.

### What `bindroot` deliberately omits vs `lite`

- **No cgroup memory/cpu limits.** The cgroup tree is root-owned and not
  delegated to the unprivileged user on HPC hosts, so `tmp_cgroup` is skipped.
- **No persistent network namespace.** `/run/netns` sits on a read-only
  filesystem under Apptainer, so `tmp_netns` is skipped. (The recursive bind of
  `/` still gives the sandbox a working resolver via the host's `/etc/*`.)

Per-exec isolation therefore comes purely from the unshared filesystem + unique
cwd path, not from resource cgroups. This lack of resource isolation is the
source of several of the test issues in §5 (under load, neighbours that Docker
would have capped instead contend freely).

---

## 4. Launch / harness changes

- **`Makefile`**: new `pull-apptainer-images`, `start-apptainer-container`, and
  `test-apptainer-bindroot` targets. The standalone launcher binds the host
  `sandbox/` over the SIF's copy (`-B $(CURDIR)/sandbox:/root/sandbox/sandbox`)
  so local code edits are picked up without rebuilding the SIF.
- **`conftest.py`**: `--sandbox-backend {docker,apptainer}` and `--sandbox-mode
  {full,lite,bindroot}` options; an Apptainer server launcher
  (`_start_apptainer_server`) that resolves the SIF from `$SANDBOX_APPTAINER_SIF`
  or `$WORK`, starts it with `apptainer run`, and waits on `/v1/ping`.
- **`docker_bindroot.yaml`**: server config selecting `isolation: bindroot`.

> **Testing gotcha:** killing the `apptainer run` launcher pid does **not** kill
> the `uvicorn` server child — a stale server keeps binding the port and
> silently answers `/v1/ping`, so a "restart" can quietly keep serving old code.
> Kill by listener instead: `ss -ltnp | grep :<port>` → kill that uvicorn pid.

---

## 5. Fixes required to pass the test suite under Apptainer

`make test-apptainer-bindroot` initially had 11 failures that did not occur
under Docker. Four independent root causes; all now green (212 passed,
2 skipped).

### 5.1 Host conda environment leaked into the SIF — `--cleanenv`

Apptainer **inherits the launching shell's environment by default**. When the
launching shell has an active conda environment, its compiler-toolchain
variables leak into the container and then into every sandboxed compile:

```
CC = CXX = CPP = LD = AS = AR = x86_64-conda-linux-gnu-*
CONDA_PREFIX = /…/micromamba/envs/<env>
```

Go's cgo, D's `dmd`, and Swift's linker read `CC`/`CXX`/`LD` and try to invoke
`x86_64-conda-linux-gnu-cc`, which exists only in the host conda env, **not in
the SIF** → compile failures (`cgo: C compiler … not found`, `linker exited with
status …`). Docker starts from a clean environment and is unaffected.

**Fix:** add `--cleanenv` to the Apptainer invocations (`conftest.py`
`_start_apptainer_server` and the Makefile `start-apptainer-container`). `PORT`
and `SANDBOX_CONFIG` are re-injected explicitly via `--env`. This makes
Apptainer match Docker's clean-env behaviour and fixed ~6 tests (go_test,
D_ut, swift).

### 5.2 cpp/swift `.o` corruption under concurrency — host-backed `TMPDIR`

Under heavy mixed-language load, ~4% of C++/Swift compiles failed with:

```
ld: cannot find /tmp/ccXXXX.o: file format not recognized
collect2: error: ld returned 1 exit status
```

The assembler writes its intermediate object to the chroot's **tmpfs `/tmp`**,
and the linker then reads it back empty/corrupt. This is *not* OOM (the box has
512 GB and there were no OOM kills) — the chroot's per-exec tmpfs `/tmp`
intermittently loses files written to it mid-build when many bindroot sandboxes
churn mounts concurrently. Docker is immune because each execution is a separate
container with its own stable `/tmp`. It does **not** reproduce with standalone
`unshare` loops — only the running server under mixed-language load triggers it.

**Fix:** give compilers a **host-backed (xfs) `TMPDIR`** instead of the racy
tmpfs:

- `sandbox/runners/isolation.py`: new constant `OBJ_TMPDIR = '/tmp/.sandbox_obj'`;
  `build_bindroot_wrapper` bind-mounts a per-exec host directory
  (`<base_dir>/tmproot`) onto `OBJ_TMPDIR` inside the chroot. `/tmp` itself stays
  a tmpfs (see §5.4 for why).
- `sandbox/runners/base.py`: the bindroot `_build_cmd` exports
  `TMPDIR=$OBJ_TMPDIR` for the compile and run commands.

Verified at 160/160 cpp and 40/40 swift under heavy concurrent mixed load.

### 5.3 Test timeouts too tight for bindroot

With no cgroup CPU isolation (§3), compiles contend for CPU under the 16-worker
test load, so a few timeout-oriented tests that assume a fast compile became
flaky:

- `test_lean_error` used the **default 10 s** run timeout while its sibling
  `test_lean_pass` used 30 s for the same Mathlib import → bumped to 30 s.
- `test_rust_timeout` used `compile_timeout=1` (rustc can't reliably finish in
  1 s under load) → bumped to 20 s.

Both tests verify **run-timeout** behaviour; the compile budget was incidentally
too tight. (`sandbox/tests/runners/test_lean.py`, `test_rust.py`.)

### 5.4 Constraint discovered while fixing 5.2 — `go test` and `os.TempDir()`

`go test` **ignores a `go.mod` when its module root equals `os.TempDir()`**
(Go source `cmd/go/internal/modload/init.go`:
`search.InDir(modRoot, os.TempDir()) == "."`). The bindroot working directory is
under `/tmp`, so:

- Pointing `TMPDIR` at the cwd makes `os.TempDir() == modRoot` → go ignores the
  staged `go.mod` → `no required module provides package …`.
- Making `/tmp` *itself* a host bind (instead of tmpfs) also breaks `go test` —
  the tmpfs↔xfs device boundary at `/tmp` is what keeps go from treating the cwd
  as a throwaway temp module.

This is why the §5.2 fix **keeps `/tmp` a tmpfs** and points `TMPDIR` at a
*separate* host-backed directory (`/tmp/.sandbox_obj`) that is **not** the cwd.
With that arrangement, cpp/swift get stable compiler temps **and** `go test`
keeps working (40/40 under load).

### Defensive: `mount --make-rprivate /`

The wrapper makes the namespace's mount tree private before laying down per-exec
mounts. On Apptainer this is a no-op (its `/tmp` is already private), but it is
correct hygiene for hosts where `/` is `rshared` (e.g. systemd defaults under a
privileged docker `bindroot` run), preventing a concurrent exec's mount from
propagating in and shadowing this sandbox's `/tmp` mid-build.

---

## 6. Summary of changed files

| File | Change |
|------|--------|
| `sandbox/configs/run_config.py` | add `bindroot` to the `isolation` literal |
| `sandbox/configs/docker_bindroot.yaml` | new server config (`isolation: bindroot`) |
| `sandbox/runners/isolation.py` | `tmp_bindroot`, `build_bindroot_wrapper`, `OBJ_TMPDIR` host-backed compiler scratch, `--make-rprivate` hygiene, orphan/signal cleanup for bindroot dirs |
| `sandbox/runners/base.py` | `run_commands` bindroot branch; `_build_cmd` exports `TMPDIR=$OBJ_TMPDIR` |
| `conftest.py` | `--sandbox-backend`/`--sandbox-mode`; Apptainer server launcher; `--cleanenv` |
| `Makefile` | `pull-apptainer-images`, `start-apptainer-container`, `test-apptainer-bindroot`; `--cleanenv` |
| `sandbox/tests/runners/test_lean.py` | `test_lean_error` run timeout 10 → 30 |
| `sandbox/tests/runners/test_rust.py` | `test_rust_timeout` compile timeout 1 → 20 |
| `sandbox/tests/runners/test_python.py` | skip absolute-path fetch test under `bindroot` (only the cwd is exposed, like `full`) |
