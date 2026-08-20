# Design notes

## What is missing in rules_apple

The current `rules_apple` architecture has almost all of the mechanism needed
for local simulator parallelism, but no first-class policy tying it together.

1. `ios_ui_test` assembles one shared test bundle and one executable test rule.
   The runner is injected through `AppleTestRunnerInfo`, which makes an external
   runner policy possible without forking the bundle rules.
2. The default `ios_xctestrun_runner` creates one simulator, emits an
   `.xctestrun`, and invokes `xcodebuild test-without-building`. Its
   `xcodebuild_args` attribute is appended directly to that command.
3. The shell runner already has special handling for parallel-testing output.
   The repository tests exercise the Xcode parallel flags for a unit test, but
   only with one worker. There is no public parallel-UI-test abstraction.
4. The file-level documentation for `ios_xctestrun_runner` still says UI tests
   are unsupported, even though the template has an explicit `XCUITEST` path
   and the `rules_apple` examples use this runner for UI tests. That stale
   comment hides the best extension seam.
5. `shard_count` is inherited from Bazel's common test attributes, but neither
   iOS runner consumes `TEST_SHARD_INDEX` or `TEST_TOTAL_SHARDS`. Setting
   `shard_count = 5` therefore starts five copies of the complete test suite.
6. Simulator reuse is keyed by a deterministic device name. Concurrent Bazel
   test actions can select the same reused simulator; non-reuse mode avoids the
   collision with a random name, at the cost of repeated creation and booting.
7. `ios_ui_test_suite` is a destination matrix. It repeats the whole suite for
   each runner and is not a test partitioning primitive.

## The useful seam

For UI tests, the xctestrun runner already chooses `xcodebuild` because a test
host is present. These supported Xcode arguments turn that one action into a
local fleet:

```text
-parallel-testing-enabled YES
-parallel-testing-worker-count 5
```

Xcode owns clone creation, scheduling, log aggregation, result-bundle
aggregation, crash handling, and clone teardown. Depending on those behaviors
is substantially smaller and less brittle than recreating CoreSimulator's
pooling semantics in a daemon.

## Method-level sharding

Xcode's native parallel distribution is class-granular. To parallelize methods
from one class, `ios_method_sharded_ui_test` creates one ordinary
`ios_ui_test` with Bazel `shard_count` set to the requested simulator count and
wraps the upstream runner template.

Before each shard enters the upstream template, the wrapper:

1. validates `TEST_TOTAL_SHARDS` and `TEST_SHARD_INDEX`;
2. selects the methods statically assigned to that shard;
3. exports them as a comma-separated `TESTBRIDGE_TEST_ONLY` filter; and
4. touches `TEST_SHARD_STATUS_FILE` to declare real sharding support to Bazel.

The upstream xctestrun runner translates that environment variable into exact
`OnlyTestIdentifiers` entries. Unlike calling `ios_ui_test` once per method,
this keeps a single bundle target and lets Bazel represent each partition as a
separately cached test action.

Every shard consumes `resources:ios_simulator:1`. In ephemeral mode, the base
runner disables simulator reuse so concurrent shards receive the unique random
simulator names already implemented by `rules_apple`.

## Prepared simulator pools

Ephemeral devices are inappropriate when a company needs persistent keychain
configuration such as an intercepting-proxy root certificate.
`prepare_simulators` creates a stable indexed pool and installs each supplied
certificate with `simctl keychain add-root-cert`. Re-running preparation reuses
the named devices and never implicitly erases or deletes them.

When `prepared_simulator_pool` is set, the method runner replaces
`rules_apple`'s creator and cleanup executables:

- the creator atomically leases one matching prepared UDID, boots it, and
  prints only that UDID to the upstream runner;
- the cleanup executable releases the lease without shutting down, erasing, or
  deleting the simulator; and
- a pool-wide `flock` plus per-UDID owner records prevents two Bazel processes
  on the same Mac from selecting the same device.

Device names are stable and intentionally simple: `simfleet_simulator_1`,
`simfleet_simulator_2`, and so on. Leases are global per UDID rather than scoped
only by logical pool name, preventing two pools from concurrently selecting the
same persistent device.

Lease ownership uses the parent test-runner PID already provided by
`ios_xctestrun_runner`. A lease is reclaimed when that process no longer
exists, covering canceled or killed Bazel tests. The configured shard count is
also passed as the pool size, so a test asking for three devices uses exactly
the stable `_1` through `_3` members even if a larger pool existed earlier.

## Avoiding machine-wide oversubscription

Internal Xcode workers are invisible to Bazel's scheduler. Without accounting,
two tests with five workers can boot ten simulators concurrently.

Bazel supports custom local resources. `ios_parallel_ui_test(worker_count = 5)`
adds this test tag:

```text
resources:ios_simulator:5
```

With the machine configured as follows, Bazel admits at most five total slots:

```text
--local_resources=ios_simulator=5
```

This accounting also composes with other local test rules if they declare the
same resource.

## Remote-cache behavior

The test target stays a normal Bazel action. Build and bundle inputs can be
downloaded from a remote cache. With `--cache_test_results=yes`, a previously
successful test action can also be served from cache when its action key and
declared environment are unchanged. On a miss, only the test executes locally
and Xcode supplies the intra-action parallelism.

The macro adds `no-remote-exec` because CoreSimulator state exists only on the
host. This prevents remote execution while still allowing remote cache reads
and uploads. It deliberately does not use the test rule's `local` attribute:
in Bazel, the resulting `local` execution requirement also makes the spawn
ineligible for caching.

## Constraints

- Xcode-managed mode distributes by class. Method-sharded mode bypasses that
  limit by giving independent Bazel actions exact method filters.
- The explicit method manifest is authoritative. A missing identifier means the
  corresponding test does not execute.
- All workers use the same device type and runtime. A device/OS matrix is still
  a separate test suite concern.
- Coverage and one xcresult are aggregated by Xcode. The project deliberately
  does not split those artifacts across Bazel shards.
- `extra_xcodebuild_args` is tokenized. Each flag and value must be a separate
  list item because the upstream runner joins these values into its shell
  command.
- Prepared simulators intentionally preserve keychain and device state. Tests
  must clean up application-specific state without erasing the device.

## Sensible next steps

1. Add a `simfleet doctor` command that checks CPU, memory, installed runtimes,
   and recommends four versus five workers.
2. Parse the Build Event Protocol and xcresult timing data to warn when test
   class granularity prevents effective parallelism.
3. Add a shared pre-action that resets the reusable base simulator to a named
   snapshot before Xcode clones it.
4. Upstream a smaller `parallel_testing_worker_count` attribute to
   `ios_xctestrun_runner`; keep this project focused on resource accounting and
   ergonomics.
