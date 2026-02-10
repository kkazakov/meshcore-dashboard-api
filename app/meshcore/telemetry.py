#!/usr/bin/env python3
"""
MeshCore Status Fetching Script

This script connects to a MeshCore device and retrieves status
(including battery info) from a specific repeater device specified by name.

Configuration is loaded from a .env file.
Usage: python telemetry.py <repeater_name> [password]
"""

import asyncio
import sys
import telemetry_common


def display_status_formated(status_data):
    """Display formatted status data"""
    print("=" * 60)
    print("DEVICE STATUS")
    print("=" * 60 + "\n")

    if not status_data:
        print("No status data received.")
        return

    bat_mv = status_data.get("bat", 0)
    bat_v = bat_mv / 1000
    bat_pct = telemetry_common.calculate_battery_percentage(bat_mv)
    print(f"Battery: {bat_v:.3f} V ({bat_mv} mV)")
    print(f"Battery Percentage: {bat_pct:.1f}%")

    uptime = status_data.get("uptime", 0)
    uptime_days = uptime // 86400
    uptime_hours = (uptime % 86400) // 3600
    uptime_mins = (uptime % 3600) // 60
    uptime_secs = uptime % 60
    uptime_str = (
        f"{uptime_days}d {uptime_hours}h {uptime_mins}m {uptime_secs}s"
        if uptime_days
        else f"{uptime_hours}h {uptime_mins}m {uptime_secs}s"
    )
    print(f"Uptime: {uptime_str} ({uptime}s)")

    print("\nRadio Statistics:")
    print(f"  Noise Floor: {status_data.get('noise_floor', 0)}")
    print(f"  Last RSSI: {status_data.get('last_rssi', 0)}")
    print(f"  Last SNR: {status_data.get('last_snr', 0):.2f} dB")
    print(f"  TX Queue: {status_data.get('tx_queue_len', 0)}")
    print(f"  Queue Full Events: {status_data.get('full_evts', 0)}")

    print("\nPacket Counts:")
    print(f"  Packets Sent: {status_data.get('nb_sent', 0)}")
    print(f"    - Flood: {status_data.get('sent_flood', 0)}")
    print(f"    - Direct: {status_data.get('sent_direct', 0)}")
    print(f"  Packets Received: {status_data.get('nb_recv', 0)}")
    print(f"    - Flood: {status_data.get('recv_flood', 0)}")
    print(f"    - Direct: {status_data.get('recv_direct', 0)}")
    print(f"  Direct Duplicates: {status_data.get('direct_dups', 0)}")
    print(f"  Flood Duplicates: {status_data.get('flood_dups', 0)}")

    print("\nAirtime:")
    print(f"  TX Airtime: {status_data.get('airtime', 0)}")
    print(f"  RX Airtime: {status_data.get('rx_airtime', 0)}")

    print(f"\nPubkey Prefix: {status_data.get('pubkey_pre', 'N/A')}")
    print("\n" + "=" * 60)


async def main():
    """Main function"""
    if len(sys.argv) < 2:
        print("Usage: python telemetry.py <repeater_name> [password]")
        print("\nExample:")
        print("  python telemetry.py Vardar")
        print("  python telemetry.py Buxton mypassword")
        sys.exit(1)

    repeater_name = sys.argv[1]
    password = sys.argv[2] if len(sys.argv) > 2 else ""

    print("=" * 60)
    print("MeshCore Status Fetching Tool")
    print("=" * 60 + "\n")

    config = telemetry_common.load_config()
    meshcore = await telemetry_common.connect_to_device(config, verbose=True)

    try:
        contact = await telemetry_common.find_contact_by_name(
            meshcore, repeater_name, verbose=True
        )
        if not contact:
            sys.exit(1)

        status_data = await telemetry_common.get_status(
            meshcore, contact, password, verbose=True
        )
        if status_data is not None:
            display_status_formated(status_data)

    finally:
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
