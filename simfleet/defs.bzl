"""Public macros for running iOS UI tests on a local simulator fleet."""

load(
    "@rules_apple//apple:ios.bzl",
    "ios_ui_test",
    "ios_xctestrun_runner",
)
load(
    "@rules_apple//apple:providers.bzl",
    "AppleTestRunnerInfo",
    "apple_provider",
)

_PARALLEL_XCODEBUILD_FLAGS = [
    "-maximum-parallel-testing-workers",
    "-parallel-testing-enabled",
    "-parallel-testing-worker-count",
]
_ACQUIRE_PREPARED_SIMULATOR = Label("//simfleet:acquire_simulator")
_RELEASE_PREPARED_SIMULATOR = Label("//simfleet:release_simulator")

def _method_sharding_runner_impl(ctx):
    base_runner = ctx.attr.base_runner
    base_info = base_runner[AppleTestRunnerInfo]

    manifest = ctx.actions.declare_file(ctx.label.name + ".manifest")
    ctx.actions.write(
        output = manifest,
        content = "\n".join(ctx.attr.assignments) + "\n",
    )

    prelude = ctx.actions.declare_file(ctx.label.name + ".prelude.sh")
    ctx.actions.write(
        output = prelude,
        content = """#!/bin/bash
set -euo pipefail

readonly simfleet_expected_shards={expected_shards}
readonly simfleet_manifest="{manifest}"

if [[ "${{TEST_TOTAL_SHARDS:-0}}" -ne "$simfleet_expected_shards" ]]; then
  echo "error: expected $simfleet_expected_shards Bazel shards, got '${{TEST_TOTAL_SHARDS:-unset}}'" >&2
  exit 1
fi

if [[ ! "${{TEST_SHARD_INDEX:-}}" =~ ^[0-9]+$ ]] ||
   [[ "$TEST_SHARD_INDEX" -ge "$TEST_TOTAL_SHARDS" ]]; then
  echo "error: invalid TEST_SHARD_INDEX '${{TEST_SHARD_INDEX:-unset}}'" >&2
  exit 1
fi

selected_tests=()
while IFS=$'\\t' read -r assigned_shard test_identifier; do
  if [[ "$assigned_shard" == "$TEST_SHARD_INDEX" ]]; then
    selected_tests+=("$test_identifier")
  fi
done < "$simfleet_manifest"

if (( ${{#selected_tests[@]}} == 0 )); then
  echo "error: shard $TEST_SHARD_INDEX has no assigned UI tests" >&2
  exit 1
fi

if [[ -n "${{TESTBRIDGE_TEST_ONLY:-}}" ]]; then
  echo "error: command-line --test_filter cannot be combined with SimFleet method sharding" >&2
  exit 1
fi

export TESTBRIDGE_TEST_ONLY="$(IFS=,; echo "${{selected_tests[*]}}")"
touch "$TEST_SHARD_STATUS_FILE"
echo "SimFleet shard $TEST_SHARD_INDEX/$TEST_TOTAL_SHARDS: $TESTBRIDGE_TEST_ONLY"

""".format(
            expected_shards = ctx.attr.shard_count,
            manifest = manifest.short_path,
        ),
    )

    output = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.run_shell(
        inputs = [
            base_info.test_runner_template,
            prelude,
        ],
        outputs = [output],
        arguments = [
            prelude.path,
            base_info.test_runner_template.path,
            output.path,
        ],
        command = "cat \"$1\" \"$2\" > \"$3\"",
        mnemonic = "SimFleetRunnerTemplate",
    )

    execution_environment = dict(getattr(base_info, "execution_environment", {}))
    if ctx.attr.prepared_simulator_pool:
        execution_environment["SIMFLEET_POOL_NAME"] = ctx.attr.prepared_simulator_pool
        execution_environment["SIMFLEET_POOL_SIZE"] = str(ctx.attr.shard_count)

    return [
        apple_provider.make_apple_test_runner_info(
            execution_environment = execution_environment,
            execution_requirements = getattr(base_info, "execution_requirements", {}),
            test_environment = getattr(base_info, "test_environment", {}),
            test_runner_template = output,
        ),
        DefaultInfo(
            files = depset([output, manifest]),
            runfiles = ctx.runfiles(files = [manifest]).merge(
                base_runner[DefaultInfo].default_runfiles,
            ),
        ),
    ]

_method_sharding_runner = rule(
    implementation = _method_sharding_runner_impl,
    attrs = {
        "assignments": attr.string_list(mandatory = True),
        "base_runner": attr.label(
            mandatory = True,
            providers = [AppleTestRunnerInfo],
        ),
        "prepared_simulator_pool": attr.string(),
        "shard_count": attr.int(mandatory = True),
    },
)

