# Docker on other NASes and on Linux

The [user guide's Docker section](USER_GUIDE.md#9-docker) builds the image from source on a Synology NAS. This page covers the published image instead, on QNAP, Unraid, TrueNAS SCALE, and Docker or Podman on any Linux computer.

From the first release, each release publishes a signed image, `ghcr.io/acornelissen/inky-departures`, for 64-bit computers (`linux/amd64` and `linux/arm64`). There is no image for 32-bit ARM. Until the first release, build from source as in the user guide.

Contents:

- [Every platform](#every-platform): pick a release, check it, and keep it on the home network.
- [QNAP (Container Station)](#qnap-container-station)
- [Unraid](#unraid)
- [TrueNAS SCALE](#truenas-scale)
- [Docker or Podman on Linux](#docker-or-podman-on-linux)

---

## Every platform

### Pick a release and check it

Run the image by its version and its digest, so it can't change under you. On any computer with Docker and [cosign](https://docs.sigstore.dev/cosign/system_config/installation/), for version 0.1.0 say:

```bash
VERSION=0.1.0
docker pull "ghcr.io/acornelissen/inky-departures:$VERSION"    # prints "Digest: sha256:..."
DIGEST=sha256:...    # the digest it printed
cosign verify "ghcr.io/acornelissen/inky-departures@$DIGEST" \
  --certificate-identity "https://github.com/acornelissen/inky-departures/.github/workflows/release.yml@refs/tags/v$VERSION" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

`cosign verify` must end without an error: then that digest was built and signed by this project's release workflow for that version. Use the image as `ghcr.io/acornelissen/inky-departures:0.1.0@sha256:...`, with the digest in full.

`docker-compose.ghcr.yml` in the project is a compose file for the published image, with the same settings as `docker-compose.yml`. Replace `X.Y.Z` and `DIGEST` in its `image:` line; until you do, compose refuses to run it.

### What the container needs

- **Port 8090**, for the dashboard at `/` and the settings at `/settings`.
- **A data folder at `/data`** that its user, uid 10001, can write to. A new named volume takes that owner from the image by itself. A folder on the NAS doesn't: give it to 10001 first (each platform below says how).
- **Nothing else.** It runs as uid 10001 with a read-only system, no Linux capabilities, no new privileges, 512 MB of memory and 128 processes at most, and a 64 MB `/tmp` in memory. Keep those settings wherever the platform allows them.
- **Keys:** paste them in the settings, which keep them in `/data`, readable by the app only. No environment variables are needed.

### Addresses it answers to

The dashboard and the settings only answer when the address in the browser is one of these:

- an IP address on the home network (such as `192.168.1.20`),
- a name with no dots (such as `nas` or `tower`),
- a name ending `.local`, `.lan` or `.home.arpa` (such as `tower.local`).

For any other name, such as `nas.example.com`, add it to a `config.toml` and mount that file read-only at `/config/config.toml`:

```toml
[dashboard]
allowed_hosts = ["nas.example.com"]
```

### Keep it on the home network

- Never forward port 8090 on your router. Don't put it behind the NAS's reverse proxy or remote-access service either. The dashboard has no password, by design, and the settings sign-in is not meant to face the internet.
- If the NAS or computer has a firewall, allow port 8090 only from your home network's addresses.
- The app turns away addresses outside the home network itself, but in Docker it may see every request as coming from Docker's own private network. The firewall is what keeps the rest out.
- On Linux, ports Docker publishes skip `ufw`'s rules ([Docker's documentation](https://docs.docker.com/engine/network/packet-filtering-firewalls/) explains why). To limit it to one network, publish the port on the computer's home-network address only: `-p 192.168.1.20:8090:8090`, or `"192.168.1.20:8090:8090"` under `ports:`.

### First start

Open `http://<NAS IP address>:8090/` and [set it up](USER_GUIDE.md#4-first-run-and-the-setup-wizard). The code to claim the settings is in the container's log. The commands that set the password or print a new code are in [Password and reset codes](USER_GUIDE.md#6-password-and-reset-codes); they name the container `departureboard`, as the files and commands here call it. If your platform names it otherwise, use that name.

### Updating

Check the new version as above, then replace the version and digest in the image line and start the container again from the new image. Your settings, keys, password, history and timetable stay in `/data`.

---

## QNAP (Container Station)

Container Station 3 runs a compose file as an application.

1. In **Container Station**, choose **Create**, then **Create Application**.
2. Name it `departureboard`. Paste `docker-compose.ghcr.yml`, with the version and digest filled in.
3. Choose **Validate YAML**, then finish the wizard.
4. Open `http://<NAS IP address>:8090/` from the home network.

Container Station can't change an application's settings in place: to update, use **Recreate** with the new image line. The data stays in the `dashboard-data` volume.

---

## Unraid

`deploy/unraid/departureboard.xml` is a template for Unraid's **Add Container** page. It is in each release's tarball. In Unraid's terminal (or over SSH, as root), for version 0.1.0 say:

```bash
VERSION=0.1.0
cd /tmp
base="https://github.com/acornelissen/inky-departures/releases/download/v$VERSION"
curl -fLO "$base/inky-departures-$VERSION.tar.gz"
curl -fLO "$base/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS &&
  tar -xzf "inky-departures-$VERSION.tar.gz" "inky-departures-$VERSION/deploy/unraid/departureboard.xml" &&
  sed "s/X\.Y\.Z/$VERSION/" "inky-departures-$VERSION/deploy/unraid/departureboard.xml" \
    > /boot/config/plugins/dockerMan/templates-user/my-departureboard.xml
mkdir -p /mnt/user/appdata/departureboard
chown 10001:10001 /mnt/user/appdata/departureboard
chmod 700 /mnt/user/appdata/departureboard
```

Then:

1. Check the image's tag with `cosign verify`, as in [Pick a release and check it](#pick-a-release-and-check-it).
2. In the **Docker** tab, choose **Add Container**, and pick `departureboard` from the **Template** list.
3. Check the settings and choose **Apply**.
4. Open `http://tower:8090/` (your server's name) or its IP address, from the home network.

The template sets the hardening in **Extra Parameters** (shown in the advanced view): read-only system, a 64 MB `/tmp`, no capabilities, no new privileges, and the memory, process and log limits. What it can't do as the compose file does:

- **Pin a digest.** The template names a version tag. Docker on Unraid runs whatever that tag points to when it pulls, so check the tag with `cosign verify` before each **Apply**.
- **Make the data folder's owner.** Hence the `chown` above. If a permissions tool resets `/mnt/user/appdata/departureboard` to another owner, the app can't save; run the `chown` again.
- **Read a keys file.** Paste the keys in the settings instead.

Unraid's own autostart switch, in the Docker tab, takes the place of a restart policy.

---

## TrueNAS SCALE

On TrueNAS SCALE 24.10 or later, apps run in Docker, and a compose file can be installed as a custom app.

1. In **Apps**, choose **Discover Apps**, then **Install via YAML** from the ⋮ menu.
2. Name it `departureboard`. Paste `docker-compose.ghcr.yml`, with the version and digest filled in, and save.
3. Open `http://<NAS IP address>:8090/` from the home network. Apps installed from YAML have no Web UI button.

Use YAML rather than the guided **Custom App** form: the form, as TrueNAS documents it, has no setting for a read-only system.

The compose file keeps the data in a named volume, which works as it is. TrueNAS recommends a dataset for data that should last. To use one, create the dataset, give user ID 10001 read and write access in its permissions, and replace `- dashboard-data:/data` with `- /mnt/<pool>/<dataset>:/data` (and remove the `volumes:` block at the end).

---

## Docker or Podman on Linux

With the version and digest checked as above:

```bash
docker run -d --name departureboard --restart unless-stopped \
  -p 8090:8090 -v departureboard-data:/data \
  --read-only --tmpfs /tmp:size=64m --cap-drop ALL \
  --security-opt no-new-privileges:true --memory 512m --pids-limit 128 \
  --log-opt max-size=1m --log-opt max-file=3 \
  "ghcr.io/acornelissen/inky-departures:$VERSION@$DIGEST"
```

Or, in a folder with `docker-compose.ghcr.yml` filled in: `docker compose -f docker-compose.ghcr.yml up -d`.

To update with `docker run`: check the new version, then `docker rm -f departureboard` and run the command again with the new version and digest. The `departureboard-data` volume keeps everything.

**Podman** takes the same command, with `podman` in place of `docker`. Without root (rootless):

- A named volume, as above, takes the image's owner by itself.
- A folder doesn't. Give it to the container's user through Podman's user mapping: `podman unshare chown 10001:10001 ~/departureboard-data`, then `-v ~/departureboard-data:/data`. On a system with SELinux (Fedora, say), add `:Z`: `-v ~/departureboard-data:/data:Z`.
- If Podman can't set `--memory` or `--pids-limit` without root, see Podman's troubleshooting guide on resource limits for rootless containers.
- Podman doesn't start containers at boot by itself. Use a Quadlet (systemd) unit for that, as Podman's documentation describes.

Codes and errors are in `docker logs departureboard` (or `podman logs departureboard`).
