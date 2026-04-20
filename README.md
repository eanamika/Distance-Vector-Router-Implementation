# Assignment 4 — Distance-vector router (testing guide)

This folder contains **`router.py`** (routing daemon) and a **`Dockerfile`** to run it in Alpine Linux with Python 3 and `iproute2`. Use this document to **build**, **deploy the triangle topology**, and **verify** behavior against the course requirements.

## What you are testing

| Layer | What to verify |
|--------|----------------|
| **Control plane** | Routers exchange **DV-JSON** over **UDP port 5000**, learn `10.0.x.0/24` prefixes, and converge using **Bellman–Ford** with **split horizon** and **timeouts** (see `router.py`). |
| **Data plane** | The daemon installs **`ip route`** rules so the Linux FIB matches the logical table (needs **`CAP_NET_ADMIN`** or **`--privileged`** in Docker). |
| **Topology** | Three routers (**A**, **B**, **C**), each on **two** Docker networks forming a triangle (`net_ab`, `net_bc`, `net_ac`). |

### DV-JSON (must match the spec)

Updates are UDP datagrams to port **5000**, JSON body:

```json
{
  "router_id": "<sender IPv4>",
  "version": 1.0,
  "routes": [
    { "subnet": "10.0.1.0/24", "distance": 0 }
  ]
}
```

### Environment variables (`router.py`)

| Variable | Purpose |
|----------|---------|
| **`MY_IP`** | Router identity / `router_id` in advertisements (use the container’s primary address as in your runbook). |
| **`NEIGHBORS`** | Comma-separated **peer IPv4 addresses** (UDP **5000** is fixed in code). |
| **`EXTRA_LOCAL_PREFIXES`** | Optional comma-separated **extra /24 prefixes** treated as on-link if interface discovery misses one (common in multi-network Docker). |
| **`ROUTER_SKIP_IP`** | Set to `1` or `true` to **skip** `ip route` calls (e.g. local syntax check without `NET_ADMIN`). |

## Prerequisites

- **Docker** installed; use `sudo docker` if your user is not in the `docker` group.
- **Linux host (recommended):** For cross-subnet pings over Docker bridges, you may need `net.bridge.bridge-nf-call-iptables=0` after bridges exist (see “Host bridge note” below).

## 1. Build the image

From **this directory** (`CN_ASSGN_4`):

```bash
docker build -t my-router .
```

(Replace `my-router` with any tag you prefer.)

## 2. Create the three virtual networks

```bash
docker network create --subnet=10.0.1.0/24 net_ab
docker network create --subnet=10.0.2.0/24 net_bc
docker network create --subnet=10.0.3.0/24 net_ac
```

Ignore “already exists” if you re-run.

## 3. Host bridge note (Linux, for remote pings)

After the networks exist, load the bridge module and relax iptables on the bridge if pings to “remote” subnets fail despite good routes:

```bash
sudo modprobe br_netfilter 2>/dev/null || true
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0
```

## 4. Run the triangle (example layout)

The assignment diagram uses one addressing pattern; your instructor may use another. Below is a **consistent** example that matches a typical lab (IPs on each `/24` are chosen so **A** has `10.0.1.x` and `10.0.3.x`, etc.). Adjust only if your sheet specifies different addresses.

**Capabilities:** `ip route` inside the container requires:

```text
--cap-add=NET_ADMIN
```

or **`--privileged`** (as in the course handout).

### Router A — `net_ab` + `net_ac`

```bash
docker create --name router_a --hostname router_a --cap-add=NET_ADMIN \
  -e MY_IP=10.0.1.2 \
  -e NEIGHBORS=10.0.1.3,10.0.3.3 \
  -e EXTRA_LOCAL_PREFIXES=10.0.1.0/24,10.0.3.0/24 \
  --network net_ab --ip 10.0.1.2 \
  my-router

docker network connect --ip 10.0.3.2 net_ac router_a
docker start router_a
```

### Router B — `net_ab` + `net_bc`

```bash
docker create --name router_b --hostname router_b --cap-add=NET_ADMIN \
  -e MY_IP=10.0.1.3 \
  -e NEIGHBORS=10.0.1.2,10.0.2.3 \
  -e EXTRA_LOCAL_PREFIXES=10.0.1.0/24,10.0.2.0/24 \
  --network net_ab --ip 10.0.1.3 \
  my-router

docker network connect --ip 10.0.2.2 net_bc router_b
docker start router_b
```

### Router C — `net_bc` + `net_ac`

```bash
docker create --name router_c --hostname router_c --cap-add=NET_ADMIN \
  -e MY_IP=10.0.2.3 \
  -e NEIGHBORS=10.0.2.2,10.0.3.2 \
  -e EXTRA_LOCAL_PREFIXES=10.0.2.0/24,10.0.3.0/24 \
  --network net_bc --ip 10.0.2.3 \
  my-router

docker network connect --ip 10.0.3.3 net_ac router_c
docker start router_c
```

Wait **~25–30 seconds** for distance-vector convergence.

## 5. Basic checks (data plane)

```bash
docker ps --filter name=router_
docker exec router_a ping -c 3 10.0.1.3
docker exec router_a ping -c 3 10.0.3.3
docker exec router_a ping -c 3 10.0.2.2
docker exec router_a ping -c 3 10.0.2.3
docker exec router_a ip route
docker logs router_a
```

**Pass criteria:** On-link neighbors reply with **0% loss**; `ip route` on **router_a** lists all three `10.0.0.0/24` prefixes with sensible **connected** or **via** next hops; logs show periodic **send** / **receive** DV traffic.

## 6. Assignment scenario: “Router C disconnected”

**Goal:** After **C** stops, **A** should still reach the **`10.0.2.0/24` subnet** using **B** (not by pinging **C’s** addresses, which are down).

1. Baseline with all routers up:

   ```bash
   docker exec router_a ping -c 4 10.0.2.2
   ```

   (`10.0.2.2` is **B** on `net_bc` — still valid when testing path to that subnet.)

2. Stop **C**:

   ```bash
   docker stop router_c
   ```

3. Wait **at least 30–45 seconds** (neighbor silence timeout + new advertisements).

4. From **router_a**, ping **B** on `net_bc` again:

   ```bash
   docker exec router_a ip route get 10.0.2.2
   docker exec router_a ping -c 4 10.0.2.2
   ```

**Do not** use **`10.0.2.3`** or **`10.0.3.3`** as the success target after stopping **C** — those are **C’s** interfaces and will not answer.

## 7. Quick syntax check (no Docker)

On any machine with Python 3:

```bash
python3 -m py_compile router.py
```

With **`ROUTER_SKIP_IP=1`**, the process will not modify kernel routes (useful for dry runs without `NET_ADMIN`).

## 8. Cleanup

```bash
docker rm -f router_a router_b router_c
docker network rm net_ab net_bc net_ac
```