def _resource_tag(resource_name, worker_count):
    if not resource_name:
        fail("simulator_resource must not be empty")
    if ":" in resource_name:
        fail("simulator_resource must not contain ':'")
    return "resources:{}:{}".format(resource_name, worker_count)

def _with_resource_tag(tags, resource_tag):
    result = []
    for tag in tags + ["no-remote-exec", resource_tag]:
        if tag not in result:
            result.append(tag)
    return result

def _validate_test_identifiers(test_identifiers, simulator_count):
    if simulator_count < 2:
        fail("simulator_count must be at least 2")
    if simulator_count > 16:
        fail("simulator_count must be 16 or less")
    if len(test_identifiers) < simulator_count:
        fail(
            "test_identifiers must contain at least one method per simulator " +
            "({} methods for {} simulators)".format(
                len(test_identifiers),
                simulator_count,
            ),
        )

    seen = {}
    for test_identifier in test_identifiers:
        if "/" not in test_identifier:
            fail("test identifier must use 'ClassName/testMethod': " + test_identifier)
        if "," in test_identifier or "\n" in test_identifier or "\t" in test_identifier:
            fail("test identifier contains an unsupported delimiter: " + test_identifier)
        if test_identifier in seen:
            fail("duplicate test identifier: " + test_identifier)
        seen[test_identifier] = True

def _ordered_by_duration(test_identifiers, estimated_durations):
    if not estimated_durations:
        return test_identifiers

    for test_identifier in estimated_durations:
        if test_identifier not in test_identifiers:
            fail("estimated_durations contains an unknown test: " + test_identifier)
        if estimated_durations[test_identifier] < 0:
            fail("estimated duration must not be negative: " + test_identifier)

    ordered = []
    for test_identifier in test_identifiers:
        next_ordered = []
        inserted = False
        for existing in ordered:
            if not inserted and estimated_durations.get(test_identifier, 0) > estimated_durations.get(existing, 0):
                next_ordered.append(test_identifier)
                inserted = True
            next_ordered.append(existing)
        if not inserted:
            next_ordered.append(test_identifier)
        ordered = next_ordered
    return ordered

def _shard_assignments(test_identifiers, simulator_count, estimated_durations):
    assignments = []
    shard_loads = [0.0 for _ in range(simulator_count)]
    ordered_tests = _ordered_by_duration(test_identifiers, estimated_durations)

    for index, test_identifier in enumerate(ordered_tests):
        if estimated_durations:
            shard = 0
            for candidate in range(1, simulator_count):
                if shard_loads[candidate] < shard_loads[shard]:
                    shard = candidate
            shard_loads[shard] += estimated_durations.get(test_identifier, 0)
        else:
            shard = index % simulator_count
        assignments.append("{}\t{}".format(shard, test_identifier))

    return assignments

def _validate_no_parallel_xcodebuild_args(extra_xcodebuild_args):
    for arg in extra_xcodebuild_args:
        if arg in _PARALLEL_XCODEBUILD_FLAGS:
            fail(
                "ios_method_sharded_ui_test already parallelizes with Bazel; " +
                "do not pass " + arg,
            )

def _validate_pool_name(pool_name):
    if not pool_name:
        return
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    if len(pool_name) > 48 or pool_name[0] not in allowed.replace("_", "").replace("-", "").replace(".", ""):
        fail("prepared_simulator_pool must start with an alphanumeric character and be at most 48 characters")
    for index in range(len(pool_name)):
        character = pool_name[index]
        if character not in allowed:
            fail("prepared_simulator_pool contains an unsupported character: " + character)

