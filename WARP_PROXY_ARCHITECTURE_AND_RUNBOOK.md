# Autonomous Cloudflare Zero Trust SOCKS5 Proxy Architecture & Runbook

**Environment:** Google Cloud Platform (GCP) Compute Engine  
**Project:** `gm-prod-devops-lab`  
**Host VM:** `cac-automation`  
**Target Subnet:** Atlanta Warehouse Network (`172.28.0.0/16`)  
**Date:** October 2026  
**Status:** Production / 100% Operational  

---

## 1. Executive Summary & Objective

Headless automated worker bots (e.g. `grafana-slack-bot` utilizing Playwright / Chromium) running in Google Cloud Platform needed 24/7 autonomous access to capture live dashboard snapshots from private warehouse applications located in Atlanta:
- `https://samsatl.greymatter.greyorange.com/md/#/v2/login` (Internal IP: `172.28.48.10`)
- `http://172.28.76.144:8088/`

These targets reside strictly within GreyOrange's internal corporate network, accessible only via **Cloudflare Zero Trust** (WARP).

### The Fatal Conflict
Connecting Cloudflare WARP directly on the host VM (`sudo warp-cli connect`) enforced corporate Zero Trust posture policies (`Firewall Scope: All interfaces`). This immediately hijacked the VM's physical interface (`ens4`), **terminating inbound SSH (port 22)** and **taking down the team's production web application on port 8081 (`gods-eye-frontend`)**.

### The Solution
We engineered an isolated **Containerized Cloudflare Zero Trust SOCKS5 Proxy (`warp-proxy`)**:
1. Cloudflare WARP runs inside an isolated Docker network namespace. Its WireGuard tunnels, split routing tables, and firewall rules are strictly confined inside the container and **never touch the host network stack**.
2. A lightweight SOCKS5 server (`microsocks`) listens inside the container on port 1080 and exposes `127.0.0.1:1080` exclusively to the host.
3. Automated bots route internal HTTP/HTTPS traffic through `socks5://127.0.0.1:1080`.
4. Host SSH (port 22) and production web services (port 8081) remain 100% stable with zero interruptions.

---

## 2. Infrastructure & Environment Specifications

| Parameter | Host Specification | Notes |
| :--- | :--- | :--- |
| **GCP VM Name** | `cac-automation` | Compute Engine Instance |
| **GCP Project** | `gm-prod-devops-lab` | Production DevOps Lab |
| **Operating System** | Debian GNU/Linux 12 (bookworm) | Kernel 6.1+ |
| **Primary Network Interface** | `ens4` | GCP Virtual Ethernet |
| **Host IP Address** | `172.27.160.170/32` | Internal VPC IP |
| **Default Gateway** | `172.27.160.1` | GCP VPC Gateway |
| **GCP VPC MTU** | `1460` | Critical (Mismatched 1500 drops packets) |
| **Host DNS Resolver** | `169.254.169.254:53` | GCP Metadata link-local resolver |
| **Cloudflare Account** | `greyorange` | Account ID: `3d985d3b-ef11-02b3-bb73-721eab7107c8` |
| **Target Endpoints** | `172.28.0.0/16` | Atlanta Warehouse Infrastructure |

### Active Host Ports & Services:
- **Port 22:** OpenSSH Daemon (Host remote administration)
- **Port 8081:** `gods-eye-frontend` (Production web portal)
- **Port 5000:** `grafana-slack-bot` (Snapshot Bot API & Web UI, host network mode)
- **Port 1080:** `warp-proxy` (Isolated Cloudflare Zero Trust SOCKS5 gateway on `127.0.0.1`)

---

## 3. High-Level Architecture Diagram

