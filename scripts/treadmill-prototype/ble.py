"""BLE manager — treadmill (FTMS) + HRM-Pro+ (HR + RSC)."""
from __future__ import annotations
import asyncio, struct, logging
from dataclasses import dataclass, field
from typing import Callable, Optional
from bleak import BleakClient, BleakScanner

log = logging.getLogger("ble")

TREADMILL_ADDR = "D5:33:36:33:16:29"
HRM_ADDR       = "E8:AB:98:2D:89:4C"

# FTMS UUIDs
TREADMILL_DATA  = "00002acd-0000-1000-8000-00805f9b34fb"
MACHINE_STATUS  = "00002ada-0000-1000-8000-00805f9b34fb"
CONTROL_POINT   = "00002ad9-0000-1000-8000-00805f9b34fb"

# HRM UUIDs
HR_MEASUREMENT  = "00002a37-0000-1000-8000-00805f9b34fb"
RSC_MEASUREMENT = "00002a53-0000-1000-8000-00805f9b34fb"

# FTMS control opcodes
OP_REQUEST_CONTROL = 0x00
OP_SET_SPEED       = 0x02   # uint16, 0.01 km/h
OP_SET_INCLINE     = 0x03   # sint16, 0.1 %
OP_START_RESUME    = 0x07
OP_STOP            = 0x08   # param 0x01=stop


@dataclass
class LiveData:
    """Latest snapshot from all BLE devices."""
    speed_kmh:    float = 0.0
    incline_pct:  float = 0.0
    distance_m:   float = 0.0
    elapsed_sec:  int   = 0
    hr_bpm:       int   = 0
    cadence_spm:  int   = 0        # steps per minute (from RSC)
    stride_len_m: float = 0.0
    hrm_speed_kmh: float = 0.0    # HRM-measured speed (cross-check)


def _parse_treadmill(data: bytes) -> dict:
    flags = struct.unpack_from("<H", data, 0)[0]
    off = 2
    out: dict = {}
    if not (flags & 0x01):                          # instantaneous speed
        out["speed_kmh"] = struct.unpack_from("<H", data, off)[0] * 0.01; off += 2
    if flags & 0x04:                                # total distance (3 bytes)
        raw = struct.unpack_from("<I", data, off)[0] & 0xFFFFFF; off += 3
        out["session_distance_m"] = raw             # cumulative on machine
    if flags & 0x08:                                # inclination
        out["incline_pct"] = struct.unpack_from("<h", data, off)[0] * 0.1; off += 2
        off += 2                                    # skip ramp angle
    if flags & 0x80:                                # heart rate
        out["hr_bpm"] = data[off]; off += 1
    if flags & 0x200:                               # elapsed time
        out["elapsed_sec"] = struct.unpack_from("<H", data, off)[0]
    return out


def _parse_hr(data: bytes) -> dict:
    flags = data[0]
    hr = struct.unpack_from("<H", data, 1)[0] if (flags & 0x01) else data[1]
    return {"hr_bpm": hr}


def _parse_rsc(data: bytes) -> dict:
    flags = data[0]
    speed_raw = struct.unpack_from("<H", data, 1)[0]   # 0.01 m/s
    cadence   = data[3]                                  # rpm (== steps/min for running)
    out = {
        "hrm_speed_kmh": speed_raw * 0.01 * 3.6,
        "cadence_spm":   cadence * 2,                   # rpm → spm (both feet)
    }
    if flags & 0x01:                                    # stride length present
        stride = struct.unpack_from("<H", data, 4)[0]  # 0.01 m
        out["stride_len_m"] = stride * 0.01
    return out


