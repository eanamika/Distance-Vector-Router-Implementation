#!/usr/bin/env python3
"""
UDP/JSON distance-vector process: Bellman-Ford path selection with split horizon.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# --- Protocol / timing (RIP-style cap) ----------------------------------------
UDP_PORT = 5000
PROTOCOL_VERSION = 1.0
MAX_DISTANCE = 16
ANNOUNCE_PERIOD_SEC = 5.0
SILENCE_LIMIT_SEC = 15.0
STALE_SWEEP_SEC = 5.0

# --- Environment --------------------------------------------------------------
SELF_ADDR = os.getenv("MY_IP", "127.0.0.1")
_RAW_NEIGHBORS = os.getenv("NEIGHBORS", "")
PEER_IPS = [p.strip() for p in _RAW_NEIGHBORS.split(",") if p.strip()]

# Skip `ip route` (e.g. unit tests or unprivileged runs)
SKIP_KERNEL_INSTALL = os.getenv("ROUTER_SKIP_IP", "").lower() in ("1", "true", "yes")
# Extra /24s treated as on-link if interface discovery misses one (Docker)
_EXTRA_NETS = os.getenv("EXTRA_LOCAL_PREFIXES", "").strip()
EXTRA_LOCAL_PREFIXES = [x.strip() for x in _EXTRA_NETS.split(",") if x.strip()]

# --- Shared state -------------------------------------------------------------
# Each prefix maps to [metric, next_hop_ip]
forwarding_table: Dict[str, List[Any]] = {}
on_link_prefixes: frozenset = frozenset()
_state_lock = threading.Lock()

_peer_last_seen: Dict[str, float] = {}
_peer_lock = threading.Lock()


def _ipv4_to_slash24(addr: str) -> str:
    octets = addr.split(".")
    if len(octets) != 4:
        return "0.0.0.0/24"
    return f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"


def _collect_iface_prefixes() -> List[str]:
    """Directly connected /24 prefixes from `ip -j addr`."""
    merged = list(EXTRA_LOCAL_PREFIXES)
    try:
        raw = subprocess.check_output(["ip", "-j", "addr", "show"], text=True, timeout=5)
        for nic in json.loads(raw):
            for entry in nic.get("addr_info", []):
                if entry.get("family") != "inet":
                    continue
                lip = entry.get("local")
                if not lip or lip.startswith("127."):
                    continue
                merged.append(_ipv4_to_slash24(lip))
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError) as err:
        print(f"[warn] interface scan failed ({err}); using MY_IP only", flush=True)
        merged.append(_ipv4_to_slash24(SELF_ADDR))

    ordered: List[str] = []
    seen: set = set()
    for p in merged:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered or [_ipv4_to_slash24(SELF_ADDR)]


def _iface_name_for_prefix(prefix: str) -> Optional[str]:
    try:
        raw = subprocess.check_output(["ip", "-j", "addr", "show"], text=True, timeout=5)
        for nic in json.loads(raw):
            for entry in nic.get("addr_info", []):
                if entry.get("family") != "inet":
                    continue
                lip = entry.get("local")
                if not lip or lip.startswith("127."):
                    continue
                if _ipv4_to_slash24(lip) == prefix:
                    return nic.get("ifname")
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError) as err:
        print(f"[warn] could not map prefix {prefix}: {err}", flush=True)
    return None


def _addr_on_prefix(prefix: str) -> Optional[str]:
    try:
        raw = subprocess.check_output(["ip", "-j", "addr", "show"], text=True, timeout=5)
        for nic in json.loads(raw):
            if nic.get("ifname") == "lo":
                continue
            for entry in nic.get("addr_info", []):
                if entry.get("family") != "inet":
                    continue
                lip = entry.get("local")
                if not lip or lip.startswith("127."):
                    continue
                if _ipv4_to_slash24(lip) == prefix:
                    return str(lip)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError, ValueError):
        pass
    return None


def _iface_for_address(addr: str) -> Optional[str]:
    try:
        raw = subprocess.check_output(["ip", "-j", "addr", "show"], text=True, timeout=5)
        for nic in json.loads(raw):
            for entry in nic.get("addr_info", []):
                if entry.get("family") != "inet":
                    continue
                if entry.get("local") == addr:
                    return nic.get("ifname")
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError, ValueError):
        pass
    return None


def _first_addr_on_iface(dev: str) -> Optional[str]:
    try:
        raw = subprocess.check_output(["ip", "-j", "addr", "show", "dev", dev], text=True, timeout=5)
        for nic in json.loads(raw):
            for entry in nic.get("addr_info", []):
                if entry.get("family") != "inet":
                    continue
                lip = entry.get("local")
                if lip and not lip.startswith("127."):
                    return str(lip)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError, ValueError):
        pass
    return None


def _egress_for_gateway(gw: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (out_if, preferred_src) for an on-link next hop."""
    shared = _ipv4_to_slash24(gw)
    src = _addr_on_prefix(shared)
    oif = _iface_for_address(src) if src else None
    if not oif:
        oif = _iface_name_for_prefix(shared)
    if not src and oif:
        src = _first_addr_on_iface(oif)
    return oif, src


