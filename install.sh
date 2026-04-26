#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename "$SCRIPT_DIR")

echo
echo "Installing $SERVICE_NAME..."

# Set permissions
chmod 755 "$SCRIPT_DIR"/*.py
chmod 755 "$SCRIPT_DIR"/*.sh
chmod 755 "$SCRIPT_DIR/service/run"
chmod 755 "$SCRIPT_DIR/service/log/run"

# Create log directory
mkdir -p /var/log/$SERVICE_NAME

# Stop serial-starter's auto-assigned service on our port if running
PORT=$(grep "^port" "$SCRIPT_DIR/config.ini" | sed 's/.*= *//' | xargs basename)
if [ -n "$PORT" ] && [ -d "/service/dbus-modbus-client.serial.$PORT" ]; then
    echo "Stopping conflicting dbus-modbus-client on $PORT..."
    svc -d "/service/dbus-modbus-client.serial.$PORT" 2>/dev/null
fi

# Install udev rule to prevent serial-starter from claiming our adapter
PORT=$(grep "^port" "$SCRIPT_DIR/config.ini" | sed 's/.*= *//')
if [ -n "$PORT" ] && [ -e "$PORT" ]; then
    SERIAL=$(udevadm info -q property -n "$PORT" 2>/dev/null | grep "ID_SERIAL_SHORT=" | cut -d= -f2)
    if [ -n "$SERIAL" ]; then
        UDEV_FILE="/etc/udev/rules.d/localextra.rules"
        UDEV_RULE="ACTION==\"add\", ENV{ID_BUS}==\"usb\", ENV{ID_SERIAL_SHORT}==\"$SERIAL\", ENV{VE_SERVICE}=\"ignore\""
        if ! grep -qF "$SERIAL" "$UDEV_FILE" 2>/dev/null; then
            echo "# Kostal Piko RS485 adapter — managed by $SERVICE_NAME" >> "$UDEV_FILE"
            echo "$UDEV_RULE" >> "$UDEV_FILE"
            echo "Added udev rule for adapter $SERIAL to prevent serial-starter conflict."
        fi
    else
        echo "WARNING: Could not detect adapter serial for $PORT. You may need to manually stop serial-starter."
    fi
fi

# Create service symlink
if [ ! -L "/service/$SERVICE_NAME" ]; then
    echo "Creating service..."
    ln -s "$SCRIPT_DIR/service" "/service/$SERVICE_NAME"
else
    echo "Service already exists, restarting..."
    svc -t "/service/$SERVICE_NAME"
fi

# Add to rc.local for firmware update persistence
filename=/data/rc.local
if [ ! -f "$filename" ]; then
    echo "#!/bin/bash" > "$filename"
    chmod 755 "$filename"
fi
grep -qxF "bash $SCRIPT_DIR/install.sh" "$filename" || echo "bash $SCRIPT_DIR/install.sh" >> "$filename"

echo "Installation complete."
echo "Check status: svstat /service/$SERVICE_NAME"
echo "View logs:    tail -f /var/log/$SERVICE_NAME/current | tai64nlocal"
