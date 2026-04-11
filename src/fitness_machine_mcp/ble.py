"""BLE manager — FTMS treadmill + FTMS cycling trainer (ERG) + HRM-Pro+ (HR + RR + RSC + CSC) + pedal power meter."""
from __future__ import annotations
import asyncio
import logging
import os
import random
import struct
from dataclasses import dataclass, field
from typing import Callable, Optional
from bleak import BleakClient

log = logging.getLogger("fitness_machine.ble")

# Device addresses are resolved at connect-time from the module-level registry.
# Importing here avoids a circular import (devices.py does not import ble.py).
from fitness_machine_mcp.devices import registry  # noqa: E402

# Reconnect loop: base delay (seconds) and ceiling for exponential backoff.
_PM_RETRY_BASE_S      = 5
_TRAINER_RETRY_BASE_S = 3
_HRM_RETRY_BASE_S     = 8
_RETRY_MAX_S          = 30

# FTMS UUIDs (shared by treadmill and trainer)
TREADMILL_DATA   = "00002acd-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA = "00002ad2-0000-1000-8000-00805f9b34fb"
MACHINE_STATUS   = "00002ada-0000-1000-8000-00805f9b34fb"
CONTROL_POINT    = "00002ad9-0000-1000-8000-00805f9b34fb"

# HRM-Pro+ UUIDs
HR_MEASUREMENT   = "00002a37-0000-1000-8000-00805f9b34fb"
RSC_MEASUREMENT  = "00002a53-0000-1000-8000-00805f9b34fb"  # Running Speed & Cadence
CSC_MEASUREMENT  = "00002a5b-0000-1000-8000-00805f9b34fb"  # Cycling Speed & Cadence

# Cycling Power Service (pedal power meter)
CYCLING_POWER_MEAS = "00002a63-0000-1000-8000-00805f9b34fb"

# FTMS control opcodes
OP_REQUEST_CONTROL  = 0x00
OP_SET_TARGET_POWER = 0x05   # uint16, watts (trainer ERG)
OP_START_RESUME     = 0x07
OP_STOP             = 0x08   # param 0x01=stop


@dataclass
class LiveData:
    """Latest sensor snapshot from all connected BLE devices.

    Data source priority for cycling:
      power   : power_w_pm (pedal meter) > power_w (trainer ERG measured)
      cadence : cadence_rpm_pm (pedal meter) > cadence_rpm_csc (HRM-Pro+ CSC) > cadence_rpm (trainer)
      HR      : hr_bpm from HRM-Pro+; trainer HR is backup only
      HRV     : rr_intervals_ms from HRM-Pro+ HR characteristic (RR flag)
    """
    # Shared / treadmill
    speed_kmh:       float = 0.0
    incline_pct:     float = 0.0
    distance_m:      float = 0.0
    elapsed_sec:     int   = 0
    hr_bpm:          int   = 0
    # Running (RSC from HRM-Pro+)
    cadence_spm:     int   = 0        # steps per minute
    stride_len_m:    float = 0.0
    hrm_speed_kmh:   float = 0.0     # RSC speed cross-check
    # Trainer (FTMS Indoor Bike)
    power_w:         int   = 0        # trainer measured/reported power
    cadence_rpm:     int   = 0        # trainer internal cadence
    # HRM-Pro+ extended
    rr_intervals_ms: list  = field(default_factory=list)  # RR intervals (ms) — for HRV
    cadence_rpm_csc: int   = 0        # HRM-Pro+ CSC cadence (cycling fallback)
    # Pedal power meter (primary source when connected)
    power_w_pm:       int   = 0       # single-side × 2 total estimate (W)
    cadence_rpm_pm:   int   = 0       # crank revolution cadence (most accurate)
    pedal_balance_pct: float = 0.0    # left-side power % (0.0 if not reported)


class CrankCounter:
    """Compute cadence (rpm) from cumulative crank-revolution BLE notifications.

    Both CSC (0x2A5B) and Cycling Power (0x2A63) report crank data as:
      cumulative_revolutions: uint16 (rolls over at 65536)
      last_event_time:        uint16 in 1/1024 s units (rolls over at 64s)

    Two consecutive notifications are required before cadence is available.
    """
    def __init__(self):
        self._prev_count: Optional[int] = None
        self._prev_time:  Optional[int] = None

    def update(self, count: int, event_time: int) -> Optional[int]:
        """Return cadence in rpm, or None if this is the first notification."""
        if self._prev_count is None:
            self._prev_count, self._prev_time = count, event_time
            return None
        count_diff = (count      - self._prev_count) & 0xFFFF
        time_diff  = (event_time - self._prev_time)  & 0xFFFF
        self._prev_count, self._prev_time = count, event_time
        if time_diff == 0:
            return None
        return int(count_diff * 1024 * 60 / time_diff)


