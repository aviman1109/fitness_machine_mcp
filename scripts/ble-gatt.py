"""Connect to candidate BLE devices and dump their GATT services."""
import asyncio
from bleak import BleakClient

FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"

# Strongest unnamed candidates (no Apple fd3d pattern)
CANDIDATES = [
    "E5:12:86:22:40:C2",
    "65:60:BA:56:9D:A3",
    "FB:43:D0:67:81:02",
    "EE:80:DA:FD:2B:7B",
    "4C:11:58:CC:1A:88",
    "28:99:A9:8B:D1:32",
]

KNOWN_SERVICES = {
    "00001826": "FTMS ← TREADMILL CONTROL",
    "00001816": "Cycling Speed & Cadence",
    "00001814": "Running Speed & Cadence",
    "0000180d": "Heart Rate",
    "0000180a": "Device Information",
    "0000180f": "Battery",
    "00001800": "Generic Access",
    "00001801": "Generic Attribute",
}

async def probe(addr):
    try:
        async with BleakClient(addr, timeout=6.0) as client:
            services = client.services
            uuids = [str(s.uuid).lower() for s in services]
            is_ftms = FTMS_SERVICE in uuids
            tag = " ★★★ TREADMILL FOUND ★★★" if is_ftms else ""
            print(f"\n{'='*50}")
            print(f"  {addr}{tag}")
            for s in services:
                uid = str(s.uuid).lower()
                label = KNOWN_SERVICES.get(uid[:8], "")
                print(f"  · {s.uuid}  {label or s.description or ''}")
            return is_ftms, addr
    except Exception as e:
        print(f"  {addr}: cannot connect ({type(e).__name__})")
        return False, addr

async def main():
    print("Probing candidates for FTMS (treadmill)...\n")
    for addr in CANDIDATES:
        found, _ = await probe(addr)
        if found:
            print(f"\n>>> Treadmill is {addr} <<<")
            break

asyncio.run(main())