def _install_kernel_path(prefix: str, gateway: str) -> None:
    if SKIP_KERNEL_INSTALL:
        return
    if prefix in on_link_prefixes:
        gateway = "0.0.0.0"
    if gateway == "0.0.0.0":
        dev = _iface_name_for_prefix(prefix)
        cmd = f"ip route replace {prefix} dev {dev}" if dev else f"ip route replace {prefix} via 0.0.0.0"
        rc = os.system(cmd)
        if rc != 0:
            print(f"[warn] ip route failed ({rc}): {cmd}", flush=True)
        return

    oif, src = _egress_for_gateway(gateway)
    line = f"ip route replace {prefix} via {gateway}"
    if oif:
        line += f" dev {oif}"
    if src:
        line += f" src {src}"
    attempts = [line, f"{line} onlink"]
    ok = False
    for attempt in attempts:
        if os.system(attempt) == 0:
            ok = True
            break
    if not ok and os.system(f"ip route replace {prefix} via {gateway}") != 0:
        print(f"[warn] ip route failed for {prefix} via {gateway}", flush=True)


def _remove_kernel_path(prefix: str) -> None:
    if SKIP_KERNEL_INSTALL:
        return
    os.system(f"ip route del {prefix} 2>/dev/null")


def _print_table() -> None:
    print("[table]", flush=True)
    for key in sorted(forwarding_table.keys()):
        cost, nh = forwarding_table[key]
        print(f"  {key}  metric={cost}  next={nh}", flush=True)
    print(flush=True)


def _bootstrap() -> List[str]:
    global on_link_prefixes
    local_list = _collect_iface_prefixes()
    on_link_prefixes = frozenset(local_list)
    with _state_lock:
        for p in local_list:
            forwarding_table[p] = [0, "0.0.0.0"]
    for p in local_list:
        _install_kernel_path(p, "0.0.0.0")
    return local_list


# --- Bellman-Ford & split horizon --------------------------------------------

def split_horizon_omit(
    table: Dict[str, List[Any]], peer_ip: str, prefix: str
) -> bool:
    """True => do not re-advertise this prefix back to the peer we use for it."""
    if prefix not in table:
        return False
    return table[prefix][1] == peer_ip


def suppress_route_for_neighbor(peer_ip: str, prefix: str) -> bool:
    with _state_lock:
        return split_horizon_omit(forwarding_table, peer_ip, prefix)


def apply_bellman_step(
    table: Dict[str, List[Any]],
    local_nets: frozenset,
    sender_ip: str,
    routes: List[Dict[str, Any]],
) -> bool:
    """Merge one advertisement into *table*; return True if anything changed."""
    changed = False
    for item in routes:
        try:
            prefix = str(item["subnet"])
            raw_cost = int(float(item["distance"]))
        except (KeyError, TypeError, ValueError):
            print(f"[warn] bad route entry: {item}", flush=True)
            continue

        if prefix in local_nets:
            continue

        existing = table.get(prefix)

        if raw_cost >= MAX_DISTANCE:
            if existing is not None and existing[1] == sender_ip:
                del table[prefix]
                changed = True
            continue

        candidate = raw_cost + 1
        if candidate >= MAX_DISTANCE:
            continue

        if existing is not None and existing[1] == sender_ip:
            if candidate != existing[0]:
                table[prefix] = [candidate, sender_ip]
                changed = True
            continue

        if existing is None:
            table[prefix] = [candidate, sender_ip]
            changed = True
        elif candidate < existing[0]:
            table[prefix] = [candidate, sender_ip]
            changed = True

    return changed


def _integrate_advertisement(sender_ip: str, routes: List[Dict[str, Any]]) -> None:
    with _state_lock:
        modified = apply_bellman_step(
            forwarding_table, on_link_prefixes, sender_ip, routes
        )
        for loc in on_link_prefixes:
            if forwarding_table.get(loc) != [0, "0.0.0.0"]:
                forwarding_table[loc] = [0, "0.0.0.0"]
                modified = True

    if not modified:
        return

    _print_table()
    with _state_lock:
        snapshot = dict(forwarding_table)
    for pfx, (cost, nh) in snapshot.items():
        if pfx in on_link_prefixes:
            _install_kernel_path(pfx, "0.0.0.0")
        elif 0 < cost < MAX_DISTANCE:
            _install_kernel_path(pfx, nh)


def _touch_peer(peer: str) -> None:
    with _peer_lock:
        _peer_last_seen[peer] = time.monotonic()