# ── BLE parsers ───────────────────────────────────────────────────────────────

def _parse_treadmill(data: bytes) -> dict:
    """Parse FTMS Treadmill Data characteristic (0x2ACD).

    Flag bits: 0=InstSpeed 1=AvgSpeed 2=TotalDist 3=Inclination+Ramp
               4=ElevGain 5=Pace 6=AvgPace 7=ExpendedEnergy
               8=HeartRate 9=MetabolicEq 10=ElapsedTime 11=RemainingTime
    """
    flags = struct.unpack_from("<H", data, 0)[0]
    off = 2
    out: dict = {}
    if not (flags & 0x01):                           # instantaneous speed (present when bit=0)
        out["speed_kmh"] = struct.unpack_from("<H", data, off)[0] * 0.01; off += 2
    if flags & 0x02: off += 2                        # average speed
    if flags & 0x04:                                 # total distance (3 bytes)
        raw = struct.unpack_from("<I", data, off)[0] & 0xFFFFFF; off += 3
        out["session_distance_m"] = raw
    if flags & 0x08:                                 # inclination + ramp angle
        out["incline_pct"] = struct.unpack_from("<h", data, off)[0] * 0.1; off += 2
        off += 2
    if flags & 0x10: off += 4                        # elevation gain
    if flags & 0x20: off += 1                        # pace
    if flags & 0x40: off += 1                        # average pace
    if flags & 0x80: off += 5                        # expended energy
    if flags & 0x100:
        out["hr_bpm"] = data[off]; off += 1
    if flags & 0x200: off += 1                       # metabolic equivalent
    if flags & 0x400:
        out["elapsed_sec"] = struct.unpack_from("<H", data, off)[0]
    return out


def _parse_hr(data: bytes) -> dict:
    """Parse Heart Rate Measurement (0x2A37), including RR intervals.

    Flags byte:
      bit 0 — HR format (0=uint8, 1=uint16)
      bit 1 — Sensor Contact Status
      bit 2 — Sensor Contact Support
      bit 3 — Energy Expended present
      bit 4 — RR-Interval present
    """
    flags = data[0]
    hr_16bit = bool(flags & 0x01)
    hr = struct.unpack_from("<H", data, 1)[0] if hr_16bit else data[1]
    out: dict = {"hr_bpm": hr}

    if flags & 0x10:                                  # RR intervals present
        off = 3 if hr_16bit else 2                    # skip 2-byte or 1-byte HR value
        rr_list = []
        while off + 1 < len(data):
            rr_raw = struct.unpack_from("<H", data, off)[0]
            rr_list.append(round(rr_raw * 1000 / 1024))  # 1/1024 s → ms
            off += 2
        if rr_list:
            out["rr_intervals_ms"] = rr_list
    return out


def _parse_rsc(data: bytes) -> dict:
    """Parse Running Speed & Cadence (0x2A53)."""
    flags = data[0]
    speed_raw = struct.unpack_from("<H", data, 1)[0]  # 0.01 m/s
    cadence   = data[3]                                # rpm
    out = {
        "hrm_speed_kmh": speed_raw * 0.01 * 3.6,
        "cadence_spm":   cadence * 2,                  # rpm → spm (both feet)
    }
    if flags & 0x01:                                   # stride length present
        stride = struct.unpack_from("<H", data, 4)[0]  # 0.01 m
        out["stride_len_m"] = stride * 0.01
    return out


def _parse_csc(data: bytes, crank: CrankCounter) -> dict:
    """Parse Cycling Speed & Cadence (0x2A5B) — crank revolution data only.

    Flag bits: 0=WheelRevolutionData 1=CrankRevolutionData
    """
    flags = data[0]
    off   = 1
    out: dict = {}
    if flags & 0x01: off += 6                          # skip Wheel Revolution Data (uint32+uint16)
    if flags & 0x02:                                   # Crank Revolution Data
        count = struct.unpack_from("<H", data, off)[0]; off += 2
        t     = struct.unpack_from("<H", data, off)[0]
        rpm = crank.update(count, t)
        if rpm is not None:
            out["cadence_rpm_csc"] = rpm
    return out


