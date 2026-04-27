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

# Remove udev rule so serial-starter resumes management of the port
UDEV_RULE_PATH="/etc/udev/rules.d/zz-dbus-kostal-piko.rules"
if [ -f "$UDEV_RULE_PATH" ]; then
    rm -f "$UDEV_RULE_PATH"
    udevadm control --reload-rules 2>/dev/null
    udevadm trigger --action=add --subsystem-match=tty 2>/dev/null
    echo "udev rule removed."
fi

echo "Uninstall complete."
echo "Optionally remove $SCRIPT_DIR manually."
