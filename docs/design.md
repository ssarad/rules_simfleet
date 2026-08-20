# Architecture and design

This document explains why SimFleet is implemented as a thin runner layer over
`rules_apple`, how method sharding remains deterministic, and which failure
modes the simulator pool is designed to contain.

## Design goals

1. Parallelize individual UI test methods, including methods from one
   `XCTestCase`.
2. Preserve Bazel remote-cache eligibility while keeping CoreSimulator work on
   the local Mac.
3. Support persistent simulators with preinstalled corporate or proxy root
   certificates.
4. Prevent simulator collisions across targets and concurrent Bazel commands.
5. Reuse `rules_apple` bundle assembly and xctestrun behavior instead of
   maintaining a fork.

## The `rules_apple` extension seam

`ios_ui_test` assembles the app, UI test bundle, `.xctestrun`, and executable
test action. Its runner arrives through `AppleTestRunnerInfo`, allowing
SimFleet to wrap the test-runner template without changing Apple bundle rules.

```mermaid
flowchart TB
    App["ios_application"] --> Assembly["rules_apple test assembly"]
    Tests["XCTest bundle"] --> Assembly
    Runner["ios_xctestrun_runner"] --> Provider["AppleTestRunnerInfo"]
    SimFleet["SimFleet prelude + manifest"] --> Wrapped["Wrapped runner template"]
    Provider --> Wrapped
    Wrapped --> Assembly
    Assembly --> Action["Bazel test action"]
    Action --> Xcode["xcodebuild test-without-building"]

    subgraph Cacheable["Remote-cache-eligible build graph"]
        App
        Tests
        Assembly
    end
    subgraph Local["Local macOS execution"]
        Action
        Xcode
    end
```

The upstream runner already knows how to:

- create an `.xctestrun` file;
- select a simulator destination;
- translate `TESTBRIDGE_TEST_ONLY` into `OnlyTestIdentifiers`;
- invoke `xcodebuild test-without-building` for a hosted UI test;
- collect logs and XCResult output; and
- run custom simulator creation and cleanup executables.

SimFleet composes those public mechanisms rather than reimplementing them.

## Why the obvious approaches fall short

### Native Xcode parallel testing

Xcode owns worker clones, scheduling, aggregation, and teardown, which makes it
an excellent default for bundles with many independent classes. Its useful
distribution unit is too coarse when most work lives in a single class.

```mermaid
flowchart LR
    subgraph S1["Simulator 1 — busy"]
        T1["testOne"] --> T2["testTwo"] --> T3["testThree"] --> T4["testFour"] --> T5["testFive"]
    end
    subgraph S2["Simulator 2"]
        I2["idle"]
    end
    subgraph S3["Simulator 3"]
        I3["idle"]
    end

    classDef active fill:#dbeafe,stroke:#2563eb,color:#172554
    classDef idle fill:#f3f4f6,stroke:#9ca3af,color:#6b7280,stroke-dasharray:5 5
    class T1,T2,T3,T4,T5 active
    class I2,I3 idle
```

SimFleet retains an `ios_parallel_ui_test` mode for the workloads where native
class distribution is the right tradeoff.

### Setting `shard_count` directly

Bazel exports `TEST_SHARD_INDEX` and `TEST_TOTAL_SHARDS`, but a runner must use
them to select different work. The standard `rules_apple` runner does not do
that partitioning. Three shards can therefore become three complete test-suite
executions rather than three partitions.

### Declaring one target per method

This produces independent test actions, but multiplies BUILD declarations,
test targets, result bundles, and maintenance. It also makes method discovery
and timing policy the consuming application's responsibility. SimFleet keeps
one public test target and generates the partitions internally.

## Method-sharding data flow

At analysis time, SimFleet validates the complete list of `Class/testMethod`
identifiers and assigns each one to a shard. At execution time, each Bazel
shard reads the same manifest but selects only its own rows.

