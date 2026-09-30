"""
Moves a Mullvad VPN to another server when the current one is down: listed as offline on Mullvad's
site, or not letting pings through. bot.py runs it after every VPN check, and tells gluetun which
server to use through gluetun's control server.

The order to try servers in: the servers in the city picked at setup, then the next-fastest cities
from the installer's ping test (VPN_FALLBACK_CITIES). Once a better server has been up for a while,
the VPN moves back to it.

When a check fails, the bot first reconnects to the same server, and moves on if the next check
fails too. gluetun doesn't restart the VPN by itself here (HEALTH_RESTART_VPN=off in vpn.env):
in gluetun v3.41.3, a server change during its connection check makes it restart in a loop
(qdm12/gluetun#3485), and the bot changes servers.
"""
import logging
import time

log = logging.getLogger('scanbot.vpn')

RELAYS_URL = 'https://api.mullvad.net/www/relays/wireguard/'
RELAYS_MAX_AGE = 300           # Seconds to reuse Mullvad's server list
FAILS_BEFORE_SWITCH = 2        # VPN checks in a row that must fail before a server counts as unreachable
COOLDOWN = 30 * 60             # Seconds before an unreachable server is tried again; doubles each time it fails again
MAX_COOLDOWN = 24 * 3600
STABLE_BEFORE_RETURN = 30 * 60 # Seconds a better server must be listed as up before moving back to it


