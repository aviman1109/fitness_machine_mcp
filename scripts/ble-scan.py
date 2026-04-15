"""Quick BLE scan — find treadmill and HR strap, dump their GATT services."""

import asyncio
from bleak import BleakScanner, BleakClient

# Known service UUIDs we care about
KNOWN = {
    "00001826-0000-1000-8000-00805f9b34fb": "FTMS (treadmill control)",
    "00001816-0000-1000-8000-00805f9b34fb": "Cycling Speed & Cadence",
    "00001814-0000-1000-8000-00805f9b34fb": "Running Speed & Cadence",
    "0000180d-0000-1000-8000-00805f9b34fb": "Heart Rate",
    "0000180a-0000-1000-8000-00805f9b34fb": "Device Information",
    "0000fe00-0000-1000-8000-00805f9b34fb": "Vendor (adidas?)",
}

async def scan():
    print("Scanning 15 seconds...\n")
    devices = await BleakScanner.discover(timeout=15.0, return_adv=True)

    interesting = []
    for addr, (dev, adv) in devices.items():
        uuids = [str(u).lower() for u in (adv.service_uuids or [])]
        labels = [KNOWN[u] for u in uuids if u in KNOWN]
        rssi = adv.rssi
        interesting.append((rssi, dev.name or "?", addr, labels, uuids))

    interesting.sort(reverse=True)  # strongest signal first

    print(f"Found {len(interesting)} devices:\n")
    for rssi, name, addr, labels, uuids in interesting:
        tag = " ← FTMS!" if any("FTMS" in l for l in labels) else \
              " ← HR!"   if any("Heart Rate" in l for l in labels) else \
              " ← RSC!"  if any("Running Speed" in l for l in labels) else ""
        print(f"  [{rssi:4d} dBm]  {name:<30s}  {addr}{tag}")
        for l in labels:
            print(f"             · {l}")
        if not labels and uuids:
            for u in uuids[:3]:
                print(f"             · {u}")
        print()

asyncio.run(scan())
