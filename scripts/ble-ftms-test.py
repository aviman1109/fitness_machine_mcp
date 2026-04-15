"""
FTMS treadmill control test.
1. Connect & subscribe to Treadmill Data
2. Request control
3. Try setting speed

ADIDAS 1380 / ChangYow SI3019
"""
import asyncio, struct
from bleak import BleakClient

ADDR = "D5:33:36:33:16:29"

# FTMS UUIDs
TREADMILL_DATA    = "00002acd-0000-1000-8000-00805f9b34fb"
MACHINE_STATUS    = "00002ada-0000-1000-8000-00805f9b34fb"
CONTROL_POINT     = "00002ad9-0000-1000-8000-00805f9b34fb"
TRAINING_STATUS   = "00002ad3-0000-1000-8000-00805f9b34fb"

# Control Point opcodes (FTMS spec)
OP_REQUEST_CONTROL = 0x00
OP_RESET           = 0x01
OP_SET_SPEED       = 0x02   # param: uint16, unit 0.01 km/h
OP_SET_INCLINE     = 0x03   # param: sint16, unit 0.1%
OP_START_RESUME    = 0x07
OP_STOP_PAUSE      = 0x08   # param: 0x01=stop, 0x02=pause

# Response codes
RESPONSE_SUCCESS = 0x01

def parse_treadmill_data(data: bytes) -> dict:
    """Parse Treadmill Data characteristic (FTMS spec Table 4.8)."""
    flags = struct.unpack_from("<H", data, 0)[0]
    offset = 2
    result = {"flags": hex(flags)}

    # Bit 0: More Data (if 0, Instantaneous Speed present)
    if not (flags & 0x01):
        speed = struct.unpack_from("<H", data, offset)[0]
        result["speed_kmh"] = speed * 0.01
        offset += 2

    # Bit 1: Average Speed present
    if flags & 0x02:
        avg_speed = struct.unpack_from("<H", data, offset)[0]
        result["avg_speed_kmh"] = avg_speed * 0.01
        offset += 2

    # Bit 2: Total Distance present
    if flags & 0x04:
        dist = struct.unpack_from("<I", data, offset)[0] & 0xFFFFFF
        result["distance_m"] = dist
        offset += 3

    # Bit 3: Inclination & Ramp Angle present
    if flags & 0x08:
        incline = struct.unpack_from("<h", data, offset)[0]
        ramp = struct.unpack_from("<h", data, offset+2)[0]
        result["incline_pct"] = incline * 0.1
        result["ramp_angle_deg"] = ramp * 0.1
        offset += 4

    # Bit 4: Elevation Gain present
    if flags & 0x10:
        pos_elev = struct.unpack_from("<H", data, offset)[0]
        neg_elev = struct.unpack_from("<H", data, offset+2)[0]
        result["elevation_gain_m"] = pos_elev * 0.1
        offset += 4

    # Bit 5: Instantaneous Pace present
    if flags & 0x20:
        pace = struct.unpack_from("<H", data, offset)[0]
        result["pace_min_per_km"] = pace
        offset += 2

    # Bit 7: HR present
    if flags & 0x80:
        result["hr_bpm"] = data[offset]
        offset += 1

    # Bit 9: Elapsed Time present
    if flags & 0x200:
        elapsed = struct.unpack_from("<H", data, offset)[0]
        result["elapsed_sec"] = elapsed
        offset += 2

    return result

def parse_status(data: bytes) -> str:
    if not data:
        return "empty"
    code = data[0]
    codes = {0x01: "Reset", 0x02: "Stopped by Safety Key", 0x03: "Stopped/Paused by User",
             0x04: "Started/Resumed by User", 0x05: "Target Speed Changed",
             0x06: "Target Inclination Changed", 0x0A: "Fitness Machine Stopped",
             0xFF: "Control Permission Lost"}
    return codes.get(code, f"Unknown({hex(code)})")

async def send_control(client, opcode: int, param: bytes = b"") -> bool:
    cmd = bytes([opcode]) + param
    print(f"  → Sending opcode {hex(opcode)}: {cmd.hex()}")

    response = asyncio.get_event_loop().create_future()

    def on_indicate(_, data):
        if not response.done():
            response.set_result(data)

    await client.start_notify(CONTROL_POINT, on_indicate)
    await client.write_gatt_char(CONTROL_POINT, cmd, response=True)
    try:
        resp = await asyncio.wait_for(response, timeout=5.0)
        # Response: 0x80, echo_opcode, result_code
        if len(resp) >= 3 and resp[0] == 0x80:
            success = resp[2] == RESPONSE_SUCCESS
            print(f"  ← Response: {'OK' if success else 'FAIL'} ({resp.hex()})")
            return success
        print(f"  ← Raw response: {resp.hex()}")
        return False
    except asyncio.TimeoutError:
        print("  ← Timeout waiting for response")
        return False
    finally:
        await client.stop_notify(CONTROL_POINT)

async def main():
    print(f"Connecting to {ADDR}...\n")
    async with BleakClient(ADDR, timeout=10.0) as client:
        print("Connected!\n")

        # Subscribe to treadmill data
        received = []
        def on_treadmill_data(_, data):
            parsed = parse_treadmill_data(data)
            received.append(parsed)
            print(f"  [Data] raw={data.hex()}  parsed={parsed}")

        def on_status(_, data):
            print(f"  [Status] {parse_status(data)} raw={data.hex()}")

        await client.start_notify(TREADMILL_DATA, on_treadmill_data)
        await client.start_notify(MACHINE_STATUS, on_status)

        print("Subscribed. Waiting 3s for initial data...\n")
        await asyncio.sleep(3)

        # Request control
        print("\n--- Requesting Control ---")
        ok = await send_control(client, OP_REQUEST_CONTROL)

        if ok:
            print("\n--- Control granted! Testing speed 5.0 km/h ---")
            # 5.0 km/h = 500 in 0.01 km/h units
            speed_param = struct.pack("<H", 500)
            await send_control(client, OP_SET_SPEED, speed_param)
            await asyncio.sleep(3)

            print("\n--- Testing incline 1.0% ---")
            incline_param = struct.pack("<h", 10)  # 10 × 0.1% = 1.0%
            await send_control(client, OP_SET_INCLINE, incline_param)
            await asyncio.sleep(3)
        else:
            print("\nControl not granted — treadmill may need to be in active mode")
            print("Try starting the treadmill manually first, then re-run")

        print(f"\nTotal data notifications received: {len(received)}")

asyncio.run(main())
