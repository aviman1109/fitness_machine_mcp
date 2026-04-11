"""Device role registry — scan, categorise, pair, persist.

Roles
-----
trainer      : FTMS indoor bike / smart trainer (ERG control)
treadmill    : FTMS treadmill (data read + incline; speed control not reliable)
hrm          : Heart-rate monitor (HR + optional RR intervals, RSC, CSC)
power_meter  : Standalone cycling power meter (Cycling Power Service 0x1818)

Discovery heuristics (from advertised service UUIDs, no connection required)
-----------------------------------------------------------------------------
FTMS (0x1826) + Cycling Power (0x1818)  → trainer
FTMS (0x1826) only                      → treadmill
Heart Rate (0x180D)                     → hrm
Cycling Power (0x1818) only             → power_meter
"""
from __future__ import annotations

import asyncio, json, logging, os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner

log = logging.getLogger("fitness_machine.devices")

# ── Config path ───────────────────────────────────────────────────────────────

CONFIG_PATH = Path(os.environ.get("FM_DEVICES_CONFIG", "/app/config/devices.json"))

# ── GATT service UUIDs used for capability detection ─────────────────────────

_SVC_FTMS          = "00001826-0000-1000-8000-00805f9b34fb"
_SVC_HEART_RATE    = "0000180d-0000-1000-8000-00805f9b34fb"
_SVC_CYCLING_POWER = "00001818-0000-1000-8000-00805f9b34fb"
_SVC_CSC           = "00001816-0000-1000-8000-00805f9b34fb"

# Cycling Power Feature (0x2A65) — bit 3: Pedal Power Balance Supported
_CHAR_CYCLING_POWER_FEATURE = "00002a65-0000-1000-8000-00805f9b34fb"

# ── Role → address seed (env-var fallback) ────────────────────────────────────

_ENV_SEEDS = [
    ("treadmill",   "TREADMILL_BLE_ADDR"),
    ("trainer",     "TRAINER_BLE_ADDR"),
    ("hrm",         "HRM_BLE_ADDR"),
    ("power_meter", "POWER_METER_BLE_ADDR"),
]

VALID_ROLES = {"trainer", "treadmill", "hrm", "power_meter"}


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class PairedDevice:
    role:         str
    addr:         str
    name:         str         = ""
    capabilities: list        = field(default_factory=list)
    bilateral:    Optional[bool] = None   # None = unknown (not yet detected)
    paired_at:    str         = ""


# ── Registry ──────────────────────────────────────────────────────────────────

class DeviceRegistry:
    """Persistent role → device mapping backed by a JSON file.

    Lifecycle
    ---------
    1. Load existing assignments from CONFIG_PATH on startup.
    2. Seed from env vars for any role not already assigned.
    3. `assign()` / `forget()` mutate state and flush to disk immediately.
    """

    def __init__(self, config_path: Path = CONFIG_PATH):
        self._path = config_path
        self._devices: dict[str, PairedDevice] = {}
        self._load()
        self._seed_from_env()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open() as f:
                data = json.load(f)
            for role, info in data.items():
                self._devices[role] = PairedDevice(**info)
            log.info("Loaded %d paired device(s) from %s", len(self._devices), self._path)
        except Exception as e:
            log.warning("Could not load device config %s: %s", self._path, e)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w") as f:
                json.dump({k: asdict(v) for k, v in self._devices.items()}, f, indent=2)
        except Exception as e:
            log.error("Could not save device config: %s", e)

    def _seed_from_env(self) -> None:
        """Pre-populate from env vars for any role that has no saved assignment."""
        changed = False
        for role, env_key in _ENV_SEEDS:
            addr = os.environ.get(env_key, "").strip()
            if addr and role not in self._devices:
                self._devices[role] = PairedDevice(
                    role=role, addr=addr, name=env_key, paired_at="env"
                )
                changed = True
                log.info("Seeded %s → %s (from %s)", role, addr, env_key)
        if changed:
            self._save()

    # ── Public API ───────────────────────────────────────────────────────────

    def assign(self, role: str, addr: str, name: str = "",
               caps: Optional[list] = None, bilateral: Optional[bool] = None) -> None:
        if role not in VALID_ROLES:
            raise ValueError(f"Unknown role '{role}'. Valid: {sorted(VALID_ROLES)}")
        self._devices[role] = PairedDevice(
            role=role, addr=addr, name=name or addr,
            capabilities=caps or [],
            bilateral=bilateral,
            paired_at=datetime.now(timezone.utc).isoformat(),
        )
        self._save()
        log.info("Paired %s → %s (%s)", role, addr, name or addr)

    def update_bilateral(self, role: str, bilateral: bool) -> None:
        """Cache the bilateral detection result after first connection."""
        d = self._devices.get(role)
        if d and d.bilateral != bilateral:
            d.bilateral = bilateral
            self._save()
            log.info("Detected %s power meter as %s",
                     role, "bilateral" if bilateral else "single-sided (×2)")

    def forget(self, role: str) -> bool:
        if role in self._devices:
            del self._devices[role]
            self._save()
            log.info("Unpaired %s", role)
            return True
        return False

    def get(self, role: str) -> Optional[PairedDevice]:
        return self._devices.get(role)

    def addr(self, role: str) -> Optional[str]:
        d = self._devices.get(role)
        return d.addr if d else None

    def all_paired(self) -> dict:
        return {k: asdict(v) for k, v in self._devices.items()}


