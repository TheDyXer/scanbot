"""
Finds the fastest VPN locations from this machine, for gluetun's SERVER_CITIES.

install.sh runs it outside the VPN tunnel, so it measures the path from the user's
network to each VPN city:

    docker run --rm --user 0 -v ./vpn:/gluetun ghcr.io/thedyxer/scanbot \
        python /app/vpn_select.py --provider mullvad

It pings up to two WireGuard servers in every city the provider has and prints the
fastest cities as tab-separated lines: rank, country, city, median ping in ms.

With --cities it pings nothing and prints every city instead (country, city), for
choosing one by name when pings don't get through.
"""
import argparse
import concurrent.futures
import json
import random
import socket
import statistics
import struct
import sys
import time
import urllib.request

# gluetun writes its own (version-matched) server list here; city names must match it
SERVERS_FILE = '/gluetun/servers.json'
FALLBACK_URL = 'https://raw.githubusercontent.com/qdm12/gluetun-servers/main/pkg/servers/{provider}.json'
USER_AGENT = 'scanbot (+https://github.com/TheDyXer/scanbot)'
PROVIDERS = ('mullvad', 'protonvpn')

def load_servers(provider, path=SERVERS_FILE):
    """Returns gluetun's server list for the provider, from gluetun's servers.json if there is one."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)[provider]['servers']
    except (OSError, KeyError, ValueError):
        pass
    req = urllib.request.Request(FALLBACK_URL.format(provider=provider), headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)['servers']

def group_by_city(servers, free_only=False, per_city=2, rng=random):
    """
    Returns {(country, city): [IPv4, ...]} for WireGuard servers, with up to per_city
    servers picked at random from each city.
    """
    cities = {}
    for s in servers:
        if s.get('vpn') != 'wireguard' or not s.get('country') or not s.get('city'):
            continue
        if free_only and not s.get('free'):
            continue
        ipv4 = [ip for ip in s.get('ips') or [] if ':' not in ip]
        if ipv4:
            cities.setdefault((s['country'], s['city']), []).append(ipv4[0])
    return {key: rng.sample(ips, min(per_city, len(ips))) for key, ips in cities.items()}

def city_rows(servers, free_only=False):
    """Every (country, city) with a WireGuard server, sorted, as tab-separated lines."""
    cities = group_by_city(servers, free_only, per_city=1)
    return '\n'.join(f"{country}\t{city}" for country, city in sorted(cities))

def icmp_checksum(data):
    if len(data) % 2:
        data += b'\0'
    total = sum(struct.unpack(f'!{len(data) // 2}H', data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF

def open_icmp_socket():
    """
    Unprivileged ICMP socket (Docker allows these for every user), raw socket as a fallback.
    The unprivileged kind gets only its own replies; with many raw sockets open at once,
    every socket receives every reply, and the extra work inflates the times.
    """
    try:
        return socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP), False
    except OSError:
        return socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP), True

def ping(ip, count=3, timeout=1.0):
    """Sends count ICMP echo requests to ip and returns the round-trip times in ms of the replies."""
    try:
        sock, raw = open_icmp_socket()
    except OSError:
        return []
    ident = random.randrange(1, 0xFFFF)
    rtts = []
    with sock:
        for seq in range(1, count + 1):
            header = struct.pack('!BBHHH', 8, 0, 0, ident, seq)
            payload = struct.pack('!d', time.monotonic()) + b'scanbot'
            packet = struct.pack('!BBHHH', 8, 0, icmp_checksum(header + payload), ident, seq) + payload
            sent = time.monotonic()
            try:
                sock.sendto(packet, (ip, 0))
            except OSError:
                return rtts
            deadline = sent + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(1024)
                except (socket.timeout, OSError):
                    break
                if addr[0] != ip:
                    continue
                if raw:
                    data = data[(data[0] & 0x0F) * 4:]  # Strip the IP header
                if len(data) < 8:
                    continue
                kind, _, _, reply_ident, reply_seq = struct.unpack('!BBHHH', data[:8])
                # Unprivileged sockets rewrite the identifier, so only check it on raw sockets
                if kind == 0 and reply_seq == seq and (not raw or reply_ident == ident):
                    rtts.append((time.monotonic() - sent) * 1000)
                    break
    return rtts

def measure(cities, count=3, timeout=1.0, workers=64, ping_fn=None):
    """Pings every server of every city in parallel. Returns {(country, city): [rtt ms, ...]}."""
    ping_fn = ping_fn or ping
    results = {key: [] for key in cities}
    jobs = [(key, ip) for key, ips in cities.items() for ip in ips]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for (key, _), rtts in zip(jobs, pool.map(lambda job: ping_fn(job[1], count, timeout), jobs)):
            results[key].extend(rtts)
    return results

def rank(results):
    """
    Returns [(country, city, median ms or None), ...], fastest first, cities that
    didn't answer at all last.
    """
    rows = [(country, city, statistics.median(rtts) if rtts else None)
            for (country, city), rtts in results.items()]
    return sorted(rows, key=lambda r: (r[2] is None, r[2] if r[2] is not None else 0, r[0], r[1]))

def format_rows(rows, top):
    lines = []
    for number, (country, city, ms) in enumerate(rows[:top], 1):
        lines.append(f"{number}\t{country}\t{city}\t{'-' if ms is None else round(ms)}")
    return '\n'.join(lines)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--provider', required=True, choices=PROVIDERS)
    parser.add_argument('--free', action='store_true', help='only free servers (Proton free plan)')
    parser.add_argument('--top', type=int, default=5, help='how many cities to print (default 5)')
    parser.add_argument('--servers-file', default=SERVERS_FILE)
    parser.add_argument('--per-city', type=int, default=2, help='servers to ping per city (default 2)')
    parser.add_argument('--count', type=int, default=3, help='pings per server (default 3)')
    parser.add_argument('--timeout', type=float, default=1.0, help='seconds to wait for each reply')
    parser.add_argument('--cities', action='store_true', help='print every city (country, city) without pinging')
    args = parser.parse_args(argv)

    servers = load_servers(args.provider, args.servers_file)
    cities = group_by_city(servers, args.free, args.per_city)
    if not cities:
        print(f"No WireGuard servers found for {args.provider}.", file=sys.stderr)
        return 1
    if args.cities:
        print(city_rows(servers, args.free))
        return 0
    print(f"Pinging {sum(len(ips) for ips in cities.values())} servers in {len(cities)} cities...", file=sys.stderr)
    rows = rank(measure(cities, args.count, args.timeout))
    if rows[0][2] is None:
        print("No VPN server answered a ping. Is ICMP blocked on this network?", file=sys.stderr)
        return 2
    print(format_rows(rows, args.top))
    return 0

if __name__ == '__main__':
    sys.exit(main())
