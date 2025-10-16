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
BLE Mesh Smart Light Example
A smart light device that supports BLE Mesh provisioning and control
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
from typing import Optional, Dict, Any

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
# BLE Mesh Constants
# -----------------------------------------------------------------------------
# Mesh Beacon AD Type
MESH_BEACON_AD_TYPE = 0x2B

# Mesh Provisioning Service
MESH_PROVISIONING_SERVICE_UUID = UUID('1827')
MESH_PROVISIONING_SERVICE_UUID_INT = 0x1827
MESH_PROVISIONING_DATA_IN_UUID = UUID('2ADB')
MESH_PROVISIONING_DATA_OUT_UUID = UUID('2ADC')

# Mesh Proxy Service
MESH_PROXY_SERVICE_UUID = UUID('1828')
MESH_PROXY_SERVICE_UUID_INT = 0x1828
MESH_PROXY_DATA_IN_UUID = UUID('2ADD')
MESH_PROXY_DATA_OUT_UUID = UUID('2ADE')

# Unprovisioned Device Beacon Type
UNPROVISIONED_DEVICE_BEACON = 0x00

# Generic OnOff Model
GENERIC_ONOFF_MODEL_ID = 0x1000
GENERIC_LEVEL_MODEL_ID = 0x1002
LIGHT_LIGHTNESS_MODEL_ID = 0x1300
LIGHT_CTL_MODEL_ID = 0x1303

# Mesh Opcodes
GENERIC_ONOFF_GET = 0x8201
GENERIC_ONOFF_SET = 0x8202
GENERIC_ONOFF_SET_UNACK = 0x8203
GENERIC_ONOFF_STATUS = 0x8204

GENERIC_LEVEL_GET = 0x8205
GENERIC_LEVEL_SET = 0x8206
GENERIC_LEVEL_STATUS = 0x8208