# ── Scanner ───────────────────────────────────────────────────────────────────

def _caps_from_services(service_uuids: list[str]) -> list[str]:
    svcs = {u.lower() for u in service_uuids}
    caps = []
    if _SVC_FTMS          in svcs: caps.append("ftms")
    if _SVC_HEART_RATE    in svcs: caps.append("hrm")
    if _SVC_CYCLING_POWER in svcs: caps.append("cycling_power")
    if _SVC_CSC           in svcs: caps.append("csc")
    return caps


def _suggest_role(caps: list[str]) -> Optional[str]:
    if "ftms" in caps and "cycling_power" in caps:
        return "trainer"
    if "ftms" in caps:
        return "treadmill"
    if "hrm" in caps:
        return "hrm"
    if "cycling_power" in caps:
        return "power_meter"
    return None


async def scan_devices(timeout: float = 12.0) -> list[dict]:
    """Scan for nearby BLE devices and return those with recognised capabilities.

    Each entry:
      addr, name, rssi, capabilities, suggested_role
    Sorted by RSSI descending (strongest first).
    """
    log.info("BLE scan starting (%.0fs)…", timeout)
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)

    results = []
    for addr, (device, adv) in discovered.items():
        caps = _caps_from_services(list(adv.service_uuids))
        if not caps and not device.name:
            continue
        role = _suggest_role(caps)
        results.append({
            "addr":           addr,
            "name":           device.name or "(unknown)",
            "rssi":           adv.rssi,
            "capabilities":   caps,
            "suggested_role": role,
        })

    results.sort(key=lambda x: x["rssi"], reverse=True)
    log.info("Scan complete — %d identifiable device(s) found", len(results))
    return results


async def detect_bilateral(addr: str, timeout: float = 8.0) -> bool:
    """Connect to a power meter and read Cycling Power Feature (0x2A65).

    Returns True if the device reports Pedal Power Balance Supported (bit 3),
    meaning it measures both sides and reports total power (no doubling needed).
    Returns False for single-sided meters (reported power must be doubled).
    """
    try:
        async with BleakClient(addr, timeout=timeout) as client:
            feat = await client.read_gatt_char(_CHAR_CYCLING_POWER_FEATURE)
            flags = int.from_bytes(feat[:4], "little")
            bilateral = bool(flags & (1 << 3))
            log.info("Power meter %s: Pedal Power Balance Supported = %s → %s",
                     addr, bilateral, "bilateral" if bilateral else "single-sided")
            return bilateral
    except Exception as e:
        log.warning("Could not detect bilateral for %s: %s — defaulting to single-sided", addr, e)
        return False


# Module-level singleton — imported by ble.py, engine.py, server.py
registry = DeviceRegistry()
