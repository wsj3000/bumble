#!/usr/bin/env python3
# Copyright 2021-2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
BLE HID Device Example
Modified from run_hid_device.py to support BLE HID over GATT
"""

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
import asyncio
import json
import struct
import sys
import logging
import os
from typing import Optional

# Configure logging from environment variable
logging.basicConfig(
    level=os.environ.get('BUMBLE_LOGLEVEL', 'DEBUG').upper()
)
logger = logging.getLogger(__name__)

# Optional websockets for web mode
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    logger.warning("websockets not installed - web mode unavailable (pip install websockets)")

# Bumble imports
try:
    from bumble.core import AdvertisingData, UUID
    from bumble.device import Device
    from bumble.gatt import Service, Characteristic, CharacteristicValue
    from bumble.transport import open_transport
except ImportError as e:
    logger.error(f"Failed to import bumble: {e}")
    logger.error("Make sure bumble is installed: pip install bumble")
    sys.exit(1)

# -----------------------------------------------------------------------------
# BLE GATT UUIDs
# -----------------------------------------------------------------------------
# HID Service
GATT_HID_SERVICE_UUID = UUID('1812')
GATT_HID_INFORMATION_UUID = UUID('2A4A')
GATT_HID_REPORT_MAP_UUID = UUID('2A4B')
GATT_HID_CONTROL_POINT_UUID = UUID('2A4C')
GATT_HID_REPORT_UUID = UUID('2A4D')
GATT_HID_PROTOCOL_MODE_UUID = UUID('2A4E')

# Device Information Service
GATT_DEVICE_INFORMATION_SERVICE_UUID = UUID('180A')
GATT_MANUFACTURER_NAME_UUID = UUID('2A29')
GATT_MODEL_NUMBER_UUID = UUID('2A24')
GATT_PNP_ID_UUID = UUID('2A50')

# Battery Service
GATT_BATTERY_SERVICE_UUID = UUID('180F')
GATT_BATTERY_LEVEL_UUID = UUID('2A19')

# -----------------------------------------------------------------------------
# HID Report Descriptor (Keyboard + Mouse)
# -----------------------------------------------------------------------------
HID_REPORT_MAP = bytes([
    # Keyboard Report (Report ID 1)
    0x05, 0x01,  # Usage Page (Generic Desktop)
    0x09, 0x06,  # Usage (Keyboard)
    0xA1, 0x01,  # Collection (Application)
    0x85, 0x01,  #   Report ID (1)
    0x05, 0x07,  #   Usage Page (Key Codes)
    0x19, 0xE0,  #   Usage Minimum (224)
    0x29, 0xE7,  #   Usage Maximum (231)
    0x15, 0x00,  #   Logical Minimum (0)
    0x25, 0x01,  #   Logical Maximum (1)
    0x75, 0x01,  #   Report Size (1)
    0x95, 0x08,  #   Report Count (8)
    0x81, 0x02,  #   Input (Data, Variable, Absolute)
    0x95, 0x01,  #   Report Count (1)
    0x75, 0x08,  #   Report Size (8)
    0x81, 0x03,  #   Input (Constant)
    0x95, 0x05,  #   Report Count (5)
    0x75, 0x01,  #   Report Size (1)
    0x05, 0x08,  #   Usage Page (LEDs)
    0x19, 0x01,  #   Usage Minimum (1)
    0x29, 0x05,  #   Usage Maximum (5)
    0x91, 0x02,  #   Output (Data, Variable, Absolute)
    0x95, 0x01,  #   Report Count (1)
    0x75, 0x03,  #   Report Size (3)
    0x91, 0x03,  #   Output (Constant)
    0x95, 0x06,  #   Report Count (6)
    0x75, 0x08,  #   Report Size (8)
    0x15, 0x00,  #   Logical Minimum (0)
    0x25, 0x65,  #   Logical Maximum (101)
    0x05, 0x07,  #   Usage Page (Key Codes)
    0x19, 0x00,  #   Usage Minimum (0)
    0x29, 0x65,  #   Usage Maximum (101)
    0x81, 0x00,  #   Input (Data, Array)
    0xC0,        # End Collection
    
    # Mouse Report (Report ID 2)
    0x05, 0x01,  # Usage Page (Generic Desktop)
    0x09, 0x02,  # Usage (Mouse)
    0xA1, 0x01,  # Collection (Application)
    0x85, 0x02,  #   Report ID (2)
    0x09, 0x01,  #   Usage (Pointer)
    0xA1, 0x00,  #   Collection (Physical)
    0x05, 0x09,  #     Usage Page (Buttons)
    0x19, 0x01,  #     Usage Minimum (1)
    0x29, 0x03,  #     Usage Maximum (3)
    0x15, 0x00,  #     Logical Minimum (0)
    0x25, 0x01,  #     Logical Maximum (1)
    0x95, 0x03,  #     Report Count (3)
    0x75, 0x01,  #     Report Size (1)
    0x81, 0x02,  #     Input (Data, Variable, Absolute)
    0x95, 0x01,  #     Report Count (1)
    0x75, 0x05,  #     Report Size (5)
    0x81, 0x03,  #     Input (Constant)
    0x05, 0x01,  #     Usage Page (Generic Desktop)
    0x09, 0x30,  #     Usage (X)
    0x09, 0x31,  #     Usage (Y)
    0x15, 0x81,  #     Logical Minimum (-127)
    0x25, 0x7F,  #     Logical Maximum (127)
    0x75, 0x08,  #     Report Size (8)
    0x95, 0x02,  #     Report Count (2)
    0x81, 0x06,  #     Input (Data, Variable, Relative)
    0xC0,        #   End Collection
    0xC0,        # End Collection
])

# Protocol modes
PROTOCOL_MODE_BOOT = 0x00
PROTOCOL_MODE_REPORT = 0x01


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------
async def get_stream_reader(pipe) -> asyncio.StreamReader:
    """Create a stream reader for stdin"""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, pipe)
    return reader


# -----------------------------------------------------------------------------
# BLE HID Device Class
# -----------------------------------------------------------------------------
class BLEHIDDevice:
    """BLE HID Device implementation using GATT"""
    
    def __init__(self, device: Device):
        self.device = device
        self.connections = set()
        self.protocol_mode = PROTOCOL_MODE_REPORT
        
        # Device data storage
        self.keyboard_data = bytearray([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        self.mouse_data = bytearray([0x02, 0x00, 0x00, 0x00])
        
        # GATT Characteristics (will be initialized in _setup_services)
        self.keyboard_input_report: Optional[Characteristic] = None
        self.mouse_input_report: Optional[Characteristic] = None
        self.protocol_mode_char: Optional[Characteristic] = None
        
        # Setup GATT services
        self._setup_services()
        
        # Setup connection callbacks
        self.device.on('connection', self._on_connection)
        self.device.on('disconnection', self._on_disconnection)
        
    def _setup_services(self):
        """Setup BLE GATT services"""
        logger.info("Setting up GATT services...")
        
        # Device Information Service
        device_info_service = Service(
            GATT_DEVICE_INFORMATION_SERVICE_UUID,
            [
                Characteristic(
                    GATT_MANUFACTURER_NAME_UUID,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    CharacteristicValue(read=lambda _: b'Bumble Technologies')
                ),
                Characteristic(
                    GATT_MODEL_NUMBER_UUID,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    CharacteristicValue(read=lambda _: b'BLE-HID-001')
                ),
                Characteristic(
                    GATT_PNP_ID_UUID,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    CharacteristicValue(read=lambda _: struct.pack('<BHHH', 0x02, 0x05AC, 0x820A, 0x0100))
                ),
            ]
        )
        
        # Battery Service
        battery_service = Service(
            GATT_BATTERY_SERVICE_UUID,
            [
                Characteristic(
                    GATT_BATTERY_LEVEL_UUID,
                    Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
                    Characteristic.READABLE,
                    CharacteristicValue(read=lambda _: bytes([100]))
                ),
            ]
        )
        
        # HID Service
        # Protocol Mode
        def read_protocol_mode(connection):
            return bytes([self.protocol_mode])
        
        def write_protocol_mode(connection, value):
            self.protocol_mode = value[0]
            logger.info(f"Protocol mode changed to: {'BOOT' if self.protocol_mode == 0 else 'REPORT'}")
            
        self.protocol_mode_char = Characteristic(
            GATT_HID_PROTOCOL_MODE_UUID,
            Characteristic.Properties.READ | Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
            Characteristic.READABLE | Characteristic.WRITEABLE,
            CharacteristicValue(read=read_protocol_mode, write=write_protocol_mode)
        )
        
        # HID Control Point
        def write_control_point(connection, value):
            if value[0] == 0x00:
                logger.info("HID Control: Suspend")
            elif value[0] == 0x01:
                logger.info("HID Control: Exit Suspend")
                
        hid_control_char = Characteristic(
            GATT_HID_CONTROL_POINT_UUID,
            Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=write_control_point)
        )
        
        # Keyboard Input Report
        self.keyboard_input_report = Characteristic(
            GATT_HID_REPORT_UUID,
            Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
            Characteristic.READABLE,
            CharacteristicValue(read=lambda _: self.keyboard_data[1:])
        )
        
        # Mouse Input Report
        self.mouse_input_report = Characteristic(
            GATT_HID_REPORT_UUID,
            Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
            Characteristic.READABLE,
            CharacteristicValue(read=lambda _: self.mouse_data[1:])
        )
        
        # Report Map
        report_map_char = Characteristic(
            GATT_HID_REPORT_MAP_UUID,
            Characteristic.Properties.READ,
            Characteristic.READABLE,
            CharacteristicValue(read=lambda _: HID_REPORT_MAP)
        )
        
        # HID Information
        hid_info = struct.pack('<HBB', 0x0111, 0x00, 0x03)
        hid_info_char = Characteristic(
            GATT_HID_INFORMATION_UUID,
            Characteristic.Properties.READ,
            Characteristic.READABLE,
            CharacteristicValue(read=lambda _: hid_info)
        )
        
        hid_service = Service(
            GATT_HID_SERVICE_UUID,
            [
                hid_info_char,
                report_map_char,
                self.keyboard_input_report,
                self.mouse_input_report,
                self.protocol_mode_char,
                hid_control_char,
            ]
        )
        
        # Add all services to device
        self.device.add_services([device_info_service, battery_service, hid_service])
        logger.info("✓ GATT services configured")
        
    def _on_connection(self, connection):
        """Handle new BLE connection"""
        logger.info(f"✓ Device connected: {connection.peer_address}")
        self.connections.add(connection)
        
    def _on_disconnection(self, connection):
        """Handle BLE disconnection"""
        logger.info(f"✗ Device disconnected: {connection.peer_address}")
        self.connections.discard(connection)
        
    async def send_data(self, data: bytearray):
        """Send HID report (compatible with original API)"""
        if not self.connections:
            logger.warning("No device connected - cannot send data")
            return
            
        report_id = data[0]
        report_data = data[1:]
        
        try:
            if report_id == 0x01:  # Keyboard
                self.keyboard_data = data
                for conn in self.connections:
                    await self.keyboard_input_report.notify(conn, report_data)
                logger.debug(f"Sent keyboard report: {report_data.hex()}")
                
            elif report_id == 0x02:  # Mouse
                self.mouse_data = data
                for conn in self.connections:
                    await self.mouse_input_report.notify(conn, report_data)
                logger.debug(f"Sent mouse report: {report_data.hex()}")
                
            else:
                logger.warning(f"Unknown report ID: 0x{report_id:02X}")
        except Exception as e:
            logger.error(f"Failed to send data: {e}")
            
    async def setup_advertising(self):
        """Setup and start BLE advertising"""
        # Create advertising data - keep it simple and under 31 bytes for legacy advertising
        advertising_data = bytes(
            AdvertisingData([
                (AdvertisingData.COMPLETE_LOCAL_NAME, self.device.name.encode('utf-8')),
                (AdvertisingData.FLAGS, bytes([0x06])),  # LE General Discoverable, BR/EDR not supported
                (AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS, 
                 struct.pack('<H', 0x1812)),  # HID Service only
            ])
        )
        
        # Create scan response data for additional info
        scan_response_data = bytes(
            AdvertisingData([
                (AdvertisingData.APPEARANCE, struct.pack('<H', 0x03C1)),  # Keyboard appearance
                (AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS, 
                 struct.pack('<HH', 0x180A, 0x180F)),  # Device Info, Battery
            ])
        )
        
        # Start advertising with both advertising and scan response data
        try:
            await self.device.start_advertising(
                advertising_data=advertising_data,
                scan_response_data=scan_response_data,
                auto_restart=True
            )
            logger.info(f"✓ Advertising as '{self.device.name}'")
        except Exception as e:
            logger.warning(f"Extended advertising failed, trying legacy mode: {e}")
            # Fallback to simpler legacy advertising if extended fails
            simple_adv_data = bytes(
                AdvertisingData([
                    (AdvertisingData.COMPLETE_LOCAL_NAME, self.device.name.encode('utf-8')),
                    (AdvertisingData.FLAGS, bytes([0x06])),
                    (AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS, 
                     struct.pack('<H', 0x1812)),
                ])
            )
            await self.device.start_advertising(advertising_data=simple_adv_data)
            logger.info(f"✓ Advertising as '{self.device.name}' (legacy mode)")
        
    # Compatibility methods (for original API)
    async def connect_control_channel(self):
        logger.info("ℹ BLE uses GATT - no separate control channel")
        
    async def connect_interrupt_channel(self):
        logger.info("ℹ BLE uses GATT - no separate interrupt channel")
        
    async def disconnect_control_channel(self):
        logger.info("Disconnecting (BLE doesn't have separate control channel)")
        await self.disconnect_interrupt_channel()
        
    async def disconnect_interrupt_channel(self):
        logger.info("Disconnecting all connections...")
        for conn in list(self.connections):
            await conn.disconnect()
            
    def virtual_cable_unplug(self):
        logger.info("Virtual cable unplug - disconnecting...")
        asyncio.create_task(self.disconnect_interrupt_channel())
        
    @property
    def connection(self):
        """Get first active connection"""
        return next(iter(self.connections), None)
        
    @property
    def remote_device_bd_address(self):
        """Get remote device address"""
        conn = self.connection
        return conn.peer_address if conn else None
    
    async def cleanup(self):
        """Clean up and disconnect all connections"""
        if not self.connections:
            logger.info("No active connections to clean up")
            return
            
        logger.info("Cleaning up connections...")
        for conn in list(self.connections):
            try:
                # Add timeout for disconnect operation
                await asyncio.wait_for(conn.disconnect(), timeout=2.0)
                logger.info(f"✓ Disconnected from {conn.peer_address}")
            except asyncio.TimeoutError:
                logger.warning(f"⚠ Disconnect timeout for {conn.peer_address} (connection may already be closed)")
            except Exception as e:
                logger.warning(f"⚠ Error disconnecting {conn.peer_address}: {e}")
        self.connections.clear()
        logger.info("✓ All connections cleaned up")


# -----------------------------------------------------------------------------
# Device Data Storage
# -----------------------------------------------------------------------------
class DeviceData:
    def __init__(self):
        self.keyboardData = bytearray([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        self.mouseData = bytearray([0x02, 0x00, 0x00, 0x00])


deviceData = DeviceData()


# -----------------------------------------------------------------------------
# Web Mode (WebSocket Server)
# -----------------------------------------------------------------------------
async def keyboard_device(hid_device: BLEHIDDevice):
    """Run keyboard device with WebSocket server for web UI"""
    
    if not WEBSOCKETS_AVAILABLE:
        logger.error("❌ websockets module not installed")
        logger.error("Install with: pip install websockets")
        return
        
    async def serve(websocket, _path):
        global deviceData
        logger.info(f"WebSocket client connected from {websocket.remote_address}")
        
        try:
            while True:
                message = await websocket.recv()
                logger.debug(f"Received: {message}")
                
                try:
                    parsed = json.loads(message)
                    message_type = parsed.get('type')
                    
                    if message_type == 'keydown':
                        key = parsed.get('key', '')
                        if len(key) == 1:
                            code = ord(key)
                            if ord('a') <= code <= ord('z'):
                                hid_code = 0x04 + code - ord('a')
                                deviceData.keyboardData = bytearray([
                                    0x01, 0x00, 0x00, hid_code, 0x00, 0x00, 0x00, 0x00, 0x00
                                ])
                                await hid_device.send_data(deviceData.keyboardData)
                                
                    elif message_type == 'keyup':
                        deviceData.keyboardData = bytearray([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
                        await hid_device.send_data(deviceData.keyboardData)
                        
                    elif message_type == 'mousemove':
                        x = max(-127, min(127, parsed.get('x', 0)))
                        y = max(-127, min(127, parsed.get('y', 0)))
                        deviceData.mouseData = bytearray([0x02, 0x00]) + struct.pack(">bb", x, y)
                        await hid_device.send_data(deviceData.mouseData)
                        
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON: {message}")
                    
        except websockets.exceptions.ConnectionClosedOK:
            logger.info("WebSocket client disconnected")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")

    server = await websockets.serve(serve, 'localhost', 8989)
    logger.info("✓ WebSocket server running on ws://localhost:8989")
    logger.info("  Open keyboard.html in your browser to control the device")
    
    await asyncio.Future()  # Run forever


# -----------------------------------------------------------------------------
# Test Mode (Interactive Menu)
# -----------------------------------------------------------------------------
async def test_mode(device: Device, hid_device: BLEHIDDevice):
    """Run interactive test menu"""
    reader = await get_stream_reader(sys.stdin)
    
    while True:
        print("\n" + "="*70)
        print(" BLE HID Device Test Menu")
        print("="*70)
        print(" 1. Connect Control Channel (N/A for BLE)")
        print(" 2. Connect Interrupt Channel (N/A for BLE)")
        print(" 3. Disconnect Control Channel")
        print(" 4. Disconnect Interrupt Channel")
        print(" 5. Send Report")
        print(" 6. Virtual Cable Unplug")
        print(" 7. Disconnect Device")
        print(" 8. Delete Bonding")
        print(" 9. Wait for Connection")
        print("10. Exit")
        print("="*70)
        print("Enter choice: ", end='', flush=True)
        
        choice = await reader.readline()
        choice = choice.decode('utf-8').strip()
        
        if choice == '1':
            await hid_device.connect_control_channel()
            
        elif choice == '2':
            await hid_device.connect_interrupt_channel()
            
        elif choice == '3':
            await hid_device.disconnect_control_channel()
            
        elif choice == '4':
            await hid_device.disconnect_interrupt_channel()
            
        elif choice == '5':
            print("\n  1. Send Keyboard Report (key 'a')")
            print("  2. Send Mouse Report (move)")
            print("  3. Invalid Report")
            print("Choice: ", end='', flush=True)
            
            sub_choice = await reader.readline()
            sub_choice = sub_choice.decode('utf-8').strip()
            
            if sub_choice == '1':
                # Press 'a'
                data = bytearray([0x01, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00])
                await hid_device.send_data(data)
                await asyncio.sleep(0.1)
                # Release
                data = bytearray([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
                await hid_device.send_data(data)
                print("✓ Sent keyboard report")
                
            elif sub_choice == '2':
                # Move mouse
                data = bytearray([0x02, 0x00, 0x0A, 0xF6])  # X=10, Y=-10
                await hid_device.send_data(data)
                await asyncio.sleep(0.1)
                data = bytearray([0x02, 0x00, 0x00, 0x00])
                await hid_device.send_data(data)
                print("✓ Sent mouse report")
                
            elif sub_choice == '3':
                data = bytearray([0xFF, 0x00, 0x00, 0x00])
                await hid_device.send_data(data)
                
        elif choice == '6':
            hid_device.virtual_cable_unplug()
            if hid_device.remote_device_bd_address:
                try:
                    await device.keystore.delete(str(hid_device.remote_device_bd_address))
                    print("✓ Bonding deleted")
                except (KeyError, AttributeError):
                    print("ℹ No bonding to delete")
                    
        elif choice == '7':
            conn = hid_device.connection
            if conn:
                await conn.disconnect()
                print("✓ Disconnected")
            else:
                print("ℹ No active connection")
                
        elif choice == '8':
            if hid_device.remote_device_bd_address:
                try:
                    await device.keystore.delete(str(hid_device.remote_device_bd_address))
                    print("✓ Bonding deleted")
                except (KeyError, AttributeError):
                    print("ℹ Device not paired")
            else:
                print("ℹ No remote device")
                
        elif choice == '9':
            print("Waiting for device to connect...")
            print("(BLE peripheral waits for central to connect)")
            
        elif choice == '10':
            print("Exiting...")
            sys.exit(0)
            
        else:
            print("❌ Invalid choice")


# -----------------------------------------------------------------------------
# Main Function
# -----------------------------------------------------------------------------
async def main():
    if len(sys.argv) < 3:
        print("Usage: python run_hid_device.py <device-config> <transport-spec> [command]")
        print()
        print("Commands:")
        print("  test-mode  - Interactive test menu")
        print("  web        - WebSocket server for web UI (default)")
        print()
        print("Examples:")
        print("  python run_hid_device.py hid_keyboard.json usb:0 web")
        print("  python run_hid_device.py hid_keyboard.json usb:0 test-mode")
        return
        
    config_file = sys.argv[1]
    transport_spec = sys.argv[2]
    command = sys.argv[3] if len(sys.argv) > 3 else 'web'
    
    logger.info("="*70)
    logger.info("BLE HID Device Starting...")
    logger.info("="*70)
    
    hid_device = None
    device = None
    
    try:
        # Open transport
        logger.info(f"Opening transport: {transport_spec}")
        async with await open_transport(transport_spec) as hci_transport:
            logger.info("✓ Transport connected")
            
            # Create device
            logger.info(f"Loading config: {config_file}")
            device = Device.from_config_file_with_hci(
                config_file, 
                hci_transport.source, 
                hci_transport.sink
            )
            logger.info(f"✓ Device created: {device.name}")
            
            # Create BLE HID device
            hid_device = BLEHIDDevice(device)
            
            # Power on
            logger.info("Powering on device...")
            await device.power_on()
            logger.info("✓ Device powered on")
            
            # Start advertising
            await hid_device.setup_advertising()
            
            # Run selected mode
            logger.info(f"Starting mode: {command}")
            
            try:
                if command == 'test-mode':
                    await test_mode(device, hid_device)
                else:  # default to web mode
                    await keyboard_device(hid_device)
            except KeyboardInterrupt:
                logger.info("\n⚠ Keyboard interrupt received")
                raise
                
    except FileNotFoundError:
        logger.error(f"❌ Config file not found: {config_file}")
        logger.error("Create a config file or use an example from bumble/examples")
    except KeyboardInterrupt:
        logger.info("\n✓ Shutdown requested")
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        raise
    finally:
        # Clean up connections
        if hid_device:
            try:
                await asyncio.wait_for(hid_device.cleanup(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("⚠ Cleanup timeout - forcing shutdown")
            except Exception as e:
                logger.warning(f"⚠ Error during cleanup: {e}")
        
        # Stop advertising
        if device:
            try:
                await asyncio.wait_for(device.stop_advertising(), timeout=2.0)
                logger.info("✓ Stopped advertising")
            except asyncio.TimeoutError:
                logger.warning("⚠ Stop advertising timeout - continuing shutdown")
            except Exception as e:
                logger.warning(f"⚠ Error stopping advertising: {e}")
        
        logger.info("✓ Shutdown complete")


# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    import signal
    
    # Flag to track if we should do graceful shutdown
    graceful_shutdown = True
    
    def signal_handler(sig, frame):
        """Handle Ctrl+C with fast exit option"""
        global graceful_shutdown
        if graceful_shutdown:
            logger.info("\n⚠ Interrupt received - shutting down gracefully...")
            logger.info("   (Press Ctrl+C again for immediate exit)")
            graceful_shutdown = False
        else:
            logger.info("\n⚠ Force exit!")
            sys.exit(1)
    
    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if graceful_shutdown:
            logger.info("\n✓ Graceful shutdown completed")
        sys.exit(0)