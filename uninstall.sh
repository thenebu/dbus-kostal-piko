#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename "$SCRIPT_DIR")

echo
echo "Uninstalling $SERVICE_NAME..."

# Remove from rc.local
sed -i "\|bash $SCRIPT_DIR/install.sh|d" /data/rc.local 2>/dev/null
echo "Removed from rc.local."

# Stop and remove service
if [ -L "/service/$SERVICE_NAME" ]; then
    svc -d "/service/$SERVICE_NAME" 2>/dev/null
    sleep 1
    rm "/service/$SERVICE_NAME"
    echo "Service removed."
else
    echo "Service not found."
fi

# Remove udev rule
if grep -qF "Kostal Piko RS485" /etc/udev/rules.d/localextra.rules 2>/dev/null; then
    sed -i "/Kostal Piko RS485/d" /etc/udev/rules.d/localextra.rules
    # Remove the udev rule line that follows the comment
    sed -i '/VE_SERVICE.*ignore.*ENV.*ID_SERIAL_SHORT/d' /etc/udev/rules.d/localextra.rules
    echo "Removed udev rule."
fi

echo "Uninstall complete."
echo "Optionally remove $SCRIPT_DIR manually."