```mermaid
sequenceDiagram
    participant A as Bazel analysis
    participant M as Method manifest
    participant S as Bazel shard N
    participant R as Wrapped rules_apple runner
    participant X as xcodebuild

    A->>M: Write shard-index / test-identifier rows
    A->>S: Set TEST_SHARD_INDEX and TEST_TOTAL_SHARDS
    S->>M: Read identifiers assigned to shard N
    S->>S: Validate index and touch TEST_SHARD_STATUS_FILE
    S->>R: Export comma-separated TESTBRIDGE_TEST_ONLY
    R->>X: Emit exact OnlyTestIdentifiers
    X-->>S: Test log and optional XCResult
```

The wrapper fails closed when:

- the shard count differs from the declared simulator count;
- the shard index is missing or invalid;
- no method is assigned to a shard; or
- a caller also supplies a command-line `--test_filter`.

That validation prevents a configuration mistake from silently running every
method on every simulator.

## Scheduling and balancing

Without estimates, method assignment is stable round-robin. This makes changes
reviewable and keeps shard identities deterministic:

```text
method 0 -> shard 0
method 1 -> shard 1
method 2 -> shard 2
method 3 -> shard 0
method 4 -> shard 1
```

When `estimated_durations` is present, SimFleet uses longest-processing-time
scheduling: sort from longest to shortest, then place the next method on the
currently lightest shard.

```mermaid
flowchart LR
    Durations["24s · 18s · 7s · 4s · 3s"] --> Sort["Longest first"]
    Sort --> Q0["Shard 0<br/>24s"]
    Sort --> Q1["Shard 1<br/>18s + 3s = 21s"]
    Sort --> Q2["Shard 2<br/>7s + 4s = 11s"]
```

Estimates influence placement only. They do not change XCTest timeouts and do
not need to be exact to improve a badly skewed suite.

## Simulator lifecycle modes

### Ephemeral mode

Without `prepared_simulator_pool`, SimFleet disables simulator reuse on the
base runner. Concurrent Bazel shards then receive randomized device names from
the upstream creator and `rules_apple` deletes them during cleanup.

This is the lowest-maintenance option when tests need no persistent keychain or
device configuration.

### Prepared mode

Prepared mode swaps in SimFleet's acquire and release executables. The devices
are long-lived; only lease ownership is temporary.

```mermaid
flowchart TB
    Prepare["prepare_simulators --count N"] --> Names["Stable names<br/>simfleet_simulator_1…N"]
    Names --> Config["Boot + install certificates"]
    Config --> Ready["Prepared fleet"]

    Shard0["Bazel shard 0"] --> Lock["Global flock"]
    Shard1["Bazel shard 1"] --> Lock
    Shard2["Bazel shard 2"] --> Lock
    Lock --> L1["UDID lease 1"]
    Lock --> L2["UDID lease 2"]
    Lock --> L3["UDID lease 3"]
    Ready --> L1
    Ready --> L2
    Ready --> L3
    L1 --> Release["Release owner record<br/>preserve device state"]
    L2 --> Release
    L3 --> Release
```

Preparation is intentionally non-destructive. If an existing stable device has
the wrong device type, preparation stops and asks the operator to resolve it;
it does not erase, replace, or delete the device automatically.

## Lease correctness

The lease root is machine-local under `/private/tmp/rules_simfleet/<uid>` by
default. A global `flock` serializes selection and owner-record updates.

Each owner record identifies the upstream test-runner parent PID. A lease is
considered stale when the owner process no longer exists or the configured
maximum age is exceeded. This covers normal completion, Bazel cancellation,
runner crashes, and most abrupt process termination.

