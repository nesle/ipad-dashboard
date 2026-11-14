import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

URL = os.environ.get('INFLUX_URL', 'http://localhost:8086').rstrip('/')
DB = os.environ.get('INFLUX_DB', 'proxmox')
USER = os.environ.get('INFLUX_USER', '')
PASSWORD = os.environ.get('INFLUX_PASSWORD', '')
POLL = max(10, int(os.environ.get('POLL_SECONDS', '30')))
STALE = max(POLL * 2, int(os.environ.get('STALE_SECONDS', '120')))
CEPH = os.environ.get('CEPH_STORAGE_PATTERN', 'ceph').lower()
NAS = os.environ.get('NAS_STORAGE_PATTERN', 'proxmox-backup').lower()
CACHE = {'updated': None, 'error': 'Waiting for first collection', 'nodes': [], 'guests': {'qemu': [], 'lxc': []}, 'ceph': None}
LOCK = threading.Lock()


def parse_time(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def number(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def pct(n, d):
    n, d = number(n), number(d)
    return round(100 * n / d, 1) if n is not None and d and d > 0 else None


def status(cpu, mem, disk, running=True):
    if not running:
        return 'stopped'
    vals = [v for v in (cpu, mem, disk) if v is not None]
    if not vals:
        return 'unknown'
    if (cpu is not None and cpu >= 80) or (mem is not None and mem >= 90) or (disk is not None and disk >= 90):
        return 'critical'
    if (cpu is not None and cpu >= 50) or (mem is not None and mem >= 80) or (disk is not None and disk >= 80):
        return 'warning'
    return 'healthy'


def query(measurement):
    # One newest row PER tag series; do not accidentally discard other hosts/VMs.
    q = 'SELECT * FROM "%s" WHERE time > now() - 5m GROUP BY * ORDER BY time DESC LIMIT 1' % measurement
    params = {'db': DB, 'q': q, 'epoch': 's'}
    if USER:
        params.update(u=USER, p=PASSWORD)
    req = Request(URL + '/query?' + urlencode(params), headers={'User-Agent': 'ipad-status/1'})
    with urlopen(req, timeout=8) as response:
        payload = json.load(response)
    if payload.get('error'):
        raise RuntimeError(payload['error'])
    rows = []
    for result in payload.get('results', []):
        if result.get('error'):
            raise RuntimeError(result['error'])
        for series in result.get('series', []):
            cols = series.get('columns', [])
            for values in series.get('values', []):
                row = dict(zip(cols, values))
                row.update(series.get('tags', {}))
                row['_ts'] = float(row.get('time') or 0)
                rows.append(row)
    return rows


def fresh(row, now):
    return 0 <= now - row['_ts'] <= STALE


def newest_by(rows, key):
    result = {}
    for row in rows:
        k = key(row)
        if k and (k not in result or row['_ts'] > result[k]['_ts']):
            result[k] = row
    return result

def get_storage(system, now, pattern):
    # Deduplicate shared Ceph storage reported by each node. This is utilization,
    # not Ceph HEALTH_OK/WARN/ERR. Only include explicitly Ceph-named storages.
    stores = [r for r in system if r.get('object') == 'storages' and
              pattern in str(r.get('host', '')).lower() and fresh(r, now)]
    unique = newest_by(stores, lambda r: r.get('host'))
    used = total = 0.0
    for r in unique.values():
        # Storage 'used'/'total' are bytes; fallback 'disk'/'maxdisk'.
        u = number(r.get('used'))
        t = number(r.get('total'))
        if u is None or t is None:
            u, t = number(r.get('disk')), number(r.get('maxdisk'))
        if u is not None and t is not None and t > 0:
            used += u
            total += t
    return {'percent': round(100 * used / total, 1), 'pools': len(unique)} if total else None


def collect():
    now = time.time()
    system = query('system')
    cpus = query('cpustat')
    memory = query('memory')
    # 'nodes' is the Proxmox object tag, not 'node'.
    node_uptime = newest_by([r for r in system if r.get('object') == 'nodes'], lambda r: r.get('host'))
    # Node-specific cpustat and memory; keep object filtering to avoid misattribution.
    node_cpu = newest_by([r for r in cpus if r.get('object') in ('nodes', 'node')], lambda r: r.get('host'))
    node_mem = newest_by([r for r in memory if r.get('object') in ('nodes', 'node')], lambda r: r.get('host'))
    nodes = []
    for host in sorted(set(node_uptime) | set(node_cpu) | set(node_mem)):
        u, c, m = node_uptime.get(host), node_cpu.get(host), node_mem.get(host)
        online = bool(u and fresh(u, now))
        cpu = round(number(c.get('cpu')) * 100, 1) if c and fresh(c, now) and number(c.get('cpu')) is not None else None
        mem = pct(m.get('memused'), m.get('memtotal')) if m and fresh(m, now) else None
        # cpustat CPU 'cpu' is expected to be 0..1; unusual values are left unknown.
        if cpu is not None and not 0 <= cpu <= 100:
            cpu = None
        nodes.append({'name': host, 'online': online, 'cpu': cpu, 'mem': mem,
                      'uptime': u.get('uptime') if online else None,
                      'status': status(cpu, mem, None) if online else 'unknown'})
    guests = {}
    for kind in ('qemu', 'lxc'):
        candidates = [r for r in system if r.get('object') == kind]
        # Same VMID can have historical series on multiple nodes; choose newest.
        selected = newest_by(candidates, lambda r: str(r.get('vmid') or ''))
        out = []
        for vmid, r in selected.items():
            is_fresh = fresh(r, now)
            running = str(r.get('status') or '').lower() == 'running'
            cpu_raw = number(r.get('cpu'))
            cpu = round(cpu_raw * 100, 1) if cpu_raw is not None and 0 <= cpu_raw <= 1 else None
            mem = pct(r.get('mem'), r.get('maxmem'))
            # Proxmox QEMU disk/maxdisk does NOT reliably represent guest filesystem usage.
            disk = pct(r.get('disk'), r.get('maxdisk')) if kind == 'lxc' else None
            state = status(cpu, mem, disk, running) if is_fresh else 'unknown'
            out.append({'id': vmid, 'name': r.get('name') or r.get('host'),
                        'node': r.get('nodename'), 'status': state, 'cpu': cpu,
                        'mem': mem, 'disk': disk, 'fresh': is_fresh})
        guests[kind] = sorted(out, key=lambda r: (0, int(r['id'])) if r['id'].isdigit() else (1, r['id']))
    ceph = get_storage(system, now, CEPH)
    nas = get_storage(system, now, NAS)
    return {'updated': datetime.now(timezone.utc).isoformat(), 'error': None,
            'nodes': nodes, 'guests': guests, 'ceph': ceph, 'nas': nas}


def worker():
    global CACHE
    while True:
        try:
            data = collect()
            with LOCK:
                CACHE = data
        except Exception as exc:
            with LOCK:
                CACHE = dict(CACHE, error=str(exc))
        time.sleep(POLL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/api/status':
            with LOCK:
                data = dict(CACHE)
            body = json.dumps(data).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        elif path == '/healthz':
            with LOCK:
                ok = CACHE['updated'] is not None and CACHE['error'] is None
            body = (b'ok' if ok else b'unavailable')
            content_type = 'text/plain'
        elif path == '/':
            body = Path('/app/static/index.html').read_bytes()
            content_type = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