def _parse_indoor_bike(data: bytes) -> dict:
    """Parse FTMS Indoor Bike Data (0x2AD2).

    Flag bits: 0=MoreData 1=AvgSpeed 2=InstCadence 3=AvgCadence
               4=TotalDist 5=ResistanceLevel 6=InstPower 7=AvgPower
               8=ExpendedEnergy 9=HeartRate 10=MetabolicEq 11=ElapsedTime 12=RemainingTime
    """
    flags = struct.unpack_from("<H", data, 0)[0]
    off = 2
    out: dict = {}
    out["speed_kmh"] = struct.unpack_from("<H", data, off)[0] * 0.01; off += 2
    if flags & 0x02: off += 2                          # average speed
    if flags & 0x04:
        out["cadence_rpm"] = struct.unpack_from("<H", data, off)[0] // 2; off += 2  # 0.5 rpm units
    if flags & 0x08: off += 2                          # average cadence
    if flags & 0x10:                                   # total distance (3 bytes)
        raw = struct.unpack_from("<I", data, off)[0] & 0xFFFFFF; off += 3
        out["session_distance_m"] = float(raw)
    if flags & 0x20: off += 2                          # resistance level (sint16)
    if flags & 0x40:
        out["power_w"] = struct.unpack_from("<h", data, off)[0]; off += 2  # instantaneous power
    if flags & 0x80: off += 2                          # average power
    if flags & 0x100: off += 5                         # expended energy
    if flags & 0x200:
        out["hr_bpm"] = data[off]
    return out


def _parse_cycling_power(data: bytes, crank: CrankCounter,
                         bilateral_override: Optional[bool] = None) -> dict:
    """Parse Cycling Power Measurement (0x2A63).

    Always present: instantaneous power (sint16, W).
    For single-sided meters (left or right pedal), power is reported per-side;
    we double it to estimate total bilateral power.

    Flag bits relevant here:
      0  — Pedal Power Balance present (uint8, 0.5 % units)
      2  — Accumulated Torque present (uint16)
      4  — Wheel Revolution Data present (uint32 + uint16)
      5  — Crank Revolution Data present (uint16 + uint16) ← cadence source

    bilateral_override: if set from the device registry, takes precedence over
    per-packet flag detection. Assioma Duo (and other bilateral meters) sometimes
    send packets without the balance field (bit 0 = 0) even though the power
    value is already total — using the registry prevents incorrect ×2 doubling.
    """
    flags = struct.unpack_from("<H", data, 0)[0]
    off   = 2
    out: dict = {}

    power_raw = struct.unpack_from("<h", data, off)[0]; off += 2  # sint16
    # Use registry bilateral flag when known; fall back to per-packet bit 0 detection.
    # Per-packet bit 0 only indicates whether the balance *field* is present in this
    # specific packet — bilateral meters may omit it without changing their power format.
    packet_bilateral = bool(flags & 0x01)
    bilateral = bilateral_override if bilateral_override is not None else packet_bilateral
    out["power_w_pm"] = power_raw if bilateral else power_raw * 2
    out["_bilateral"] = packet_bilateral  # report what the packet said for registry learning

    if flags & 0x01:                     # Pedal Power Balance (uint8, 0.5% units = left%)
        balance_raw = data[off]; off += 1
        out["pedal_balance_pct"] = round(balance_raw / 2.0, 1)
    if flags & 0x04: off += 2           # Accumulated Torque (uint16)
    if flags & 0x10: off += 6           # Wheel Revolution Data (uint32 + uint16)
    if flags & 0x20:                    # Crank Revolution Data — best cadence source
        count = struct.unpack_from("<H", data, off)[0]; off += 2
        t     = struct.unpack_from("<H", data, off)[0]
        rpm = crank.update(count, t)
        if rpm is not None:
            out["cadence_rpm_pm"] = rpm

    return out


# ── BLE Managers ─────────────────────────────────────────────────────────────

