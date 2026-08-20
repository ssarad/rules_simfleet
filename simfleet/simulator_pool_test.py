from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from simfleet import simulator_pool


def _device(index: int) -> dict:
    return {
        "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
        "isAvailable": True,
        "name": f"simfleet_simulator_{index + 1}",
        "state": "Shutdown",
        "udid": f"UDID-{index}",
    }


class SimulatorPoolTest(unittest.TestCase):
    def test_selects_latest_ios_runtime(self) -> None:
        selected = simulator_pool._latest_ios_runtime(
            [
                {"platform": "iOS", "isAvailable": True, "version": "18.5"},
                {"platform": "tvOS", "isAvailable": True, "version": "26.0"},
                {"platform": "iOS", "isAvailable": False, "version": "26.1"},
                {"platform": "iOS", "isAvailable": True, "version": "26.0"},
            ]
        )

        self.assertEqual("26.0", selected["version"])

    def test_selects_plain_iphone_from_latest_generation(self) -> None:
        selected = simulator_pool._latest_iphone_device_type(
            [
                {"name": "iPhone 16 Pro"},
                {"name": "iPhone 17 Pro Max"},
                {"name": "iPhone SE (3rd generation)"},
                {"name": "iPhone 17"},
            ]
        )

        self.assertEqual("iPhone 17", selected["name"])

    def test_prepared_device_names_are_stable_and_one_indexed(self) -> None:
        self.assertEqual("simfleet_simulator_1", simulator_pool._device_name("corporate", 0))
        self.assertEqual("simfleet_simulator_3", simulator_pool._device_name("corporate", 2))

    def test_pool_devices_uses_exact_requested_members(self) -> None:
        with mock.patch.object(
            simulator_pool,
            "_devices_for_runtime",
            return_value=[_device(3), _device(1), _device(0), _device(2)],
        ):
            devices = simulator_pool._pool_devices(
                "corporate",
                3,
                "com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
            )

        self.assertEqual(["UDID-0", "UDID-1", "UDID-2"], [item["udid"] for item in devices])

    def test_claims_are_exclusive_and_release_makes_device_available(self) -> None:
        with tempfile.TemporaryDirectory() as lease_root:
            environment = {"SIMFLEET_LEASE_ROOT": lease_root}
            with mock.patch.dict(os.environ, environment), mock.patch.object(
                simulator_pool,
                "_devices_for_runtime",
                return_value=[_device(0), _device(1)],
            ):
                first = simulator_pool._claim_device(
                    "corporate",
                    2,
                    "runtime",
                    "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
                    os.getpid(),
                )
                second = simulator_pool._claim_device(
                    "corporate",
                    2,
                    "runtime",
                    "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
                    os.getpid(),
                )

                self.assertEqual("UDID-0", first["udid"])
                self.assertEqual("UDID-1", second["udid"])

                simulator_pool._release_claim(
                    "corporate",
                    "UDID-0",
                    os.getpid(),
                    strict=True,
                )
                replacement = simulator_pool._claim_device(
                    "corporate",
                    2,
                    "runtime",
                    "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
                    os.getpid(),
                )
                self.assertEqual("UDID-0", replacement["udid"])

    def test_dead_owner_lease_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as lease_root:
            lease_dir = Path(lease_root) / "UDID-0"
            lease_dir.mkdir(parents=True)
            (lease_dir / "owner.json").write_text(json.dumps({"pid": 12345}))

            with mock.patch.dict(os.environ, {"SIMFLEET_LEASE_ROOT": lease_root}), mock.patch.object(
                simulator_pool,
                "_devices_for_runtime",
                return_value=[_device(0)],
            ), mock.patch.object(simulator_pool, "_pid_is_alive", return_value=False):
                claimed = simulator_pool._claim_device(
                    "corporate",
                    1,
                    "runtime",
                    "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
                    os.getpid(),
                )

            self.assertEqual("UDID-0", claimed["udid"])
            owner = json.loads((lease_dir / "owner.json").read_text())
            self.assertEqual(os.getpid(), owner["pid"])

    def test_acquire_prints_only_udid_to_stdout(self) -> None:
        environment = {
            "SIMFLEET_ACQUIRE_TIMEOUT": "0",
            "SIMFLEET_POOL_NAME": "corporate",
            "SIMFLEET_POOL_SIZE": "1",
            "SIMULATOR_DEVICE_TYPE": "iPhone 16 Pro",
            "SIMULATOR_OS_VERSION": "18.2",
            "XCTESTRUN_RUNNER_PID": str(os.getpid()),
        }
        standard_output = io.StringIO()
        standard_error = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
            simulator_pool,
            "_select_runtime",
            return_value={"identifier": "runtime", "version": "18.2"},
        ), mock.patch.object(
            simulator_pool,
            "_select_device_type",
            return_value={"identifier": "device-type", "name": "iPhone 16 Pro"},
        ), mock.patch.object(
            simulator_pool,
            "_pool_devices",
            return_value=[_device(0)],
        ), mock.patch.object(
            simulator_pool,
            "_claim_device",
            return_value=_device(0),
        ), mock.patch.object(simulator_pool, "_boot"), contextlib.redirect_stdout(
            standard_output
        ), contextlib.redirect_stderr(standard_error):
            simulator_pool.acquire_main()

        self.assertEqual("UDID-0\n", standard_output.getvalue())
        self.assertIn("Leased prepared simulator", standard_error.getvalue())


if __name__ == "__main__":
    unittest.main()
