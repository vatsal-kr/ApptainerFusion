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

### Resource and network isolation: unprivileged stand-ins for cgroups/netns

`lite` mode's resource isolation relies on primitives an unprivileged Apptainer
user does not have: the cgroup tree is root-owned and not delegated
(`tmp_cgroup` impossible), and `/run/netns` sits on a read-only filesystem
(`tmp_netns` impossible). The initial migration simply skipped both — sandboxed
code ran with unlimited memory/CPU and full network access, and the resulting
free-for-all contention caused several of the test flakes in §5. A later
hardening pass replaced each skipped layer with an unprivileged equivalent:

- **Memory:** `ulimit -v` sized from `memory_limit_MB` (×2 slack for virtual
  address space). Runtimes that reserve VAS far beyond any sane multiplier opt
  out via `run_commands(rlimit_as=False)` — tsx's WASM cages (typescript) and
  the .NET GC (csharp) — keeping only the `RLIMIT_CPU` backstop.
- **CPU:** `taskset` pinning onto per-exec core groups leased from the affinity
  mask (`tmp_cpuset`), plus `ulimit -t` as a runaway-spinner backstop.
  `max_concurrency` is sized to cores / `default_cpu_limit` (32 on a 64-core
  allocation at 2 cores/exec — see `docker_bindroot.yaml`).
- **Network:** `unshare -n` gives each exec a loopback-only namespace; the
  wrapper brings `lo` up so localhost servers still work. Egress is blocked by
  design (the egress test skips under bindroot; a companion test asserts the
  block actually holds).
- **PID:** `unshare -p --fork --mount-proc`, so no process survives the death
  of the namespace's init.

---

## 4. Launch / harness changes

- **`Makefile`**: new `pull-apptainer-images`, `start-apptainer-container`,
  `test-apptainer-bindroot`, and `test-rl-load` targets. The standalone launcher
  binds the host `sandbox/` over the SIF's copy
  (`-B $(CURDIR)/sandbox:/root/sandbox/sandbox`) so local code edits are picked
  up without rebuilding the SIF. It runs with
  `--cleanenv --fakeroot --ignore-fakeroot-command --no-home` (see §6.1 for why
  that exact flag combination) and `SANDBOX_LOG_LEVEL=OFF` (server log level is
  configurable via that env var; default `INFO`).
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
under Docker. Four independent root causes; all now green (213 passed,
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

## 6. Production hardening under sustained RL load

The test suite passes in minutes; an RL training run hammers the server for
hours with ~48 concurrent streams of adversarial, model-generated code. Two
server-wedging bugs only surfaced under that regime — both manifested as the
training client's read timeouts ramping up over a few RL steps until every
reward was zero, while the server still answered `/v1/ping` (and even trivial
`/run_code` probes) normally.

### 6.1 `faked` daemon corruption — `--ignore-fakeroot-command`

`--fakeroot` is required: the SIF bakes every language toolchain's state into
`/root` (rustup/elan/go/dotnet caches), so the server must appear as uid 0 with
`HOME=/root` — without it, 18 language tests fail with "could not create home
directory". But on hosts without `/etc/subuid` entries, `--fakeroot` *also*
wraps the container in the `fakeroot` LD_PRELOAD tool, whose single `faked`
daemon serializes all metadata faking over one SysV IPC channel. Under
sustained concurrent process spawning the daemon corrupts its protocol
(`libfakeroot internal error: payload not recognized!`), after which **every
new process in the container hangs** — the whole server wedges.

**Fix:** add `--ignore-fakeroot-command` alongside `--fakeroot`. This keeps the
root-mapped user namespace (uid 0, `HOME=/root`) but skips the LD_PRELOAD
wrapper entirely. The flag is meaningless without `--fakeroot`; the two must be
used together. Applied in the Makefile launcher and the `conftest.py` test
fixture.

### 6.2 stdin-drain concurrency-slot leak

In `run_command_bare` (`sandbox/runners/base.py`), stdin was written to the
child with an unbounded `await p.stdin.drain()` *before* the wall-clock
`wait_for(p.wait(), timeout)` was armed. A child that stays alive without
consuming a stdin larger than the 64 KiB pipe buffer blocks `drain()` forever:
the handler never reaches the kill, never returns, and **permanently leaks its
`max_concurrency` semaphore slot**. Each occurrence shrinks effective capacity
by one; the process burns no CPU, so `ulimit -t` never fires, and trivial
health probes keep succeeding through the remaining free slots — which is why
liveness watchdogs stayed silent while training rewards collapsed.

**Fix:** bound the stdin flush with the run timeout and charge its elapsed time
against the subsequent `p.wait()` window, so a poisoned stdin yields a normal
`TimeLimitExceeded` instead of a leaked slot.

### 6.3 RL-load regression suite — `make test-rl-load`

`sandbox/tests/test_rl_load.py` (marker: `stress`, excluded from the default
run) replays the training access pattern against a live server so these
regressions are caught without submitting GPU jobs:

- **storm**: 288 requests over 48 concurrent client streams of mixed
  workloads (fast, CPU-heavy, TLE, compile-error, big-output, poison-stdin)
  with a background liveness prober; zero client timeouts allowed.
- **poison-stdin slot leak**: 12 concurrent sleepers fed 1 MiB of stdin must
  all return `TimeLimitExceeded` (the pre-fix server fails this in seconds).
- **kill churn**: 3 waves × 32 infinite loops, exercising timeout-kill paths
  at full concurrency.

---

## 7. Summary of changed files

| File | Change |
|------|--------|
| `sandbox/configs/run_config.py` | add `bindroot` to the `isolation` literal |
| `sandbox/configs/docker_bindroot.yaml` | new server config (`isolation: bindroot`) |
| `sandbox/runners/isolation.py` | `tmp_bindroot`, `build_bindroot_wrapper`, `OBJ_TMPDIR` host-backed compiler scratch, `--make-rprivate` hygiene, orphan/signal cleanup for bindroot dirs; `tmp_cpuset` per-exec core leasing |
| `sandbox/runners/base.py` | `run_commands` bindroot branch; `_build_cmd` exports `TMPDIR=$OBJ_TMPDIR`; rlimit/taskset/netns/PID-ns stand-ins (§3); stdin-drain timeout fix (§6.2) |
| `sandbox/runners/major.py` | `rlimit_as=False` opt-outs for tsx/.NET |
| `conftest.py` | `--sandbox-backend`/`--sandbox-mode`; Apptainer server launcher; `--cleanenv --fakeroot --ignore-fakeroot-command --no-home` |
| `Makefile` | `pull-apptainer-images`, `start-apptainer-container`, `test-apptainer-bindroot`, `test-rl-load`; launch flags as above |
| `sandbox/utils/logging.py` | server log level configurable via `SANDBOX_LOG_LEVEL` |
| `sandbox/tests/test_rl_load.py` | RL-load regression suite (§6.3), `stress` marker |
| `sandbox/tests/runners/test_isolation.py` | egress test skipped under bindroot + egress-blocked assertion; port-conflict test un-skipped (per-exec netns) |
| `sandbox/tests/runners/test_lean.py` | `test_lean_error` run timeout 10 → 30 |
| `sandbox/tests/runners/test_rust.py` | `test_rust_timeout` compile timeout 1 → 20 |
| `sandbox/tests/runners/test_python.py` | skip absolute-path fetch test under `bindroot` (only the cwd is exposed, like `full`) |