def ios_method_sharded_ui_test(
        name,
        test_identifiers,
        simulator_count = 3,
        estimated_durations = {},
        prepared_simulator_pool = "",
        device_type = "",
        os_version = "",
        create_xcresult_bundle = False,
        extra_xcodebuild_args = [],
        simulator_resource = "ios_simulator",
        tags = [],
        **kwargs):
    """Runs individual XCTest methods across Bazel simulator shards.

    Unlike Xcode's class-level distribution, this macro can execute methods
    from the same XCTestCase subclass concurrently on separate simulators.

    Args:
        name: Name of the resulting ios_ui_test target.
        test_identifiers: Complete `ClassName/testMethod` identifiers to shard.
        simulator_count: Number of Bazel shards and concurrent simulators.
        estimated_durations: Optional identifier-to-seconds map for balancing.
        prepared_simulator_pool: Persistent pool created by prepare_simulators.
        device_type: CoreSimulator device type, or rules_apple's default.
        os_version: CoreSimulator runtime version, or rules_apple's default.
        create_xcresult_bundle: Whether each shard should preserve an xcresult.
        extra_xcodebuild_args: Extra tokenized arguments for xcodebuild.
        simulator_resource: Bazel custom local resource used for admission.
        tags: Additional tags for the generated ios_ui_test.
        **kwargs: Remaining arguments accepted by rules_apple's ios_ui_test.
    """
    _validate_test_identifiers(test_identifiers, simulator_count)
    _validate_no_parallel_xcodebuild_args(extra_xcodebuild_args)
    _validate_pool_name(prepared_simulator_pool)

    for forbidden_attr in ["runner", "shard_count", "test_filter"]:
        if forbidden_attr in kwargs:
            fail(
                "ios_method_sharded_ui_test manages '{}'; do not pass it".format(
                    forbidden_attr,
                ),
            )

    base_runner_name = name + ".__simfleet_base_runner"
    base_runner_kwargs = {
        "name": base_runner_name,
        "create_xcresult_bundle": create_xcresult_bundle,
        "device_type": device_type,
        "os_version": os_version,
        "tags": ["manual"],
        "visibility": ["//visibility:private"],
        "xcodebuild_args": extra_xcodebuild_args,
    }
    if prepared_simulator_pool:
        base_runner_kwargs.update({
            "clean_up_simulator_action": _RELEASE_PREPARED_SIMULATOR,
            "create_simulator_action": _ACQUIRE_PREPARED_SIMULATOR,
            "reuse_simulator": True,
        })
    else:
        # Bazel invokes all shards concurrently, so a deterministic reused
        # simulator would make them collide. The upstream creator gives each
        # non-reused simulator a random suffix and deletes it after the shard.
        base_runner_kwargs["reuse_simulator"] = False

    ios_xctestrun_runner(
        **base_runner_kwargs
    )

    method_runner_name = name + ".__simfleet_method_runner"
    _method_sharding_runner(
        name = method_runner_name,
        assignments = _shard_assignments(
            test_identifiers,
            simulator_count,
            estimated_durations,
        ),
        base_runner = ":" + base_runner_name,
        prepared_simulator_pool = prepared_simulator_pool,
        shard_count = simulator_count,
        tags = ["manual"],
        visibility = ["//visibility:private"],
    )

    ios_ui_test(
        name = name,
        runner = ":" + method_runner_name,
        shard_count = simulator_count,
        tags = _with_resource_tag(
            tags,
            _resource_tag(simulator_resource, 1),
        ),
        **kwargs
    )

def ios_parallel_ui_test(
        name,
        worker_count = 5,
        device_type = "",
        os_version = "",
        reuse_simulator = True,
        create_xcresult_bundle = False,
        extra_xcodebuild_args = [],
        simulator_resource = "ios_simulator",
        tags = [],
        **kwargs):
    """Declares an ios_ui_test that Xcode runs on cloned simulators.

    Args:
        name: Name of the resulting ios_ui_test target.
        worker_count: Exact number of parallel Xcode test workers.
        device_type: CoreSimulator device type, or rules_apple's default.
        os_version: CoreSimulator runtime version, or rules_apple's default.
        reuse_simulator: Whether rules_apple should reuse the base simulator.
        create_xcresult_bundle: Whether the runner should preserve an xcresult.
        extra_xcodebuild_args: Extra tokenized arguments for xcodebuild.
        simulator_resource: Bazel custom local resource used for admission.
        tags: Additional tags for the generated ios_ui_test.
        **kwargs: Remaining arguments accepted by rules_apple's ios_ui_test.
    """
    if worker_count < 2:
        fail("worker_count must be at least 2")

    if worker_count > 16:
        fail("worker_count must be 16 or less")

    if "runner" in kwargs:
        fail("ios_parallel_ui_test creates its own runner; do not pass runner")

    shard_count = kwargs.get("shard_count")
    if shard_count != None and shard_count > 1:
        fail(
            "Do not combine shard_count with ios_parallel_ui_test: " +
            "rules_apple does not partition tests by TEST_SHARD_INDEX, and " +
            "Bazel shards would multiply the Xcode workers.",
        )

    runner_name = name + ".__simfleet_runner"
    parallel_args = [
        "-parallel-testing-enabled",
        "YES",
        "-parallel-testing-worker-count",
        str(worker_count),
    ]

    ios_xctestrun_runner(
        name = runner_name,
        create_xcresult_bundle = create_xcresult_bundle,
        device_type = device_type,
        os_version = os_version,
        reuse_simulator = reuse_simulator,
        tags = ["manual"],
        visibility = ["//visibility:private"],
        xcodebuild_args = parallel_args + extra_xcodebuild_args,
    )

    ios_ui_test(
        name = name,
        runner = ":" + runner_name,
        tags = _with_resource_tag(
            tags,
            _resource_tag(simulator_resource, worker_count),
        ),
        **kwargs
    )
