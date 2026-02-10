#!/usr/bin/env python3
"""
MeshCore Contact Listing Script

This script connects to a MeshCore device via Bluetooth (or other connection types)
and retrieves all contacts from the device.

Configuration is loaded from a .env file.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv
from meshcore import MeshCore, EventType


def load_config():
    """Load configuration from .env file"""
    load_dotenv()

    config = {
        "connection_type": os.getenv("CONNECTION_TYPE", "ble").lower(),
        "ble_address": os.getenv("BLE_ADDRESS", ""),
        "ble_pin": os.getenv("BLE_PIN", ""),
        "serial_port": os.getenv("SERIAL_PORT", "/dev/ttyUSB0"),
        "serial_baudrate": int(os.getenv("SERIAL_BAUDRATE", "115200")),
        "tcp_host": os.getenv("TCP_HOST", "192.168.1.100"),
        "tcp_port": int(os.getenv("TCP_PORT", "4000")),
        "debug": os.getenv("DEBUG", "false").lower() == "true",
    }

    return config


async def connect_to_device(config):
    """Connect to MeshCore device based on configuration"""
    conn_type = config["connection_type"]
    debug = config["debug"]

    print(f"Connecting to MeshCore device via {conn_type.upper()}...")

    try:
        if conn_type == "ble":
            # BLE connection
            ble_address = config["ble_address"] if config["ble_address"] else None
            ble_pin = config["ble_pin"] if config["ble_pin"] else None

            if ble_address:
                print(f"Using BLE address: {ble_address}")
            else:
                print("Scanning for BLE devices...")

            if ble_pin:
                print(f"Using PIN for pairing: {ble_pin}")
                meshcore = await MeshCore.create_ble(
                    ble_address, pin=ble_pin, debug=debug
                )
            else:
                meshcore = await MeshCore.create_ble(ble_address, debug=debug)

        elif conn_type == "serial":
            # Serial connection
            print(
                f"Using serial port: {config['serial_port']} @ {config['serial_baudrate']} baud"
            )
            meshcore = await MeshCore.create_serial(
                config["serial_port"], config["serial_baudrate"], debug=debug
            )

        elif conn_type == "tcp":
            # TCP connection
            print(f"Using TCP connection: {config['tcp_host']}:{config['tcp_port']}")
            meshcore = await MeshCore.create_tcp(
                config["tcp_host"], config["tcp_port"], debug=debug
            )

        else:
            print(f"ERROR: Unknown connection type: {conn_type}")
            print("Valid types are: ble, serial, tcp")
            sys.exit(1)

        print("✓ Connected successfully!\n")
        return meshcore

    except Exception as e:
        print(f"ERROR: Failed to connect to device: {e}")
        sys.exit(1)


async def get_device_info(meshcore):
    """Get and display device information"""
    print("Fetching device information...")

    try:
        # Get device info
        result = await meshcore.commands.send_device_query()
        if result.type == EventType.ERROR:
            print(f"WARNING: Could not get device info: {result.payload}")
        else:
            info = result.payload
            print(f"\nDevice Information:")
            print(f"  Model: {info.get('model', 'Unknown')}")
            print(f"  Firmware: {info.get('firmware_version', 'Unknown')}")
            print(f"  Max Contacts: {info.get('max_contacts', 'Unknown')}")
            print(f"  Max Channels: {info.get('max_channels', 'Unknown')}")

        # Get self info
        result = await meshcore.commands.send_appstart()
        if result.type == EventType.ERROR:
            print(f"WARNING: Could not get self info: {result.payload}")
        else:
            self_info = result.payload
            print(f"\nDevice Identity:")
            print(f"  Name: {self_info.get('name', 'Unknown')}")
            print(f"  Public Key: {self_info.get('public_key', 'Unknown')}")

    except Exception as e:
        print(f"WARNING: Error getting device info: {e}")


async def list_contacts(meshcore):
    """Retrieve and display all contacts from the device"""
    print("\n" + "=" * 60)
    print("RETRIEVING CONTACTS")
    print("=" * 60 + "\n")

    try:
        result = await meshcore.commands.get_contacts()

        if result.type == EventType.ERROR:
            print(f"ERROR: Failed to get contacts: {result.payload}")
            return

        contacts = result.payload

        if not contacts:
            print("No contacts found on the device.")
            return

        print(f"Found {len(contacts)} contact(s):\n")

        for idx, (contact_id, contact) in enumerate(contacts.items(), 1):
            print(f"Contact #{idx}")
            print(f"  Name: {contact.get('adv_name', 'Unknown')}")
            print(f"  Public Key: {contact.get('public_key', contact_id)}")
            print(f"  Key Prefix: {contact.get('public_key', contact_id)[:12]}...")

            # Location info if available
            lat = contact.get("adv_lat")
            lon = contact.get("adv_lon")
            if lat is not None and lon is not None:
                print(f"  Location: {lat:.6f}, {lon:.6f}")

            # SNR info if available
            snr = contact.get("snr")
            if snr is not None:
                print(f"  SNR: {snr} dB")

            # Last seen if available
            last_seen = contact.get("last_seen")
            if last_seen:
                print(f"  Last Seen: {last_seen}")

            print()

    except Exception as e:
        print(f"ERROR: Exception while retrieving contacts: {e}")


async def main():
    """Main function"""
    print("=" * 60)
    print("MeshCore Contact Listing Tool")
    print("=" * 60 + "\n")

    # Load configuration
    config = load_config()

    # Connect to device
    meshcore = await connect_to_device(config)

    try:
        # Get device info
        await get_device_info(meshcore)

        # List all contacts
        await list_contacts(meshcore)

    finally:
        # Disconnect
        print("\nDisconnecting from device...")
        await meshcore.disconnect()
        print("✓ Disconnected\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Exiting...")
        sys.exit(0)
    except Exception as e:
        print(f"\n\nFATAL ERROR: {e}")
        sys.exit(1)
