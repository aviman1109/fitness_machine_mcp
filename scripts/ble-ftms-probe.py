"""Connect to ADIDAS 1380 and dump full GATT profile."""
import asyncio
from bleak import BleakClient

TREADMILL_ADDR = "D5:33:36:33:16:29"

# FTMS characteristics we care about
FTMS_CHARS = {
    "00002acc-0000-1000-8000-00805f9b34fb": "Fitness Machine Feature",
    "00002acd-0000-1000-8000-00805f9b34fb": "Treadmill Data",
    "00002ada-0000-1000-8000-00805f9b34fb": "Fitness Machine Status",
    "00002ad9-0000-1000-8000-00805f9b34fb": "Fitness Machine Control Point",
    "00002ad3-0000-1000-8000-00805f9b34fb": "Training Status",
    "00002ad8-0000-1000-8000-00805f9b34fb": "Supported Speed Range",
    "00002ad5-0000-1000-8000-00805f9b34fb": "Supported Resistance Level Range",
    "00002ace-0000-1000-8000-00805f9b34fb": "Cross Trainer Data",
}

async def main():
    print(f"Connecting to {TREADMILL_ADDR}...\n")
    async with BleakClient(TREADMILL_ADDR, timeout=10.0) as client:
        print(f"Connected: {client.is_connected}\n")

        for service in client.services:
            print(f"Service: {service.uuid}  [{service.description}]")
            for char in service.characteristics:
                uid = str(char.uuid).lower()
                label = FTMS_CHARS.get(uid, "")
                props = ",".join(char.properties)
                marker = " ◄◄◄" if label else ""
                print(f"  Char: {char.uuid}  [{props}]{marker}")
                if label:
                    print(f"        → {label}")

                # Read readable characteristics
                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char.uuid)
                        print(f"        value: {val.hex()}  {list(val)}")
                    except Exception as e:
                        print(f"        read error: {e}")
            print()

asyncio.run(main())
