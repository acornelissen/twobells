# Docker

The dashboard and its settings in one container, `twobells`, on a NAS or any always-on computer. The container runs as an unprivileged user (uid 10001) with a read-only system, keeps its data in the `twobells-data` volume, and restarts by itself.

There are two ways to run it:

- **The published image**, `ghcr.io/acornelissen/twobells`: signed, for 64-bit computers (`linux/amd64` and `linux/arm64`), with nothing to build. `deploy/docker/docker-compose.ghcr.yml` runs it. Every platform below can use it. There is no image for 32-bit ARM.
- **Built from source** with `docker-compose.yml` at the top of the project, as on [Synology](#built-from-source).

Contents:

- [Every platform](#every-platform): pick a release, check it, and keep it on the home network.
- [Synology (Container Manager)](#synology-container-manager)
- [QNAP (Container Station)](#qnap-container-station)
- [Unraid](#unraid)
- [TrueNAS SCALE](#truenas-scale)
- [Docker or Podman on Linux](#docker-or-podman-on-linux)

---

## Every platform

### Pick a release and check it

Run the image by its version and its digest, so it can't change under you. On any computer with Docker and [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) 3.0 or later, for version 0.4.2 say:

```bash
VERSION=0.4.2
docker pull "ghcr.io/acornelissen/twobells:$VERSION"    # prints "Digest: sha256:..."
DIGEST=sha256:...    # the digest it printed
cosign verify "ghcr.io/acornelissen/twobells@$DIGEST" \
  --certificate-identity "https://github.com/acornelissen/twobells/.github/workflows/release.yml@refs/tags/v$VERSION" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

`cosign verify` must end without an error: then that digest was built and signed by this project's release workflow for that version. Use the image as `ghcr.io/acornelissen/twobells:0.4.2@sha256:...`, with the digest in full. An older cosign says `no signatures found`: the release signs with cosign 3, which older versions can't find.

`deploy/docker/docker-compose.ghcr.yml` in the project is a compose file for the published image, with the same settings as `docker-compose.yml`. Replace `X.Y.Z` and `DIGEST` in its `image:` line; until you do, compose refuses to run it.

### What the container needs

- **Port 8090**, for the dashboard at `/` and the settings at `/settings`, published on IPv4 only (`0.0.0.0:8090:8090`; see [Keep it on the home network](#keep-it-on-the-home-network)).
- **A data folder at `/data`** that its user, uid 10001, can write to. A new named volume takes that owner from the image by itself. A folder on the NAS doesn't: give it to 10001 first (each platform below says how).
- **Nothing else.** It runs as uid 10001 with a read-only system, no Linux capabilities, no new privileges, 512 MB of memory and 128 processes at most, and a 64 MB `/tmp` in memory. Keep those settings wherever the platform allows them. The bus timetable's build fits in that memory beside the dashboard: a timetable file over 100 MB is skipped with a warning in the log, and a build with more than 8,000 journeys calling at your stops fails, saying so in the log. Metrolink's tram timetable, about 45 MB, is read a line at a time, so it fits too.
- **Keys:** paste them in the settings, which keep them in `/data`, readable by the app only. No environment variables are needed.

### Addresses it answers to

The dashboard and the settings only answer when the address in the browser is one of these:

- an IP address on the home network (such as `192.168.1.20`),
- a name with no dots (such as `nas` or `tower`),
- a name ending `.local`, `.lan` or `.home.arpa` (such as `tower.local`).

For any other name, such as `nas.example.com`, add it to a [`config.toml` of your own](#a-configtoml-of-your-own):

```toml
[dashboard]
allowed_hosts = ["nas.example.com"]
```

### A config.toml of your own

No `config.toml` is needed. To keep one by hand (for `allowed_hosts`, say), mount it read-only at `/config/config.toml`. The settings save over it in the volume, never into it. The compose files show how: uncomment the line in `deploy/docker/docker-compose.ghcr.yml`, or, building from source, copy `docker-compose.override.yml.example` to `docker-compose.override.yml` next to `config.toml`. [Settings by hand](configuration.md) says what goes in it.

### Keep it on the home network

- Never forward port 8090 on your router. Don't put it behind the NAS's reverse proxy or remote-access service either. The dashboard has no password, by design, and the settings sign-in is not meant to face the internet.
- If the NAS or computer has a firewall, allow port 8090 only from your home network's addresses.
- The app turns away addresses outside the home network itself, but in Docker it may see every request as coming from Docker's own private network. The firewall is what keeps the rest out.
- The files and commands here publish the port on IPv4 only: `0.0.0.0:8090:8090`, not `8090:8090`. Docker passes on a visitor's IPv6 connection from its own network's gateway, a private address, so the app would take anyone reaching it over IPv6 for the home network. Keep the `0.0.0.0:` if you change the port.
- On Linux, ports Docker publishes skip `ufw`'s rules ([Docker's documentation](https://docs.docker.com/engine/network/packet-filtering-firewalls/) explains why). To limit it to one network, publish the port on the computer's home-network IPv4 address only: `-p 192.168.1.20:8090:8090`, or `"192.168.1.20:8090:8090"` under `ports:`.

### First start

Open `http://<NAS IP address>:8090/` and [set it up](setup-wizard.md). The code to claim the settings is in the container's log. The commands that set the password or print a new code are in [Password and reset codes](password.md); they name the container `twobells`, as the files and commands here call it. If your platform names it otherwise, use that name.

### Updating

Check the new version as above, then replace the version and digest in the image line and start the container again from the new image. Your settings, keys, password, history and timetable stay in `/data`; the bus timetable is built again once if the new release keeps it differently, as 0.2.0 and 0.2.2 do (see [Bus times](buses.md#how-bus-times-are-worked-out)). Building from source, see [Built from source](#built-from-source).

Going back to an older version, the older one may not read what a newer one keeps. The punctuality history is one: 0.1.0 reads only its version 1, and stops at the start, saying so in its log, if the history is newer. Before moving the history on, the newer version kept a copy of it as it was, `history.sqlite3.v1` in `/data`. To put it back (what was recorded since is lost), with the older image in place of `IMAGE` and your data volume or folder in place of `twobells-data`:

```bash
docker stop twobells
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --user 10001:10001 -v twobells-data:/data --entrypoint sh IMAGE \
  -c 'cp /data/history.sqlite3.v1 /data/history.sqlite3 && rm -f /data/history.sqlite3-wal /data/history.sqlite3-shm'
docker start twobells
```

---

## Synology (Container Manager)

Either run the published image, or let Container Manager build the image from the project's own `docker-compose.yml`. Both keep the data in the `twobells-data` volume, so you can move from one to the other. Their health shows in Container Manager.

### The published image

1. Make a folder on the NAS, for example `docker/twobells` (File Station).
2. Copy `deploy/docker/docker-compose.ghcr.yml` into it as `docker-compose.yml`, with the version and digest filled in, as in [Pick a release and check it](#pick-a-release-and-check-it).
3. For keys in a file rather than the settings, copy `deploy/secrets.env.example` there as `secrets.env` and uncomment the `env_file:` lines in the compose file. Keep it private: in File Station, give only your own account access.
4. In **Container Manager**, go to **Project**, then **Create**. Pick the folder; it uses the `docker-compose.yml` there. Create the project. It pulls the image and starts it.
5. Open `http://<NAS name>:8090/` or `http://<NAS IP address>:8090/` from the home network, and [set it up](setup-wizard.md). Codes go to the container's log, under **Container**, `twobells`, **Log**.

To update, put the new version and digest in the compose file, then run `docker compose up -d` in the folder over SSH (Container Manager's **Action**, then **Build**, does the same from the project).

### Built from source

Elsewhere, `docker compose up -d --build` in the folder does the same as these steps.

1. Copy the project folder to the NAS, for example to `docker/twobells` (File Station, or `scp`).
2. In that folder, copy `deploy/secrets.env.example` to `secrets.env`. Fill in your keys, or leave them empty and paste them in the settings later. Keep it private: in File Station, give only your own account access.
3. In **Container Manager**, go to **Project**, then **Create**. Pick the folder; it uses the `docker-compose.yml` there. Create the project. It builds the image on the NAS and starts it.
4. Open `http://<NAS name>:8090/` or `http://<NAS IP address>:8090/` from the home network, and [set it up](setup-wizard.md). Codes go to the container's log, under **Container**, `twobells`, **Log**.

**Updating:** copy the new project files over the old ones, then in Container Manager choose the project, then **Action**, then **Build** (or `docker compose up -d --build`). Your history, timetable, saved settings, keys and password stay in the volume. After editing `secrets.env`, build again too: just restarting the container doesn't read it again.

### On any Synology

Don't publish port 8090 through QuickConnect or DSM's reverse proxy (see [Keep it on the home network](#keep-it-on-the-home-network)). For extra safety, add a DSM firewall rule that allows 8090 only from your home network's addresses.

---

## QNAP (Container Station)

Container Station 3 runs a compose file as an application.

1. In **Container Station**, choose **Create**, then **Create Application**.
2. Name it `twobells`. Paste `deploy/docker/docker-compose.ghcr.yml`, with the version and digest filled in.
3. Choose **Validate YAML**, then finish the wizard.
4. Open `http://<NAS IP address>:8090/` from the home network.

Container Station can't change an application's settings in place: to update, use **Recreate** with the new image line. The data stays in the `twobells-data` volume.

---

## Unraid

`deploy/unraid/twobells.xml` is a template for Unraid's **Add Container** page. It is in each release's tarball. In Unraid's terminal (or over SSH, as root), for version 0.4.2 say:

```bash
VERSION=0.4.2
cd /tmp
base="https://github.com/acornelissen/twobells/releases/download/v$VERSION"
curl -fLO "$base/twobells-$VERSION.tar.gz"
curl -fLO "$base/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS &&
  tar -xzf "twobells-$VERSION.tar.gz" "twobells-$VERSION/deploy/unraid/twobells.xml" &&
  sed "s/X\.Y\.Z/$VERSION/" "twobells-$VERSION/deploy/unraid/twobells.xml" \
    > /boot/config/plugins/dockerMan/templates-user/my-twobells.xml
mkdir -p /mnt/user/appdata/twobells
chown 10001:10001 /mnt/user/appdata/twobells
chmod 700 /mnt/user/appdata/twobells
```

The `sed` puts the version in the template's image tag (its **Repository**).

Then:

1. Check the image's tag with `cosign verify`, as in [Pick a release and check it](#pick-a-release-and-check-it).
2. In the **Docker** tab, choose **Add Container**, and pick `twobells` from the **Template** list.
3. Check the settings and choose **Apply**.
4. Open `http://tower:8090/` (your server's name) or its IP address, from the home network.

The template sets the hardening in **Extra Parameters** (shown in the advanced view): read-only system, a 64 MB `/tmp`, no capabilities, no new privileges, and the memory, process and log limits. What it can't do as the compose file does:

- **Pin a digest.** The template names a version tag. Docker on Unraid runs whatever that tag points to when it pulls, so check the tag with `cosign verify` before each **Apply**.
- **Make the data folder's owner.** Hence the `chown` above. If a permissions tool resets `/mnt/user/appdata/twobells` to another owner, the app can't save; run the `chown` again.
- **Read a keys file.** Paste the keys in the settings instead.

Unraid's own autostart switch, in the Docker tab, takes the place of a restart policy.

---

## TrueNAS SCALE

On TrueNAS SCALE 24.10 or later, apps run in Docker, and a compose file can be installed as a custom app.

1. In **Apps**, choose **Discover Apps**, then **Install via YAML** from the ⋮ menu.
2. Name it `twobells`. Paste `deploy/docker/docker-compose.ghcr.yml`, with the version and digest filled in, and save.
3. Open `http://<NAS IP address>:8090/` from the home network. Apps installed from YAML have no Web UI button.

Use YAML rather than the guided **Custom App** form: the form, as TrueNAS documents it, has no setting for a read-only system.

The compose file keeps the data in a named volume, which works as it is. TrueNAS recommends a dataset for data that should last. To use one, create the dataset, give user ID 10001 read and write access in its permissions, and replace `- data:/data` with `- /mnt/<pool>/<dataset>:/data` (and remove the `volumes:` block at the end).

---

## Docker or Podman on Linux

With the version and digest checked as above:

```bash
docker run -d --name twobells --restart unless-stopped \
  -p 0.0.0.0:8090:8090 -v twobells-data:/data \
  --read-only --tmpfs /tmp:size=64m --cap-drop ALL \
  --security-opt no-new-privileges:true --memory 512m --pids-limit 128 \
  --log-opt max-size=1m --log-opt max-file=3 \
  "ghcr.io/acornelissen/twobells:$VERSION@$DIGEST"
```

Or copy `deploy/docker/docker-compose.ghcr.yml` to a folder, fill it in, and run `docker compose -f docker-compose.ghcr.yml up -d` there.

To update with `docker run`: check the new version, then `docker rm -f twobells` and run the command again with the new version and digest. The `twobells-data` volume keeps everything.

**Podman** takes the same command, with `podman` in place of `docker`. Without root (rootless):

- A named volume, as above, takes the image's owner by itself.
- A folder doesn't. Give it to the container's user through Podman's user mapping: `podman unshare chown 10001:10001 ~/twobells-data`, then `-v ~/twobells-data:/data`. On a system with SELinux (Fedora, say), add `:Z`: `-v ~/twobells-data:/data:Z`.
- If Podman can't set `--memory` or `--pids-limit` without root, see Podman's troubleshooting guide on resource limits for rootless containers.
- Podman doesn't start containers at boot by itself. Use a Quadlet (systemd) unit for that, as Podman's documentation describes.

Codes and errors are in `docker logs twobells` (or `podman logs twobells`).