```
+-----------------------------------------------------------------------------------------+
| GCP Compute Engine VM: cac-automation (IP: 172.27.160.170)                              |
|                                                                                         |
|  +---------------------------+               +--------------------------------------+   |
|  | grafana-slack-bot         |               | Host Protected Services              |   |
|  | (Playwright / Chromium)   |               | - SSH Daemon (Port 22)               |   |
|  |                           |               | - gods-eye-frontend (Port 8081)      |   |
|  +-------------+-------------+               +--------------------------------------+   |
|                |                                                                        |
|                | HTTP_PROXY=socks5://127.0.0.1:1080                                     |
|                v                                                                        |
|  +----------------------------------------------------------------------------------+   |
|  | Docker Container: warp-proxy                                                     |   |
|  | Network: warp-corp-bridge (Subnet: 10.200.0.0/24, MTU: 1460)                     |   |
|  | Container IP: 10.200.0.2 | Container Gateway: 10.200.0.1                         |   |
|  |                                                                                  |   |
|  |   [microsocks SOCKS5 Server] <--- Listens on 0.0.0.0:1080                        |   |
|  |              |                                                                   |   |
|  |              v                                                                   |   |
|  |   [warp-svc Daemon (WireGuard)]                                                  |   |
|  |   - Enrolled in GreyOrange Zero Trust                                            |   |
|  |   - Interface: CloudflareWARP (100.96.0.188)                                     |   |
|  |   - Excludes LAN: 10.200.0.0/24 ("Docker_local" corporate whitelist)             |   |
|  +-------------------------------------------+--------------------------------------+   |
+----------------------------------------------|------------------------------------------+
                                               | (WireGuard UDP 2408 via ens4)
                                               v
                             Cloudflare Zero Trust Edge Gateway
                                               |
                                               v
                           Atlanta Warehouse Network (172.28.0.0/16)
                           - samsatl.greymatter.greyorange.com (172.28.48.10)
                           - 172.28.76.144:8088
```

---

## 4. Root Cause Analysis & Problem Resolution Timeline

During implementation, four deep networking issues were uncovered and methodically solved:

### Issue 1: Non-Interactive Terms of Service Rejection
- **Symptom:** `warp-proxy` container started, but `warp-cli status` remained `Disconnected(Manual)`.
- **Log finding:**
  ```text
  Please accept the WARP Terms of Service by running this command in a TTY or by passing the --accept-tos flag.
  ```
