# How the web server is kept safe

What protects the settings site and the dashboard, and, on a Pi, what keeps the web server off the internet. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

- [On every install](#on-every-install)
- [On a Pi: the web server can't connect out](#on-a-pi-the-web-server-cant-connect-out)
- [The network guard](#the-network-guard)
- [Checking the guard](#checking-the-guard)
- [The fetch service](#the-fetch-service)

## On every install

- **Home network only.** Only private and link-local addresses get in, and requests must name a local host, which stops DNS-rebinding pages. In Docker, the firewall matters too: see [Keep it on the home network](docker.md#keep-it-on-the-home-network).
- **Keep it behind your router,** with no port forwarded to 8090. The settings use plain HTTP on your home network, so use a password you don't use anywhere else.
- **Changing anything** needs a signed-in session, a form token and a same-origin check. The dashboard itself only shows, and needs no password; its rules are in [the dashboard API](dashboard-api.md#access-rules-every-request).
- **Scripts.** The wizard under `/settings/setup/` is the only part that needs JavaScript. It serves a fixed list of files with set content types, and its JSON API needs the session, its form token in an `X-CSRF-Token` header, a same-origin request and a body under 16 kB. Its pages, and a signed-in visitor's settings pages, are the only ones whose Content-Security-Policy allows a script (`script-src 'self'`): on the settings pages that is `/settings/settings.js` alone, which marks unsaved changes and connects to nothing. The sign-in, reset and first-claim pages allow none.
- **Guessing the password.** Five wrong passwords lock an address out for 15 minutes (an IPv6 address by its /64, as a device can take any address in it). Twenty from all addresses together slow every sign-in after them, down to one try a minute, so changing address gains a guesser nothing. Being on the network is not enough to set a password: that needs a [one-time code](password.md).
- **Keys** are never shown, not even in the settings. See [Keeping keys safe](keys.md#keeping-keys-safe).

## On a Pi: the web server can't connect out

The server checks every visitor's address itself, and two layers in the Pi's kernel back it up, each covering both directions:

- **The unit's systemd IP firewall:** `IPAddressDeny=any`, with `IPAddressAllow=` the home network's ranges.
- **The network guard:** nftables rules for the web server alone (below).

Sign-in, lockout, the task limit and the two-at-a-time password checks apply whatever the kernel does.

## The network guard

- **What it is:** nftables rules for the web server alone, in `/opt/twobells/twobells-web.nft` (from `deploy/pi/twobells-web.nft`, owned by root). `twobells-guard.service` loads them as root at boot, before the web server starts (`nft -f`), and removes them as it stops.
- **How it finds the server:** the rules match the server's own connections by its cgroup, through a set that systemd fills. The web server's unit has `NFTSet=`, so systemd puts its cgroup in the set as the server starts and takes it out as it stops. The fetch service, the same user in another cgroup, still reaches the internet. The rules also drop connections to port 8090 from outside the home network.
- **Installing it:** `install.sh` installs `nftables` for them, and `update.sh` installs it if it's missing. `sudo nft list set inet twobells_web web` shows the set: an element while the web server runs, none while it doesn't.
- **On a Pi Zero or Pi 1,** systemd can't apply its IP firewall: Raspberry Pi OS boots those with `cgroup_disable=memory`, and the BPF firewall can't be set up (the log says "Preparation of BPF allow maps failed: Cannot allocate memory" each time the web server starts). The memory cap (`MemoryMax=`) doesn't apply there either, nor, on the Pi Zero W tried, the fetch unit's `SocketBindDeny=` (it may listen on nothing), as its systemd was built without BPF support. The nftables guard doesn't need either of them, so it covers for the web server's firewall; the fetch service listens on nothing by its own code alone there.
- **It needs systemd 254 or later** for `NFTSet=`. Raspberry Pi OS Trixie has it; Bookworm has systemd 252, so there the set stays empty and the guard holds nothing. On a newer Pi that is fine, as systemd's IP firewall works there, but a Pi Zero or Pi 1 on Bookworm has nothing in the kernel stopping the web server connecting out: use Raspberry Pi OS Trixie on those. `install.sh` and `update.sh` say so when it happens.
- **By id, not by path:** the guard matches the cgroup by the id systemd reads from the kernel. nft turns a path into an id by its folder's inode number, which on a 32-bit Pi isn't the kernel's id, so a rule by path never matches there.

## Checking the guard

If the guard can't load (nft missing, say), the web server starts anyway, as its own address check still turns away visitors from outside. It says so: `install.sh` and `update.sh` print a warning when the guard isn't holding the running server, and so does the server's log.

**Network guard**, under **On this device** in the settings' [Overview](settings.md#overview), says whether the server can connect out:

- **On:** something stops it, the guard or the systemd firewall.
- **Off:** nothing stops it. This counts as a problem. The server still turns away visitors from outside your home network itself. Run `sudo systemctl restart twobells-guard twobells-web`, then look for the reason in `journalctl -u twobells-guard -u twobells-web`.
- **Can't tell:** the check couldn't see either way, most often because there was no route out yet, over IPv4 or IPv6 (Wi-Fi still connecting, say). This isn't a problem in itself; it checks again the next time the Overview is drawn.

The server checks when it starts, and again whenever the Overview is drawn, at most once a minute, by sending one empty UDP packet to `192.0.2.1`, an address kept for documentation that is never routed; with no IPv4 route it tries `2001:db8::1`, IPv6's documentation address, the same way. A guard refuses it at once, and the send never waits. **Checked** gives the time of the last check.

Something that flushes every nftables rule while the server runs, like `sudo nft flush ruleset` or `sudo systemctl restart nftables`, removes the guard until you run `sudo systemctl restart twobells-guard twobells-web` or the Pi restarts. Restarting the guard alone loads an empty set: the web server must restart after it to be put back in. The Overview says **Off** the next time it checks, unless the systemd firewall still stops it.

## The fetch service

On a Pi, the web server's internet work runs in a separate service, `twobells-fetch.service`: the wizard's lookups, key tests and bus company search, and the dashboard's feeds. It goes out to the internet and listens on nothing.

- **Why:** the web server can't open a connection to the internet itself, which narrows what a flaw in it could do. It isn't a wall: the web server and the fetch service run as the same user, and the web server still tells the fetch service what to look up.
- **How they talk:** the web server passes each request through a private folder in the Pi's memory (`/run/twobells-fetch`), deleted as soon as it is read. Nothing from setup, not your home's position nor a pasted key, is written to the SD card as a file. Memory itself can reach the card if the Pi swaps to it (with `dphys-swapfile`, say), as with anything else the Pi is doing. Saved keys never go that way, as the fetch service reads them itself.
- **Priority:** the bus company search runs at a lower priority than everything else, so the board and the wizard's other lookups stay quick while it works.

In Docker and on a desktop, the lookups run in the server itself.
