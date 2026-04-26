#!/usr/bin/env python

from gi.repository import GLib
import platform
import logging
import sys
import os
import struct
import configparser

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
from vedbus import VeDbusService

from pymodbus.client.sync import ModbusSerialClient


# --- Config ---

config_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini")
if not os.path.exists(config_file):
    print(f"ERROR: {config_file} not found. Driver restarts in 60 seconds.")
    import time; time.sleep(60)
    sys.exit(1)

config = configparser.ConfigParser()
config.read(config_file)

log_level = getattr(logging, config.get("DEFAULT", "logging", fallback="WARNING"))
logging.basicConfig(level=log_level)

DEVICE_NAME = config.get("DEFAULT", "device_name", fallback="Kostal Piko 17")
DEVICE_INSTANCE = config.getint("DEFAULT", "device_instance", fallback=52)
SERIAL_PORT = config.get("MODBUS", "port", fallback="/dev/ttyUSB3")
SLAVE_ADDR = config.getint("MODBUS", "slave_address", fallback=3)
BAUDRATE = config.getint("MODBUS", "baudrate", fallback=19200)
PARITY = config.get("MODBUS", "parity", fallback="E")
STOPBITS = config.getint("MODBUS", "stopbits", fallback=1)
MB_TIMEOUT = config.getint("MODBUS", "timeout", fallback=2)
PV_MAX = config.getint("PV", "max", fallback=17000)
PV_POSITION = config.getint("PV", "position", fallback=1)
POLL_INTERVAL = config.getint("DEFAULT", "poll_interval", fallback=1000)


# --- Modbus helpers ---

def read_ascii(regs, count):
    """Decode a list of uint16 registers as ASCII string."""
    raw = b""
    for r in regs[:count]:
        raw += struct.pack(">H", r)
    return raw.replace(b"\x00", b"").decode("ascii", errors="replace").strip()


def connect_modbus():
    client = ModbusSerialClient(
        method="rtu",
        port=SERIAL_PORT,
        baudrate=BAUDRATE,
        parity=PARITY,
        stopbits=STOPBITS,
        timeout=MB_TIMEOUT,
    )
    if not client.connect():
        logging.error(f"Cannot open serial port {SERIAL_PORT}")
        return None
    return client


def read_device_info(client):
    """Read device name and serial number (registers 30071-30096)."""
    result = client.read_holding_registers(30071 - 1, count=26, unit=SLAVE_ADDR)
    if result.isError():
        logging.warning("Could not read device info registers")
        return DEVICE_NAME, "unknown"
    name = read_ascii(result.registers[0:13], 13)
    serial = read_ascii(result.registers[13:26], 13)
    return name or DEVICE_NAME, serial or "unknown"


# --- dbus service ---