class BLEManager:
    def __init__(self, on_data: Callable[[LiveData], None]):
        self._on_data = on_data
        self._live = LiveData()
        self._treadmill: Optional[BleakClient] = None
        self._hrm: Optional[BleakClient] = None
        self._ctrl_lock = asyncio.Lock()

    # ── Connection ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        log.info("Connecting to treadmill %s…", TREADMILL_ADDR)
        self._treadmill = BleakClient(TREADMILL_ADDR, timeout=12.0,
                                      disconnected_callback=self._on_treadmill_disconnect)
        await self._treadmill.connect()
        await self._treadmill.start_notify(TREADMILL_DATA, self._on_treadmill_data)
        await self._treadmill.start_notify(MACHINE_STATUS, self._on_machine_status)
        log.info("Treadmill connected")

        log.info("Connecting to HRM-Pro+ %s…", HRM_ADDR)
        self._hrm = BleakClient(HRM_ADDR, timeout=12.0)
        await self._hrm.connect()
        await self._hrm.start_notify(HR_MEASUREMENT, self._on_hr)
        try:
            await self._hrm.start_notify(RSC_MEASUREMENT, self._on_rsc)
            log.info("HRM-Pro+ connected (HR + RSC)")
        except Exception:
            log.warning("RSC not available on HRM — HR only")

    async def disconnect(self) -> None:
        for client in (self._hrm, self._treadmill):
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    def _on_treadmill_disconnect(self, _client):
        log.warning("Treadmill disconnected unexpectedly")

    # ── Data callbacks ───────────────────────────────────────────────────────

    def _on_treadmill_data(self, _c, data: bytes):
        parsed = _parse_treadmill(data)
        if "speed_kmh"   in parsed: self._live.speed_kmh   = parsed["speed_kmh"]
        if "incline_pct" in parsed: self._live.incline_pct = parsed["incline_pct"]
        if "elapsed_sec" in parsed: self._live.elapsed_sec = parsed["elapsed_sec"]
        if "hr_bpm"      in parsed and parsed["hr_bpm"] > 0:
            self._live.hr_bpm = parsed["hr_bpm"]
        self._on_data(self._live)

    def _on_machine_status(self, _c, data: bytes):
        codes = {0x02: "Stopped(SafetyKey)", 0x03: "Stopped/Paused(User)",
                 0x04: "Started/Resumed",   0x05: "SpeedChanged",
                 0x06: "InclineChanged",    0x0A: "MachineStopped"}
        log.info("Machine status: %s", codes.get(data[0], hex(data[0])))

    def _on_hr(self, _c, data: bytes):
        self._live.hr_bpm = _parse_hr(data)["hr_bpm"]
        self._on_data(self._live)

    def _on_rsc(self, _c, data: bytes):
        parsed = _parse_rsc(data)
        self._live.cadence_spm   = parsed.get("cadence_spm", self._live.cadence_spm)
        self._live.stride_len_m  = parsed.get("stride_len_m", self._live.stride_len_m)
        self._live.hrm_speed_kmh = parsed.get("hrm_speed_kmh", self._live.hrm_speed_kmh)
        self._on_data(self._live)

    # ── FTMS Control ─────────────────────────────────────────────────────────

    async def _send(self, opcode: int, param: bytes = b"") -> bool:
        if not self._treadmill or not self._treadmill.is_connected:
            raise RuntimeError("Treadmill not connected")
        cmd = bytes([opcode]) + param
        async with self._ctrl_lock:
            response: asyncio.Future = asyncio.get_event_loop().create_future()
            def on_indicate(_c, data):
                if not response.done():
                    response.set_result(data)
            await self._treadmill.start_notify(CONTROL_POINT, on_indicate)
            await self._treadmill.write_gatt_char(CONTROL_POINT, cmd, response=True)
            try:
                resp = await asyncio.wait_for(response, timeout=5.0)
                ok = len(resp) >= 3 and resp[0] == 0x80 and resp[2] == 0x01
                if not ok:
                    log.warning("Control response: %s", resp.hex())
                return ok
            except asyncio.TimeoutError:
                log.warning("Control point timeout for opcode %s", hex(opcode))
                return False
            finally:
                await self._treadmill.stop_notify(CONTROL_POINT)

    async def request_control(self) -> bool:
        ok = await self._send(OP_REQUEST_CONTROL)
        log.info("Request control: %s", "OK" if ok else "FAIL")
        return ok

    async def set_speed(self, kmh: float) -> bool:
        param = struct.pack("<H", int(round(kmh * 100)))
        return await self._send(OP_SET_SPEED, param)

    async def set_incline(self, pct: float) -> bool:
        param = struct.pack("<h", int(round(pct * 10)))
        return await self._send(OP_SET_INCLINE, param)

    async def start(self) -> bool:
        return await self._send(OP_START_RESUME)

    async def stop(self) -> bool:
        return await self._send(OP_STOP, bytes([0x01]))

    async def ramp_speed(self, target_kmh: float, duration_secs: float = 8.0):
        """Gradually ramp speed to target over duration_secs."""
        current = self._live.speed_kmh or target_kmh
        steps = max(int(duration_secs), 1)
        delta = (target_kmh - current) / steps
        for i in range(steps):
            new_speed = current + delta * (i + 1)
            await self.set_speed(new_speed)
            await asyncio.sleep(1.0)
        await self.set_speed(target_kmh)   # ensure exact target
