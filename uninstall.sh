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
SERIAL="B000083X"
if grep -qF "$SERIAL" /etc/udev/rules.d/localextra.rules 2>/dev/null; then
    sed -i "/$SERIAL/d" /etc/udev/rules.d/localextra.rules
    sed -i "/Kostal Piko RS485/d" /etc/udev/rules.d/localextra.rules
    echo "Removed udev rule."
fi

echo "Uninstall complete."
echo "Optionally remove $SCRIPT_DIR manually."
