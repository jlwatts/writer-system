#!/bin/sh
# Publish the writer and Syncthing only on the tailnet.
# Run this on the Pi before recreating the containers. HTTPS has to be
# enabled once in the Tailscale admin console so the certificate can be issued.
set -eu

# tailscaled only accepts serve config from root unless this user is the operator.
if ! tailscale serve status >/dev/null 2>&1; then
    sudo tailscale set --operator="$USER"
fi

tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve --bg --https=8443 http://127.0.0.1:8384
tailscale serve --bg --tcp=22000 tcp://127.0.0.1:22000

echo "Tailscale serve is configured."
echo "Writer: https://<machine>.<tailnet>.ts.net"
echo "Syncthing UI: https://<machine>.<tailnet>.ts.net:8443"
echo "Recreate the containers so the published ports stay on localhost:"
echo "  docker compose up -d --build"
