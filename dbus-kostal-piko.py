#!/usr/bin/env python3

from gi.repository import GLib
import json
import logging
import signal
import sys
import os
import struct
import configparser
import time

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
from vedbus import VeDbusService

try:
    # pymodbus <=2.x
    from pymodbus.client.sync import ModbusSerialClient
except ImportError:
    # pymodbus >=3.x compatibility import
    from pymodbus.client import ModbusSerialClient


# --- Config ---

config_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini")
if not os.path.exists(config_file):
    print(f"ERROR: {config_file} not found. Driver restarts in 60 seconds.")
    time.sleep(60)
    sys.exit(1)

config = configparser.ConfigParser()
config.read(config_file)

log_level_name = config.get("DEFAULT", "logging", fallback="WARNING").strip().upper()
log_level = getattr(logging, log_level_name, None)
if not isinstance(log_level, int):
    print(f"WARNING: Invalid logging level '{log_level_name}', fallback to WARNING.")
    log_level = logging.WARNING
logging.basicConfig(level=log_level)

try:
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
    CONNECT_RETRY_BASE = config.getfloat("DEFAULT", "connect_retry_base", fallback=max(1.0, POLL_INTERVAL / 1000.0))
    CONNECT_RETRY_MAX = config.getfloat("DEFAULT", "connect_retry_max", fallback=60.0)
    RECONNECT_AFTER_ERRORS = config.getint("DEFAULT", "reconnect_after_errors", fallback=15)
    STALE_THRESHOLD = config.getfloat("DEFAULT", "stale_threshold", fallback=60.0)
except (ValueError, configparser.Error) as e:
    logging.error(f"Invalid config.ini: {e}")
    time.sleep(60)
    sys.exit(1)

if SLAVE_ADDR < 1 or SLAVE_ADDR > 247:
    logging.error(f"Invalid slave_address: {SLAVE_ADDR} (must be 1-247)")
    sys.exit(1)
if PV_POSITION not in (0, 1, 2):
    logging.error(f"Invalid position: {PV_POSITION} (must be 0, 1, or 2)")
    sys.exit(1)
if PARITY not in ("N", "E", "O"):
    logging.error(f"Invalid parity: {PARITY} (must be N, E, or O)")
    sys.exit(1)
if POLL_INTERVAL <= 0:
    logging.error(f"Invalid poll_interval: {POLL_INTERVAL} (must be > 0)")
    sys.exit(1)
if CONNECT_RETRY_BASE <= 0:
    logging.error(f"Invalid connect_retry_base: {CONNECT_RETRY_BASE} (must be > 0)")
    sys.exit(1)
if CONNECT_RETRY_MAX < CONNECT_RETRY_BASE:
    logging.error(
        f"Invalid connect_retry_max: {CONNECT_RETRY_MAX} (must be >= connect_retry_base={CONNECT_RETRY_BASE})"
    )
    sys.exit(1)
if RECONNECT_AFTER_ERRORS < 1:
    logging.error(f"Invalid reconnect_after_errors: {RECONNECT_AFTER_ERRORS} (must be >= 1)")
    sys.exit(1)
if STALE_THRESHOLD <= 0:
    logging.error(f"Invalid stale_threshold: {STALE_THRESHOLD} (must be > 0)")
    sys.exit(1)


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


def read_holding_registers(client, address, count):
    """Compat wrapper for pymodbus unit/slave argument changes."""
    try:
        return client.read_holding_registers(address, count=count, unit=SLAVE_ADDR)
    except TypeError:
        return client.read_holding_registers(address, count=count, slave=SLAVE_ADDR)


def read_device_info(client):
    """Read device name and serial number (registers 30071-30096)."""
    try:
        result = read_holding_registers(client, 30071 - 1, 26)
        if result is None or result.isError():
            logging.warning("Could not read device info registers")
            return DEVICE_NAME, "unknown"
        name = read_ascii(result.registers[0:13], 13)
        serial = read_ascii(result.registers[13:26], 13)
        return name or DEVICE_NAME, serial or "unknown"
    except Exception:
        logging.exception("Error reading device info")
        return DEVICE_NAME, "unknown"


# --- dbus service ---

