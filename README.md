# iPad 3 Proxmox status POC

A read-only, ES5-compatible compact dashboard for iOS 9. No frontend dependencies. The backend queries InfluxDB 1.x over HTTP; Proxmox's existing UDP ingestion is unchanged.

## Run

```sh
cp .env.example .env
# edit INFLUX_URL and optionally INFLUX_USER / INFLUX_PASSWORD
docker compose up -d --build
docker compose logs -f
```

Visit `http://DOCKER-HOST:8080/` on the iPad. Safari > Share > Add to Home Screen; launch from the icon. Inspect `http://DOCKER-HOST:8080/api/status` to debug data.

## Metrics and assumptions

- Node identities and uptime: `system` with `object=nodes`, by `host`.
- Node CPU: `cpustat` with `object=nodes` and `cpu` as fraction 0..1.
- Node memory: `memory` with `object=nodes`, `memused/memtotal`.
- Guests: `system` with `object=qemu` or `object=lxc`, deduplicated by VMID using newest timestamp.
- VM CPU `cpu` is fractional. For QEMU guests, Proxmox `mem/maxmem` may reflect host-side QEMU process memory rather than guest memory pressure, even when the guest agent is installed. The percentage remains in `/api/status` for diagnostics but **does not affect QEMU status dots**. QEMU status is based on CPU, running/stopped state, and freshness. LXC status still evaluates `mem/maxmem` and disk usage when available.
- LXC disk uses `disk/maxdisk` where populated. VM guest filesystem utilization is **not** inferred from Proxmox's VM disk fields, so is unavailable for VMs.
- Ceph capacity uses `system` storage rows with host names containing `CEPH_STORAGE_PATTERN` (default `ceph`), deduplicated by storage name across nodes. This is not Ceph cluster health. Verify storage usage semantics before treating the percentage as authoritative.
- Status: green under CPU 50%, RAM 80%, disk 80%; yellow at CPU >=50%, RAM >=80%, disk >=80%; red at CPU >=80%, RAM >=90%, disk >=90%; stopped grey; stale/unavailable purple. Only applicable metrics participate (QEMU memory and disk are excluded). Missing individual metrics are ignored, not treated as zero.
- Node CPU/memory field selection is a **hypothesis** until you inspect `cpustat` and `memory` sample rows. Missing values show `—` rather than being fabricated.
- InfluxDB 1.x `GROUP BY * ORDER BY time DESC LIMIT 1` fetches one newest row per tag series over five minutes. Guest migration is handled by choosing the newest series for each VMID.

## Security

No authentication on the dashboard HTTP endpoint. Restrict port 8080 with host firewall/network rules; use a separate SSID/VLAN when ready. Store `.env` securely and use an InfluxDB read-only user. Do not expose this container to the public internet.

## Next validation queries

```sql
SELECT * FROM "cpustat" WHERE time > now() - 5m LIMIT 3
SELECT * FROM "memory" WHERE time > now() - 5m LIMIT 3
SELECT * FROM "system" WHERE "object"='storages' AND time > now() - 5m LIMIT 5
```

If CPU/RAM show `—`, inspect `/api/status` and these queries before adjusting the mapping. Storage can be absent if its name does not contain `ceph`; set `CEPH_STORAGE_PATTERN` to a unique substring of the actual storage name.
