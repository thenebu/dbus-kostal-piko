#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename "$SCRIPT_DIR")
CONFIG_FILE="$SCRIPT_DIR/config.ini"
UDEV_RULE_PATH="/etc/udev/rules.d/zz-dbus-kostal-piko.rules"
UDEV_RULE_TEMPLATE="$SCRIPT_DIR/udev/zz-dbus-kostal-piko.rules.template"

# ─────────────────────────────────────────────────────────────
# Install udev rule that takes our RS485 adapter out of the
# serial-starter rotation. Without this, Victron's serial-starter
# probes 6 different drivers on every USB-RS485 port (cgwacs,
# fzsonick, imt, modbus, gps, vedirect), each holding the port
# for ~30s and clobbering our exclusive Modbus session.
#
# Args: $1 = device path (e.g. /dev/ttyUSB3)
# ─────────────────────────────────────────────────────────────
install_udev_rule() {
    local dev="$1"
    [ -z "$dev" ] && return 1
    [ ! -e "$dev" ] && return 1
    [ ! -f "$UDEV_RULE_TEMPLATE" ] && return 1

    local info vendor_id model_id serial_short
    info=$(udevadm info -q property -n "$dev" 2>/dev/null)
    vendor_id=$(echo "$info"  | grep "^ID_VENDOR_ID="    | cut -d= -f2)
    model_id=$(echo "$info"   | grep "^ID_MODEL_ID="     | cut -d= -f2)
    serial_short=$(echo "$info" | grep "^ID_SERIAL_SHORT=" | cut -d= -f2)
    if [ -z "$vendor_id" ] || [ -z "$model_id" ] || [ -z "$serial_short" ]; then
        echo "  ! Konnte USB-IDs für $dev nicht ermitteln, überspringe udev-Regel."
        return 1
    fi

    # Render template
    local tmp
    tmp=$(mktemp 2>/dev/null || echo "/tmp/zz-dbus-kostal-piko.rules.$$")
    sed -e "s/@VENDOR_ID@/$vendor_id/g" \
        -e "s/@MODEL_ID@/$model_id/g" \
        -e "s/@SERIAL_SHORT@/$serial_short/g" \
        "$UDEV_RULE_TEMPLATE" > "$tmp"

    # Only write if changed (avoids spurious udev reloads on every boot)
    if [ ! -f "$UDEV_RULE_PATH" ] || ! cmp -s "$tmp" "$UDEV_RULE_PATH"; then
        cp "$tmp" "$UDEV_RULE_PATH"
        chmod 644 "$UDEV_RULE_PATH"
        udevadm control --reload-rules 2>/dev/null
        udevadm trigger --action=add --subsystem-match=tty 2>/dev/null
        echo "  ✓ udev-Regel installiert: $UDEV_RULE_PATH (Adapter $serial_short)"
    fi
    rm -f "$tmp"

    # Stop all serial-starter probe services for this port; with the
    # udev rule active they should not auto-start again.
    local port_base
    port_base=$(basename "$dev")
    for svc_name in dbus-cgwacs dbus-fzsonick-48tl dbus-imt-si-rs485tc \
                    dbus-modbus-client.serial gps-dbus vedirect-interface; do
        local svc_path="/service/$svc_name.$port_base"
        if [ -d "$svc_path" ]; then
            svc -d "$svc_path" 2>/dev/null
        fi
    done
}