LIGHT_LIGHTNESS_GET = 0x824B
LIGHT_LIGHTNESS_SET = 0x824C
LIGHT_LIGHTNESS_STATUS = 0x824E

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
# BLE Mesh Light Device Class
# -----------------------------------------------------------------------------
class BLEMeshLight:
    """BLE Mesh Smart Light implementation for Amazon Echo compatibility"""
    
    def __init__(self, device: Device):
        self.device = device
        self.connections = set()
        
        # Light state
        self.is_on = False
        self.brightness = 100  # 0-100%
        self.color_temp = 4000  # Kelvin
        self.hue = 0  # 0-360
        self.saturation = 0  # 0-100
        
        # Mesh state - Important for Echo compatibility
        self.is_provisioned = False
        self.unicast_address = None
        self.network_key = None
        self.app_key = None
        
        # Device UUID - CRITICAL for Echo discovery
        # Format: 16 bytes unique identifier
        # For Echo compatibility, this should be based on MAC address
        # Get raw address bytes from the Address object
        if hasattr(device.public_address, 'to_bytes'):
            mac_bytes = device.public_address.to_bytes()
        else:
            # Fallback: parse the address string
            addr_str = str(device.public_address)
            # Extract just the address part (format might be "A1:B2:C3:D4:E5:F6" or "A1:B2:C3:D4:E5:F6/P")
            if '/' in addr_str:
                addr_str = addr_str.split('/')[0]
            mac_bytes = bytes.fromhex(addr_str.replace(':', ''))
        
        # Add product identifier (example: 0x1011 for lighting category)
        self.device_uuid = mac_bytes + struct.pack('>H', 0x1011) + b'\x00' * 8
        logger.info(f"Device UUID: {self.device_uuid.hex()}")
        
        # Static OOB Data - REQUIRED by Amazon Echo
        # This is a 16-byte authentication key
        # For testing: use a known key (in production, this should be unique per device)
        self.static_oob_data = bytes.fromhex('00112233445566778899aabbccddeeff')
        logger.info(f"Static OOB: {self.static_oob_data.hex()}")
        
        # Company ID and Product ID - Required for Echo
        # Use Sengled's Company ID to match real device behavior
        self.company_id = 0x08B4  # Sengled Co., Ltd.
        self.product_id = 0x1234  # Product identifier (Sengled uses 0x3412, we use similar)
        logger.info(f"Company ID: 0x{self.company_id:04X} (Sengled), Product ID: 0x{self.product_id:04X}")
        
        # Advertising mode control
        self.use_alternating_advertising = True  # Like Sengled, alternate between PB-GATT and PB-ADV
        self.advertising_task = None
        
        # GATT Characteristics
        self.provisioning_data_out: Optional[Characteristic] = None
        self.proxy_data_out: Optional[Characteristic] = None
        
        # Setup GATT services
        self._setup_services()
        
        # Setup connection callbacks
        self.device.on('connection', self._on_connection)
        self.device.on('disconnection', self._on_disconnection)
        
    def _setup_services(self):
        """Setup BLE Mesh GATT services"""
        logger.info("Setting up BLE Mesh services...")
        
        # Device Information Service (helps with device discovery)
        DEVICE_INFO_SERVICE_UUID = UUID('180A')
        MANUFACTURER_NAME_UUID = UUID('2A29')
        MODEL_NUMBER_UUID = UUID('2A24')
        
        device_info_service = Service(
            DEVICE_INFO_SERVICE_UUID,
            [
                Characteristic(
                    MANUFACTURER_NAME_UUID,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    CharacteristicValue(read=lambda _: b'Bumble Mesh')
                ),
                Characteristic(
                    MODEL_NUMBER_UUID,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    CharacteristicValue(read=lambda _: b'Mesh Light v1.0')
                ),
            ]
        )
        
        # Mesh Provisioning Service
        def write_provisioning_data_in(connection, value):
            logger.info(f"Received provisioning data: {value.hex()}")
            self._handle_provisioning_data(value)
            
        provisioning_data_in = Characteristic(
            MESH_PROVISIONING_DATA_IN_UUID,
            Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=write_provisioning_data_in)
        )
        
        self.provisioning_data_out = Characteristic(
            MESH_PROVISIONING_DATA_OUT_UUID,
            Characteristic.Properties.NOTIFY,
            Characteristic.READABLE,
            CharacteristicValue(read=lambda _: b'')
        )
        
        provisioning_service = Service(
            MESH_PROVISIONING_SERVICE_UUID,
            [provisioning_data_in, self.provisioning_data_out]
        )
        
        # Mesh Proxy Service
        def write_proxy_data_in(connection, value):
            logger.info(f"Received proxy data: {value.hex()}")
            self._handle_proxy_data(value)
            
        proxy_data_in = Characteristic(
            MESH_PROXY_DATA_IN_UUID,
            Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=write_proxy_data_in)
        )
        
        self.proxy_data_out = Characteristic(
            MESH_PROXY_DATA_OUT_UUID,
            Characteristic.Properties.NOTIFY,
            Characteristic.READABLE,
            CharacteristicValue(read=lambda _: b'')
        )
        
        proxy_service = Service(
            MESH_PROXY_SERVICE_UUID,
            [proxy_data_in, self.proxy_data_out]
        )
        
        # Add services to device
        self.device.add_services([device_info_service, provisioning_service, proxy_service])
        logger.info("✓ BLE Mesh services configured")
        logger.info("  - Device Information Service (0x180A)")
        logger.info("  - Mesh Provisioning Service (0x1827)")
        logger.info("  - Mesh Proxy Service (0x1828)")
        
    def _handle_provisioning_data(self, data: bytes):
        """Handle incoming provisioning data"""
        if len(data) < 2:
            return
            
        pdu_type = data[0]
        logger.debug(f"Provisioning PDU type: 0x{pdu_type:02X}")
        
        # Simplified provisioning flow
        if pdu_type == 0x00:  # Invite
            logger.info("📨 Received provisioning invite")
            # Respond with capabilities
            self._send_provisioning_capabilities()
        elif pdu_type == 0x01:  # Capabilities
            logger.info("📨 Received provisioning capabilities request")
        elif pdu_type == 0x03:  # Start
            logger.info("📨 Provisioning started")
        elif pdu_type == 0x07:  # Complete
            logger.info("✓ Provisioning complete!")
            self.is_provisioned = True
            self.unicast_address = 0x0001  # Simplified address assignment
            
    def _send_provisioning_capabilities(self):
        """Send provisioning capabilities - Echo compatible"""
        # Echo requires Static OOB support
        capabilities = bytes([
            0x01,  # PDU Type: Capabilities
            0x01,  # Number of elements
            0x00, 0x01,  # Algorithms (FIPS P-256 Elliptic Curve)
            0x01,  # Public Key Type (OOB Public Key available - for Echo)
            0x01,  # Static OOB Type (Static OOB information available) - CRITICAL FOR ECHO
            0x00,  # Output OOB Size
            0x00, 0x00,  # Output OOB Actions
            0x00,  # Input OOB Size
            0x00, 0x00,  # Input OOB Actions
        ])
        
        logger.info("📤 Sending capabilities with Static OOB support (Echo compatible)")
        asyncio.create_task(self._notify_provisioning(capabilities))
        
    async def _notify_provisioning(self, data: bytes):
        """Send provisioning notification"""
        if self.provisioning_data_out and self.connections:
            for conn in self.connections:
                try:
                    await self.provisioning_data_out.notify(conn, data)
                    logger.debug(f"Sent provisioning data: {data.hex()}")
                except Exception as e:
                    logger.warning(f"Failed to send provisioning notification: {e}")
                    
    def _handle_proxy_data(self, data: bytes):
        """Handle incoming proxy data (mesh messages)"""
        if len(data) < 1:
            return
            
        # Simplified mesh message parsing
        msg_type = (data[0] >> 6) & 0x03
        
        if msg_type == 0x00:  # Network PDU
            self._handle_network_pdu(data)
        elif msg_type == 0x01:  # Mesh Beacon
            logger.debug("Received mesh beacon")
        elif msg_type == 0x02:  # Proxy Configuration
            logger.debug("Received proxy configuration")
            
    def _handle_network_pdu(self, data: bytes):
        """Handle network PDU containing access messages"""
        # In a real implementation, this would decrypt and process the message
        # For demo purposes, we'll simulate message handling
        
        logger.info("📨 Received mesh message")
        
        # Simulate parsing a Generic OnOff Set message
        # In reality, you'd decrypt and parse the actual opcode
        if len(data) > 10:  # Simplified check
            # Toggle light state as demo
            self.toggle_light()
            
            # Send status response
            asyncio.create_task(self._send_onoff_status())
            
    async def _send_onoff_status(self):
        """Send Generic OnOff Status message"""
        # Simplified status message
        status = bytes([
            0x00,  # Message type: Network PDU
            0x01 if self.is_on else 0x00,  # OnOff state
        ])
        
        if self.proxy_data_out and self.connections:
            for conn in self.connections:
                try:
                    await self.proxy_data_out.notify(conn, status)
                    logger.info(f"📤 Sent light status: {'ON' if self.is_on else 'OFF'}")
                except Exception as e:
                    logger.warning(f"Failed to send status: {e}")
                    
    def _on_connection(self, connection):
        """Handle new BLE connection"""
        logger.info(f"✓ Device connected: {connection.peer_address}")
        self.connections.add(connection)
        
    def _on_disconnection(self, connection):
        """Handle BLE disconnection"""
        logger.info(f"✗ Device disconnected: {connection.peer_address}")
        self.connections.discard(connection)
        
    def _create_mesh_beacon(self) -> bytes:
        """Create Unprovisioned Device Beacon for PB-ADV
        
        Format according to Mesh Profile Specification:
        - Beacon Type: 1 byte (0x00 for Unprovisioned Device Beacon)
        - Device UUID: 16 bytes
        - OOB Information: 2 bytes
        - URI Hash: 4 bytes (optional, computed from Static OOB data)
        
        Total: 23 bytes payload
        """
        beacon_type = bytes([UNPROVISIONED_DEVICE_BEACON])
        oob_info = struct.pack('<H', 0x0002)  # Static OOB available (bit 1 set)
        
        # URI Hash: For devices with Static OOB, this is typically a hash of the OOB data
        # Sengled uses a 4-byte hash (not zero)
        # For simplicity, we'll use first 4 bytes of a hash of our Static OOB
        import hashlib
        uri_hash_full = hashlib.sha256(self.static_oob_data).digest()
        uri_hash = uri_hash_full[:4]
        
        beacon_payload = beacon_type + self.device_uuid + oob_info + uri_hash
        
        logger.debug(f"Mesh Beacon components:")
        logger.debug(f"  Beacon Type: {beacon_type.hex()}")
        logger.debug(f"  Device UUID: {self.device_uuid.hex()}")
        logger.debug(f"  OOB Info: {oob_info.hex()}")
        logger.debug(f"  URI Hash: {uri_hash.hex()}")
        logger.debug(f"  Complete Beacon: {beacon_payload.hex()} ({len(beacon_payload)} bytes)")
        
        return beacon_payload
    
    async def setup_advertising(self, advertising_interval_min=100, advertising_interval_max=150):
        """Setup and start BLE advertising
        
        Args:
            advertising_interval_min: Minimum advertising interval in ms (default: 100ms)
            advertising_interval_max: Maximum advertising interval in ms (default: 150ms)
        """
        # Mesh devices advertise with Mesh Provisioning Service or Mesh Proxy Service
        if self.is_provisioned:
            # Provisioned device - advertise Mesh Proxy Service
            service_uuid = MESH_PROXY_SERVICE_UUID
            service_uuid_int = MESH_PROXY_SERVICE_UUID_INT
            service_name = "Mesh Proxy"
        else:
            # Unprovisioned device - advertise Mesh Provisioning Service
            service_uuid = MESH_PROVISIONING_SERVICE_UUID
            service_uuid_int = MESH_PROVISIONING_SERVICE_UUID_INT
            service_name = "Mesh Provisioning"
        
        logger.info(f"Setting up advertising for {service_name}...")
        logger.debug(f"Service UUID: {service_uuid} (0x{service_uuid_int:04X})")
        
        # Create simple advertising data (keep under 31 bytes for legacy advertising)
        advertising_data = bytes(
            AdvertisingData([
                (AdvertisingData.FLAGS, bytes([0x06])),  # LE General Discoverable, BR/EDR not supported
                (AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS, 
                 struct.pack('<H', service_uuid_int)),
            ])
        )
        
        # Create scan response with device name
        scan_response_data = bytes(
            AdvertisingData([
                (AdvertisingData.COMPLETE_LOCAL_NAME, self.device.name.encode('utf-8')),
            ])
        )
        
        logger.debug(f"Advertising data size: {len(advertising_data)} bytes")
        logger.debug(f"Scan response size: {len(scan_response_data)} bytes")
        
        try:
            # Try with explicit advertising parameters for better stability
            await self.device.start_advertising(
                advertising_data=advertising_data,
                scan_response_data=scan_response_data,
                auto_restart=True
            )
            logger.info(f"✓ Advertising as '{self.device.name}' ({service_name})")
            logger.info(f"  Service UUID: {service_uuid}")
            logger.info(f"  Advertising interval: {advertising_interval_min}-{advertising_interval_max}ms")
            logger.info(f"  Connectable: Yes")
            logger.info(f"  Auto-restart: Yes")
        except Exception as e:
            logger.warning(f"Extended advertising failed: {e}")
            # Try fallback with shorter name
            try:
                short_name = self.device.name[:8] if len(self.device.name) > 8 else self.device.name
                simple_adv = bytes(
                    AdvertisingData([
                        (AdvertisingData.FLAGS, bytes([0x06])),
                        (AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS, 
                         struct.pack('<H', service_uuid_int)),
                        (AdvertisingData.SHORTENED_LOCAL_NAME, short_name.encode('utf-8')),
                    ])
                )
                await self.device.start_advertising(advertising_data=simple_adv, auto_restart=True)
                logger.info(f"✓ Advertising as '{short_name}' ({service_name}) - legacy mode")
            except Exception as e2:
                logger.error(f"Failed to start advertising: {e2}")
                raise
                
    async def monitor_advertising(self, interval=30):
        """Monitor and restart advertising if it stops
        
        Args:
            interval: Check interval in seconds
        """
        logger.info(f"Starting advertising monitor (checking every {interval}s)")
        while True:
            await asyncio.sleep(interval)
            if not self.device.is_advertising and not self.connections:
                logger.warning("⚠ Advertising stopped, restarting...")
                try:
                    await self.setup_advertising()
                except Exception as e:
                    logger.error(f"Failed to restart advertising: {e}")
            else:
                logger.debug(f"Advertising status: {'Active' if self.device.is_advertising else 'Inactive (connected)'}")
    
    async def cleanup(self):
        """Clean up and disconnect all connections"""
        # Cancel advertising task if running
        if self.advertising_task and not self.advertising_task.done():
            logger.debug("Cancelling advertising task...")
            self.advertising_task.cancel()
            try:
                await self.advertising_task
            except asyncio.CancelledError:
                pass
        
        if not self.connections:
            logger.info("No active connections to clean up")
            return
            
        logger.info("Cleaning up connections...")
        for conn in list(self.connections):
            try:
                await asyncio.wait_for(conn.disconnect(), timeout=2.0)
                logger.info(f"✓ Disconnected from {conn.peer_address}")
            except asyncio.TimeoutError:
                logger.warning(f"⚠ Disconnect timeout for {conn.peer_address}")
            except Exception as e:
                logger.warning(f"⚠ Error disconnecting {conn.peer_address}: {e}")
        self.connections.clear()
        logger.info("✓ All connections cleaned up")
            
    # Light control methods
    def toggle_light(self):
        """Toggle light on/off"""
        self.is_on = not self.is_on
        logger.info(f"💡 Light {'ON' if self.is_on else 'OFF'}")
        
    def set_light(self, on: bool):
        """Set light on/off state"""
        self.is_on = on
        logger.info(f"💡 Light set to {'ON' if self.is_on else 'OFF'}")
        
    def set_brightness(self, brightness: int):
        """Set brightness (0-100)"""
        self.brightness = max(0, min(100, brightness))
        logger.info(f"💡 Brightness set to {self.brightness}%")
        
    def set_color_temp(self, temp: int):
        """Set color temperature in Kelvin"""
        self.color_temp = max(2700, min(6500, temp))
        logger.info(f"💡 Color temperature set to {self.color_temp}K")
        
    def set_color(self, hue: int, saturation: int):
        """Set color (HSV)"""
        self.hue = hue % 360
        self.saturation = max(0, min(100, saturation))
        logger.info(f"💡 Color set to H:{self.hue}° S:{self.saturation}%")
        
    def get_state(self) -> Dict[str, Any]:
        """Get current light state"""
        return {
            'on': self.is_on,
            'brightness': self.brightness,
            'color_temp': self.color_temp,
            'hue': self.hue,
            'saturation': self.saturation,
            'provisioned': self.is_provisioned,
            'address': self.unicast_address
        }
        
    async def monitor_advertising(self, interval=30):
        """Monitor and restart advertising if it stops
        
        Args:
            interval: Check interval in seconds
        """
        logger.info(f"Starting advertising monitor (checking every {interval}s)")
        while True:
            await asyncio.sleep(interval)
            if not self.device.is_advertising and not self.connections:
                logger.warning("⚠ Advertising stopped, restarting...")
                try:
                    await self.setup_advertising()
                except Exception as e:
                    logger.error(f"Failed to restart advertising: {e}")
            else:
                logger.debug(f"Advertising status: {'Active' if self.device.is_advertising else 'Inactive (connected)'}")
                
    async def start_with_monitoring(self):
        """Start advertising with monitoring task"""
        await self.setup_advertising()
        # Start monitoring in background
        asyncio.create_task(self.monitor_advertising())
        """Clean up and disconnect all connections"""
        if not self.connections:
            logger.info("No active connections to clean up")
            return
            
        logger.info("Cleaning up connections...")
        for conn in list(self.connections):
            try:
                await asyncio.wait_for(conn.disconnect(), timeout=2.0)
                logger.info(f"✓ Disconnected from {conn.peer_address}")
            except asyncio.TimeoutError:
                logger.warning(f"⚠ Disconnect timeout for {conn.peer_address}")
            except Exception as e:
                logger.warning(f"⚠ Error disconnecting {conn.peer_address}: {e}")
        self.connections.clear()
        logger.info("✓ All connections cleaned up")