class BLEManager:
    """BLE manager for FTMS treadmill (monitor-only) + HRM-Pro+ (HR + RR + RSC).

    The treadmill is NOT controlled — speed and incline are set by the user
    on the machine panel. This manager only reads sensor data and provides
    emergency stop capability.
    """

    def __init__(self, on_data: Callable[[LiveData], None]):
        self._on_data = on_data
        self._live = LiveData()
        self._treadmill: Optional[BleakClient] = None
        self._hrm: Optional[BleakClient] = None
        self._ctrl_lock = asyncio.Lock()   # used only by emergency stop
        self._ble_connect_lock = asyncio.Lock()
        self._hrm_reconnect_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        treadmill_addr = registry.addr("treadmill")
        if not treadmill_addr:
            raise RuntimeError("No treadmill address configured — use pair_device to set one")
        log.info("Connecting to treadmill %s…", treadmill_addr)
        self._treadmill = BleakClient(treadmill_addr, timeout=12.0,
                                      disconnected_callback=self._on_treadmill_disconnect)
        await self._treadmill.connect()

        services = self._treadmill.services
        char_uuids = [str(c.uuid) for s in services for c in s.characteristics]
        log.info("Discovered %d characteristics", len(char_uuids))

        if TREADMILL_DATA not in char_uuids:
            await self._treadmill.disconnect()
            raise RuntimeError(
                f"FTMS Treadmill Data characteristic not found — "
                f"GATT may not be established. Found: {char_uuids or 'none'}"
            )

        await self._treadmill.start_notify(TREADMILL_DATA, self._on_treadmill_data)
        await self._treadmill.start_notify(MACHINE_STATUS, self._on_machine_status)
        log.info("Treadmill subscribed (monitor-only)")

        hrm_addr = registry.addr("hrm")
        if hrm_addr:
            log.info("Connecting to HRM %s…", hrm_addr)
            try:
                await self._connect_hrm(hrm_addr)
                log.info("HRM connected (HR + RR intervals)")
            except Exception as e:
                log.warning("HRM initial connect failed (%s): %s — reconnect loop active", hrm_addr, e)
            self._hrm_reconnect_task = asyncio.create_task(self._hrm_reconnect_loop())
        else:
            log.info("No HRM configured — HR from treadmill sensor only")

    async def disconnect(self) -> None:
        if self._hrm_reconnect_task and not self._hrm_reconnect_task.done():
            self._hrm_reconnect_task.cancel()
            try:
                await self._hrm_reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
        for client in (self._hrm, self._treadmill):
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    def _on_hrm_disconnect(self, _client):
        log.warning("HRM disconnected — reconnect loop will retry in %ds", _HRM_RETRY_BASE_S)
        self._hrm = None
        self._live.hr_bpm = 0
        self._live.rr_intervals_ms = []

    async def _connect_hrm(self, addr: str) -> None:
        """Connect HRM and subscribe to HR + RR + optional RSC. Raises on failure."""
        async with self._ble_connect_lock:
            client = BleakClient(addr, timeout=12.0,
                                 disconnected_callback=self._on_hrm_disconnect)
            await client.connect()
        await client.start_notify(HR_MEASUREMENT, self._on_hr)
        try:
            await client.start_notify(RSC_MEASUREMENT, self._on_rsc)
            log.info("HRM RSC (cadence + stride) subscribed")
        except Exception:
            log.warning("RSC not available — HR + RR intervals only")
        self._hrm = client

    async def _hrm_reconnect_loop(self) -> None:
        """Background task: reconnect HRM whenever it is not connected."""
        backoff = _HRM_RETRY_BASE_S
        while True:
            await asyncio.sleep(backoff + random.uniform(0, 1))
            if self._hrm and self._hrm.is_connected:
                backoff = _HRM_RETRY_BASE_S
                continue
            hrm_addr = registry.addr("hrm")
            if not hrm_addr:
                continue
            log.info("HRM not connected — attempting %s…", hrm_addr)
            try:
                if self._hrm:
                    try:
                        await self._hrm.disconnect()
                    except Exception:
                        pass
                    self._hrm = None
                await self._connect_hrm(hrm_addr)
                log.info("HRM reconnected")
                backoff = _HRM_RETRY_BASE_S
            except asyncio.CancelledError:
                raise
            except Exception as e:
                backoff = min(backoff * 2, _RETRY_MAX_S)
                log.debug("HRM reconnect failed (next in %.0fs): %s", backoff, e)

    def _on_treadmill_disconnect(self, _client):
        log.warning("Treadmill disconnected unexpectedly")

    def _on_treadmill_data(self, _c, data: bytes):
        try:
            parsed = _parse_treadmill(data)
        except Exception as e:
            log.warning("Malformed treadmill data (%d bytes): %s", len(data), e)
            return
        if "speed_kmh"   in parsed: self._live.speed_kmh   = parsed["speed_kmh"]
        if "incline_pct" in parsed: self._live.incline_pct = parsed["incline_pct"]
        if "elapsed_sec" in parsed: self._live.elapsed_sec = parsed["elapsed_sec"]
        if "hr_bpm"      in parsed and parsed["hr_bpm"] > 0:
            self._live.hr_bpm = parsed["hr_bpm"]
        self._on_data(self._live)

    def _on_machine_status(self, _c, data: bytes):
        if not data:
            return
        code = data[0]
        codes = {0x02: "Stopped(SafetyKey)", 0x03: "Stopped/Paused(User)",
                 0x04: "Started/Resumed",   0x05: "SpeedChanged",
                 0x06: "InclineChanged",    0x0A: "MachineStopped"}
        log.info("Treadmill status: %s", codes.get(code, hex(code)))

    def _on_hr(self, _c, data: bytes):
        try:
            parsed = _parse_hr(data)
        except Exception as e:
            log.warning("Malformed HR data (%d bytes): %s", len(data), e)
            return
        self._live.hr_bpm = parsed["hr_bpm"]
        if "rr_intervals_ms" in parsed:
            self._live.rr_intervals_ms = parsed["rr_intervals_ms"]
        self._on_data(self._live)

    def _on_rsc(self, _c, data: bytes):
        try:
            parsed = _parse_rsc(data)
        except Exception as e:
            log.warning("Malformed RSC data (%d bytes): %s", len(data), e)
            return
        self._live.cadence_spm   = parsed.get("cadence_spm",   self._live.cadence_spm)
        self._live.stride_len_m  = parsed.get("stride_len_m",  self._live.stride_len_m)
        self._live.hrm_speed_kmh = parsed.get("hrm_speed_kmh", self._live.hrm_speed_kmh)
        self._on_data(self._live)

    async def _send(self, opcode: int, param: bytes = b"") -> bool:
        if not self._treadmill or not self._treadmill.is_connected:
            raise RuntimeError("Treadmill not connected")
        cmd = bytes([opcode]) + param
        async with self._ctrl_lock:
            response: asyncio.Future = asyncio.get_running_loop().create_future()
            subscribed = False
            def on_indicate(_c, data):
                if not response.done():
                    response.set_result(data)
            try:
                await self._treadmill.start_notify(CONTROL_POINT, on_indicate)
                subscribed = True
                await self._treadmill.write_gatt_char(CONTROL_POINT, cmd, response=True)
                resp = await asyncio.wait_for(response, timeout=5.0)
                ok = len(resp) >= 3 and resp[0] == 0x80 and resp[2] == 0x01
                if not ok:
                    log.warning("Control response opcode=%s: %s", hex(opcode), resp.hex())
                return ok
            except asyncio.TimeoutError:
                log.warning("Control point timeout for opcode %s", hex(opcode))
                return False
            finally:
                if subscribed:
                    try:
                        await self._treadmill.stop_notify(CONTROL_POINT)
                    except Exception:
                        pass

    async def stop(self) -> bool:
        """Emergency stop — send FTMS STOP command to treadmill."""
        return await self._send(OP_STOP, bytes([0x01]))

    @property
    def live(self) -> LiveData:
        return self._live


