"""
Global device connection lock.

The MeshCore companion device accepts only one TCP/BLE/Serial connection at a
time.  All code that connects to the device must acquire ``device_lock`` first
so that API routes and the background message poller never attempt to open
simultaneous connections.

Usage
-----
    from app.meshcore.connection import device_lock

    async with device_lock:
        meshcore = await connect_to_device(config)
        try:
            ...
        finally:
            await meshcore.disconnect()
"""

import asyncio

# Module-level lock — created once, shared across the entire process.
device_lock: asyncio.Lock = asyncio.Lock()
