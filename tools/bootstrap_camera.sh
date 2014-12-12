#!/usr/bin/env bash

function usage() {
    echo "This will tell a new camera to look for its management server"
    echo "at a particular IP address. It can be used to configure new"
    echo "cameras without having to start up the ubiquiti management software."
    echo
    echo "Usage: $0 [user@]camera_ip management_ip"
}

function failed() {
    echo "Failed to configure camera"
    exit 3
}

dest="$1"
mgmt="$2"

if [ -z "$dest" ]; then
    echo "ERROR: You must specify a camera to connect to"
    usage
    exit 1
fi

if [ -z "$mgmt" ]; then
    echo "ERROR: You must specify a management server IP"
    usage
    exit 2
fi

if ! echo "$dest" | grep -q '@'; then
    dest="ubnt@${dest}"
fi

opts="-oForwardX11=no"
result=$(ssh $opts $dest "ubnt_system_cfg setUnifiVideoServer $mgmt") || failed

if [ "$result" = "1" ]; then
    echo SUCCESS
else
    failed
fi