class KostalPikoService:
    def __init__(self, servicename, deviceinstance, device_name, serial_number):
        self._dbusservice = VeDbusService(servicename, register=False)
        self._client = None
        self._error_count = 0
        self._connect_failures = 0
        self._next_connect_ts = 0.0
        self._last_good_read_ts = time.monotonic()
        self._stale = False

        # Outage tracking — for diagnostics and JSON event log lines.
        # An "outage" spans from the first failed read until the next
        # successful read. We log one summary line per outage (greppable
        # prefix OUTAGE_EVENT, JSON body) and surface counters on D-Bus.
        self._outage_start_mono = None     # time.monotonic() at first error
        self._outage_start_wall = None     # time.time() for ISO log
        self._outage_error_count = 0
        self._outage_first_error = ""
        self._last_known_power = None      # AC power on last good read

        self._diag_outage_count = 0
        self._diag_total_read_errors = 0
        self._diag_longest_outage_sec = 0.0
        self._diag_last_outage_ts = 0
        self._diag_last_outage_duration_sec = 0.0
        self._service_start_ts = time.monotonic()

        # Management paths
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion", "1.5.0-thenebu")
        self._dbusservice.add_path("/Mgmt/Connection", f"Modbus RTU {SERIAL_PORT} @{SLAVE_ADDR}")

        # Mandatory paths
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", 0xFFFF)
        self._dbusservice.add_path("/ProductName", device_name)
        self._dbusservice.add_path("/CustomName", device_name)
        self._dbusservice.add_path("/Serial", serial_number)
        self._dbusservice.add_path("/FirmwareVersion", "1.5.0-thenebu")
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Latency", None)
        self._dbusservice.add_path("/ErrorCode", 0)
        self._dbusservice.add_path("/Position", PV_POSITION)
        self._dbusservice.add_path("/StatusCode", 0)

        # Formatters (v can be None after _invalidate)
        def _w(p, v): return f"{v:.0f}W" if v is not None else "---"
        def _v(p, v): return f"{v:.1f}V" if v is not None else "---"
        def _a(p, v): return f"{v:.2f}A" if v is not None else "---"
        def _hz(p, v): return f"{v:.2f}Hz" if v is not None else "---"
        def _kwh(p, v): return f"{v:.2f}kWh" if v is not None else "---"
        def _n(p, v): return f"{v}"

        def _sec(p, v): return f"{v:.1f}s" if v is not None else "---"

        # AC total
        paths = {
            "/Ac/Power":          {"initial": None, "textformat": _w},
            "/Ac/Current":        {"initial": None, "textformat": _a},
            "/Ac/Voltage":        {"initial": None, "textformat": _v},
            "/Ac/Energy/Forward": {"initial": None, "textformat": _kwh},
            "/Ac/Energy/Day":     {"initial": None, "textformat": _kwh},
            "/Ac/MaxPower":       {"initial": PV_MAX, "textformat": _w},
            "/Ac/Position":       {"initial": PV_POSITION, "textformat": _n},
            "/Ac/StatusCode":     {"initial": 0, "textformat": _n},
            "/HoursOfOperation":  {"initial": None, "textformat": _n},
            "/UpdateIndex":       {"initial": 0, "textformat": _n},
            # Diagnostics — counters since service start
            "/Diagnostics/InOutage":                {"initial": 0,   "textformat": _n},
            "/Diagnostics/OutageCount":             {"initial": 0,   "textformat": _n},
            "/Diagnostics/TotalReadErrors":         {"initial": 0,   "textformat": _n},
            "/Diagnostics/LongestOutageSec":        {"initial": 0.0, "textformat": _sec},
            "/Diagnostics/LastOutageTs":            {"initial": 0,   "textformat": _n},
            "/Diagnostics/LastOutageDurationSec":   {"initial": 0.0, "textformat": _sec},
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

    def _on_read_error(self, err_text):
        """Account for one failed read, starting an outage if not already in one."""
        self._diag_total_read_errors += 1
        self._dbusservice["/Diagnostics/TotalReadErrors"] = self._diag_total_read_errors

        if self._outage_start_mono is None:
            self._outage_start_mono = time.monotonic()
            self._outage_start_wall = time.time()
            self._outage_first_error = (err_text or "")[:200]
            self._outage_error_count = 1
            self._dbusservice["/Diagnostics/InOutage"] = 1
        else:
            self._outage_error_count += 1

    def _on_read_success(self):
        """Account for one good read; closes any active outage and emits a summary line."""
        if self._outage_start_mono is None:
            return  # no outage to close

        duration = time.monotonic() - self._outage_start_mono
        end_wall = time.time()
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._outage_start_wall))
        end_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(end_wall))

        event = {
            "event": "outage",
            "start": start_iso,
            "end": end_iso,
            "duration_sec": round(duration, 2),
            "errors": self._outage_error_count,
            "first_error": self._outage_first_error,
            "power_before_w": self._last_known_power,
        }
        # Single-line JSON, greppable prefix for easy extraction:
        #   grep OUTAGE_EVENT current | sed 's/.*OUTAGE_EVENT //' | jq -s '.'
        logging.info("OUTAGE_EVENT " + json.dumps(event, separators=(",", ":")))

        # Update diagnostics
        self._diag_outage_count += 1
        self._diag_last_outage_ts = int(end_wall)
        self._diag_last_outage_duration_sec = round(duration, 2)
        if duration > self._diag_longest_outage_sec:
            self._diag_longest_outage_sec = round(duration, 2)
        self._dbusservice["/Diagnostics/OutageCount"] = self._diag_outage_count
        self._dbusservice["/Diagnostics/LastOutageTs"] = self._diag_last_outage_ts
        self._dbusservice["/Diagnostics/LastOutageDurationSec"] = self._diag_last_outage_duration_sec
        self._dbusservice["/Diagnostics/LongestOutageSec"] = self._diag_longest_outage_sec
        self._dbusservice["/Diagnostics/InOutage"] = 0

        # Reset outage state
        self._outage_start_mono = None
        self._outage_start_wall = None
        self._outage_error_count = 0
        self._outage_first_error = ""

    def _invalidate(self):
        """Set all measurement paths to None so consumers see stale data is gone."""
        for phase in ["L1", "L2", "L3"]:
            self._dbusservice[f"/Ac/{phase}/Power"] = None
            self._dbusservice[f"/Ac/{phase}/Voltage"] = None
            self._dbusservice[f"/Ac/{phase}/Current"] = None
            self._dbusservice[f"/Ac/{phase}/Frequency"] = None
            self._dbusservice[f"/Ac/{phase}/Energy/Forward"] = None
        self._dbusservice["/Ac/Power"] = None
        self._dbusservice["/Ac/Current"] = None
        self._dbusservice["/Ac/Voltage"] = None
        self._dbusservice["/Ac/Energy/Forward"] = None
        self._dbusservice["/Ac/Energy/Day"] = None
        self._dbusservice["/HoursOfOperation"] = None
        self._dbusservice["/Connected"] = 0
        # 10 = Error (per Victron pvinverter status codes). Avoid 0 (Inbetriebnahme),
        # which is more alarming and shown by gui-v2 even for transient blips.
        self._dbusservice["/StatusCode"] = 10
        self._stale = True

    def _maybe_invalidate_stale(self):
        """Invalidate dbus values only after readings have been stale longer than threshold."""
        if self._stale:
            return
        if time.monotonic() - self._last_good_read_ts > STALE_THRESHOLD:
            logging.warning(f"No valid Modbus data for {STALE_THRESHOLD}s — marking values stale")
            self._invalidate()

    def _schedule_connect_retry(self):
        failures = min(self._connect_failures, 6)
        delay = min(CONNECT_RETRY_BASE * (2 ** failures), CONNECT_RETRY_MAX)
        self._next_connect_ts = time.monotonic() + delay
        logging.warning(f"Retrying Modbus connect in {delay:.1f}s (failures={self._connect_failures})")

    def _reconnect(self):
        logging.error("Too many errors, reconnecting...")
        if self._client:
            try:
                self._client.close()
            except Exception:
                logging.warning("Error closing Modbus client during reconnect")
        self._client = None
        self._error_count = 0
        self._connect_failures += 1
        self._schedule_connect_retry()
        # Don't invalidate here — keep last-known values visible.
        # _maybe_invalidate_stale will clear them only if the outage exceeds STALE_THRESHOLD.
        self._maybe_invalidate_stale()

    def _ensure_connection(self):
        if self._client is not None:
            return True

        if time.monotonic() < self._next_connect_ts:
            return False

        self._client = connect_modbus()
        if self._client is None:
            self._connect_failures += 1
            self._schedule_connect_retry()
            return False

        self._connect_failures = 0
        self._next_connect_ts = 0.0
        return True

    def _poll(self):
        try:
            if not self._ensure_connection():
                self._on_read_error("connect_failed")
                self._maybe_invalidate_stale()
                return True

            # Read registers 30001-30056 (address 30000-30055, count=56)
            result = read_holding_registers(self._client, 30001 - 1, 56)

            valid = (
                result is not None
                and not result.isError()
                and hasattr(result, "registers")
                and len(result.registers) >= 56
            )

            if not valid:
                self._error_count += 1
                logging.warning(f"Modbus read error ({self._error_count}): {result}")
                self._on_read_error(str(result))
                if self._error_count >= RECONNECT_AFTER_ERRORS:
                    self._reconnect()
                else:
                    self._maybe_invalidate_stale()
                return True

            self._error_count = 0
            self._last_good_read_ts = time.monotonic()
            self._stale = False
            self._on_read_success()
            self._dbusservice["/Connected"] = 1
            regs = result.registers

            # Register offsets (0-based, regs[N] = Modbus 1-indexed register 30001+N)
            # DC: 30001-30015 (3 strings x 5 regs)
            # AC: 30016-30027 (3 phases x 4 regs)
            # Totals: 30029 (DC P), 30031 (AC P), 30033 (cosφ×100), 30034 (Hz×10)
            # Energy: 30051-30052 lifetime Wh (32-bit BE), 30056 daily Wh
            # Hours:  30054 operating hours

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

            ac_total_power  = regs[30]                                # 30031 W
            grid_freq       = regs[33] / 10.0                         # 30034 Hz × 10
            total_energy    = ((regs[50] << 16) | regs[51]) / 1000.0  # 30051-52 lifetime Wh (32-bit BE) → kWh
            operating_hours = regs[53]                                # 30054 h
            daily_energy    = regs[55] / 1000.0                       # 30056 daily Wh → kWh

            # AC totals
            ac_total_current = ac_l1_current + ac_l2_current + ac_l3_current
            ac_avg_voltage = (ac_l1_voltage + ac_l2_voltage + ac_l3_voltage) / 3.0
            self._last_known_power = ac_total_power

            # Update dbus
            self._dbusservice["/Ac/Power"] = ac_total_power
            self._dbusservice["/Ac/Current"] = round(ac_total_current, 2)
            self._dbusservice["/Ac/Voltage"] = round(ac_avg_voltage, 1)
            self._dbusservice["/Ac/Energy/Forward"] = round(total_energy, 2)
            self._dbusservice["/Ac/Energy/Day"] = round(daily_energy, 3)
            self._dbusservice["/HoursOfOperation"] = operating_hours

            self._dbusservice["/Ac/L1/Power"] = ac_l1_power
            self._dbusservice["/Ac/L1/Voltage"] = round(ac_l1_voltage, 1)
            self._dbusservice["/Ac/L1/Current"] = round(ac_l1_current, 2)
            self._dbusservice["/Ac/L1/Frequency"] = round(grid_freq, 2)
            # Per-phase energy: equal split (no per-phase register available)
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
                f"{grid_freq}Hz | Daily: {daily_energy:.3f}kWh | "
                f"Total: {total_energy:.1f}kWh | Hours: {operating_hours}"
            )

        except Exception as exc:
            logging.exception("Unexpected error in poll cycle")
            self._error_count += 1
            self._on_read_error(f"exception: {exc}")
            if self._error_count >= RECONNECT_AFTER_ERRORS:
                self._reconnect()
            else:
                self._maybe_invalidate_stale()

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

    service = KostalPikoService(
        servicename=servicename,
        deviceinstance=DEVICE_INSTANCE,
        device_name=device_name,
        serial_number=serial_number,
    )

    logging.info(f"Registered on dbus as {servicename}")

    mainloop = GLib.MainLoop()

    def shutdown(signum, frame):
        GLib.idle_add(_cleanup)

    def _cleanup():
        logging.info("Shutting down...")
        if service._client:
            try:
                service._client.close()
            except Exception:
                logging.warning("Error closing Modbus client during shutdown")
        mainloop.quit()
        return False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    mainloop.run()


if __name__ == "__main__":
    main()