def _forget_silent_peers() -> None:
    now = time.monotonic()
    dead: List[str] = []
    with _peer_lock:
        for ip in PEER_IPS:
            ts = _peer_last_seen.get(ip)
            if ts is not None and now - ts > SILENCE_LIMIT_SEC:
                dead.append(ip)
    if not dead:
        return

    removed: List[str] = []
    changed = False
    with _state_lock:
        for prefix, (_c, nh) in list(forwarding_table.items()):
            if prefix in on_link_prefixes:
                continue
            if nh in dead:
                removed.append(prefix)
        for prefix in removed:
            del forwarding_table[prefix]
            changed = True

    if not changed:
        return

    print(f"[timeout] no DV from {dead}; dropping paths via them", flush=True)
    for prefix in removed:
        _remove_kernel_path(prefix)

    _print_table()
    with _state_lock:
        snapshot = dict(forwarding_table)
    for pfx, (cost, nh) in snapshot.items():
        if pfx in on_link_prefixes:
            _install_kernel_path(pfx, "0.0.0.0")
        elif 0 < cost < MAX_DISTANCE:
            _install_kernel_path(pfx, nh)
        elif cost == 0:
            _install_kernel_path(pfx, "0.0.0.0")


# --- Networking -------------------------------------------------------------

def _periodic_announce() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while True:
            with _state_lock:
                rows = [
                    {"subnet": p, "distance": d}
                    for p, (d, _) in forwarding_table.items()
                ]
            for peer in PEER_IPS:
                payload_routes: List[Dict[str, Any]] = []
                for row in rows:
                    if suppress_route_for_neighbor(peer, row["subnet"]):
                        continue
                    payload_routes.append(dict(row))
                body = {
                    "router_id": SELF_ADDR,
                    "version": PROTOCOL_VERSION,
                    "routes": payload_routes,
                }
                data = json.dumps(body).encode()
                try:
                    sock.sendto(data, (peer, UDP_PORT))
                    print(f"[send] {peer}:{UDP_PORT} ({len(payload_routes)} prefixes)", flush=True)
                except OSError as err:
                    print(f"[err] sendto {peer}: {err}", flush=True)
            time.sleep(ANNOUNCE_PERIOD_SEC)
    finally:
        sock.close()


def _receive_loop() -> None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", UDP_PORT))
    except OSError as err:
        print(f"[fatal] bind 0.0.0.0:{UDP_PORT}: {err}", flush=True)
        sys.exit(1)

    print(f"--- node {SELF_ADDR} ---", flush=True)
    print(f"on-link: {sorted(on_link_prefixes)}", flush=True)
    print(f"peers: {PEER_IPS}", flush=True)
    _print_table()

    while True:
        try:
            blob, src = sock.recvfrom(65535)
        except OSError as err:
            print(f"[err] recv: {err}", flush=True)
            continue

        peer_ip = src[0]
        if PEER_IPS and peer_ip not in PEER_IPS:
            print(f"[receive] ignore non-peer {peer_ip}", flush=True)
            continue

        try:
            msg = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            print(f"[err] bad JSON from {peer_ip}: {err}", flush=True)
            continue

        routes = msg.get("routes", [])
        if not isinstance(routes, list):
            print("[warn] routes is not a list", flush=True)
            continue

        rid = msg.get("router_id", "?")
        ver = msg.get("version", "?")
        print(f"[receive] from {peer_ip} router_id={rid} version={ver} n={len(routes)}", flush=True)

        _touch_peer(peer_ip)
        _integrate_advertisement(peer_ip, routes)


def _stale_loop() -> None:
    while True:
        time.sleep(STALE_SWEEP_SEC)
        _forget_silent_peers()


def _write_proc(path: str, value: str) -> None:
    try:
        with open(path, "w") as fh:
            fh.write(value)
    except OSError as err:
        print(f"[warn] {path}: {err}", flush=True)


def _relax_reverse_path_filter() -> None:
    """Avoid strict rp_filter drops when replies exit a different interface."""
    base = "/proc/sys/net/ipv4/conf"
    for name in ("all", "default"):
        p = os.path.join(base, name, "rp_filter")
        if os.path.isfile(p):
            _write_proc(p, "0")
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name, "rp_filter")
            if os.path.isfile(p):
                _write_proc(p, "0")
    except OSError as err:
        print(f"[warn] rp_filter scan: {err}", flush=True)
    os.system("sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null 2>&1")
    os.system("sysctl -w net.ipv4.conf.default.rp_filter=0 >/dev/null 2>&1")


def main() -> None:
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as fh:
            fh.write("1")
    except OSError:
        os.system("sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1")

    _relax_reverse_path_filter()

    nets = _bootstrap()
    _relax_reverse_path_filter()
    print(f"[init] connected {nets} (metric 0)", flush=True)

    threading.Thread(target=_periodic_announce, name="announce", daemon=True).start()
    threading.Thread(target=_stale_loop, name="stale", daemon=True).start()
    _receive_loop()


if __name__ == "__main__":
    main()