class KostalPikoService:
    def __init__(self, servicename, deviceinstance, device_name, serial_number):
        self._dbusservice = VeDbusService(servicename, register=False)
        self._client = None
        self._error_count = 0

        # Management paths
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion", "1.0.0-thenebu")
        self._dbusservice.add_path("/Mgmt/Connection", f"Modbus RTU {SERIAL_PORT} @{SLAVE_ADDR}")

        # Mandatory paths
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", 0xFFFF)
        self._dbusservice.add_path("/ProductName", device_name)
        self._dbusservice.add_path("/CustomName", device_name)
        self._dbusservice.add_path("/Serial", serial_number)
        self._dbusservice.add_path("/FirmwareVersion", "1.0.0-thenebu")
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Latency", None)
        self._dbusservice.add_path("/ErrorCode", 0)
        self._dbusservice.add_path("/Position", PV_POSITION)
        self._dbusservice.add_path("/StatusCode", 0)

        # Formatters
        def _w(p, v): return f"{v:.0f}W"
        def _v(p, v): return f"{v:.1f}V"
        def _a(p, v): return f"{v:.2f}A"
        def _hz(p, v): return f"{v:.2f}Hz"
        def _kwh(p, v): return f"{v:.2f}kWh"
        def _n(p, v): return f"{v}"

        # AC total
        paths = {
            "/Ac/Power":          {"initial": None, "textformat": _w},
            "/Ac/Current":        {"initial": None, "textformat": _a},
            "/Ac/Voltage":        {"initial": None, "textformat": _v},
            "/Ac/Energy/Forward": {"initial": None, "textformat": _kwh},
            "/Ac/MaxPower":       {"initial": PV_MAX, "textformat": _w},
            "/Ac/Position":       {"initial": PV_POSITION, "textformat": _n},
            "/Ac/StatusCode":     {"initial": 0, "textformat": _n},
            "/UpdateIndex":       {"initial": 0, "textformat": _n},
        }

        # Per-phase paths
        for phase in ["L1", "L2", "L3"]:
            paths.update({
                f"/Ac/{phase}/Power":          {"initial": None, "textformat": _w},
                f"/Ac/{phase}/Current":        {"initial": None, "textformat": _a},
                f"/Ac/{phase}/Voltage":        {"initial": None, "textformat": _v},
                f"/Ac/{phase}/Frequency":      {"initial": None, "textformat": _hz},
                f"/Ac/{phase}/Energy/Forward": {"initial": None, "textformat": _kwh},
            })

        for path, settings in paths.items():
            self._dbusservice.add_path(
                path, settings["initial"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handle_changed,
            )

        self._dbusservice.register()
        GLib.timeout_add(POLL_INTERVAL, self._poll)

    def _ensure_connection(self):
        if self._client is None:
            self._client = connect_modbus()
        return self._client is not None

    def _poll(self):
        if not self._ensure_connection():
            self._dbusservice["/Connected"] = 0
            logging.error("Modbus not connected, retrying in 5s...")
            GLib.timeout_add(5000, self._poll)
            return False

        # Read registers 30001-30044 (address 30000-30043, count=44)
        result = self._client.read_holding_registers(30001 - 1, count=44, unit=SLAVE_ADDR)

        if result.isError():
            self._error_count += 1
            logging.warning(f"Modbus read error ({self._error_count}): {result}")
            if self._error_count >= 5:
                logging.error("Too many errors, reconnecting...")
                self._client.close()
                self._client = None
                self._error_count = 0
                self._dbusservice["/Connected"] = 0
            return True

        self._error_count = 0
        self._dbusservice["/Connected"] = 1
        regs = result.registers

        # Register offsets (0-based from 30001)
        # DC: 30001-30015 (3 strings x 5 regs)
        # AC: 30016-30027 (3 phases x 4 regs)
        # Totals: 30029, 30031, 30033, 30034
        # Energy: 30038, 30039

        # AC phase data
        ac_l1_voltage = regs[15] / 10.0   # 30016
        ac_l1_current = regs[16] / 100.0  # 30017
        ac_l1_power   = regs[17]          # 30018

        ac_l2_voltage = regs[19] / 10.0   # 30020
        ac_l2_current = regs[20] / 100.0  # 30021
        ac_l2_power   = regs[21]          # 30022

        ac_l3_voltage = regs[23] / 10.0   # 30024
        ac_l3_current = regs[24] / 100.0  # 30025
        ac_l3_power   = regs[25]          # 30026

        ac_total_power = regs[30]         # 30031
        grid_freq      = regs[33] / 10.0  # 30034
        daily_energy   = regs[37] / 1000.0  # 30038 Wh → kWh
        total_energy   = regs[38] * 3     # 30039 kWh per string × 3 strings = total

        # AC totals
        ac_total_current = ac_l1_current + ac_l2_current + ac_l3_current
        ac_avg_voltage = (ac_l1_voltage + ac_l2_voltage + ac_l3_voltage) / 3.0

        # Update dbus
        self._dbusservice["/Ac/Power"] = ac_total_power
        self._dbusservice["/Ac/Current"] = round(ac_total_current, 2)
        self._dbusservice["/Ac/Voltage"] = round(ac_avg_voltage, 1)
        self._dbusservice["/Ac/Energy/Forward"] = round(total_energy, 2)

        self._dbusservice["/Ac/L1/Power"] = ac_l1_power
        self._dbusservice["/Ac/L1/Voltage"] = round(ac_l1_voltage, 1)
        self._dbusservice["/Ac/L1/Current"] = round(ac_l1_current, 2)
        self._dbusservice["/Ac/L1/Frequency"] = round(grid_freq, 2)
        # Distribute total energy proportionally by power share
        if ac_total_power > 0:
            l1_energy = round(total_energy * ac_l1_power / ac_total_power, 2)
            l2_energy = round(total_energy * ac_l2_power / ac_total_power, 2)
            l3_energy = round(total_energy * ac_l3_power / ac_total_power, 2)
        else:
            l1_energy = round(total_energy / 3.0, 2)
            l2_energy = round(total_energy / 3.0, 2)
            l3_energy = round(total_energy / 3.0, 2)
        self._dbusservice["/Ac/L1/Energy/Forward"] = l1_energy

        self._dbusservice["/Ac/L2/Power"] = ac_l2_power
        self._dbusservice["/Ac/L2/Voltage"] = round(ac_l2_voltage, 1)
        self._dbusservice["/Ac/L2/Current"] = round(ac_l2_current, 2)
        self._dbusservice["/Ac/L2/Frequency"] = round(grid_freq, 2)
        self._dbusservice["/Ac/L2/Energy/Forward"] = l2_energy

        self._dbusservice["/Ac/L3/Power"] = ac_l3_power
        self._dbusservice["/Ac/L3/Voltage"] = round(ac_l3_voltage, 1)
        self._dbusservice["/Ac/L3/Current"] = round(ac_l3_current, 2)
        self._dbusservice["/Ac/L3/Frequency"] = round(grid_freq, 2)
        self._dbusservice["/Ac/L3/Energy/Forward"] = l3_energy

        # Status: 7=running, 8=standby
        if ac_total_power >= 10:
            self._dbusservice["/StatusCode"] = 7
        else:
            self._dbusservice["/StatusCode"] = 8

        # UpdateIndex
        idx = (self._dbusservice["/UpdateIndex"] + 1) % 256
        self._dbusservice["/UpdateIndex"] = idx

        logging.info(
            f"Piko: {ac_total_power}W | "
            f"L1: {ac_l1_power}W {ac_l1_voltage}V {ac_l1_current}A | "
            f"L2: {ac_l2_power}W {ac_l2_voltage}V {ac_l2_current}A | "
            f"L3: {ac_l3_power}W {ac_l3_voltage}V {ac_l3_current}A | "
            f"{grid_freq}Hz | Daily: {daily_energy}kWh"
        )

        return True

    def _handle_changed(self, path, value):
        logging.debug(f"External change: {path} = {value}")
        return True


def main():
    from dbus.mainloop.glib import DBusGMainLoop
    DBusGMainLoop(set_as_default=True)

    # Connect to read device info
    client = connect_modbus()
    if client:
        device_name, serial_number = read_device_info(client)
        client.close()
        logging.info(f"Device: {device_name}, Serial: {serial_number}")
    else:
        device_name = DEVICE_NAME
        serial_number = "unknown"
        logging.warning("Could not read device info at startup, using defaults")

    servicename = f"com.victronenergy.pvinverter.kostal_piko_{DEVICE_INSTANCE}"

    KostalPikoService(
        servicename=servicename,
        deviceinstance=DEVICE_INSTANCE,
        device_name=device_name,
        serial_number=serial_number,
    )

    logging.info(f"Registered on dbus as {servicename}")
    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
