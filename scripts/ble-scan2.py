"""Scan specifically for FTMS service, then probe all unnamed non-Apple devices."""
import asyncio
from bleak import BleakScanner, BleakClient

FTMS_UUID  = "00001826-0000-1000-8000-00805f9b34fb"
APPLE_UUIDS = {"fd3d", "fcb2", "7905f431", "d0611e78", "9fa480e0", "89d3502b"}

KNOWN = {
    "00001826": "FTMS ← TREADMILL",
    "00001816": "Cycling Speed & Cadence",
    "00001814": "Running Speed & Cadence",
    "0000180d": "Heart Rate",
    "0000180a": "Device Information",
    "0000180f": "Battery",
    "00001800": "Generic Access",
    "00001801": "Generic Attribute",
}

async def probe(addr, name):
    try:
        async with BleakClient(addr, timeout=8.0) as client:
            uuids = [str(s.uuid).lower() for s in client.services]
            has_ftms = FTMS_UUID in uuids
            marker = " ★★★ TREADMILL ★★★" if has_ftms else ""
            print(f"\n  [{addr}] {name}{marker}")
            for s in client.services:
                uid = str(s.uuid).lower()
                label = KNOWN.get(uid[:8], "")
                if label or has_ftms:
                    print(f"    · {s.uuid}  {label or s.description or uid}")
            return has_ftms
    except Exception as e:
        print(f"  [{addr}] {name}: {type(e).__name__}")
        return False

async def main():
    print("Scanning 20s for ALL devices (looking for FTMS)...\n")
    devices = await BleakScanner.discover(timeout=20.0, return_adv=True,
        service_uuids=[FTMS_UUID])   # ← scan filter: only FTMS advertisers

    print(f"FTMS-advertising devices found: {len(devices)}")
    for addr, (dev, adv) in devices.items():
        print(f"  {addr}  {dev.name}  RSSI {adv.rssi}")

    if not devices:
        print("\nNo FTMS found via filtered scan.")
        print("Trying unfiltered scan to find unnamed non-Apple devices...\n")
        all_devs = await BleakScanner.discover(timeout=20.0, return_adv=True)
        candidates = []
        for addr, (dev, adv) in all_devs.items():
            uuids = [str(u)[:8].lower() for u in (adv.service_uuids or [])]
            is_apple = any(u in APPLE_UUIDS for u in uuids)
            if not is_apple and adv.rssi > -88:
                candidates.append((adv.rssi, addr, dev.name or "?"))
        candidates.sort(reverse=True)
        print(f"Non-Apple candidates ({len(candidates)}):")
        for rssi, addr, name in candidates:
            print(f"  [{rssi:4d}] {addr}  {name}")

        print("\nProbing top candidates...")
        for _, addr, name in candidates[:8]:
            found = await probe(addr, name)
            if found:
                print(f"\n>>> TREADMILL FOUND: {addr} <<<")
                return

asyncio.run(main())