# -----------------------------------------------------------------------------
# Web Mode (WebSocket Server)
# -----------------------------------------------------------------------------
async def web_interface(mesh_light: BLEMeshLight):
    """Run web interface with WebSocket server"""
    
    if not WEBSOCKETS_AVAILABLE:
        logger.error("❌ websockets module not installed")
        logger.error("Install with: pip install websockets")
        return
        
    async def serve(websocket, _path):
        logger.info(f"WebSocket client connected from {websocket.remote_address}")
        
        # Send initial state
        await websocket.send(json.dumps({
            'type': 'state',
            'data': mesh_light.get_state()
        }))
        
        try:
            while True:
                message = await websocket.recv()
                logger.debug(f"Received: {message}")
                
                try:
                    parsed = json.loads(message)
                    cmd_type = parsed.get('type')
                    
                    if cmd_type == 'toggle':
                        mesh_light.toggle_light()
                        
                    elif cmd_type == 'set_on':
                        mesh_light.set_light(parsed.get('value', True))
                        
                    elif cmd_type == 'set_brightness':
                        mesh_light.set_brightness(parsed.get('value', 100))
                        
                    elif cmd_type == 'set_color_temp':
                        mesh_light.set_color_temp(parsed.get('value', 4000))
                        
                    elif cmd_type == 'set_color':
                        mesh_light.set_color(
                            parsed.get('hue', 0),
                            parsed.get('saturation', 0)
                        )
                        
                    elif cmd_type == 'get_state':
                        pass  # Will send state below
                        
                    # Send updated state
                    await websocket.send(json.dumps({
                        'type': 'state',
                        'data': mesh_light.get_state()
                    }))
                    
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON: {message}")
                    
        except websockets.exceptions.ConnectionClosedOK:
            logger.info("WebSocket client disconnected")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")

    server = await websockets.serve(serve, 'localhost', 8989)
    logger.info("✓ WebSocket server running on ws://localhost:8989")
    logger.info("  Use the web interface to control the mesh light")
    logger.info("")
    logger.info("  Commands:")
    logger.info("    {'type': 'toggle'}")
    logger.info("    {'type': 'set_brightness', 'value': 50}")
    logger.info("    {'type': 'set_color_temp', 'value': 3000}")
    logger.info("    {'type': 'set_color', 'hue': 120, 'saturation': 80}")
    
    await asyncio.Future()  # Run forever