class TrainerBLEManager:
    """BLE manager for eHuntSun T-SoX trainer (FTMS ERG) + HRM-Pro+ (HR + RR + CSC) + optional pedal power meter.

    Device priority for cycling data:
      power   : pedal power meter (_pm) > trainer FTMS (power_w)
      cadence : pedal power meter (_pm) > HRM-Pro+ CSC > trainer FTMS
      HR/HRV  : HRM-Pro+ (HR + RR intervals)
    """

    def __init__(self, on_data: Callable[[LiveData], None]):
        self._on_data = on_data
        self._live = LiveData()
        self._trainer: Optional[BleakClient] = None
        self._hrm: Optional[BleakClient] = None
        self._pm: Optional[BleakClient] = None     # pedal power meter
        self._pm_task: Optional[asyncio.Task] = None
        self._trainer_task: Optional[asyncio.Task] = None
        self._hrm_task: Optional[asyncio.Task] = None
        self._current_target_power: int = 0        # track for re-send after reconnect
        self._ctrl_lock = asyncio.Lock()
        # Serialise all BLE connect() calls — BlueZ rejects concurrent connection attempts
        self._ble_connect_lock = asyncio.Lock()
        self._csc_crank = CrankCounter()           # HRM-Pro+ CSC cadence
        self._pm_crank  = CrankCounter()           # pedal power meter cadence
        # Persistent CONTROL_POINT subscription: avoids subscribe/unsubscribe per command.
        # Indications are routed here; _send() drains and awaits responses.
        self._cp_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
        self._cp_subscribed: bool = False

    async def connect(self) -> None:
        # ── Trainer ──────────────────────────────────────────────────────────
        trainer_addr = registry.addr("trainer")
        if not trainer_addr:
            raise RuntimeError("No trainer address configured — use pair_device to set one")
        log.info("Connecting to trainer %s…", trainer_addr)
        _trainer_max_attempts = 5
        for _attempt in range(1, _trainer_max_attempts + 1):
            try:
                async with self._ble_connect_lock:
                    self._trainer = BleakClient(trainer_addr, timeout=12.0,
                                                disconnected_callback=self._on_trainer_disconnect)
                    await self._trainer.connect()
                await self._trainer.start_notify(INDOOR_BIKE_DATA, self._on_bike_data)
                await self._trainer.start_notify(MACHINE_STATUS,   self._on_machine_status)
                await self._subscribe_control_point()
                break
            except Exception as e:
                log.warning("Trainer connect attempt %d/%d failed: %s",
                            _attempt, _trainer_max_attempts, e)
                if _attempt == _trainer_max_attempts:
                    raise RuntimeError(
                        f"Could not connect to trainer after {_trainer_max_attempts} attempts: {e}"
                    ) from e
                await asyncio.sleep(3.0)
        self._trainer_task = asyncio.create_task(self._trainer_reconnect_loop())
        log.info("Trainer connected and subscribed")

        # ── HRM (optional) ───────────────────────────────────────────────────
        hrm_addr = registry.addr("hrm")
        if hrm_addr:
            log.info("Connecting to HRM %s…", hrm_addr)
            try:
                await self._connect_hrm(hrm_addr)
            except Exception as e:
                log.warning("HRM initial connect failed (%s): %s — reconnect loop active", hrm_addr, e)
                self._hrm = None
            self._hrm_task = asyncio.create_task(self._hrm_reconnect_loop())
        else:
            log.info("No HRM configured — HR will come from trainer if available")

        # ── Pedal power meter (optional, background reconnect loop) ──────────
        pm_addr = registry.addr("power_meter")
        if pm_addr:
            pm = registry.get("power_meter")
            log.info("Starting pedal power meter reconnect loop (addr=%s bilateral=%s retry=%ds)…",
                     pm_addr, pm.bilateral, _PM_RETRY_BASE_S)
            self._pm_task = asyncio.create_task(self._pm_connect_loop())
        else:
            log.info("No power meter configured — power from trainer only")

    async def disconnect(self) -> None:
        for task in (self._pm_task, self._trainer_task, self._hrm_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for client in (self._pm, self._hrm, self._trainer):
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    def _on_trainer_disconnect(self, _client):
        self._cp_subscribed = False
        log.warning("Trainer disconnected unexpectedly — reconnect loop active")

    async def _subscribe_control_point(self) -> None:
        """Subscribe to CONTROL_POINT once; indications routed to _cp_queue."""
        if self._cp_subscribed:
            return
        await self._trainer.start_notify(CONTROL_POINT, self._on_cp_indicate)
        self._cp_subscribed = True
        log.debug("CONTROL_POINT persistent subscription active")

    def _on_cp_indicate(self, _c, data: bytes) -> None:
        try:
            self._cp_queue.put_nowait(data)
        except asyncio.QueueFull:
            log.warning("CP indication queue full — dropping stale response")

    async def _trainer_reconnect_loop(self):
        """Background task: reconnect trainer on unexpected disconnect and restore ERG state."""
        backoff = _TRAINER_RETRY_BASE_S
        while True:
            await asyncio.sleep(backoff + random.uniform(0, 1))

            if self._trainer and self._trainer.is_connected:
                backoff = _TRAINER_RETRY_BASE_S  # reset on healthy connection
                continue

            trainer_addr = registry.addr("trainer")
            if not trainer_addr:
                continue

            log.info("Trainer not connected — attempting reconnect %s…", trainer_addr)
            try:
                if self._trainer:
                    try:
                        await self._trainer.disconnect()
                    except Exception:
                        pass

                async with self._ble_connect_lock:
                    client = BleakClient(trainer_addr, timeout=12.0,
                                         disconnected_callback=self._on_trainer_disconnect)
                    await client.connect()
                await client.start_notify(INDOOR_BIKE_DATA, self._on_bike_data)
                await client.start_notify(MACHINE_STATUS,   self._on_machine_status)
                self._trainer = client
                await self._subscribe_control_point()
                log.info("Trainer reconnected successfully")
                backoff = _TRAINER_RETRY_BASE_S  # reset backoff on success

                # Restore FTMS control and ERG target with retry
                for _attempt in range(5):
                    try:
                        await self.request_control()
                        await self.start()
                        break
                    except Exception as e:
                        log.warning("ERG restore request_control attempt %d/5: %s", _attempt + 1, e)
                        await asyncio.sleep(1.0)

                if self._current_target_power > 0:
                    for _attempt in range(5):
                        try:
                            param = struct.pack("<H", self._current_target_power)
                            await self._send(OP_SET_TARGET_POWER, param)
                            log.info("Trainer ERG target restored to %dW", self._current_target_power)
                            break
                        except Exception as e:
                            log.warning("ERG restore set_power attempt %d/5: %s", _attempt + 1, e)
                            await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                backoff = min(backoff * 2, _RETRY_MAX_S)
                log.warning("Trainer reconnect failed (next in %.0fs): %s", backoff, e)

    def _on_hrm_disconnect(self, _client):
        log.warning("HRM-Pro+ disconnected — reconnect loop will retry in %ds", _HRM_RETRY_BASE_S)
        self._hrm = None
        self._live.hr_bpm = 0

    async def _connect_hrm(self, addr: str) -> None:
        """Connect HRM and subscribe to HR + optional CSC. Raises on failure."""
        async with self._ble_connect_lock:
            client = BleakClient(addr, timeout=12.0,
                                 disconnected_callback=self._on_hrm_disconnect)
            await client.connect()
        await client.start_notify(HR_MEASUREMENT, self._on_hr)
        log.info("HRM HR + RR connected")
        try:
            await client.start_notify(CSC_MEASUREMENT, self._on_csc)
            log.info("HRM CSC (cycling cadence fallback) subscribed")
        except Exception:
            log.warning("HRM CSC not available — cadence from trainer / power meter only")
        self._hrm = client

    async def _hrm_reconnect_loop(self) -> None:
        """Background task: reconnect HRM whenever it is not connected."""
        backoff = _HRM_RETRY_BASE_S
        while True:
            await asyncio.sleep(backoff + random.uniform(0, 1))
            if self._hrm and self._hrm.is_connected:
                backoff = _HRM_RETRY_BASE_S
                continue
            hrm_addr = registry.addr("hrm")
            if not hrm_addr:
                continue
            log.info("HRM not connected — attempting %s…", hrm_addr)
            try:
                if self._hrm:
                    try:
                        await self._hrm.disconnect()
                    except Exception:
                        pass
                    self._hrm = None
                    self._csc_crank = CrankCounter()
                await self._connect_hrm(hrm_addr)
                log.info("HRM reconnected")
                backoff = _HRM_RETRY_BASE_S
            except asyncio.CancelledError:
                raise
            except Exception as e:
                backoff = min(backoff * 2, _RETRY_MAX_S)
                log.debug("HRM reconnect failed (next in %.0fs): %s", backoff, e)

    def _on_pm_disconnect(self, _client):
        log.warning("Pedal power meter disconnected — reconnect loop will retry in %ds",
                    _PM_RETRY_BASE_S)
        self._pm = None
        # Zero out PM data so engine falls back to CSC / trainer values immediately
        self._live.power_w_pm    = 0
        self._live.cadence_rpm_pm = 0

    async def _pm_connect_loop(self):
        """Background task: attempt to connect the pedal power meter until cancelled.

        Handles both first-time connect (meter wakes on first pedal stroke)
        and mid-session reconnect after BLE drop. Uses exponential backoff.
        """
        backoff = _PM_RETRY_BASE_S
        while True:
            await asyncio.sleep(backoff + random.uniform(0, 1))

            if self._pm and self._pm.is_connected:
                backoff = _PM_RETRY_BASE_S
                continue

            pm_addr = registry.addr("power_meter")
            if not pm_addr:
                continue

            log.info("Power meter not connected — attempting %s…", pm_addr)
            if self._pm:
                try:
                    await self._pm.disconnect()
                except Exception:
                    pass
                self._pm = None
                self._pm_crank = CrankCounter()

            try:
                async with self._ble_connect_lock:
                    client = BleakClient(pm_addr, timeout=10.0,
                                         disconnected_callback=self._on_pm_disconnect)
                    await client.connect()
                await client.start_notify(CYCLING_POWER_MEAS, self._on_pm_data)
                self._pm = client
                pm = registry.get("power_meter")
                log.info("Power meter connected (bilateral cached=%s)", pm.bilateral if pm else None)
                backoff = _PM_RETRY_BASE_S
            except asyncio.CancelledError:
                raise
            except Exception as e:
                backoff = min(backoff * 2, _RETRY_MAX_S)
                log.debug("Power meter connect failed (next in %.0fs): %s", backoff, e)

    # ── Data callbacks ───────────────────────────────────────────────────────

    def _on_bike_data(self, _c, data: bytes):
        try:
            parsed = _parse_indoor_bike(data)
        except Exception as e:
            log.warning("Malformed indoor bike data (%d bytes): %s", len(data), e)
            return
        if "speed_kmh"          in parsed: self._live.speed_kmh   = parsed["speed_kmh"]
        if "cadence_rpm"        in parsed: self._live.cadence_rpm  = parsed["cadence_rpm"]
        if "power_w"            in parsed: self._live.power_w      = parsed["power_w"]
        if "session_distance_m" in parsed: self._live.distance_m   = parsed["session_distance_m"]
        if "hr_bpm"             in parsed and parsed["hr_bpm"] > 0:
            self._live.hr_bpm = parsed["hr_bpm"]
        self._on_data(self._live)

    def _on_machine_status(self, _c, data: bytes):
        if data:
            log.info("Trainer machine status: 0x%02x", data[0])

    def _on_hr(self, _c, data: bytes):
        """HRM-Pro+ heart rate + RR intervals."""
        try:
            parsed = _parse_hr(data)
        except Exception as e:
            log.warning("Malformed HR data (%d bytes): %s", len(data), e)
            return
        self._live.hr_bpm = parsed["hr_bpm"]
        if "rr_intervals_ms" in parsed:
            self._live.rr_intervals_ms = parsed["rr_intervals_ms"]
        self._on_data(self._live)

    def _on_csc(self, _c, data: bytes):
        """HRM-Pro+ Cycling Speed & Cadence — cadence fallback when no power meter."""
        try:
            parsed = _parse_csc(data, self._csc_crank)
        except Exception as e:
            log.warning("Malformed CSC data (%d bytes): %s", len(data), e)
            return
        if "cadence_rpm_csc" in parsed:
            self._live.cadence_rpm_csc = parsed["cadence_rpm_csc"]
            self._on_data(self._live)

    def _on_pm_data(self, _c, data: bytes):
        """Pedal power meter — primary power and cadence source for cycling."""
        pm = registry.get("power_meter")
        try:
            parsed = _parse_cycling_power(data, self._pm_crank,
                                          bilateral_override=pm.bilateral if pm else None)
        except Exception as e:
            log.warning("Malformed cycling power data (%d bytes): %s", len(data), e)
            return
        # Persist bilateral detection on first notification so future sessions
        # can log it correctly (the actual power calculation is always in the parser).
        if "_bilateral" in parsed and pm and pm.bilateral is None:
            registry.update_bilateral("power_meter", parsed["_bilateral"])
        if "power_w_pm"          in parsed: self._live.power_w_pm         = parsed["power_w_pm"]
        if "cadence_rpm_pm"      in parsed: self._live.cadence_rpm_pm      = parsed["cadence_rpm_pm"]
        if "pedal_balance_pct"   in parsed: self._live.pedal_balance_pct   = parsed["pedal_balance_pct"]
        self._on_data(self._live)

    # ── FTMS Control ─────────────────────────────────────────────────────────

    async def _send(self, opcode: int, param: bytes = b"") -> bool:
        """Send FTMS control point command and await indication response.

        Uses a persistent CONTROL_POINT subscription established at connect time.
        Avoids the subscribe/unsubscribe-per-command pattern that can permanently
        break the control channel if stop_notify() raises after a timeout.
        """
        if not self._trainer or not self._trainer.is_connected:
            raise RuntimeError("Trainer not connected")
        cmd = bytes([opcode]) + param
        async with self._ctrl_lock:
            # Drain any stale indications from a previous failed command
            while not self._cp_queue.empty():
                self._cp_queue.get_nowait()
            try:
                await self._trainer.write_gatt_char(CONTROL_POINT, cmd, response=True)
                resp = await asyncio.wait_for(self._cp_queue.get(), timeout=5.0)
                ok = len(resp) >= 3 and resp[0] == 0x80 and resp[2] == 0x01
                if not ok:
                    log.warning("Trainer CP opcode=%s response: %s", hex(opcode), resp.hex())
                return ok
            except asyncio.TimeoutError:
                log.warning("Trainer CP timeout opcode=%s", hex(opcode))
                return False

    async def request_control(self) -> bool:
        ok = await self._send(OP_REQUEST_CONTROL)
        log.info("Trainer request control: %s", "OK" if ok else "FAIL")
        return ok

    async def start(self) -> bool:
        return await self._send(OP_START_RESUME)

    async def stop(self) -> bool:
        return await self._send(OP_STOP, bytes([0x01]))

    async def set_target_power(self, watts: int) -> bool:
        clamped = max(0, min(2000, watts))
        self._current_target_power = clamped
        param = struct.pack("<H", clamped)
        ok = await self._send(OP_SET_TARGET_POWER, param)
        if ok:
            log.info("Trainer target power → %dW OK", clamped)
        else:
            log.warning("Trainer target power FAILED for %dW", clamped)
        return ok

    @property
    def live(self) -> LiveData:
        return self._live