# ─────────────────────────────────────────────────────────────
# If config.ini already exists → silent install (for rc.local)
# ─────────────────────────────────────────────────────────────
if [ -f "$CONFIG_FILE" ] && [ "$1" != "--setup" ]; then
    # Set permissions
    chmod 755 "$SCRIPT_DIR"/*.py 2>/dev/null
    chmod 755 "$SCRIPT_DIR"/*.sh 2>/dev/null
    chmod 755 "$SCRIPT_DIR/service/run" 2>/dev/null
    chmod 755 "$SCRIPT_DIR/service/log/run" 2>/dev/null
    mkdir -p /var/log/$SERVICE_NAME

    # Reinstall udev rule + stop serial-starter probes for our port.
    # Needed on every boot because /etc/udev/rules.d/ is wiped by VenusOS
    # firmware updates.
    PORT_PATH=$(python3 -c "
import configparser, sys
c = configparser.ConfigParser()
c.read('$CONFIG_FILE')
try:
    print(c.get('MODBUS', 'port'))
except:
    sys.exit(1)
" 2>/dev/null)
    if [ -n "$PORT_PATH" ]; then
        install_udev_rule "$PORT_PATH"
    fi

    # Create/restart service
    if [ ! -L "/service/$SERVICE_NAME" ]; then
        ln -s "$SCRIPT_DIR/service" "/service/$SERVICE_NAME"
    else
        svc -t "/service/$SERVICE_NAME"
    fi

    # Ensure rc.local entry
    filename=/data/rc.local
    if [ ! -f "$filename" ]; then
        echo "#!/bin/bash" > "$filename"
        chmod 755 "$filename"
    fi
    grep -qxF "bash $SCRIPT_DIR/install.sh" "$filename" || echo "bash $SCRIPT_DIR/install.sh" >> "$filename"
    exit 0
fi

# ─────────────────────────────────────────────────────────────
# Interactive setup (first install or --setup flag)
# ─────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Kostal Piko — VenusOS Installer                ║"
echo "║   Modbus RTU driver for old Piko series          ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. USB Port ──────────────────────────────────────────────
echo "── USB-RS485 Adapter ──────────────────────────────"
echo ""
echo "Available USB serial ports:"
echo ""

# Collect ports into an array
PORTS=()
i=1
for dev in /dev/ttyUSB*; do
    if [ -e "$dev" ]; then
        # Get adapter info
        INFO=$(udevadm info -q property -n "$dev" 2>/dev/null)
        MANUFACTURER=$(echo "$INFO" | grep "ID_VENDOR=" | cut -d= -f2)
        MODEL=$(echo "$INFO" | grep "ID_MODEL=" | cut -d= -f2)
        USB_SER=$(echo "$INFO" | grep "ID_SERIAL_SHORT=" | cut -d= -f2)
        # Check if already used by a service
        USED=""
        if [ -d "/service/dbus-modbus-client.serial.$(basename $dev)" ]; then
            USED=" (used by serial-starter)"
        fi
        echo "  $i) $dev — $MANUFACTURER $MODEL [$USB_SER]$USED"
        PORTS+=("$dev")
        i=$((i+1))
    fi
done

if [ ${#PORTS[@]} -eq 0 ]; then
    echo "  No USB serial ports found!"
    echo "  Please connect your USB-RS485 adapter and try again."
    exit 1
fi

echo ""
read -p "Select port [1-${#PORTS[@]}]: " PORT_SEL
PORT_SEL=$((PORT_SEL-1))
if [ $PORT_SEL -lt 0 ] || [ $PORT_SEL -ge ${#PORTS[@]} ]; then
    echo "Invalid selection."
    exit 1
fi
SELECTED_PORT="${PORTS[$PORT_SEL]}"
echo "→ Using: $SELECTED_PORT"
echo ""

# Stop our own service and all serial-starter probes on this port so we
# can probe the inverter without contention. Also installs the udev rule
# right away so newly arriving "add" events don't trigger probes again.
PORT_BASE=$(basename "$SELECTED_PORT")
if [ -L "/service/$SERVICE_NAME" ]; then
    svc -d "/service/$SERVICE_NAME" 2>/dev/null
    sleep 1
fi
install_udev_rule "$SELECTED_PORT"
sleep 1

# ── 2. Modbus Slave Address ─────────────────────────────────
echo "── Modbus Einstellungen ───────────────────────────"
echo ""
read -p "Modbus Slave-Adresse [Standard: 3]: " SLAVE_ADDR
SLAVE_ADDR=${SLAVE_ADDR:-3}
if ! echo "$SLAVE_ADDR" | grep -qE '^[0-9]+$' || [ "$SLAVE_ADDR" -lt 1 ] || [ "$SLAVE_ADDR" -gt 247 ]; then
    echo "Ungültige Slave-Adresse (1-247)."
    exit 1
fi
echo "→ Slave-Adresse: $SLAVE_ADDR"
echo ""

# ── 3. Try to read device name from inverter ────────────────
echo "Verbindungstest zum Wechselrichter..."
DEVICE_NAME_DEFAULT="Kostal Piko"
SERIAL_NUMBER="unknown"

# Quick test read using python + pymodbus
PROBE_RESULT=$(python3 << PYEOF
try:
    from pymodbus.client.sync import ModbusSerialClient
    import struct, time
    client = ModbusSerialClient(method='rtu', port='$SELECTED_PORT', baudrate=19200, parity='E', stopbits=1, timeout=3)
    if client.connect():
        # Read registers 30001-30044 for rated power
        result = client.read_holding_registers(30000, count=44, unit=$SLAVE_ADDR)
        rated = 0
        if result and not result.isError() and hasattr(result, 'registers') and len(result.registers) >= 44:
            rated = result.registers[43]  # 30044 = rated power
        time.sleep(0.5)
        # Read device name (30071-30083) and serial (30084-30096)
        result2 = client.read_holding_registers(30070, count=26, unit=$SLAVE_ADDR)
        if result2 and not result2.isError() and hasattr(result2, 'registers') and len(result2.registers) >= 26:
            raw = b''
            for r in result2.registers[:13]:
                raw += struct.pack('>H', r)
            name = raw.replace(b'\x00', b'').decode('ascii', errors='replace').strip()
            raw2 = b''
            for r in result2.registers[13:26]:
                raw2 += struct.pack('>H', r)
            serial = raw2.replace(b'\x00', b'').decode('ascii', errors='replace').strip()
            print(f'{name}|{serial}|{rated}')
        else:
            print('ERROR|read_failed|0')
        client.close()
    else:
        print('ERROR|no_connection|0')
except Exception as e:
    print(f'ERROR|{e}|0')
PYEOF
)

if echo "$PROBE_RESULT" | grep -q "^ERROR"; then
    echo "⚠  Konnte nicht mit dem Wechselrichter kommunizieren."
    echo "   Prüfe Verkabelung, Slave-Adresse und Baudrate."
    echo ""
    DETECTED_NAME=""
    DETECTED_SERIAL=""
    DETECTED_POWER=0
else
    DETECTED_NAME=$(echo "$PROBE_RESULT" | cut -d'|' -f1)
    DETECTED_SERIAL=$(echo "$PROBE_RESULT" | cut -d'|' -f2)
    DETECTED_POWER=$(echo "$PROBE_RESULT" | cut -d'|' -f3)
    echo "✓  Verbunden!"
    echo "   Gerätename:   $DETECTED_NAME"
    echo "   Seriennummer: $DETECTED_SERIAL"
    if [ "$DETECTED_POWER" -gt 0 ] 2>/dev/null; then
        echo "   Nennleistung: ${DETECTED_POWER}W ($((DETECTED_POWER/1000))kW)"
    fi
    echo ""
fi

# ── 4. Device Name ──────────────────────────────────────────
echo "── Gerätename ─────────────────────────────────────"
echo ""
if [ -n "$DETECTED_NAME" ] && [ "$DETECTED_NAME" != "ERROR" ]; then
    read -p "Gerätename [Enter = '$DETECTED_NAME']: " USER_NAME
    DEVICE_NAME="${USER_NAME:-$DETECTED_NAME}"
else
    read -p "Gerätename [Standard: Kostal Piko]: " USER_NAME
    DEVICE_NAME="${USER_NAME:-Kostal Piko}"
fi
echo "→ Name: $DEVICE_NAME"
echo ""

# ── 5. Rated Power ──────────────────────────────────────────
echo "── Nennleistung ───────────────────────────────────"
echo ""
if [ "$DETECTED_POWER" -gt 0 ] 2>/dev/null; then
    DEFAULT_POWER=$DETECTED_POWER
    echo "  Vom Wechselrichter erkannt: ${DETECTED_POWER}W"
else
    DEFAULT_POWER=17000
fi
echo "  Übliche Werte: 5500 (Piko 5.5), 8500 (Piko 8.5),"
echo "  10000 (Piko 10), 12000 (Piko 12), 15000 (Piko 15),"
echo "  17000 (Piko 17), 20000 (Piko 20)"
echo ""
read -p "Nennleistung in Watt [Standard: $DEFAULT_POWER]: " MAX_POWER
MAX_POWER=${MAX_POWER:-$DEFAULT_POWER}
if ! echo "$MAX_POWER" | grep -qE '^[0-9]+$' || [ "$MAX_POWER" -lt 100 ] || [ "$MAX_POWER" -gt 100000 ]; then
    echo "Ungültige Nennleistung (100-100000 W)."
    exit 1
fi
echo "→ Nennleistung: ${MAX_POWER}W"
echo ""

# ── 6. AC Position ──────────────────────────────────────────
echo "── AC-Position ────────────────────────────────────"
echo ""
echo "  Wo ist der Piko am Victron-System angeschlossen?"
echo ""
echo "  0) AC-Eingang 1 (kein MultiPlus/Quattro vorhanden)"
echo "  1) AC-Ausgang (hinter dem MultiPlus/Quattro)"
echo "  2) AC-Eingang 2"
echo ""
read -p "Position [Standard: 0]: " PV_POSITION
PV_POSITION=${PV_POSITION:-0}
case $PV_POSITION in
    0) POS_DESC="AC-Eingang 1" ;;
    1) POS_DESC="AC-Ausgang" ;;
    2) POS_DESC="AC-Eingang 2" ;;
    *) echo "Ungültige Auswahl."; exit 1 ;;
esac
echo "→ Position: $POS_DESC"
echo ""

# ── 7. VRM Device Instance ─────────────────────────────────
echo "── VRM Device Instance ────────────────────────────"
echo ""
echo "  Eindeutige Nummer für das VRM-Portal."
echo "  Muss sich von anderen Geräten unterscheiden."
echo ""
# Find next free instance
USED_INSTANCES=$(dbus -y 2>/dev/null | grep pvinverter | while read svc; do
    dbus -y "$svc" /DeviceInstance GetValue 2>/dev/null
done | tr -d "'" | sort -n)
if [ -n "$USED_INSTANCES" ]; then
    echo "  Bereits belegte Instanzen: $USED_INSTANCES"
fi
DEFAULT_INSTANCE=52
while echo "$USED_INSTANCES" | grep -qw "$DEFAULT_INSTANCE" 2>/dev/null; do
    DEFAULT_INSTANCE=$((DEFAULT_INSTANCE+1))
done
read -p "Device Instance [Standard: $DEFAULT_INSTANCE]: " DEV_INSTANCE
DEV_INSTANCE=${DEV_INSTANCE:-$DEFAULT_INSTANCE}
if ! echo "$DEV_INSTANCE" | grep -qE '^[0-9]+$' || [ "$DEV_INSTANCE" -lt 1 ] || [ "$DEV_INSTANCE" -gt 512 ]; then
    echo "Ungültige Device Instance (1-512)."
    exit 1
fi
echo "→ Instance: $DEV_INSTANCE"
echo ""

# ─────────────────────────────────────────────────────────────
# Summary & Confirmation
# ─────────────────────────────────────────────────────────────
echo "══════════════════════════════════════════════════"
echo "  Zusammenfassung"
echo "══════════════════════════════════════════════════"
echo "  USB-Port:       $SELECTED_PORT"
echo "  Slave-Adresse:  $SLAVE_ADDR"
echo "  Gerätename:     $DEVICE_NAME"
echo "  Nennleistung:   ${MAX_POWER}W"
echo "  AC-Position:    $POS_DESC ($PV_POSITION)"
echo "  VRM-Instance:   $DEV_INSTANCE"
if [ -n "$DETECTED_SERIAL" ] && [ "$DETECTED_SERIAL" != "unknown" ]; then
    echo "  Seriennummer:   $DETECTED_SERIAL"
fi
echo "══════════════════════════════════════════════════"
echo ""
read -p "Installation starten? [J/n]: " CONFIRM
CONFIRM=${CONFIRM:-J}
if [ "$CONFIRM" != "J" ] && [ "$CONFIRM" != "j" ] && [ "$CONFIRM" != "Y" ] && [ "$CONFIRM" != "y" ]; then
    echo "Abgebrochen."
    exit 0
fi

# ─────────────────────────────────────────────────────────────
# Generate config.ini
# ─────────────────────────────────────────────────────────────
echo ""
echo "Erstelle config.ini..."

cat > "$CONFIG_FILE" << CONF
[DEFAULT]
logging = WARNING
device_name = $DEVICE_NAME
device_instance = $DEV_INSTANCE
poll_interval = 1000

[MODBUS]
port = $SELECTED_PORT
slave_address = $SLAVE_ADDR
baudrate = 19200
parity = E
stopbits = 1
timeout = 3

[PV]
max = $MAX_POWER
position = $PV_POSITION
CONF

# ─────────────────────────────────────────────────────────────
# Install service
# ─────────────────────────────────────────────────────────────

# Set permissions
chmod 755 "$SCRIPT_DIR"/*.py 2>/dev/null
chmod 755 "$SCRIPT_DIR"/*.sh 2>/dev/null
chmod 755 "$SCRIPT_DIR/service/run" 2>/dev/null
chmod 755 "$SCRIPT_DIR/service/log/run" 2>/dev/null
mkdir -p /var/log/$SERVICE_NAME

# Download velib_python if missing
if [ ! -f "$SCRIPT_DIR/ext/velib_python/vedbus.py" ]; then
    echo "Lade velib_python herunter..."
    mkdir -p "$SCRIPT_DIR/ext"
    if command -v git >/dev/null 2>&1; then
        git clone --depth 1 https://github.com/victronenergy/velib_python.git "$SCRIPT_DIR/ext/velib_python" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        mkdir -p "$SCRIPT_DIR/ext/velib_python"
        wget -q -O "$SCRIPT_DIR/ext/velib_python/vedbus.py" "https://raw.githubusercontent.com/victronenergy/velib_python/master/vedbus.py"
        wget -q -O "$SCRIPT_DIR/ext/velib_python/ve_utils.py" "https://raw.githubusercontent.com/victronenergy/velib_python/master/ve_utils.py"
    else
        # Fallback: copy from another driver
        for src in /opt/victronenergy/dbus-systemcalc-py/ext/velib_python \
                   /opt/victronenergy/dbus-modbus-client/ext/velib_python \
                   /data/etc/dbus-mqtt-pv/ext/velib_python; do
            if [ -f "$src/vedbus.py" ]; then
                echo "Kopiere velib_python von $src..."
                cp -r "$src" "$SCRIPT_DIR/ext/velib_python"
                break
            fi
        done
    fi

    if [ ! -f "$SCRIPT_DIR/ext/velib_python/vedbus.py" ]; then
        echo "FEHLER: velib_python konnte nicht installiert werden!"
        echo "Bitte manuell kopieren: cp -r /opt/victronenergy/dbus-systemcalc-py/ext/velib_python $SCRIPT_DIR/ext/"
        exit 1
    fi
fi

# Take the port out of serial-starter rotation and stop all probe services
echo "Konfiguriere udev-Regel für exklusive Portnutzung..."
install_udev_rule "$SELECTED_PORT"

# Create service symlink
if [ ! -L "/service/$SERVICE_NAME" ]; then
    echo "Erstelle Service..."
    ln -s "$SCRIPT_DIR/service" "/service/$SERVICE_NAME"
else
    echo "Service existiert bereits, Neustart..."
    svc -t "/service/$SERVICE_NAME"
fi

# Add to rc.local
filename=/data/rc.local
if [ ! -f "$filename" ]; then
    echo "#!/bin/bash" > "$filename"
    chmod 755 "$filename"
fi
grep -qxF "bash $SCRIPT_DIR/install.sh" "$filename" || echo "bash $SCRIPT_DIR/install.sh" >> "$filename"

echo ""
echo "══════════════════════════════════════════════════"
echo "  Installation abgeschlossen!"
echo "══════════════════════════════════════════════════"
echo ""
echo "  Status:  svstat /service/$SERVICE_NAME"
echo "  Logs:    tail -f /var/log/$SERVICE_NAME/current | tai64nlocal"
echo "  Setup:   bash $SCRIPT_DIR/install.sh --setup"
echo ""
echo "  Der Wechselrichter sollte in wenigen Sekunden"
echo "  im VRM-Portal und auf dem GX-Display erscheinen."
echo ""