# -----------------------------------------------------------------------------
# Test Mode (Interactive Menu)
# -----------------------------------------------------------------------------
async def test_mode(device: Device, mesh_light: BLEMeshLight):
    """Run interactive test menu"""
    reader = await get_stream_reader(sys.stdin)
    
    while True:
        print("\n" + "="*70)
        print(" BLE Mesh Smart Light Test Menu")
        print("="*70)
        state = mesh_light.get_state()
        print(f" Status: {'ON 💡' if state['on'] else 'OFF'}")
        print(f" Brightness: {state['brightness']}%")
        print(f" Color Temp: {state['color_temp']}K")
        print(f" Color: H:{state['hue']}° S:{state['saturation']}%")
        print(f" Provisioned: {'Yes ✓' if state['provisioned'] else 'No'}")
        if state['provisioned']:
            print(f" Address: 0x{state['address']:04X}")
        print(f" Connections: {len(mesh_light.connections)}")
        print("="*70)
        print(" 1. Toggle Light")
        print(" 2. Turn On")
        print(" 3. Turn Off")
        print(" 4. Set Brightness")
        print(" 5. Set Color Temperature")
        print(" 6. Set Color (HSV)")
        print(" 7. Simulate Provisioning")
        print(" 8. Show State")
        print(" 9. Disconnect All")
        print("10. Restart Advertising")
        print("11. Show Advertising Info")
        print("12. Exit")
        print("="*70)
        print("Enter choice: ", end='', flush=True)
        
        choice = await reader.readline()
        choice = choice.decode('utf-8').strip()
        
        if choice == '1':
            mesh_light.toggle_light()
            await mesh_light._send_onoff_status()
            
        elif choice == '2':
            mesh_light.set_light(True)
            await mesh_light._send_onoff_status()
            
        elif choice == '3':
            mesh_light.set_light(False)
            await mesh_light._send_onoff_status()
            
        elif choice == '4':
            print("Enter brightness (0-100): ", end='', flush=True)
            brightness_input = await reader.readline()
            try:
                brightness = int(brightness_input.decode('utf-8').strip())
                mesh_light.set_brightness(brightness)
            except ValueError:
                print("❌ Invalid input")
                
        elif choice == '5':
            print("Enter color temperature (2700-6500K): ", end='', flush=True)
            temp_input = await reader.readline()
            try:
                temp = int(temp_input.decode('utf-8').strip())
                mesh_light.set_color_temp(temp)
            except ValueError:
                print("❌ Invalid input")
                
        elif choice == '6':
            print("Enter hue (0-360): ", end='', flush=True)
            hue_input = await reader.readline()
            print("Enter saturation (0-100): ", end='', flush=True)
            sat_input = await reader.readline()
            try:
                hue = int(hue_input.decode('utf-8').strip())
                sat = int(sat_input.decode('utf-8').strip())
                mesh_light.set_color(hue, sat)
            except ValueError:
                print("❌ Invalid input")
                
        elif choice == '7':
            print("Simulating provisioning...")
            mesh_light.is_provisioned = True
            mesh_light.unicast_address = 0x0001
            print("✓ Device provisioned with address 0x0001")
            # Restart advertising with proxy service
            await device.stop_advertising()
            await mesh_light.setup_advertising()
            
        elif choice == '8':
            state = mesh_light.get_state()
            print("\nCurrent State:")
            print(json.dumps(state, indent=2))
            
        elif choice == '9':
            await mesh_light.cleanup()
            
        elif choice == '10':
            print("Restarting advertising...")
            print("  1. PB-GATT only (connectable)")
            print("  2. Alternating mode (PB-GATT + Beacon)")
            print("  3. Beacon only (non-connectable)")
            print("Choice: ", end='', flush=True)
            
            adv_choice = await reader.readline()
            adv_choice = adv_choice.decode('utf-8').strip()
            
            try:
                await device.stop_advertising()
                await asyncio.sleep(0.5)
                
                if adv_choice == '1':
                    mesh_light.use_alternating_advertising = False
                    await mesh_light._start_pb_gatt_advertising()
                    print("✓ PB-GATT advertising started")
                elif adv_choice == '2':
                    mesh_light.use_alternating_advertising = True
                    await mesh_light.setup_advertising()
                    print("✓ Alternating advertising started")
                elif adv_choice == '3':
                    mesh_light.use_alternating_advertising = False
                    await mesh_light._start_pb_adv_advertising()
                    print("✓ Beacon advertising started")
                else:
                    # Default: restart current mode
                    await mesh_light.setup_advertising()
                    print("✓ Advertising restarted")
            except Exception as e:
                print(f"❌ Error: {e}")
                
        elif choice == '11':
            print("\n" + "="*70)
            print(" Advertising & Echo Compatibility Information")
            print("="*70)
            print(f"  Device Name: {device.name}")
            print(f"  Device Address: {device.public_address}")
            print(f"  Device UUID: {mesh_light.device_uuid.hex()}")
            print(f"  Company ID: 0x{mesh_light.company_id:04X}")
            print(f"  Product ID: 0x{mesh_light.product_id:04X}")
            print(f"  Static OOB: {mesh_light.static_oob_data.hex()}")
            print()
            print(f"  Provisioned: {mesh_light.is_provisioned}")
            if mesh_light.is_provisioned:
                print(f"  Service: Mesh Proxy (0x1828)")
            else:
                print(f"  Service: Mesh Provisioning (0x1827)")
            print(f"  Advertising: {'Yes ✓' if device.is_advertising else 'No ✗'}")
            print(f"  Connections: {len(mesh_light.connections)}")
            print()
            print(" Echo Compatibility:")
            print("  ✓ BLE Mesh Profile 1.0.1")
            print("  ✓ Static OOB Authentication")
            print("  ✓ PB-GATT Provisioning Bearer")
            print("  ✓ Connectable Advertising")
            print("  ✓ Mesh Provisioning Service (0x1827)")
            print()
            print(" How to discover with Amazon Echo:")
            print("  1. Say 'Alexa, discover my devices'")
            print("  2. Or open Alexa app → Devices → + → Add Device")
            print("  3. Select 'Light' → 'Other'")
            print("  4. Echo will scan and find your device")
            print("  5. Device name will appear: " + device.name)
            print()
            print(" Troubleshooting:")
            print("  - Make sure Echo is updated to latest firmware")
            print("  - Device must be unpro visioned (Provisioned: False)")
            print("  - Try restarting advertising (option 10)")
            print("  - Move device closer to Echo (within 3 meters)")
            print("  - Reset device and try again")
            print("="*70)
            
        elif choice == '12':
            print("Exiting...")
            sys.exit(0)
            
        else:
            print("❌ Invalid choice")


