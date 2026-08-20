"""Persistent CoreSimulator pool preparation and leasing."""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterator, Optional, Sequence


_DEVICE_NAME_PREFIX = "simfleet_simulator"
_DEFAULT_LEASE_ROOT = Path("/private/tmp/rules_simfleet") / str(os.getuid()) / "leases"
_POOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")


def _simctl(*args: str) -> str:
    result = subprocess.run(
        ["xcrun", "simctl", *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result.stdout


def _simctl_json(*args: str) -> dict[str, Any]:
    return json.loads(_simctl(*args))


def _validate_pool_name(pool_name: str) -> str:
    if not _POOL_NAME_PATTERN.fullmatch(pool_name):
        raise ValueError(
            "pool name must start with an alphanumeric character, contain only "
            "letters, numbers, '.', '_' or '-', and be at most 48 characters"
        )
    return pool_name


def _device_name(pool_name: str, index: int) -> str:
    del pool_name
    return f"{_DEVICE_NAME_PREFIX}_{index + 1}"


def _version_key(version: str) -> tuple[int, ...]:
    parts = []
    for component in version.split("."):
        match = re.match(r"^(\d+)", component)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts)


def _select_runtime(os_version: str) -> dict[str, Any]:
    runtimes = [
        runtime
        for runtime in _simctl_json("list", "runtimes", "--json")["runtimes"]
        if runtime.get("platform") == "iOS" and runtime.get("isAvailable", False)
    ]
    matches = [runtime for runtime in runtimes if runtime.get("version") == os_version]
    if not matches:
        available = ", ".join(
            sorted({runtime.get("version", "unknown") for runtime in runtimes}, key=_version_key)
        )
        raise RuntimeError(
            f"no available iOS runtime exactly matches {os_version}; available: {available or 'none'}"
        )
    return max(matches, key=lambda runtime: _version_key(runtime["version"]))


def _select_device_type(device_type: str) -> dict[str, Any]:
    device_types = _simctl_json("list", "devicetypes", "--json")["devicetypes"]
    matches = [
        candidate
        for candidate in device_types
        if candidate.get("name") == device_type or candidate.get("identifier") == device_type
    ]
    if not matches:
        raise RuntimeError(f"no CoreSimulator device type matches '{device_type}'")
    return matches[0]


def _latest_ios_runtime(runtimes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    available = [
        runtime
        for runtime in runtimes
        if runtime.get("platform") == "iOS" and runtime.get("isAvailable", False)
    ]
    if not available:
        raise RuntimeError("Xcode has no available iOS simulator runtime")
    return max(available, key=lambda runtime: _version_key(runtime.get("version", "0")))


def _latest_iphone_device_type(device_types: Sequence[dict[str, Any]]) -> dict[str, Any]:
    numeric_iphones = []
    other_iphones = []
    for device_type in device_types:
        name = device_type.get("name", "")
        if not name.startswith("iPhone ") or " SE" in name:
            continue
        match = re.match(r"^iPhone (\d+)(?: |$)", name)
        if match:
            # Prefer a plain model over Pro/Plus/Max variants. All share the
            # same generation, while the plain model is the least surprising
            # CI destination and is present on GitHub's Xcode images.
            numeric_iphones.append(
                (
                    int(match.group(1)),
                    name == f"iPhone {match.group(1)}",
                    name,
                    device_type,
                )
            )
        else:
            other_iphones.append(device_type)

    if numeric_iphones:
        return max(numeric_iphones, key=lambda item: (item[0], item[1], item[2]))[3]
    if other_iphones:
        return sorted(other_iphones, key=lambda item: item["name"])[-1]
    raise RuntimeError("Xcode has no available iPhone simulator device type")


def select_main() -> None:
    """Prints a GitHub-runner-safe simulator destination as JSON."""
    runtime = _latest_ios_runtime(_simctl_json("list", "runtimes", "--json")["runtimes"])
    device_type = _latest_iphone_device_type(
        _simctl_json("list", "devicetypes", "--json")["devicetypes"]
    )
    print(
        json.dumps(
            {
                "device_type": device_type["name"],
                "os_version": runtime["version"],
                "runtime_identifier": runtime["identifier"],
            },
            sort_keys=True,
        )
    )


def _devices_for_runtime(runtime_identifier: str) -> list[dict[str, Any]]:
    devices = _simctl_json("list", "devices", "--json")["devices"]
    return list(devices.get(runtime_identifier, []))


def _boot(device: dict[str, Any]) -> None:
    udid = device["udid"]
    if device.get("state", "").lower() != "booted":
        try:
            _simctl("boot", udid)
        except subprocess.CalledProcessError as error:
            # A concurrent CoreSimulator state update can report "already booted".
            if error.returncode != 149:
                raise
    _simctl("bootstatus", udid, "-b")


def _certificate_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as certificate:
        for chunk in iter(lambda: certificate.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_certificates(udid: str, certificates: Sequence[Path]) -> None:
    for certificate in certificates:
        print(
            f"Installing root certificate {certificate.name} "
            f"(sha256:{_certificate_digest(certificate)[:12]}) on {udid}",
            file=sys.stderr,
        )
        _simctl("keychain", udid, "add-root-cert", str(certificate))


def _prepare_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or reuse a persistent pool of certificate-ready iOS simulators.",
    )
    parser.add_argument("--pool", required=True, help="Stable pool name used by the test macro.")
    parser.add_argument("--count", required=True, type=int, help="Number of prepared simulators.")
    parser.add_argument("--device-type", required=True, help="Exact simctl device type name.")
    parser.add_argument("--os-version", required=True, help="Exact installed iOS runtime version.")
    parser.add_argument(
        "--developer-dir",
        help="Optional Xcode Developer directory to use instead of the active xcode-select path.",
    )
    parser.add_argument(
        "--certificate",
        action="append",
        default=[],
        help="Root certificate to install. May be specified more than once.",
    )
    parser.add_argument(
        "--keep-booted",
        action="store_true",
        help="Leave prepared simulators booted instead of shutting them down.",
    )
    return parser


def prepare_main() -> None:
    args = _prepare_parser().parse_args()
    pool_name = _validate_pool_name(args.pool)
    if args.count < 1:
        raise ValueError("--count must be at least 1")
    if args.developer_dir:
        developer_dir = Path(args.developer_dir).expanduser().resolve()
        if not (developer_dir / "usr" / "bin" / "xcodebuild").is_file():
            raise FileNotFoundError(f"invalid Xcode Developer directory: {developer_dir}")
        os.environ["DEVELOPER_DIR"] = str(developer_dir)

    certificates = [Path(path).expanduser().resolve() for path in args.certificate]
    missing = [str(path) for path in certificates if not path.is_file()]
    if missing:
        raise FileNotFoundError("certificate does not exist: " + ", ".join(missing))

    runtime = _select_runtime(args.os_version)
    device_type = _select_device_type(args.device_type)
    existing_by_name = {
        device["name"]: device
        for device in _devices_for_runtime(runtime["identifier"])
        if device.get("isAvailable", True)
    }

    prepared = []
    for index in range(args.count):
        name = _device_name(pool_name, index)
        device = existing_by_name.get(name)
        if device:
            actual_type = device.get("deviceTypeIdentifier")
            if actual_type and actual_type != device_type["identifier"]:
                raise RuntimeError(
                    f"prepared simulator '{name}' has type {actual_type}, expected "
                    f"{device_type['identifier']}; rename or delete it explicitly before retrying"
                )
            print(f"Reusing {name} ({device['udid']})", file=sys.stderr)
        else:
            udid = _simctl(
                "create",
                name,
                device_type["identifier"],
                runtime["identifier"],
            ).strip()
            device = {
                "deviceTypeIdentifier": device_type["identifier"],
                "isAvailable": True,
                "name": name,
                "state": "Shutdown",
                "udid": udid,
            }
            print(f"Created {name} ({udid})", file=sys.stderr)

        _boot(device)
        _install_certificates(device["udid"], certificates)
        if not args.keep_booted:
            _simctl("shutdown", device["udid"])
        prepared.append({"name": name, "udid": device["udid"]})

    print(
        json.dumps(
            {
                "count": len(prepared),
                "device_type": device_type["name"],
                "os_version": runtime["version"],
                "pool": pool_name,
                "simulators": prepared,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _lease_root() -> Path:
    configured = os.environ.get("SIMFLEET_LEASE_ROOT")
    return Path(configured) if configured else _DEFAULT_LEASE_ROOT


@contextlib.contextmanager
def _lease_lock() -> Iterator[Path]:
    lease_root = _lease_root()
    lease_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lease_root / ".global.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield lease_root
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _owner_path(lease_dir: Path) -> Path:
    return lease_dir / "owner.json"


def _read_owner(lease_dir: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(_owner_path(lease_dir).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError as error:
        return error.errno == errno.EPERM


def _lease_is_stale(lease_dir: Path) -> bool:
    owner = _read_owner(lease_dir)
    if owner and isinstance(owner.get("pid"), int):
        max_age = float(os.environ.get("SIMFLEET_MAX_LEASE_AGE", "21600"))
        if time.time() - float(owner.get("created_at", 0)) > max_age:
            return True
        return not _pid_is_alive(owner["pid"])
    try:
        return time.time() - lease_dir.stat().st_mtime > 60
    except FileNotFoundError:
        return True


def _pool_devices(
    pool_name: str,
    pool_size: int,
    runtime_identifier: str,
    device_type: str,
) -> list[dict[str, Any]]:
    allowed_names = {_device_name(pool_name, index) for index in range(pool_size)}
    devices = []
    for device in _devices_for_runtime(runtime_identifier):
        if device.get("name") not in allowed_names or not device.get("isAvailable", True):
            continue
        actual_type = device.get("deviceTypeIdentifier")
        if actual_type and actual_type != device_type:
            continue
        devices.append(device)
    return sorted(devices, key=lambda device: device["name"])


def _claim_device(
    pool_name: str,
    pool_size: int,
    runtime_identifier: str,
    device_type_identifier: str,
    runner_pid: int,
) -> Optional[dict[str, Any]]:
    with _lease_lock() as lease_root:
        for device in _pool_devices(
            pool_name,
            pool_size,
            runtime_identifier,
            device_type_identifier,
        ):
            lease_dir = lease_root / device["udid"]
            if lease_dir.exists():
                if not _lease_is_stale(lease_dir):
                    continue
                shutil.rmtree(lease_dir)

            lease_dir.mkdir(mode=0o700)
            _owner_path(lease_dir).write_text(
                json.dumps(
                    {
                        "created_at": time.time(),
                        "device_name": device["name"],
                        "pid": runner_pid,
                        "pool": pool_name,
                        "udid": device["udid"],
                    },
                    sort_keys=True,
                )
            )
            return device
    return None


def _release_claim(pool_name: str, udid: str, runner_pid: int, strict: bool) -> None:
    with _lease_lock() as lease_root:
        lease_dir = lease_root / udid
        if not lease_dir.exists():
            if strict:
                raise RuntimeError(f"no active lease for prepared simulator {udid}")
            return
        owner = _read_owner(lease_dir)
        if owner and owner.get("pid") != runner_pid:
            raise RuntimeError(
                f"prepared simulator {udid} is leased by pid {owner.get('pid')}, "
                f"not runner pid {runner_pid}"
            )
        if owner and owner.get("pool") != pool_name:
            raise RuntimeError(
                f"prepared simulator {udid} belongs to active pool lease "
                f"'{owner.get('pool')}', not '{pool_name}'"
            )
        shutil.rmtree(lease_dir)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def acquire_main() -> None:
    pool_name = _validate_pool_name(_required_environment("SIMFLEET_POOL_NAME"))
    pool_size = int(_required_environment("SIMFLEET_POOL_SIZE"))
    if pool_size < 1:
        raise ValueError("SIMFLEET_POOL_SIZE must be at least 1")
    os_version = _required_environment("SIMULATOR_OS_VERSION")
    device_type_name = _required_environment("SIMULATOR_DEVICE_TYPE")
    runner_pid = int(_required_environment("XCTESTRUN_RUNNER_PID"))
    timeout = float(os.environ.get("SIMFLEET_ACQUIRE_TIMEOUT", "300"))

    runtime = _select_runtime(os_version)
    device_type = _select_device_type(device_type_name)
    prepared_devices = _pool_devices(
        pool_name,
        pool_size,
        runtime["identifier"],
        device_type["identifier"],
    )
    if len(prepared_devices) != pool_size:
        raise RuntimeError(
            f"pool '{pool_name}' has {len(prepared_devices)} matching prepared simulators, "
            f"but this test requires {pool_size}; run prepare_simulators with "
            f"--count {pool_size}, --device-type '{device_type_name}', and "
            f"--os-version '{os_version}'"
        )
    deadline = time.monotonic() + timeout
    while True:
        device = _claim_device(
            pool_name,
            pool_size,
            runtime["identifier"],
            device_type["identifier"],
            runner_pid,
        )
        if device:
            try:
                _boot(device)
            except BaseException:
                _release_claim(pool_name, device["udid"], runner_pid, strict=False)
                raise
            print(
                f"Leased prepared simulator {device['name']} ({device['udid']})",
                file=sys.stderr,
            )
            # The rules_apple creator contract requires only the UDID on stdout.
            print(device["udid"])
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out after {timeout:g}s waiting for a free simulator in pool "
                f"'{pool_name}'; run prepare_simulators with a sufficient --count"
            )
        time.sleep(0.5)


def release_main() -> None:
    pool_name = _validate_pool_name(_required_environment("SIMFLEET_POOL_NAME"))
    udid = _required_environment("SIMULATOR_UDID")
    runner_pid = int(_required_environment("XCTESTRUN_RUNNER_PID"))
    _release_claim(pool_name, udid, runner_pid, strict=False)
    print(f"Released prepared simulator {udid} to pool '{pool_name}'", file=sys.stderr)