class MullvadSwitcher:
    def __init__(self, cities, fetch_relays, set_server, reconnect=None, clock=time.monotonic):
        """
        cities: city names in the order to use them; the one picked at setup first.
        fetch_relays(): coroutine returning Mullvad's WireGuard server list (RELAYS_URL). Raises on failure.
        set_server(hostname): coroutine that makes gluetun use that server. Returns False if gluetun
            doesn't know the server (its built-in list can be older than Mullvad's); raises on other errors.
        reconnect(): coroutine that makes gluetun connect to its server again. Raises on failure.
        """
        self.cities = [c.strip().lower() for c in cities if c and c.strip()]
        self.fetch_relays = fetch_relays
        self.set_server = set_server
        self.reconnect = reconnect
        self.clock = clock
        self.current = None       # The server gluetun was last told to use
        self.fails = 0            # VPN checks in a row that failed on the current server
        self.cooldown = {}        # Server -> when it may be used again
        self.strikes = {}         # Server -> times it was unreachable since it last worked
        self.up_since = {}        # Server -> since when Mullvad has listed it as up, without a break
        self.unknown = set()      # Servers gluetun doesn't know
        self._relays = None
        self._fetched_at = None
        self._no_server_logged = False
        self._reconnect_logged = False  # Reconnects are logged once per outage
        self._reconnect_error = None

    async def relays(self, fresh=False):
        """Mullvad's server list, at most RELAYS_MAX_AGE old (or fetched now). None if it can't be fetched."""
        now = self.clock()
        if fresh or self._relays is None or self._fetched_at is None or now - self._fetched_at >= RELAYS_MAX_AGE:
            try:
                relays = await self.fetch_relays()
                if not isinstance(relays, list):
                    raise ValueError("unexpected answer")
            except Exception as e:
                log.warning("Could not get Mullvad's server list: %s", e)
                return None
            self._relays = [r for r in relays if isinstance(r, dict) and isinstance(r.get('hostname'), str)]
            self._fetched_at = now
        return self._relays

    def preference(self, relays):
        """Every server in the chosen cities, best first: by city order, then by name."""
        by_city = {}
        for relay in relays:
            by_city.setdefault(str(relay.get('city_name', '')).lower(), []).append(relay)
        ordered = []
        for city in self.cities:
            ordered += sorted(by_city.get(city, []), key=lambda r: r['hostname'])
        return [r['hostname'] for r in ordered]

    async def tick(self, vpn_ok):
        """
        Call after each VPN check with whether pings through the VPN worked.
        Returns True if gluetun was told to reconnect or to use another server, so the bot should
        check again soon.
        """
        now = self.clock()
        # After a failed check, only judge the server if Mullvad's site answers right now: if this
        # machine's own internet is down, the VPN looks down too, and switching wouldn't help
        relays = await self.relays(fresh=not vpn_ok)
        if relays is None:
            # Can't see Mullvad's list, maybe because this machine's own internet is down:
            # don't judge any server, just keep gluetun on the current one
            if self.current and not vpn_ok:
                return await self._reconnect("Mullvad's server list can't be fetched either")
            if self.current:
                await self._pin(self.current)
            return False

        active = {r['hostname'] for r in relays if r.get('active')}
        for hostname in list(self.up_since):
            if hostname not in active:
                del self.up_since[hostname]
        for hostname in active:
            self.up_since.setdefault(hostname, now)

        ordered = self.preference(relays)
        rank = {hostname: i for i, hostname in enumerate(ordered)}

        def usable(hostname):
            return hostname in active and hostname not in self.unknown and self.cooldown.get(hostname, 0) <= now

        reason, targets = None, []
        if self.current is None:
            reason, targets = "start", [h for h in ordered if usable(h)]
        elif self.current not in active:
            reason = f"{self.current} is listed as offline on Mullvad's site"
            targets = [h for h in ordered if usable(h) and h != self.current]
        elif not vpn_ok:
            self.fails += 1
            if self.fails >= FAILS_BEFORE_SWITCH:
                reason = f"pings through {self.current} failed {self.fails} checks in a row"
                strikes = self.strikes[self.current] = self.strikes.get(self.current, 0) + 1
                self.cooldown[self.current] = now + min(COOLDOWN * 2 ** (strikes - 1), MAX_COOLDOWN)
                self.up_since[self.current] = now  # Only "up for a while" again from now on
                targets = [h for h in ordered if usable(h)]
        else:
            self.fails = 0
            self._reconnect_logged = False
            self.strikes.pop(self.current, None)
            current_rank = rank.get(self.current, len(ordered))
            better = [h for h in ordered[:current_rank]
                      if usable(h) and now - self.up_since[h] >= STABLE_BEFORE_RETURN]
            if better:
                reason, targets = f"back to {better[0]}, which has been up for a while", better

        if reason is None:
            if not vpn_ok:
                return await self._reconnect("pings through it failed")  # First failure: same server again
            await self._pin(self.current, quiet=True)  # A no-op for gluetun unless it restarted and forgot
            return False
        if not targets:
            if not self._no_server_logged:
                log.warning("No other Mullvad server in %s is available (%s); staying on %s",
                            ", ".join(c.title() for c in self.cities), reason, self.current or "gluetun's choice")
                self._no_server_logged = True
            if self.current and not vpn_ok:
                return await self._reconnect("pings through it failed, and there's no other server to use")
            if self.current:
                await self._pin(self.current, quiet=True)
            return False

        for hostname in targets:
            outcome = await self._pin(hostname)
            if outcome == 'error':
                return False  # gluetun can't be reached right now; try again at the next check
            if outcome == 'ok':
                old, self.current, self.fails = self.current, hostname, 0
                self._no_server_logged = False
                if reason == "start":
                    log.info("Using Mullvad server %s", hostname)
                else:
                    log.warning("Switched Mullvad server from %s to %s: %s", old, hostname, reason)
                return True
        if self.current and not vpn_ok:
            return await self._reconnect("pings through it failed, and gluetun knows no other server")
        return False

    async def _reconnect(self, why):
        """Makes gluetun connect to its server again. Returns True if it was asked to."""
        if self.reconnect is None:
            return False
        try:
            await self.reconnect()
        except Exception as e:
            if str(e) != self._reconnect_error:  # Once per kind of error, not every minute
                log.warning("Could not reconnect the VPN: %s", e)
                self._reconnect_error = str(e)
            return False
        self._reconnect_error = None
        (log.debug if self._reconnect_logged else log.info)("Reconnecting the VPN to %s: %s", self.current, why)
        self._reconnect_logged = True
        return True

    async def _pin(self, hostname, quiet=False):
        """Tells gluetun to use hostname. Returns 'ok', 'unknown' (gluetun doesn't know it) or 'error'."""
        try:
            known = await self.set_server(hostname)
        except Exception as e:
            (log.debug if quiet else log.warning)("Could not tell the VPN to use %s: %s", hostname, e)
            return 'error'
        if known:
            return 'ok'
        log.warning("The VPN doesn't know Mullvad server %s yet (its server list is older); skipping it", hostname)
        self.unknown.add(hostname)
        if self.current == hostname:
            self.current = None
        return 'unknown'