```mermaid
flowchart TD
    Acquire["Shard requests simulator"] --> Locked["Take global flock"]
    Locked --> Scan["Inspect first N stable devices"]
    Scan --> Free{"Unleased device?"}
    Free -->|yes| Owner["Write owner record atomically"]
    Free -->|no| Stale{"Owner dead or lease expired?"}
    Stale -->|yes| Reclaim["Remove stale owner record"]
    Reclaim --> Owner
    Stale -->|no| Wait["Back off until timeout"]
    Wait --> Locked
    Owner --> Boot["Release flock, boot device"]
    Boot --> Output["Print only UDID to rules_apple"]
```

Leases are keyed by UDID rather than pool name. Two logical pools cannot
concurrently select the same physical simulator.

## Resource accounting

CoreSimulator capacity is finite, but internal Xcode workers are invisible to
Bazel unless the test declares demand. SimFleet adds a custom execution tag:

```text
resources:ios_simulator:1
```

to each method-sharded test action, or `resources:ios_simulator:N` to an
Xcode-managed target with `N` workers. The machine declares its total capacity:

```bazelrc
build --local_resources=ios_simulator=5
```

```mermaid
flowchart LR
    Capacity["Machine capacity: 5"] --> Scheduler["Bazel local resource manager"]
    T1["Checkout UI<br/>needs 3"] --> Scheduler
    T2["Search UI<br/>needs 2"] --> Scheduler
    T3["Profile UI<br/>needs 4"] --> Scheduler
    Scheduler -->|admit together| T1
    Scheduler -->|admit together| T2
    Scheduler -. "wait: 0 slots free" .-> T3
```

This prevents unrelated test targets or concurrent builds from accidentally
booting more simulators than the Mac can sustain.

## Remote-cache boundary

CoreSimulator cannot run on a generic non-macOS worker, and prepared devices
exist only on one host. SimFleet therefore marks tests `no-remote-exec`.

```mermaid
flowchart LR
    Source["Source + BUILD inputs"] --> Cache{"Remote cache hit?"}
    Cache -->|yes| Download["Download app/test bundles"]
    Cache -->|no| Compile["Compile and assemble"]
    Compile --> Upload["Upload cacheable outputs"]
    Download --> Local["Local simulator test"]
    Upload --> Local
    Local --> Result["Test result / XCResult"]
```

The macro deliberately avoids Bazel's `local` test attribute because that
execution requirement also makes the spawn ineligible for caching. With
`--cache_test_results=yes`, a successful shard may itself be served from cache
when its complete action key and declared environment are unchanged.

## Failure behavior

| Failure | Behavior |
| --- | --- |
| Missing method assignment | Shard fails before launching Xcode |
| Invalid shard environment | Shard fails closed with a diagnostic |
| Pool exhausted | Acquire retries until its timeout |
| Bazel process is killed | Dead-owner lease is reclaimed later |
| Certificate path is missing | Preparation fails before modifying devices |
| Stable name has the wrong device type | Preparation stops without deleting it |
| Test fails | Cleanup releases the lease; logs/XCResult remain available |
| Two Bazel commands race | Global lock and per-UDID records serialize claims |

Prepared devices intentionally retain application and keychain state. Test
code must reset application-specific state; SimFleet does not erase a device
as an implicit cleanup strategy.

## Constraints

- The explicit method manifest is authoritative and currently maintained by
  the target author.
- Every shard in one target uses the same device type and runtime.
- A device/OS matrix remains a separate test-suite concern.
- `extra_xcodebuild_args` is tokenized; flags and values are separate list
  entries because the upstream runner joins them into its command.
- Four or five simultaneous UI-test simulators generally require more CPU and
  memory than a small hosted macOS runner provides.

## Future work

1. Add `simfleet doctor` to report CPU, memory, runtimes, device types, stale
   leases, and a recommended fleet size.
2. Generate method manifests from XCTest discovery output while preserving a
   reviewable checked-in mode.
3. Learn duration estimates from Build Event Protocol and XCResult history.
4. Add an optional pre-action for deterministic application-data cleanup on
   persistent devices.
5. Upstream a smaller parallel-worker API to `ios_xctestrun_runner` and keep
   SimFleet focused on method sharding and pool management.
