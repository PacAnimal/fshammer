#!/bin/sh
# A second client host on the same machine: a network namespace joined to the host by a veth pair, so the
# server sees a different address and the NFS client keeps separate state (one nfs_client per namespace).
#   netns.sh up                         create it; the server is reachable from it at 10.231.0.1
#   netns.sh mount ARGS...              run mount(8) with ARGS from the namespace, under its own hostname,
#                                       so its NFSv4 client ID differs too; the mount is visible on the host
set -eu

ns=fshammer-client2
case "$1" in
    up)
        if ! ip netns list | grep -qw "$ns"; then
            sudo ip netns add "$ns"
            sudo ip link add fshv0 type veth peer name fshv1
            sudo ip link set fshv1 netns "$ns"
            sudo ip addr add 10.231.0.1/24 dev fshv0
            sudo ip link set fshv0 up
            sudo ip netns exec "$ns" ip addr add 10.231.0.2/24 dev fshv1
            sudo ip netns exec "$ns" ip link set fshv1 up
            sudo ip netns exec "$ns" ip link set lo up
        fi
        ;;
    mount)
        shift
        # nsenter --net leaves the mount namespace alone, which `ip netns exec` would not
        sudo unshare --uts sh -c 'hostname fshammer-client2 && exec nsenter --net=/run/netns/'"$ns"' mount "$@"' \
            mount "$@"
        ;;
    *)
        echo "usage: $0 up | mount ARGS..." >&2
        exit 2
        ;;
esac
