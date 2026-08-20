# rules_simfleet

`rules_simfleet` is a small companion to
[`rules_apple`](https://github.com/bazelbuild/rules_apple) that runs one iOS UI
test target on a local fleet of simulators. It supports both Xcode's normal
class-level distribution and true method-level sharding.

It is designed for teams that use remote caching, but do not have macOS remote
execution. Compilation and bundle assembly can still come from the remote
cache; a cache miss fans out locally through Bazel shards or Xcode workers.

The initial implementation is intentionally thin. `rules_apple` already:

- produces an `.xctestrun` file for UI tests;
- accepts additional `xcodebuild` arguments on `ios_xctestrun_runner`; and
- recognizes parallel-XCTest logs.

`rules_simfleet` supplies the missing policy and method-aware runner:

- enable Xcode parallel testing;
- request an exact Xcode worker count in class-distribution mode;
- reserve the same number of Bazel `ios_simulator` resources;
- force local execution without disabling remote cache access; and
- reject Bazel-level sharding, which would otherwise run every test repeatedly
  and could create `shard_count * worker_count` simulators; or
- deliberately enable Bazel sharding with an exact method manifest, converting
  each shard index into a `TESTBRIDGE_TEST_ONLY` filter.

## Use it

For a local checkout of this repository, add it to the consuming workspace's
module graph:

```starlark
bazel_dep(name = "rules_apple", version = "5.0.0-rc2")
bazel_dep(name = "rules_simfleet", version = "0.1.0")

local_path_override(
    module_name = "rules_simfleet",
    path = "../simulator_management",
)
```

Then load the method-sharding macro:

```starlark
load("@rules_simfleet//simfleet:defs.bzl", "ios_method_sharded_ui_test")

ios_method_sharded_ui_test(
    name = "CheckoutUITests",
    test_identifiers = [
        "CheckoutUITests/testAddItem",
        "CheckoutUITests/testRemoveItem",
        "CheckoutUITests/testApplyCoupon",
        "CheckoutUITests/testDeclinedCard",
        "CheckoutUITests/testSuccessfulPurchase",
    ],
    simulator_count = 3,
    minimum_os_version = "18.0",
    test_host = ":CheckoutApp",
    deps = [":CheckoutUITestsLib"],
)
```

Even though all five methods belong to one `XCTestCase`, the resulting Bazel
shards are:

```text
shard 0 / simulator 0: testAddItem, testDeclinedCard
shard 1 / simulator 1: testRemoveItem, testSuccessfulPurchase
shard 2 / simulator 2: testApplyCoupon
```

Every shard receives an exact method filter before the upstream
`ios_xctestrun_runner` starts. The test bundle is still assembled once.

Tell Bazel how many simulator slots the machine has. Each method shard reserves
one slot:

```bazelrc
test --local_resources=ios_simulator=3
```

Run it like an ordinary test:

```shell
bazel test //app:CheckoutUITests --cache_test_results=yes
```

The method manifest must be complete: methods omitted from `test_identifiers`
will not run. Do not combine this macro with a rule-level or command-line
`test_filter`; SimFleet owns the per-shard filter.

## Configuration

### Persistent prepared simulators

Some environments install corporate or debugging proxy root certificates in
the simulator keychain. Prepare a persistent named pool once:

```shell
bazel run @rules_simfleet//simfleet:prepare_simulators -- \
  --pool corporate-proxy \
  --count 3 \
  --device-type "iPhone 16 Pro" \
  --os-version 18.2 \
  --developer-dir /Applications/Xcode.app/Contents/Developer \
  --certificate "$HOME/certs/corporate-proxy-root.pem"
```

`--certificate` may be repeated. Preparation is non-destructive: devices named
`simfleet_simulator_1`, `simfleet_simulator_2`, and so on are created when
missing and reused when present. SimFleet boots each device, installs the certificates with
`simctl keychain add-root-cert`, then shuts it down unless `--keep-booted` is
passed. It never erases or deletes prepared devices.

Select that pool from the test:

```starlark
ios_method_sharded_ui_test(
    name = "CheckoutUITests",
    test_identifiers = CHECKOUT_UI_TESTS,
    simulator_count = 3,
    prepared_simulator_pool = "corporate-proxy",
    minimum_os_version = "18.0",
    test_host = ":CheckoutApp",
    deps = [":CheckoutUITestsLib"],
)
```

Each Bazel shard takes an atomic machine-wide, per-UDID lease on one of the first three
prepared devices. The simulator is released—not deleted or erased—after the
test. Stale leases from killed test runners are reclaimed automatically.

`device_type` and `os_version` can be declared on the macro or supplied with
the standard `rules_apple` build settings. The latter is useful on CI images
whose newest installed simulator changes over time:

```shell
bazel test //app:CheckoutUITests \
  --@rules_apple//apple/build_settings:ios_simulator_device="iPhone 16" \
  --@rules_apple//apple/build_settings:ios_simulator_version="18.5"
```

Certificate installation establishes trust; configuring the Mac's proxy or
the simulator's network route remains environment-specific.

### Shard balancing

```starlark
ios_method_sharded_ui_test(
    name = "CheckoutUITests",
    test_identifiers = [
        "CheckoutUITests/testAddItem",
        "CheckoutUITests/testRemoveItem",
        "CheckoutUITests/testApplyCoupon",
        "CheckoutUITests/testDeclinedCard",
        "CheckoutUITests/testSuccessfulPurchase",
    ],
    simulator_count = 3,
    estimated_durations = {
        "CheckoutUITests/testApplyCoupon": 18,
        "CheckoutUITests/testDeclinedCard": 7,
        "CheckoutUITests/testSuccessfulPurchase": 24,
    },
    device_type = "iPhone 16 Pro",
    os_version = "18.2",
    create_xcresult_bundle = True,
    extra_xcodebuild_args = [
        "-test-timeouts-enabled",
        "YES",
    ],
    minimum_os_version = "18.0",
    test_host = ":CheckoutApp",
    deps = [":CheckoutUITestsLib"],
)
```

When durations are supplied, SimFleet uses longest-processing-time scheduling
to balance the shard totals. Unlisted methods have an estimate of zero. Without
durations it uses stable round-robin assignment.

Without `prepared_simulator_pool`, each shard receives a unique temporary
simulator because the upstream reusable simulator is not concurrency-safe.
`rules_apple` deletes those temporary devices when the shards finish.

The original `ios_parallel_ui_test` macro remains available for bundles that
already have enough independent test classes and prefer Xcode-managed clones.

## GitHub Actions PR checks

The checked-in [CI workflow](.github/workflows/ci.yml) runs on pull requests,
pushes to `main`, and manual dispatches. It has two required-check-friendly
jobs:

- `Unit and analysis checks` runs the Python tests and analyzes the complete
  iOS integration target.
- `UI methods on 3 prepared simulators` discovers the newest installed iOS
  runtime and iPhone model, generates a one-day test root certificate, prepares
  `simfleet_simulator_1` through `simfleet_simulator_3`, and runs the five
  methods in one XCTest class across three Bazel shards.

The UI job starts after the unit job and restores the same Bazel disk and
repository caches, so compilation is reused while simulator execution remains
local to the second Mac.

No repository secret is required, so the workflow is safe to run for forked
pull requests. Failed UI runs upload `bazel-testlogs` for seven days.

The default runner is `macos-15`. To use a larger or self-hosted macOS runner,
create the repository Actions variable `SIMFLEET_MACOS_RUNNER` containing its
runner label; the workflow uses that label without requiring a source change.

See [the design notes](docs/design.md) for the `rules_apple` findings, cache
model, constraints, and possible next steps.
