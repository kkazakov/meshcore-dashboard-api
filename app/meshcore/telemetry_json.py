#!/usr/bin/env python3
"""
MeshCore Status Fetching Script (JSON Output)

This script connects to a MeshCore device and retrieves status
(including battery info) from a specific repeater device specified by name.

Configuration is loaded from a .env file.
Usage: python telemetry_json.py <repeater_name> [password]

Output: JSON only, to stdout
"""

import logging
import sys

logging.disable(logging.CRITICAL)

import telemetry_common
import asyncio
import json


async def main():
    """Main function"""
    meshcore = None

    try:
        if len(sys.argv) < 2:
            print(
                json.dumps(
                    {
                        "error": "Usage: python telemetry_json.py <repeater_name> [password]"
                    }
                )
            )
            sys.exit(1)

        repeater_name = sys.argv[1]
        password = sys.argv[2] if len(sys.argv) > 2 else ""

        config = telemetry_common.load_config()
        meshcore = await telemetry_common.connect_to_device(config, verbose=False)

        contact = await telemetry_common.find_contact_by_name(
            meshcore, repeater_name, verbose=False, debug=False
        )
        if not contact:
            print(json.dumps({"error": f"Contact '{repeater_name}' not found"}))
            sys.exit(1)

        status_data = await telemetry_common.get_status(
            meshcore, contact, password, verbose=False, max_retries=3
        )
        if status_data is not None:
            result = telemetry_common.status_to_dict(status_data, contact["name"])
            print(json.dumps(result, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "error": "No status response received after 3 attempts",
                        "help": "Device may be offline, out of range, or experiencing radio interference",
                    }
                )
            )
            sys.exit(1)

    finally:
        if meshcore:
            try:
                await meshcore.disconnect()
            except:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