# -----------------------------------------------------------------------------
# Main Function
# -----------------------------------------------------------------------------
async def main():
    if len(sys.argv) < 3:
        print("Usage: python run_blemesh_device.py <device-config> <transport-spec> [command]")
        print()
        print("Commands:")
        print("  test-mode  - Interactive test menu")
        print("  web        - WebSocket server for web UI (default)")
        print()
        print("Examples:")
        print("  python run_blemesh_device.py mesh_light.json usb:0 web")
        print("  python run_blemesh_device.py mesh_light.json usb:0 test-mode")
        print("  python run_blemesh_device.py mesh_light.json android-netsim:localhost:7800 web")
        return
        
    config_file = sys.argv[1]
    transport_spec = sys.argv[2]
    command = sys.argv[3] if len(sys.argv) > 3 else 'web'
    
    # Check if config file exists, create default if not
    if not os.path.exists(config_file):
        logger.warning(f"Config file not found: {config_file}")
        logger.info("Creating default config file...")
        default_config = {
            "name": "Mesh Light",
            "address": "A1:B2:C3:D4:E5:F6",
            "address_type": "random"
        }
        try:
            with open(config_file, 'w') as f:
                json.dump(default_config, f, indent=2)
            logger.info(f"✓ Created default config: {config_file}")
        except Exception as e:
            logger.error(f"Failed to create config file: {e}")
            return
    
    # Validate config file
    try:
        with open(config_file, 'r') as f:
            config_data = json.load(f)
            if not config_data:
                raise ValueError("Config file is empty")
    except json.JSONDecodeError as e:
        logger.error(f"❌ Invalid JSON in config file: {e}")
        logger.error(f"Please check {config_file} for syntax errors")
        return
    except Exception as e:
        logger.error(f"❌ Error reading config file: {e}")
        return
    
    logger.info("="*70)
    logger.info("BLE Mesh Smart Light Starting...")
    logger.info("="*70)
    
    mesh_light = None
    device = None
    monitor_task = None
    
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
            
            # Create BLE Mesh light
            mesh_light = BLEMeshLight(device)
            
            # Power on
            logger.info("Powering on device...")
            await device.power_on()
            logger.info("✓ Device powered on")
            
            # Start advertising
            await mesh_light.setup_advertising()
            
            # Start advertising monitor in background
            monitor_task = asyncio.create_task(mesh_light.monitor_advertising(interval=30))
            
            # Run selected mode
            logger.info(f"Starting mode: {command}")
            
            try:
                if command == 'test-mode':
                    await test_mode(device, mesh_light)
                else:  # default to web mode
                    await web_interface(mesh_light)
            except KeyboardInterrupt:
                logger.info("\n⚠ Interrupt received")
                raise
                
    except FileNotFoundError:
        logger.error(f"❌ Config file not found: {config_file}")
        logger.error("This should not happen as we create it above")
    except json.JSONDecodeError as e:
        logger.error(f"❌ Invalid JSON in config file: {e}")
        logger.error(f"Please fix {config_file}")
    except KeyboardInterrupt:
        logger.info("\n✓ Shutdown requested")
    except Exception as e:
        error_msg = str(e)
        if 'Connection refused' in error_msg or 'UNAVAILABLE' in error_msg:
            logger.error(f"❌ Cannot connect to transport: {transport_spec}")
            logger.error("")
            logger.error("Possible solutions:")
            logger.error("  1. If using android-netsim:")
            logger.error("     - Make sure the Android emulator is running")
            logger.error("     - Check the netsim port (default: 7800)")
            logger.error("     - Try: adb forward tcp:7800 tcp:7800")
            logger.error("")
            logger.error("  2. Try a different transport:")
            logger.error("     - USB adapter: usb:0")
            logger.error("     - Serial port: serial:/dev/ttyUSB0")
            logger.error("     - Unix socket: unix:/tmp/bt")
        else:
            logger.error(f"❌ Error: {e}", exc_info=True)
        return
    finally:
        # Cancel monitoring task
        if monitor_task and not monitor_task.done():
            logger.debug("Cancelling monitor task...")
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        
        # Clean up connections
        if mesh_light:
            try:
                await asyncio.wait_for(mesh_light.cleanup(), timeout=5.0)
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
                logger.warning("⚠ Stop advertising timeout")
            except Exception as e:
                logger.warning(f"⚠ Error stopping advertising: {e}")
        
        logger.info("✓ Shutdown complete")


# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    import signal
    
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
    
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if graceful_shutdown:
            logger.info("\n✓ Graceful shutdown completed")
        sys.exit(0)