- **Root Cause:** Newer versions of Cloudflare WARP client (`2026.7.x`) block CLI operations non-interactively unless `--accept-tos` is explicitly passed.
- **Resolution:** Updated [`entrypoint.sh`](file:///opt/warp-proxy/entrypoint.sh) to append `--accept-tos` to all CLI commands and added an IPC wait-loop.

---

### Issue 2: Host Policy Routing Black Hole (`10.0.0.0/8 lookup 100`)
- **Symptom:** All container outbound packets (DNS to `169.254.169.254`, NTP, and TCP to Cloudflare edge IPs `162.159.137.105`) timed out after 10,000ms.
- **Diagnostic Discovery:**
  Checking `ip rule show` on `cac-automation` revealed:
  ```text
  1:  from all to 10.0.0.0/8 lookup 100
  ...
  10.240.0.0/24 dev docker0 proto kernel scope link src 10.240.0.1
  ```
- **Root Cause:**
  1. Docker's default bridge `docker0` picked subnet `10.240.0.0/24` (container IP: `10.240.0.3`).
  2. `10.240.0.0/24` falls inside `10.0.0.0/8`.
  3. Priority Rule 1 (`to 10.0.0.0/8 lookup 100`) intercepted all return packets coming from the internet destined for `10.240.0.3` and routed them out `ens4` to `172.27.160.1` instead of `docker0`! The container never received any inbound response packets.
- **Resolution:** Docker networks cannot use subnets inside `10.0.0.0/8` without an explicit bypass rule.

---

### Issue 3: GCP VPC MTU Drop (1500 vs 1460)
- **Symptom:** Small UDP packets passed, but SSL handshakes and package downloads hung indefinitely.
- **Root Cause:** Standard Docker networks create virtual interfaces with MTU `1500`. Google Cloud VPC network interfaces (`ens4`) enforce an MTU of `1460`. Packets exceeding 1460 bytes were dropped silently by GCP routers without ICMP fragmentation notifications.
- **Resolution:** Configured Docker custom bridge networks with `--opt "com.docker.network.driver.mtu=1460"`.

---

### Issue 4: Corporate Split-Tunnel Gateway Severing & "Docker_local" Discovery
- **Symptom:** When testing an arbitrary non-10.x subnet (`192.168.100.0/24`), Cloudflare WARP connected in 68ms, but abruptly dropped 12 seconds later with:
  ```text
  WARN: Overlapping tunnel IPs with local network. Failed to exclude lan: 192.168.100.0/24
  WARN: Connectivity checks failed ... trace_failed=Dns
  ERROR: Connection failed to start error=FailedConnectivityCheck(DNSLookupFailed)
  ```
- **Root Cause:**
  GreyOrange's Zero Trust profile does not exclude arbitrary subnets. Because `192.168.100.0/24` was not in the corporate exclusion list, WARP routed the container's gateway (`192.168.100.1`) **into** the WireGuard tunnel! The container could no longer reach `192.168.100.1`, which severed the WireGuard tunnel's outbound UDP path to Cloudflare (`162.159.193.2:2408`).
- **The Breakthrough:**
  Inspecting the full corporate split-tunnel configuration logged by `warp-svc` revealed GreyOrange's IT team had already created dedicated exclusions specifically for Docker:
  ```text
  (10.200.0.0/24, Some("Docker_local")),
  (10.201.0.0/16, Some("Docker_compose_1")),
  (10.202.0.0/16, Some("Docker_Compose_2")),
  ```
- **The Resolution:**
  1. Built the dedicated bridge `warp-corp-bridge` on **`10.200.0.0/24`** (`gateway: 10.200.0.1`, `MTU: 1460`).
  2. Injected a **priority 0** host policy routing rule to bypass rule 1:
     ```bash
     sudo ip rule add to 10.200.0.0/24 priority 0 lookup main
     ```
  3. When `warp-svc` launched on `10.200.0.0/24`, it matched the corporate `"Docker_local"` exclusion. The gateway was preserved, WireGuard remained fully connected, and `Network: healthy` was achieved!

---

## 5. Complete Implementation & Configuration Files

### 5.1 Dockerfile (`/opt/warp-proxy/Dockerfile`)
```dockerfile
FROM debian:12-slim
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gpg lsb-release ca-certificates procps microsocks iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" > /etc/apt/sources.list.d/cloudflare-warp.list && \
    apt-get update && apt-get install -y --no-install-recommends cloudflare-warp && \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 1080
ENTRYPOINT ["/entrypoint.sh"]
```

### 5.2 Container Entrypoint (`/opt/warp-proxy/entrypoint.sh`)
```bash
#!/bin/bash
set -e

echo "Starting warp-svc daemon..."
warp-svc &

echo "Waiting for warp-svc IPC socket..."
for i in {1..30}; do
  if warp-cli --accept-tos status &>/dev/null; then
    echo "warp-svc is ready."
    break
  fi
  sleep 1
done

echo "Connecting to Cloudflare Zero Trust..."
warp-cli --accept-tos connect || true

# Verification loop
for i in {1..15}; do
  STATUS=$(warp-cli --accept-tos status 2>/dev/null || true)
  echo "$STATUS" | grep -i "Status update:" || true
  if echo "$STATUS" | grep -qi "Connected"; then
    echo "✅ WARP is successfully connected!"
    break
  fi
  sleep 2
done

echo "🚀 Starting SOCKS5 proxy on 0.0.0.0:1080..."
exec microsocks -i 0.0.0.0 -p 1080
```

### 5.3 Systemd Persistence Service (`/etc/systemd/system/warp-routing.service`)
Ensures the priority-0 routing bypass survives VM reboots:
```ini
[Unit]
Description=Ensure Docker Local routing rule for Cloudflare WARP
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '/sbin/ip rule del to 10.200.0.0/24 priority 0 2>/dev/null || true; /sbin/ip rule add to 10.200.0.0/24 priority 0 lookup main'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

### 5.4 Docker Network & Container Launch Command
```bash
# 1. Enable host IP forwarding
sudo sysctl -w net.ipv4.ip_forward=1
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-docker-forward.conf

# 2. Add priority 0 routing rule
sudo ip rule del to 10.200.0.0/24 priority 0 2>/dev/null || true
sudo ip rule add to 10.200.0.0/24 priority 0 lookup main

# 3. Create dedicated bridge network
sudo docker network rm warp-corp-bridge 2>/dev/null || true
sudo docker network create \
  --driver bridge \
  --subnet 10.200.0.0/24 \
  --gateway 10.200.0.1 \
  --opt "com.docker.network.driver.mtu=1460" \
  warp-corp-bridge

# 4. Run the proxy container
sudo docker rm -f warp-proxy 2>/dev/null || true
sudo docker run -d \
  --name warp-proxy \
  --net=warp-corp-bridge \
  --restart unless-stopped \
  --cap-add=NET_ADMIN \
  --device /dev/net/tun \
  -v /var/lib/cloudflare-warp:/var/lib/cloudflare-warp \
  -v /opt/warp-proxy/entrypoint.sh:/entrypoint.sh:ro \
  -p 127.0.0.1:1080:1080 \
  cloudflare-warp-proxy
```

### 5.5 Bot Environment Configuration (`/AUTO BOT/.env`)
```ini
# Internal Zero Trust Proxy
HTTP_PROXY=socks5://127.0.0.1:1080
```

### 5.6 Host Static Name Resolution (`/etc/hosts`)
```text
172.28.48.10 samsatl.greymatter.greyorange.com
```

---

## 6. Verification & Health Check Playbook

Run these commands on `cac-automation` to verify health anytime:

```bash
# 1. Verify container is healthy
sudo docker ps --filter name=warp-proxy

# 2. Check Cloudflare WARP internal status
sudo docker exec warp-proxy warp-cli --accept-tos status
# Expected output:
# Status update: Connected
# Network: healthy

# 3. Verify SOCKS5 proxy port is listening on host
sudo ss -tulpn | grep 1080
# Expected: 127.0.0.1:1080 LISTEN

# 4. Test live connectivity to Atlanta warehouse targets
curl -x socks5h://127.0.0.1:1080 -I -k https://samsatl.greymatter.greyorange.com/md/
# Expected: HTTP/2 200

curl -x socks5h://127.0.0.1:1080 -I --connect-timeout 5 http://172.28.76.144:8088/
# Expected: HTTP/1.0 501 Unsupported method ('HEAD')
```

---

## 7. Golden Rules for Future Deployments

When deploying Cloudflare Zero Trust inside Google Cloud or corporate cloud environments:

1. **NEVER run `warp-cli connect` on the host OS of multi-service VMs.** Corporate posture policies will lockdown the physical interface and disrupt SSH and web servers.
2. **Always isolate WARP inside a Docker container or network namespace** with `--cap-add=NET_ADMIN` and `--device /dev/net/tun`.
3. **Always check policy routing (`ip rule show`).** If cloud providers or VPNs have `lookup 100` rules on `10.0.0.0/8`, Docker's default `docker0` bridge will be black-holed. Use a priority 0 rule (`priority 0 lookup main`) for your Docker bridge.
4. **Always align MTU with the host cloud VPC.** In GCP, set Docker network MTU to `1460`.
5. **Always check the corporate split-tunnel exclusion list.** The container's local bridge subnet must be explicitly present in the corporate exclusion list (e.g. `10.200.0.0/24` `"Docker_local"`), otherwise WARP will hijack the container gateway and self-destruct the tunnel.
