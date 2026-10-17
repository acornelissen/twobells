
"""Deployment security settings (the dashboard container, the Pi's units), so an edit can't quietly drop one."""

import json
import re
import shlex
from pathlib import Path

import pytest

from tests.paths import ROOT
from twobells.dashboard.history import FILE_NAME

DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "docker-compose.yml").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text()
PI = ROOT / "deploy" / "pi"  # the Pi's units, scripts and pins
REQUIREMENTS = (ROOT / "deploy" / "docker" / "requirements-docker.txt").read_text()


def test_base_image_is_pinned_by_digest():
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", DOCKERFILE, re.M)


def test_dependencies_come_from_hashed_wheels_only():
    assert "--require-hashes" in DOCKERFILE and "--only-binary=:all:" in DOCKERFILE
    pins = re.findall(r"^([a-z0-9-]+)==\S+ \\$", REQUIREMENTS, re.M)
    assert pins == ["pillow", "tzdata"]
    for block in re.split(r"\n(?=[a-z])", REQUIREMENTS.split("\n", 2)[2]):
        if "==" in block:
            assert re.search(r"--hash=sha256:[0-9a-f]{64}", block)


def test_the_pi_builds_the_code_with_hashed_tools_and_fetches_nothing_else():
    # Build isolation would fetch whatever setuptools is newest, unpinned, and
    # run it as root; the build instead uses the hashed one, put in the venv first.
    build_tools = (PI / "requirements-build.txt").read_text()
    assert re.findall(r"^([a-z0-9-]+)==\S+ \\$", build_tools, re.M) == ["setuptools"]
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", build_tools)) >= 1
    for name in ("install.sh", "update.sh"):
        lines = [line.strip() for line in _script(name).splitlines()
                 if line.strip().startswith('"$PREFIX/venv/bin/pip" install')]
        tools = next(i for i, line in enumerate(lines) if "requirements-build.txt" in line)
        assert "--require-hashes" in lines[tools] and "--no-deps" in lines[tools], name
        code = [i for i, line in enumerate(lines) if '"$build"' in line]
        assert code and all(i > tools and "--no-build-isolation" in lines[i] for i in code), name


def test_it_runs_as_a_non_root_user_and_checks_its_health():
    assert re.search(r"^USER 10001:10001$", DOCKERFILE, re.M)
    assert "HEALTHCHECK" in DOCKERFILE and "/healthz" in DOCKERFILE
    assert "ADD " not in DOCKERFILE  # COPY only: nothing fetched by URL


def test_the_image_never_carries_keys_or_state():
    assert DOCKERIGNORE.splitlines()[1] == "*"  # everything out, then only what's needed back in
    allowed = [line for line in DOCKERIGNORE.splitlines() if line.startswith("!")]
    assert allowed == ["!src/", "!pyproject.toml", "!deploy/docker/requirements-docker.txt"]
    assert "secrets" not in DOCKERFILE.replace("--secrets", "")


def test_compose_locks_the_container_down():
    for setting in ("read_only: true", "- ALL", "no-new-privileges:true", "mem_limit:", "pids_limit:"):
        assert setting in COMPOSE, setting
    for risky in ("privileged", "network_mode", "docker.sock", "cap_add"):
        assert risky not in COMPOSE, risky


def test_compose_runs_twobells_on_its_named_volume():
    lines = [line.split("#")[0].strip() for line in COMPOSE.splitlines()]
    for line in ("twobells:", "image: twobells:local", "container_name: twobells",
                 "- secrets.env", "- data:/data", "- \"0.0.0.0:8090:8090\""):
        assert line in lines, line
    assert lines[lines.index("env_file:") + 1] == "- secrets.env"
    # Named outright, so another folder or project name never starts it on a new, empty volume.
    for compose in (COMPOSE, (ROOT / "deploy" / "docker" / "docker-compose.ghcr.yml").read_text()):
        assert re.search(r"^volumes:\n  data:\n    name: twobells-data\n", compose, re.M)
    # config.toml only through the optional override, which stays out of git.
    assert "config.toml:" not in COMPOSE and "dashboard.env" not in COMPOSE
    override = (ROOT / "docker-compose.override.yml.example").read_text()
    assert "services:\n  twobells:\n    volumes:\n      - ./config.toml:/config/config.toml:ro\n" in override
    assert "docker-compose.override.yml" in (ROOT / ".gitignore").read_text().splitlines()
    assert "/config/config.toml" in _entrypoint()  # where the override puts it


def test_the_image_knows_its_version_without_installing_the_package():
    # Nothing is installed, so version.py reads pyproject.toml beside src/; the build fails without it.
    assert "PYTHONPATH=/app/src" in DOCKERFILE
    assert re.search(r"^COPY pyproject\.toml /app/pyproject\.toml$", DOCKERFILE, re.M)
    assert "assert v() != 'unknown'" in DOCKERFILE


def test_the_image_runs_as_twobells_with_the_volumes_uid():
    assert re.search(r"useradd --system --uid 10001 .* twobells \\$", DOCKERFILE, re.M)
    assert "chown 10001:10001 /data" in DOCKERFILE


def _entrypoint() -> list[str]:
    found = re.search(r"^ENTRYPOINT (\[.*?\])$", DOCKERFILE, re.M | re.S).group(1)
    return json.loads(found.replace("\\\n", ""))


def test_the_container_serves_as_a_docker_install_in_its_one_writable_folder():
    from twobells.cli.main import _parser

    entrypoint = _entrypoint()
    assert entrypoint[:3] == ["python", "-m", "twobells"]
    args = _parser().parse_args(entrypoint[3:])
    assert (args.command, args.install, args.state_dir, args.host, args.port) == ("serve", "docker", "/data",
                                                                                  "0.0.0.0", 8090)
    assert (args.config, args.secrets) == ("/config/config.toml", "/nonexistent")
    assert "EXPOSE 8090" in DOCKERFILE and "127.0.0.1:8090/healthz" in DOCKERFILE


def test_the_documented_password_commands_fit_the_container():
    # Run with docker exec as the image's user, in the one writable place: /data.
    from twobells.cli.main import _parser
    from twobells.settings.installs import install_named

    assert "WORKDIR /data" in DOCKERFILE
    guide, readme = (ROOT / "docs" / "password.md").read_text(), (ROOT / "README.md").read_text()
    docker = install_named("docker")
    # What the settings pages tell the owner to run, as the docs give it.
    for flag, command in (("--set-password", docker.password_command), ("--reset-code", docker.reset_command)):
        assert command in guide and command in readme, flag
        words = shlex.split(command)
        assert words[:4] == ["docker", "exec", "-it", "twobells"]  # the compose file's container_name
        assert words[4:7] == ["python", "-m", "twobells"]
        args = _parser().parse_args(words[7:])
        assert (args.command, args.state_dir, getattr(args, flag[2:].replace("-", "_"))) == ("serve", "/data", True)
    assert "docker logs twobells<" in docker.log_help and "Container, twobells, Log" in docker.log_help


def test_a_desktop_says_where_its_log_is_however_it_runs():
    from twobells.settings.installs import install_named

    desktop = install_named("desktop").log_help
    # In a terminal, or started at login by the files in deploy/desktop.
    plist = (ROOT / "deploy" / "desktop" / "uk.twobells.serve.plist").read_text()
    assert "Terminal" in desktop and "~/Library/Logs/twobells.log" in desktop
    assert "/Library/Logs/twobells.log" in plist
    assert "journalctl --user -u twobells" in desktop


def test_keys_files_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "secrets.env" in ignored  # anywhere: the Pi's copy, Docker's next to docker-compose.yml
    assert "dashboard.env" in ignored  # Docker's old name, until every NAS has moved it


def test_one_key_template_for_the_pi_and_docker_in_groups():
    assert not (ROOT / "deploy" / "dashboard.env.example").exists()
    from twobells.settings import schema

    template = (ROOT / "deploy" / "secrets.env.example").read_text()
    assert re.findall(r"^[A-Z_]+=.", template, re.M) == []  # every value empty
    # Each group's keys under its heading, as the settings registry has them, and no others.
    groups = ("general", "board", "dashboard")
    starts = [template.index(f"# {group.capitalize()}:") for group in groups]
    for group, start, end in zip(groups, starts, [*starts[1:], len(template)], strict=True):
        names = re.findall(r"^([A-Z_]+)=$", template[start:end], re.M)
        assert names == [name for name, _, _ in schema.keys(group)], group
    assert len(re.findall(r"^[A-Z_]+=", template, re.M)) == sum(len(schema.keys(group)) for group in groups)
    assert "/etc/twobells/secrets.env" in template and "next to docker-compose.yml" in template
    install = (PI / "install.sh").read_text()
    assert '"$REPO/deploy/secrets.env.example" "$CONF/secrets.env"' in install


# The Pi's units: the web server stays off the internet; the fetch service goes out for it.


def _unit(name: str, folder: Path = PI) -> dict[str, list[str]]:
    settings: dict[str, list[str]] = {}
    for line in (folder / name).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            settings.setdefault(key, []).append(value)
    return settings


def test_the_settings_page_stays_on_the_home_network_and_hands_its_internet_work_on():
    web = _unit("twobells-web.service")
    assert web["IPAddressDeny"] == ["any"]  # with the dashboard view on too: its feeds come from the fetch service
    assert web["IPAddressAllow"] == ["localhost link-local 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 fc00::/7"]
    assert "twobells-fetch.path" in web["Wants"]


# The second layer of the same, for where systemd can't apply IPAddressAllow= (a Pi Zero's kernel).
GUARD = PI / "twobells-web.nft"
GUARD_UNIT = "twobells-guard.service"  # loads it at boot, before the web server
GUARD_FILE = "/opt/twobells/twobells-web.nft"  # where install.sh and update.sh put it
GUARD_TABLE = "inet twobells_web"
GUARD_SET = "web"  # the web server's cgroup, put in by systemd (the web unit's NFTSet=)
FETCH_SET = "fetch"  # the fetch service's, the same way: it listens on nothing
# systemd's names in IPAddressAllow=, as systemd.resource-control(5) gives them.
SYSTEMD_NAMES = {"localhost": ("127.0.0.0/8", "::1/128"), "link-local": ("169.254.0.0/16", "fe80::/64")}


def _rules() -> str:
    """The guard's rules without their comments."""
    return "\n".join(line.split("#")[0].rstrip() for line in GUARD.read_text().splitlines()
                     if line.split("#")[0].strip())


def _block(text: str, opening: str) -> str:
    """What is between `opening` (ending in "{") and its closing brace."""
    start = text.index(opening) + len(opening)
    depth = 1
    for i, char in enumerate(text[start:], start):
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth == 0:
            return text[start:i]
    raise AssertionError(f"{opening} never closes")


def _guard_set(name: str) -> set:
    import ipaddress

    body = _block(_rules(), f"set {name} {{")
    elements = _block(body, "elements = {")
    return {ipaddress.ip_network(element.strip()) for element in elements.split(",")}


def test_the_guard_unit_loads_the_table_as_root_at_boot_before_the_services_it_holds():
    guard = _unit(GUARD_UNIT)
    assert guard["Type"] == ["oneshot"] and guard["RemainAfterExit"] == ["yes"]
    assert guard["ExecStart"] == [f"/usr/sbin/nft -f {GUARD_FILE}"]
    # "-": stopping never fails on a table already gone (nft flush ruleset, say).
    assert guard["ExecStop"] == [f"-/usr/sbin/nft delete table {GUARD_TABLE}"]
    assert guard["Before"] == ["twobells-web.service twobells-fetch.service"]
    # After nftables' own rules, which begin by flushing every table, this one too.
    assert guard["After"] == ["nftables.service"]
    assert guard["WantedBy"] == ["multi-user.target"]
    # Root, but with nothing more than nft needs: CAP_NET_ADMIN and a netlink socket.
    assert "User" not in guard and "DynamicUser" not in guard
    assert guard["CapabilityBoundingSet"] == ["CAP_NET_ADMIN"]
    assert guard["RestrictAddressFamilies"] == ["AF_NETLINK"]
    assert "AmbientCapabilities" not in guard
    # In the Pi's own network namespace and user namespace, or the rules would guard nothing.
    for setting in ("PrivateNetwork", "PrivateUsers", "NetworkNamespacePath"):
        assert setting not in guard, setting


def test_the_web_unit_puts_its_cgroup_in_the_guards_set_and_starts_without_it():
    web = _unit("twobells-web.service")
    # systemd puts the cgroup's id, as the kernel has it, in the guard's set as
    # the server starts, and takes it out as it stops.
    assert web["NFTSet"] == [f"cgroup:{GUARD_TABLE.replace(' ', ':')}:{GUARD_SET}"]
    # After the guard, so its set is there to fill; but only wanted: if it
    # can't load, the server starts anyway (its own address check still
    # holds) and says the guard is off.
    assert GUARD_UNIT in web["Wants"] and GUARD_UNIT in web["After"]
    for binding in ("Requires", "Requisite", "BindsTo", "PartOf"):
        assert GUARD_UNIT not in " ".join(web.get(binding, [])), binding
    assert web["User"] == ["twobells"]  # the server itself is never root


def test_the_fetch_unit_puts_its_cgroup_in_the_guards_other_set_and_starts_without_it():
    # Where systemd can't apply SocketBindDeny= (built without BPF, as on a Pi
    # Zero W), the guard refuses connections to anything it listens on.
    fetch = _unit("twobells-fetch.service")
    assert fetch["NFTSet"] == [f"cgroup:{GUARD_TABLE.replace(' ', ':')}:{FETCH_SET}"]
    assert GUARD_UNIT in fetch["Wants"] and GUARD_UNIT in fetch["After"]
    for binding in ("Requires", "Requisite", "BindsTo", "PartOf"):
        assert GUARD_UNIT not in " ".join(fetch.get(binding, [])), binding
    assert fetch["SocketBindDeny"] == ["any"]  # still the first layer, where systemd can


def test_the_guide_says_how_the_guard_holds_and_how_to_bring_it_back():
    guide = (ROOT / "docs" / "security.md").read_text()
    for said in (f"`{GUARD_UNIT}`", "`NFTSet=`", "systemd 254",
                 # A Pi Zero or Pi 1 has neither systemd's firewall nor, on Bookworm (252), the guard.
                 "Raspberry Pi OS Trixie",
                 # Flushing the ruleset removes it; this loads it, then puts the server in its set again.
                 f"sudo systemctl restart {GUARD_UNIT.removesuffix('.service')} twobells-web twobells-fetch",
                 f"sudo nft list set {GUARD_TABLE} {GUARD_SET}"):
        assert said in guide, said
    assert "The unit loads them as root as the server starts" not in guide


def test_nothing_matches_the_web_server_by_a_cgroup_path():
    # nft turns a cgroup path into an id by its inode number, which on a 32-bit
    # kernel isn't the kernel's id (that has generation bits in its high
    # word), so a rule by path never matches there. The set holds the id
    # systemd gets from the kernel instead.
    assert "system.slice" not in _rules() and not re.search(r'cgroupv2 level \d+ "', _rules())
    for unit in PI.iterdir():
        if unit.suffix in (".service", ".timer", ".path"):
            for key, values in _unit(unit.name).items():
                if key.startswith("Exec") and unit.name != GUARD_UNIT:
                    assert not [value for value in values if "nft" in value], (unit.name, key)


def test_the_guard_replaces_its_own_table_in_one_go_and_nothing_else():
    rules = _rules()
    lines = rules.splitlines()
    # Made if missing, deleted, then made afresh: loading twice never adds the rules twice.
    assert lines[:3] == [f"table {GUARD_TABLE}", f"delete table {GUARD_TABLE}", f"table {GUARD_TABLE} {{"]
    assert rules.count(f"table {GUARD_TABLE}") == 3 and rules.count("table ") == 3  # no other table
    for risky in ("flush ruleset", "include", "policy drop"):  # nobody else's rules touched
        assert risky not in rules, risky
    assert rules.count("{") == rules.count("}")
    assert _block(rules, f"table {GUARD_TABLE} {{").strip().endswith("}")


def test_the_guard_lets_the_web_server_reach_only_the_home_network():
    rules = _rules()
    output = _block(rules, "chain output {")
    assert "type filter hook output priority filter; policy accept;" in output
    # Only the web service's own sockets, by its cgroup (system.slice/<unit> is
    # level 2) in the set systemd fills: the fetch service, the same user,
    # still goes out.
    cgroup = f"socket cgroupv2 level 2 @{GUARD_SET} jump web_out"
    assert [line.strip() for line in output.strip().splitlines()][1:] == [cgroup]
    assert (PI / f"{GUARD.stem}.service").is_file()
    # Empty as loaded: what is in it is systemd's to say.
    assert [line.strip() for line in _block(rules, f"set {GUARD_SET} {{").strip().splitlines()] == [
        "type cgroupsv2"]
    web_out = [line.strip() for line in _block(rules, "chain web_out {").strip().splitlines()]
    assert web_out == ["ip daddr @home4 accept", "ip6 daddr @home6 accept", "reject with icmpx admin-prohibited"]


def test_the_guard_holds_the_same_addresses_as_the_unit():
    import ipaddress

    [allowed] = _unit("twobells-web.service")["IPAddressAllow"]
    wanted = set()
    for word in allowed.split():
        wanted |= {ipaddress.ip_network(net) for net in SYSTEMD_NAMES.get(word, (word,))}
    home4, home6 = _guard_set("home4"), _guard_set("home6")
    assert home4 | home6 == wanted
    assert {net.version for net in home4} == {4} and {net.version for net in home6} == {6}
    for name, kind in (("home4", "ipv4_addr"), ("home6", "ipv6_addr")):
        body = _block(_rules(), f"set {name} {{")
        assert f"type {kind}" in body and "flags interval" in body, name


def test_the_guard_keeps_the_units_port_to_the_home_network():
    from twobells.cli.main import _parser

    port = _parser().parse_args(shlex.split(_unit("twobells-web.service")["ExecStart"][0])[1:]).port
    incoming = [line.strip() for line in _block(_rules(), "chain input {").strip().splitlines()
                if line.strip() and not line.strip().startswith("#")]
    assert incoming[:3] == ["type filter hook input priority filter; policy accept;",
                            f"tcp dport {port} ip saddr != @home4 drop",
                            f"tcp dport {port} ip6 saddr != @home6 drop"]


def test_the_guard_refuses_new_connections_to_the_fetch_service():
    rules = _rules()
    incoming = [line.strip() for line in _block(rules, "chain input {").strip().splitlines()
                if line.strip() and not line.strip().startswith("#")]
    # Only new connections: the replies to its own requests are established, and still come in.
    assert incoming[3:] == [f"ct state new socket cgroupv2 level 2 @{FETCH_SET} drop"]
    # Empty as loaded: what is in it is systemd's to say.
    assert [line.strip() for line in _block(rules, f"set {FETCH_SET} {{").strip().splitlines()] == [
        "type cgroupsv2"]


@pytest.mark.parametrize("name", ["install.sh", "update.sh"])
def test_install_and_update_put_the_guard_where_only_root_can_change_it(name):
    script = _script(name)
    assert re.search(r"^PREFIX=/opt/twobells$", script, re.M)
    put = f'install -m 644 -o root -g root "$REPO/deploy/pi/{GUARD.name}" "$PREFIX/{GUARD.name}"'
    assert put.replace("$PREFIX", "/opt/twobells").endswith(f'"{GUARD_FILE}"')
    assert put in script
    # In place before the guard loads it afresh, and that before the web
    # server starts, or restarts: loading the table empties its set, which
    # systemd fills again as the server starts. Enabled, so it loads at boot.
    # A guard that fails to load doesn't stop the script (|| true): the rest
    # still goes in, and it says so at the end.
    steps = [put, "\nsystemctl daemon-reload\n", f"\nsystemctl enable {GUARD_UNIT}\n",
             f"\nsystemctl restart {GUARD_UNIT} || true\n",
             "systemctl enable --now twobells-web.service" if name == "install.sh" else
             "systemctl try-restart twobells-web.service"]
    places = [script.index(step) for step in steps]
    assert places == sorted(places)
    started = steps[-1]
    # And afterwards, said if it isn't holding the server.
    assert script.index(started) < script.index("\nsay_if_unguarded\n")


SAY_IF_UNGUARDED = re.compile(r"^say_if_unguarded\(\) \{\n.*?^\}$", re.M | re.S)
# What nft lists for the guard's set once systemd has put the web server in (a
# 32-bit Pi may give the id as a number), and for one still empty.
FILLED = (f"table {GUARD_TABLE} {{\n\tset {GUARD_SET} {{\n\t\ttype cgroupsv2\n"
          '\t\telements = { "system.slice/twobells-web.service" }\n\t}\n}\n')
EMPTY = f"table {GUARD_TABLE} {{\n\tset {GUARD_SET} {{\n\t\ttype cgroupsv2\n\t}}\n}}\n"


def _say_if_unguarded(name: str, active: bool, listed: str | None, systemd: str = "257 (257.9-1~deb13u1)"):
    """Runs the script's say_if_unguarded, under its set -euo pipefail, with
    systemctl and nft stood in for: the web server running or not, nft
    listing `listed` for the guard's set (None: no table), and `systemd` as
    the first line of systemctl --version has it."""
    import subprocess

    [function] = SAY_IF_UNGUARDED.findall(_script(name))
    stubs = ('systemctl() {\n  case "$1" in\n    is-active) [[ "$ACTIVE" == yes ]] ;;\n'
             '    --version) echo "systemd $SYSTEMD"; echo "+PAM +AUDIT" ;;\n    *) return 9 ;;\n  esac\n}\n'
             f'nft() {{\n  [[ "$*" == "list set {GUARD_TABLE} {GUARD_SET}" && -n "$LISTED" ]] '
             '|| { echo "Error: No such file or directory" >&2; return 1; }\n  printf "%s" "$LISTED"\n}\n')
    env = {"PATH": "/usr/bin:/bin", "ACTIVE": "yes" if active else "no", "LISTED": listed or "", "SYSTEMD": systemd}
    return subprocess.run(["bash", "-c", f"set -euo pipefail\n{stubs}{function}\nsay_if_unguarded\necho carried on"],
                          capture_output=True, text=True, env=env, check=True)


# install.sh's only: update.sh's is the same (test_install_and_update_say_the_same_about_the_guard).
def test_install_and_update_say_when_the_guard_isnt_holding_the_web_server():
    # Holding it: the table in, and the server's cgroup in its set.
    held = _say_if_unguarded("install.sh", True, FILLED)
    assert (held.stdout, held.stderr) == ("carried on\n", "")
    assert _say_if_unguarded("install.sh", False, None).stderr == ""  # no server running, nothing to guard
    for listed in (None, EMPTY):  # no table; or a table, but systemd hasn't put the server in its set
        said = _say_if_unguarded("install.sh", True, listed)
        assert said.stdout == "carried on\n"  # a warning, never a stop: the board and dashboard still work
        assert said.stderr.startswith("Warning:"), said.stderr
        assert "journalctl -u twobells-guard -u twobells-web" in said.stderr
        assert "Trixie" not in said.stderr


@pytest.mark.parametrize("systemd, old", [("252 (252.38-1~deb12u1)", True), ("253", True), ("254", False),
                                           ("257 (257.9-1~deb13u1)", False), ("unknown", False)])
def test_install_and_update_say_when_systemd_is_too_old_to_fill_the_guards_set(systemd, old):
    # NFTSet= came in systemd 254; Bookworm has 252, Trixie 257.
    said = _say_if_unguarded("install.sh", True, EMPTY, systemd).stderr
    assert said.startswith("Warning:"), said
    assert ("254" in said and "Trixie" in said) == old, said
    assert ("journalctl -u twobells-guard -u twobells-web" in said) == (not old), said
    assert _say_if_unguarded("install.sh", True, FILLED, systemd).stderr == ""


def test_install_and_update_say_the_same_about_the_guard():
    [said] = SAY_IF_UNGUARDED.findall(_script("install.sh"))
    assert SAY_IF_UNGUARDED.findall(_script("update.sh")) == [said]


def test_install_and_update_install_nftables():
    install = _script("install.sh")
    [packages] = [line for line in install.splitlines() if line.startswith("apt-get install -y python3-venv")]
    assert "nftables" in packages.split()
    update = _script("update.sh")
    # Looked for where the guard's unit runs it (a package removed but not
    # purged still passes dpkg -s), and the package lists refreshed first: an
    # installed Pi's may be months old, naming versions the mirror no longer has.
    nft = shlex.split(_unit(GUARD_UNIT)["ExecStart"][0])[0]
    # An if block: set -e ignores a failing apt-get update inside an && list.
    needed = f"if [[ ! -x {nft} ]]; then\n  apt-get update\n  apt-get install -y nftables\nfi\n"
    # Before the new code goes in, so if it fails the old code still runs.
    assert update.index(needed) < update.index('"$PREFIX/venv/bin/pip" install')


def test_update_refuses_where_nothing_is_installed_before_changing_anything():
    # Run by mistake on a Pi without the app (upgrade.sh from a fresh download,
    # say), it must stop before it writes /etc/twobells or installs packages.
    update = _script("update.sh")
    needed = 'if [[ ! -x "$PREFIX/venv/bin/twobells" ]]; then\n'
    assert needed in update
    check = update.index(needed)
    for change in ('install -m 644 "$config"', "apt-get update", '"$PREFIX/venv/bin/pip" install'):
        assert check < update.index(change), change
    block = update[check:update.index("fi\n", check)]
    assert "install.sh" in block and "exit 1" in block  # says what to run instead


def test_install_run_again_restarts_a_running_web_server_onto_its_new_unit_and_guard():
    # enable --now leaves a running service on its old unit, without the guard
    # it may now load, and the check after it would blame the guard.
    install = _script("install.sh")
    units_go_in = install.index("for unit in ")
    asked = "systemctl is-active --quiet twobells-web.service && restart_web=true\n"
    assert "restart_web=false\n" + asked in install
    assert install.index(asked) < units_go_in  # as it was before this run
    started = install.index("systemctl enable --now twobells-web.service\n")
    restart = "if $restart_web; then\n  systemctl try-restart twobells-web.service\nfi\n"
    assert started < install.index(restart)
    # The guard loaded afresh first, so systemd puts the restarted server in its set.
    assert install.index(f"systemctl restart {GUARD_UNIT}") < install.index(restart)
    assert install.index(restart) < install.index("\nsay_if_unguarded\n")


def test_install_run_again_restarts_a_running_fetch_service_onto_its_new_unit_and_guard():
    # Loading the guard afresh empties its sets, so a running fetch service
    # restarts after it, for systemd to put it in again, as the web server does.
    install = _script("install.sh")
    asked = "systemctl is-active --quiet twobells-fetch.service && restart_fetch=true\n"
    assert "restart_fetch=false\n" + asked in install
    assert install.index(asked) < install.index("for unit in ")  # as it was before this run
    started = install.index("systemctl enable --now twobells-fetch.path twobells-fetch.service\n")
    restart = "if $restart_fetch; then\n  systemctl try-restart twobells-fetch.service\nfi\n"
    assert started < install.index(restart)
    assert install.index(f"systemctl restart {GUARD_UNIT}") < install.index(restart)


def test_update_restarts_the_fetch_service_after_loading_the_guard():
    update = _script("update.sh")
    assert update.index(f"systemctl restart {GUARD_UNIT}") < update.index(
        "systemctl restart twobells-fetch.path twobells-fetch.service")


def test_install_never_starts_a_service_by_restarting_it_but_the_guard():
    # try-restart leaves a stopped service stopped; restart would start it. The
    # guard is install.sh's own, enabled just before, and should run.
    restarts = [line for line in _run_lines("install.sh") if line.startswith("systemctl restart")]
    assert restarts == [f"systemctl restart {GUARD_UNIT} || true"]


def _import_checks(name: str) -> list[tuple[int, set[str]]]:
    """Where each of the script's import checks is, and the modules it imports.
    Each stops the script, saying why, when an import fails."""
    stop = '  || { echo "A dependency is missing; see the error above" >&2; exit 1; }'
    pattern = r'^"\$PREFIX/venv/bin/python" -c "import ([\w., ]+)" \\\n' + re.escape(stop) + "$"
    return [(found.start(), set(found.group(1).replace(",", " ").split()))
            for found in re.finditer(pattern, _script(name), re.M)]


def test_update_installs_the_pis_pypi_packages_and_checks_they_import_before_the_new_code():
    pinned = '--require-hashes --no-deps -r "$REPO/deploy/pi/requirements-pi.txt"'
    assert f'"$PREFIX/venv/bin/pip" install {pinned}' in _script("install.sh")
    update = _script("update.sh")
    # A new release may pin another inky; without this the Pi kept the one it was installed with.
    pi = update.index(f'"$PREFIX/venv/bin/pip" install --quiet {pinned}')
    code = update.index('--force-reinstall "$build"')
    # The same import check as install.sh: the dependencies before the new code
    # goes in, so if one is missing the old code still runs; the new code after.
    [(_, installed)] = _import_checks("install.sh")
    (deps_at, deps), (new_at, new) = _import_checks("update.sh")
    assert deps | new == installed and new == {"twobells.cli.main"}
    assert pi < deps_at < code < new_at


def test_update_installs_pygame_for_the_screen_before_the_new_code():
    update = _script("update.sh")
    # Looked for where Debian's python3-pygame puts it, not asked of dpkg (a
    # package removed but not purged still passes dpkg -s), with the package
    # lists refreshed first. An if block: set -e ignores a failing apt-get
    # update inside an && list.
    assert not re.search(r"^[^#\n]*dpkg -s", update, re.M)
    needed = ('if [[ "$display" == hdmi-* && ! -f /usr/lib/python3/dist-packages/pygame/__init__.py ]]; then\n'
              "  apt-get update\n  apt-get install -y python3-pygame\nfi\n")
    assert update.index('display="$(query display)" || display=""') < update.index(needed)
    # Before the new code goes in, so if it fails the old code still runs.
    assert update.index(needed) < update.index('"$PREFIX/venv/bin/pip" install')


def test_a_screen_gets_espeak_ng_for_its_announcements_and_e_paper_does_not():
    # install.sh: with pygame, only for a screen.
    install = _script("install.sh")
    [block] = re.findall(r"^if \$screen; then\n(.*?)^fi$", install, re.M | re.S)
    assert block == "  apt-get install -y python3-pygame espeak-ng\n"
    assert len(re.findall(r"^[^#\n]*espeak-ng", install, re.M)) == 1  # nowhere else
    # update.sh: a screen installed before announcements gets it, before the new code goes in.
    update = _script("update.sh")
    needed = ('if [[ "$display" == hdmi-* && ! -x /usr/bin/espeak-ng ]]; then\n'
              "  apt-get update\n  apt-get install -y espeak-ng\nfi\n")
    assert update.index('display="$(query display)" || display=""') < update.index(needed)
    assert update.index(needed) < update.index('"$PREFIX/venv/bin/pip" install')


def test_the_screen_s_service_may_use_the_sound_devices_and_nothing_more():
    screen = _unit("twobells-screen.service")
    # audio for /dev/snd (espeak-ng), beside video and render for the screen; not input.
    assert screen["SupplementaryGroups"] == ["video render audio"]
    # Nothing else is opened up for sound: no device list, capabilities or realtime.
    assert "DeviceAllow" not in screen and screen["CapabilityBoundingSet"] == [""]
    assert screen["RestrictRealtime"] == ["yes"]
    assert screen["RestrictAddressFamilies"] == ["AF_INET AF_INET6 AF_UNIX"]
    # E-paper never speaks, so its service has no sound.
    assert "audio" not in " ".join(_unit("twobells.service").get("SupplementaryGroups", []))


def test_the_pi_serves_its_settings_at_8090_and_nothing_at_8080():
    from twobells.cli.main import _parser
    from twobells.settings.installs import install_named

    web = _unit("twobells-web.service")
    assert web["ExecStart"] == ["/opt/twobells/venv/bin/twobells serve --install pi "
                                "--config /etc/twobells/config.toml --port 8090"]
    args = _parser().parse_args(shlex.split(web["ExecStart"][0])[1:])
    assert (args.command, args.install, args.config, args.port, args.host, args.lan) == (
        "serve", "pi", "/etc/twobells/config.toml", 8090, None, False)
    assert install_named("pi").host == "0.0.0.0"  # every address, kept to the home network above
    # No second listener on the old port: bookmarks to it break, on purpose.
    assert "8080" not in (PI / "twobells-web.service").read_text()


def test_the_settings_service_has_room_for_every_connection_the_server_takes():
    # A thread per connection, and a few of the server's own; past TasksMax a
    # connection can't be answered at all.
    from twobells.web import server as dashserver

    web = _unit("twobells-web.service")
    assert int(web["TasksMax"][0]) >= max(64, dashserver.MAX_CONNECTIONS + 8)
    assert web["MemoryMax"] == ["128M"]


def _timetable_memory(install) -> int:
    """The most a timetable build or setup search holds on `install`, but for
    Python and its imports. Read whole, a document can be held twice while it
    is unpacked; parsed, it is held with up to as much again for what is read
    from it, but its journeys (tests/timetable/test_build.py measures both):
    twice its size. Once the documents are let go, NaPTAN's stops are read,
    up to routesetup.MAX_NAPTAN_BYTES, decoded a little at a time
    (tests/timetable/test_build.py and test_routesetup.py check both).
    Beside either are the journeys found, from it and the documents before:
    those calling at your stops, up to the install's limit, and inbound-only
    ones, up to half that (build.inbound_limit), each taking up to
    build.JOURNEY_BYTES. The timetable is saved a journey at a time, and
    loaded so (before each build, and by the board every minute), holding
    no more than its journeys again (tests/timetable/test_build.py checks
    both), so neither takes what a build does. A rebuild lets go of the
    last timetable's journeys first, keeping only its stops and roads (a
    few MB, in Python's room below). On a Pi the fetch service's dashboard
    holds the timetable it reads, but sets it aside while a setup search
    runs there (tests/fetch/test_service.py and
    tests/dashboard/test_collector.py check both), so it is never held
    beside the search's build: alone, it is no more than loading takes."""
    from twobells.timetable import build, routesetup

    limits = routesetup.document_limits(install)
    journeys = (limits.journeys + build.inbound_limit(limits)) * build.JOURNEY_BYTES
    read = max(2 * max(limits.read, limits.parse), routesetup.MAX_NAPTAN_BYTES)
    building = read + journeys
    saving_or_loading = journeys
    return max(building, saving_or_loading)


def test_the_timetable_unit_has_room_for_the_largest_document_it_reads_and_its_journeys():
    # Room is kept for Python and the rest of the build (40 MB; importing
    # the command line takes about 22 MB on top of the interpreter's own). The
    # fetch service reads documents for the setup search, and builds too, its
    # dashboard's timetable set aside meanwhile.
    # Checked whole on BODS data set 2100 (First South Yorkshire) with a Pi's
    # limits, Python 3.11 under a 256 MB Linux cgroup: the busiest stop that
    # fits, 4,732 journeys and 2,500 held inbound-only, peaked at 221 MB; one
    # with 5,816 failed cleanly at 229 MB. That was with the limit at 5,000,
    # before journeys had slots and shared their values (build.JOURNEY_BYTES).
    # Saving and loading 10,500 journeys of 80 calls (a Pi's limit and its
    # inbound-only ones) peaked at 92 MB on Python 3.12, 39 MB of it Python
    # and the command line's imports; written and read whole, as they were
    # before, 7,000 peaked at 350 MB saving and 307 MB loading.
    # Again in October 2026, with slots and sharing, on data set 2100 grown to
    # all of First Bus (largest file 48 MB): 11,691 journeys at ten busy
    # Sheffield stops and 3,222 inbound-only held peaked at 205 MiB, built,
    # saved and loaded, under a 256 MiB cgroup with no swap.
    from twobells.cli.main import _parser
    from twobells.settings.installs import install_named

    room = 40_000_000
    for name in ("twobells-timetable.service", "twobells-fetch.service"):
        unit = _unit(name)
        assert _parser().parse_args(shlex.split(unit["ExecStart"][0])[1:]).install == "pi", name
        memory = unit["MemoryMax"][0]
        assert memory.endswith("M"), name
        limit = int(memory[:-1]) * 1024 * 1024
        assert _timetable_memory(install_named("pi")) + room <= limit, name


def test_the_container_has_room_for_the_largest_timetable_document_it_reads_and_its_journeys():
    # As the Pi's units above, but the build shares the container with the web
    # server and the dashboard's pollers (serve's upkeep runs it), so more room
    # is kept for them.
    import re
    import xml.etree.ElementTree as ET

    from twobells.settings.installs import install_named

    room = 150_000_000
    extra = ET.parse(ROOT / "deploy" / "unraid" / "twobells.xml").getroot().findtext("ExtraParams")
    limits = {
        "docker-compose.yml": re.search(r"^\s*mem_limit: (\d+)m$", COMPOSE, re.M),
        "docker-compose.ghcr.yml": re.search(r"^\s*mem_limit: (\d+)m$", GHCR_COMPOSE, re.M),
        "twobells.xml": re.search(r"--memory (\d+)m\b", extra or ""),
    }
    for name, found in limits.items():
        assert found, name
        limit = int(found.group(1)) * 1024 * 1024
        assert _timetable_memory(install_named("docker")) + _cached_timetable(install_named("docker")) + room <= limit, name


# What a timetable takes once loaded, a journey at a time (measured with
# tracemalloc at 4.84 to 4.94 KB for an 80-call journey, commits 0832a6a and 183d128).
LOADED_JOURNEY_BYTES = 5_000


def _cached_timetable(install) -> int:
    """The dashboard's own copy of the timetable, held beside a build where
    serve runs both (Docker): its poller keeps it through upkeep's weekly
    rebuild, so the buses don't blank."""
    from twobells.timetable import build, routesetup

    limits = routesetup.document_limits(install)
    return (limits.journeys + build.inbound_limit(limits)) * LOADED_JOURNEY_BYTES


def test_the_fetch_service_goes_out_and_listens_on_nothing():
    from twobells.setup.spool.folder import RUNTIME

    unit = _unit("twobells-fetch.service")
    assert "IPAddressDeny" not in unit and "IPAddressAllow" not in unit
    assert unit["SocketBindDeny"] == ["any"]
    assert unit["ExecStart"] == [
        "/opt/twobells/venv/bin/twobells fetch --config /etc/twobells/config.toml"]
    assert unit["Type"] == ["simple"] and unit["User"] == ["twobells"]
    assert unit["Restart"] == ["on-failure"] and unit["RestartSec"] == ["10"]
    assert unit["StateDirectory"] == ["twobells"]
    assert unit["EnvironmentFile"] == ["/etc/twobells/secrets.env"]
    assert unit["WantedBy"] == ["multi-user.target"]  # at boot, so the dashboard's feeds start with the Pi
    assert int(unit["TasksMax"][0]) >= 32
    timetable = _unit("twobells-timetable.service")
    hardening = ("NoNewPrivileges", "CapabilityBoundingSet", "ProtectSystem", "ProtectHome", "PrivateTmp",
                 "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups",
                 "RestrictAddressFamilies", "RestrictNamespaces", "RestrictSUIDSGID", "LockPersonality",
                 "MemoryDenyWriteExecute", "SystemCallFilter", "MemoryMax", "UMask")
    for setting in hardening:
        assert unit[setting] == timetable[setting], setting
    path = _unit("twobells-fetch.path")
    assert path["DirectoryNotEmpty"] == [f"{RUNTIME}/requests"]
    assert path["Unit"] == ["twobells-fetch.service"]


def test_the_setup_units_are_gone():
    for name in ("departureboard-setup.service", "departureboard-setup.path"):
        assert not (PI / name).exists(), name


def test_the_spool_is_in_memory_and_shared_by_both_units():
    from twobells.setup.spool.folder import RUNTIME

    spool = {"RuntimeDirectory": ["twobells-fetch"], "RuntimeDirectoryMode": ["0700"],
             "RuntimeDirectoryPreserve": ["yes"]}  # kept when either stops
    web, fetch = _unit("twobells-web.service"), _unit("twobells-fetch.service")
    for unit in (web, fetch):
        assert {key: unit.get(key) for key in spool} == spool
    assert web["User"] == fetch["User"] == ["twobells"]  # so both can open the 0700 folder
    assert Path("/run") / spool["RuntimeDirectory"][0] == RUNTIME
    watched = _unit("twobells-fetch.path")["DirectoryNotEmpty"][0]
    assert watched.startswith("/run/twobells-fetch/") and "/var/lib" not in watched


def test_a_fetch_service_that_keeps_stopping_at_once_is_given_up_on_not_looped():
    # With the dashboard off, each run waits a minute for more, so ten starts in two minutes is a loop.
    for name in ("twobells-fetch.service", "twobells-fetch.path"):
        unit = _unit(name)
        assert unit["StartLimitIntervalSec"] == ["2min"] and unit["StartLimitBurst"] == ["10"], name
    guide = (ROOT / "docs" / "troubleshooting.md").read_text()
    assert "systemctl reset-failed twobells-fetch.service twobells-fetch.path" in guide


def test_install_and_update_set_up_the_fetch_service():
    install = (PI / "install.sh").read_text()
    update = (PI / "update.sh").read_text()
    for text in (install, update):
        assert "twobells-fetch.service" in text and "twobells-fetch.path" in text
    assert "systemctl enable --now twobells-fetch.path twobells-fetch.service" in install
    # The fetch units go in, then restart onto the new code, and the web server after them.
    order = [
        "systemctl enable twobells-fetch.path twobells-fetch.service\n",
        "systemctl restart twobells-fetch.path twobells-fetch.service\n",
        "systemctl try-restart twobells-web.service\n",
    ]
    places = [update.index(step) for step in order]
    assert places == sorted(places)
    units = re.search(r"for unit in (.*?); do", update, re.S).group(1)
    assert "twobells-fetch.service" in units


SCRIPTS = ("install.sh", "update.sh")


def _script(name: str) -> str:
    return (PI / name).read_text()


def _run_lines(name: str) -> list[str]:
    """The script's systemctl commands: those it runs, not those it prints."""
    return [line.strip() for line in _script(name).splitlines()
            if line.strip().startswith("systemctl ") and not line.startswith("       ")]


def _query(name: str, what: str, conf: Path) -> list[str]:
    """The twobells command line `query WHAT` runs in the script
    `name`, as it would with its config folder at `conf`."""
    [body] = re.findall(r"^query\(\) \{\n  (.*)\n\}$", _script(name), re.M)
    words = shlex.split(body)
    # Asked as the services' user, not as root.
    assert words[:4] == ["runuser", "-u", "twobells", "--"], words
    assert words[4] == "$PREFIX/venv/bin/twobells"
    return [what if word == "$1" else word.replace("$CONF", str(conf)) for word in words[5:]]


def _settings(tmp_path: Path, config: str = "", saved: str = "") -> None:
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "config.toml").write_text(f'state_file = "{state / "state.json"}"\n{config}')
    if saved:
        (state / "settings.toml").write_text(saved)


@pytest.mark.parametrize("name", SCRIPTS)
def test_install_and_update_ask_the_app_not_config_toml(name):
    script = _script(name)
    for grepped in ("grep -E", "[[:space:]]", "timetable --needed"):
        assert grepped not in script, grepped
    assert '"$(query display)"' in script


@pytest.mark.parametrize("name", SCRIPTS)
def test_install_and_update_always_turn_the_timetable_timer_on(name):
    # Trams or BODS buses set up after installing need it, and nothing else turns it on (the
    # Pi's fetch service never builds a timetable). It builds nothing until a build is due.
    assert "\nsystemctl enable --now twobells-timetable.timer\n" in _script(name)


def test_update_starts_a_timetable_build_only_when_the_settings_want_one():
    build = "if query needs-timetable >/dev/null; then\n  systemctl start --no-block twobells-timetable.service\n"
    assert build in _script("update.sh")


@pytest.mark.parametrize("config, saved, wanted", [
    ('[bus]\nsource = "bods"\n', "", True),
    ("[bus]\nsource = \"transportapi\"\n", '[bus]\nsource = "bods"\n', True),  # chosen on the settings site or in the wizard
    ('[bus]\nsource = "bods"\n', "[bus]\nsource = \"transportapi\"\nenabled = false\n", False),  # buses switched off since
    ("[bus]\nsource = \"transportapi\"\n", "", False),  # TransportAPI
    ('[train]\ncrs = "GLO"\n', "", False),  # no buses at all
    ('[metro]\n[metro.stops]\nT = "9400ZZMAPIC"\n', "", True),  # trams, from TfGM's timetable
    ('[metro]\n[metro.stops]\nT = "9400ZZMAPIC"\n', "[metro]\nenabled = false\n", False),  # switched off
])
def test_update_asks_whether_the_settings_want_a_timetable(tmp_path, config, saved, wanted):
    from twobells.cli.main import main

    _settings(tmp_path, config, saved)
    assert (main(_query("update.sh", "needs-timetable", tmp_path)) == 0) == wanted


def test_install_starts_the_settings_and_the_fetch_service_and_installs_every_unit():
    install = _script("install.sh")
    started = " ".join(line for line in _run_lines("install.sh") if line.startswith("systemctl enable --now "))
    for unit in ("twobells-web.service", "twobells-fetch.path", "twobells-fetch.service",
                 "twobells-timetable.timer"):
        assert unit in started, unit
    units = re.search(r"for unit in (.*?); do", install, re.S).group(1).replace("\\", " ").split()
    shipped = [path.name for path in PI.iterdir() if path.suffix in (".service", ".timer", ".path")]
    assert sorted(units) == sorted(shipped)


@pytest.mark.parametrize("saved, board", [("", True), ('[app]\nviews = ["dashboard"]\n', False)])
def test_install_names_the_boards_unit_only_while_its_view_is_on(tmp_path, capsys, saved, board):
    from twobells.cli.main import main

    _settings(tmp_path, saved=saved)
    assert main(_query("install.sh", "views", tmp_path)) == 0
    assert ("board" in capsys.readouterr().out.splitlines()) == board
    install = _script("install.sh")
    assert '\nif grep -qx board <<<"$views"; then\n' in install


@pytest.mark.parametrize("display, renderer", [
    ("", "twobells.timer"),  # an Inky, unless config.toml says
    ('display = "waveshare-10in85g"\n', "twobells.timer"),
    ('display = "hdmi-1280x400"\n', "twobells-screen.service"),
])
def test_install_names_the_boards_unit_for_the_display_and_never_starts_it(tmp_path, capsys, display, renderer):
    import subprocess

    from twobells.cli.main import main

    _settings(tmp_path, display)
    assert main(_query("install.sh", "display", tmp_path)) == 0
    said = capsys.readouterr().out.strip()
    [choice] = re.findall(r'^if \[\[ "\$display" == hdmi-\* \]\]; then\n  renderer=.*?\nfi$',
                          _script("install.sh"), re.M | re.S)
    shown = subprocess.run(["bash", "-c", f'display="$1"\n{choice}\necho "$renderer"', "bash", said],
                           capture_output=True, text=True, check=True).stdout
    assert shown == f"{renderer}\n"
    for line in _run_lines("install.sh"):  # only printed, for the owner to run
        assert "$renderer" not in line and "twobells.timer" not in line and "screen" not in line, line


def test_update_never_enables_or_disables_the_board():
    for line in _run_lines("update.sh"):
        if line.startswith(("systemctl enable", "systemctl disable")):
            assert "twobells.timer" not in line and "twobells-screen" not in line, line


def test_update_holds_the_boards_timer_while_the_code_is_reinstalled():
    # pip takes the twobells command away for a moment while it puts the
    # new code in; a tick the timer starts then fails. The timer is held from
    # before the code goes in until the units are in, and only one that was
    # running is started again, even when the update stops part way.
    script = _script("update.sh")
    held = script.index("systemctl stop twobells.timer twobells.service")
    assert script.index("is-active --quiet twobells.timer") < held
    assert held < script.index('--force-reinstall "$build"') < script.index("systemctl daemon-reload")
    [trap] = [line for line in script.splitlines() if line.startswith("trap ")]
    assert "systemctl start twobells.timer" in trap and "timer_was_running" in trap


def test_update_restarts_a_running_screen_onto_the_new_code():
    # The screen's service runs for good; without a restart it keeps the old
    # code. try-restart leaves a stopped one stopped, so the board stays yours.
    assert "systemctl try-restart twobells-screen.service" in _run_lines("update.sh")


SERVICES = ("twobells.service", "twobells-screen.service", "twobells-web.service",
            "twobells-timetable.service", "twobells-fetch.service", GUARD_UNIT)


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_is_locked_down_as_far_as_its_work_allows(name):
    unit = _unit(name)
    for setting, value in (("ProtectHostname", "yes"), ("ProtectKernelLogs", "yes"), ("RestrictRealtime", "yes"),
                           ("PrivateIPC", "yes"), ("SystemCallErrorNumber", "EPERM")):
        assert unit.get(setting) == [value], setting
    # Not ~@resources as well: the bus company search lowers its own priority (setpriority).
    assert unit["SystemCallFilter"] == ["@system-service", "~@privileged"]
    devices = unit.get("PrivateDevices") == ["yes"]
    # ProtectClock allows the clock's devices only, so not where the panel's or screen's are needed.
    assert unit.get("ProtectClock") == (["yes"] if devices else None)
    # The e-paper drivers learn which Pi this is from /proc/cpuinfo and /proc/device-tree.
    assert unit.get("ProcSubset") == (None if name == "twobells.service" else ["pid"])


def test_install_and_update_give_the_settings_new_address():
    from twobells.settings.installs import install_named

    install = (PI / "install.sh").read_text()
    update = (PI / "update.sh").read_text()
    assert "http://$(hostname).local:8090/settings" in install
    assert ":8090/settings" in update
    for text in (install, update):
        assert ":8080" not in text and "twobells web " not in text
    # The command the settings site gives for the password, as install.sh gives it.
    assert install_named("pi").password_command.replace("/opt/twobells", "$PREFIX") in install


# Installing from a release: the Pi's tarball, upgrade.sh, the published image and the desktop files.

RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text()
IMAGE = re.search(r"^  IMAGE: (\S+)$", RELEASE, re.M).group(1)
REPO = IMAGE.removeprefix("ghcr.io/")
PI_GUIDE = (ROOT / "docs" / "install-pi.md").read_text()
DESKTOP_GUIDE = (ROOT / "docs" / "desktop.md").read_text()
DOCKER_DOCS = (ROOT / "docs" / "docker.md").read_text()
GHCR_COMPOSE = (ROOT / "deploy" / "docker" / "docker-compose.ghcr.yml").read_text()


def _identity(tag: str) -> str:
    """The signing identity release.yml's notes give for a tag."""
    template = re.search(r'identity="(\S+)"', RELEASE).group(1)
    return template.replace("$GITHUB_REPOSITORY", REPO).replace("$TAG", tag)


def _tarball_excludes() -> list[str]:
    archive = RELEASE[RELEASE.index("git archive "):RELEASE.index('listing="$(tar -tzf')]
    return re.findall(r"':\(exclude\)([^']+)'", archive)


def _in_tarball(path: str) -> bool:
    from fnmatch import fnmatch

    return not any(path == ex or path.startswith(f"{ex}/") or fnmatch(path, ex) for ex in _tarball_excludes())


def _needed_on_the_pi() -> set[str]:
    """Every file install.sh, update.sh and upgrade.sh use from the release."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    # pip builds the package from these.
    needed = {"pyproject.toml", *project["project"]["license-files"]}
    needed |= {str(path.relative_to(ROOT)) for path in (ROOT / "src" / "twobells").rglob("*")
               if path.is_file() and "__pycache__" not in path.parts}
    for name in ("install.sh", "update.sh"):
        script = _script(name)
        needed |= {path for path in re.findall(r"\$REPO/([\w./-]+)", script) if not path.endswith(("/", "."))}
        units = re.search(r"for unit in (.*?); do", script, re.S).group(1).replace("\\", " ").split()
        needed |= {f"deploy/pi/{unit}" for unit in units}
    needed |= {"deploy/pi/install.sh", "deploy/pi/update.sh", "deploy/pi/upgrade.sh"}  # upgrade.sh runs the release's update.sh
    return needed


def test_the_release_holds_the_pi_tarball_under_the_name_upgrade_fetches_not_the_sdist():
    # The sdist and the Pi's tarball share a name, twobells-X.Y.Z.tar.gz. The
    # release keeps the tarball, the one upgrade.sh fetches and unpacks, and
    # drops the sdist (it is on PyPI) before the archive takes its place.
    job = RELEASE[RELEASE.index("  tarball:"):RELEASE.index("  pypi:")]
    dropped = job.index('rm "out/twobells-$VERSION.tar.gz"  # the sdist, from dist')
    assert job.index("name: dist") < dropped < job.index('-o "out/twobells-$VERSION.tar.gz"')
    assert 'local name="twobells-$version.tar.gz"' in _script("upgrade.sh")


def test_the_release_tarball_holds_everything_the_pi_scripts_use():
    excludes = _tarball_excludes()
    assert {"tests", ".github", ".beads", ".claude"} <= set(excludes)  # read from release.yml, not empty
    needed = _needed_on_the_pi()
    assert {"examples/config.example.toml", "deploy/secrets.env.example", "deploy/pi/requirements-pi.txt",
            "examples/glossop.toml", "deploy/pi/twobells-fetch.path", "LICENSE"} <= needed
    for path in sorted(needed):
        assert (ROOT / path).is_file(), path
        assert _in_tarball(path), path
    # git archive also leaves out whatever .gitattributes marks export-ignore.
    attributes = ROOT / ".gitattributes"
    assert not attributes.exists() or "export-ignore" not in attributes.read_text()
    # And the scripts never need git: a tarball has no .git.
    for name in ("install.sh", "update.sh", "upgrade.sh"):
        assert not re.search(r"(^|[\s;&|(])git\s", _script(name), re.M), name
    # The release checks the scripts are in the tarball it built.
    assert "for script in install.sh update.sh upgrade.sh; do" in RELEASE


def _upgrade(*args: str):
    import subprocess

    return subprocess.run(["bash", str(PI / "upgrade.sh"), *args], capture_output=True, text=True)


def _sourced(call: str, *args: str):
    """Runs `call` in bash with upgrade.sh sourced (so its main doesn't run)."""
    import subprocess

    return subprocess.run(["bash", "-c", f'source "$1"; shift; {call}', "bash", str(PI / "upgrade.sh"),
                           *args], capture_output=True, text=True)


@pytest.mark.parametrize("version, valid", [
    ("0.1.0", True), ("10.20.300", True),
    ("", False), ("0.1", False), ("v0.1.0", False), ("0.1.0-rc1", False), ("0.1.0 ", False), ("0.1.0\n", False),
    ("0.1.0;id", False), ("../0.1.0", False), ("0.1.0/../../x", False), ("0..1", False), ("a.b.c", False),
])
def test_upgrade_takes_only_a_plain_release_version(version, valid):
    assert (_sourced('valid_version "$1"', version).returncode == 0) == valid


@pytest.mark.parametrize("a, b, older", [
    ("0.1.0", "0.2.0", True), ("0.2.0", "0.10.0", True), ("0.9.9", "1.0.0", True), ("1.2.3", "1.2.4", True),
    ("0.2.0", "0.2.0", False), ("0.2.0", "0.1.0", False), ("0.10.0", "0.9.0", False), ("0.08.0", "0.7.0", False),
])
def test_upgrade_tells_an_older_version_by_its_numbers(a, b, older):
    assert (_sourced('older "$1" "$2"', a, b).returncode == 0) == older


def test_upgrade_reads_the_installed_version_from_its_package_metadata(tmp_path):
    site = tmp_path / "venv/lib/python3.11/site-packages"
    (site / "twobells-0.2.0.dist-info").mkdir(parents=True)
    (site / "inky-2.1.0.dist-info").mkdir()
    assert _sourced('installed_version "$1"', str(tmp_path)).stdout == "0.2.0\n"


@pytest.mark.parametrize("folders", [[], ["twobells-0.2.0.dev3+g1234.dist-info"], ["twobells-0.2.0;id.dist-info"]])
def test_upgrade_reads_no_version_without_a_release_installed(tmp_path, folders):
    site = tmp_path / "venv/lib/python3.11/site-packages"
    site.mkdir(parents=True)
    for name in folders:
        (site / name).mkdir()
    done = _sourced('installed_version "$1"', str(tmp_path))
    assert (done.returncode, done.stdout) == (0, "")


def test_upgrade_going_back_says_how_to_put_the_history_back():
    # An older release refuses a history a newer one moved on (dashboard/history.py), so its
    # dashboard has none until the copy kept from before is put back, as the guide says too.
    script = _script("upgrade.sh")
    assert script.index('installed="$(installed_version "$PREFIX")"') < script.index('bash "$unpacked/')
    assert script.index('bash "$unpacked/') < script.index('going_back "$installed" "$version"')
    told = _sourced('going_back 0.2.0 0.1.0').stdout
    assert "Went back from 0.2.0 to 0.1.0." in told
    steps = [line.strip() for line in told.splitlines() if line.startswith("  sudo ")]
    assert steps == [
        "sudo systemctl stop twobells-web twobells-fetch",
        f"sudo -u twobells cp /var/lib/twobells/{FILE_NAME}.v1 /var/lib/twobells/{FILE_NAME}",
        f"sudo -u twobells rm -f /var/lib/twobells/{FILE_NAME}-wal /var/lib/twobells/{FILE_NAME}-shm",
        "sudo systemctl start twobells-fetch twobells-web",
    ]
    assert "\n".join(steps) in PI_GUIDE
    assert "PREFIX=/opt/twobells\n" in script and "PREFIX=/opt/twobells\n" in _script("update.sh")


def test_the_docker_guide_says_how_to_put_the_history_back():
    assert f"cp /data/{FILE_NAME}.v1 /data/{FILE_NAME}" in DOCKER_DOCS
    assert f"rm -f /data/{FILE_NAME}-wal /data/{FILE_NAME}-shm" in DOCKER_DOCS


@pytest.mark.parametrize("args", [[], ["0.2"], ["v0.2.0"], ["0.2.0; reboot"], ["--config", "x"]])
def test_upgrade_refuses_a_bad_version_before_doing_anything(args):
    done = _upgrade(*args)
    assert done.returncode == 2
    assert "Usage: sudo" in done.stderr and "like 0.2.0" in done.stderr


def test_upgrade_needs_root_after_the_version():
    import os

    if os.geteuid() == 0:
        pytest.skip("run as root, it would go on to download")
    done = _upgrade("0.2.0")
    assert (done.returncode, done.stderr) == (1, "Run with sudo\n")


def _release_folder(tmp_path: Path, sums: str | None = None) -> Path:
    import hashlib

    folder = tmp_path / "download"
    folder.mkdir()
    name = "twobells-0.2.0.tar.gz"
    (folder / name).write_bytes(b"the release")
    digest = hashlib.sha256(b"the release").hexdigest()
    wheel = hashlib.sha256(b"a wheel").hexdigest()
    lines = f"{wheel}  twobells-0.2.0-py3-none-any.whl\n{digest}  {name}\n"  # as release.yml writes them
    (folder / "SHA256SUMS").write_text(lines if sums is None else sums.format(digest=digest, name=name))
    return folder


def _check(folder: Path):
    import shutil

    if shutil.which("sha256sum") is None:
        pytest.skip("no sha256sum here")
    return _sourced('check_tarball "$1" 0.2.0', str(folder))


def test_upgrade_accepts_a_tarball_that_matches_its_sum(tmp_path):
    done = _check(_release_folder(tmp_path))
    assert (done.returncode, done.stdout) == (0, "twobells-0.2.0.tar.gz: OK\n")


def test_upgrade_refuses_a_tarball_that_doesnt_match(tmp_path):
    folder = _release_folder(tmp_path)
    (folder / "twobells-0.2.0.tar.gz").write_bytes(b"the release, changed")
    done = _check(folder)
    assert done.returncode == 1 and "does not match SHA256SUMS" in done.stderr


@pytest.mark.parametrize("sums", [
    "",  # nothing for it
    "{digest}  twobells-0.1.0.tar.gz\n",  # another release's
    "{digest}  {name}.old\n",
    "{digest}  {name}\n{digest}  {name}\n",  # twice: not one sum
    "{digest}0  {name}\n",
    "{name}  {digest}\n",
])
def test_upgrade_refuses_without_exactly_one_sum_for_the_tarball(tmp_path, sums):
    done = _check(_release_folder(tmp_path, sums=sums))
    assert done.returncode == 1 and "no single sum" in done.stderr


def test_upgrade_downloads_the_releases_files_over_https_only_and_cleans_up():
    script = _script("upgrade.sh")
    assert f"RELEASES=https://github.com/{REPO}/releases/download\n" in script
    assert 'local base="$RELEASES/v$version"' in script
    assert "--proto '=https' --proto-redir '=https'" in script
    for wanted in ('fetch "$base/$name" "$work/$name"', 'fetch "$base/SHA256SUMS" "$work/SHA256SUMS"'):
        assert wanted in script
    assert "| sh" not in script and "| bash" not in script and "eval " not in script
    assert "trap 'rm -rf \"$work\"' EXIT" in script
    # The tarball is checked before it is unpacked, and update.sh comes from the release it holds.
    assert script.index('check_tarball "$work" "$version"') < script.index("tar -xzf")
    assert 'bash "$unpacked/deploy/pi/update.sh" "$@"' in script
    # The release's own names for the tarball, its folder and the signature, and its signing identity.
    assert '-o "out/twobells-$VERSION.tar.gz"' in RELEASE and '--prefix="twobells-$VERSION/"' in RELEASE
    assert "--bundle SHA256SUMS.sigstore.json SHA256SUMS" in RELEASE
    identity = re.search(r"^IDENTITY=(\S+)$", script, re.M).group(1)
    assert f"{identity}@refs/tags/v$version" == _identity("v$version")
    assert '--certificate-identity "$IDENTITY@refs/tags/v$version"' in script


def test_the_guide_installs_a_pi_from_a_checked_release():
    import tomllib

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    # The guide's example is the release this checkout is.
    block = re.search(rf"```bash\nVERSION={re.escape(version)}\n(.*?)```", PI_GUIDE, re.S).group(1)
    assert f'base="https://github.com/{REPO}/releases/download/v$VERSION"' in block
    for name in ("twobells-$VERSION.tar.gz", "SHA256SUMS", "SHA256SUMS.sigstore.json"):
        assert f'curl -fLO "$base/{name}"' in block
    # Nothing is unpacked or run unless the sum is right, even pasted as a block.
    last = block[block.rindex("sha256sum "):]
    steps = [step.strip() for step in last.split("&&")]
    assert steps == ["sha256sum --check --ignore-missing SHA256SUMS", 'tar -xzf "twobells-$VERSION.tar.gz"',
                     'cd "twobells-$VERSION"', "sudo ./deploy/pi/install.sh"]
    assert f'--certificate-identity "{_identity("v$VERSION")}"' in PI_GUIDE
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in PI_GUIDE
    assert "sudo ./deploy/pi/upgrade.sh 0.2.0" in PI_GUIDE
    assert "| sudo bash" not in PI_GUIDE


# The published image: the same lock-down as docker-compose.yml, wherever it runs.


def _live(text: str) -> list[str]:
    return [line.split("#")[0].rstrip() for line in text.splitlines() if line.split("#")[0].strip()]


def test_the_published_image_compose_file_is_pinned_and_locked_down_as_the_built_one():
    built, published = _live(COMPOSE), _live(GHCR_COMPOSE)
    hardening = built[built.index("    read_only: true"):built.index("volumes:")]
    assert len(hardening) > 10
    for line in hardening:
        assert line in published, line
    for line in ("    container_name: twobells", "    restart: unless-stopped", '      - "0.0.0.0:8090:8090"',
                 "      - data:/data", "volumes:", "  data:", "    name: twobells-data"):
        assert line in published, line
    # Pinned by version and digest: the placeholder fails until both are filled in.
    assert f"    image: {IMAGE}:X.Y.Z@sha256:DIGEST" in published
    assert not any("build:" in line or "env_file" in line for line in published)
    for risky in ("privileged", "network_mode", "docker.sock", "cap_add"):
        assert risky not in GHCR_COMPOSE, risky
    assert f"--certificate-identity {_identity('vX.Y.Z')}" in GHCR_COMPOSE


def _docker_run_flags(words: list[str]) -> dict[str, str]:
    """docker run's flags as {flag: value}, the last value of each."""
    flags, i = {}, 0
    while i < len(words):
        if words[i] == "--read-only":
            flags[words[i]], i = "", i + 1
        else:
            flags[words[i]], i = words[i + 1], i + 2
    return flags


HARDENING_FLAGS = {"--read-only": "", "--tmpfs": "/tmp:size=64m", "--cap-drop": "ALL",
                   "--security-opt": "no-new-privileges:true", "--memory": "512m", "--pids-limit": "128",
                   "--log-opt": "max-file=3"}


def test_unraids_template_runs_the_published_image_locked_down():
    import xml.etree.ElementTree as ET

    root = ET.parse(ROOT / "deploy" / "unraid" / "twobells.xml").getroot()
    assert (root.tag, root.get("version")) == ("Container", "2")
    assert root.findtext("Name") == "twobells"
    assert root.findtext("Repository") == f"{IMAGE}:X.Y.Z"
    assert (root.findtext("Network"), root.findtext("Privileged")) == ("bridge", "false")
    extra = shlex.split(root.findtext("ExtraParams"))
    assert extra.count("--log-opt") == 2 and "max-size=1m" in extra
    assert _docker_run_flags(extra) == HARDENING_FLAGS
    # Compose's numbers, as docker run flags.
    assert "mem_limit: 512m" in COMPOSE and "pids_limit: 128" in COMPOSE and "- /tmp:size=64m" in COMPOSE
    configs = {config.get("Target"): config for config in root.findall("Config")}
    assert set(configs) == {"8090", "/data"}
    assert (configs["8090"].get("Type"), configs["8090"].get("Mode")) == ("Port", "tcp")
    data = configs["/data"]
    assert (data.get("Type"), data.get("Mode"), data.text) == ("Path", "rw", "/mnt/user/appdata/twobells")
    # A folder, not a volume: the guide gives it to the image's user first.
    assert "chown 10001:10001 /mnt/user/appdata/twobells" in DOCKER_DOCS
    assert 's/X\\.Y\\.Z/$VERSION/' in DOCKER_DOCS and "templates-user/my-twobells.xml" in DOCKER_DOCS


def test_the_documented_docker_run_is_locked_down_as_compose():
    command = re.search(r"```bash\n(docker run .*?)\n```", DOCKER_DOCS, re.S).group(1)
    words = shlex.split(command.replace("\\\n", " "))
    assert words[:5] == ["docker", "run", "-d", "--name", "twobells"]
    assert words[-1] == f"{IMAGE}:$VERSION@$DIGEST"
    rest = _docker_run_flags(words[5:-1])
    assert {key: rest.pop(key) for key in ("--restart", "-p", "-v")} == {
        "--restart": "unless-stopped", "-p": "0.0.0.0:8090:8090", "-v": "twobells-data:/data"}  # the compose file's volume
    assert rest == HARDENING_FLAGS
    assert f'--certificate-identity "{_identity("v$VERSION")}"' in DOCKER_DOCS
    assert f'cosign verify "{IMAGE}@$DIGEST"' in DOCKER_DOCS


def _compose_ports(text: str) -> list[str]:
    """The entries under the compose file's `ports:`."""
    lines = _live(text)
    ports = []
    for line in lines[lines.index("    ports:") + 1:]:
        if not line.startswith("      - "):
            break
        ports.append(line.removeprefix("      - ").strip('"'))
    return ports


def test_docker_publishes_its_port_on_ipv4_only():
    import ipaddress
    import xml.etree.ElementTree as ET

    # docker-proxy hands an IPv6 visitor's connection on from the bridge's
    # gateway, a private IPv4 address, so the app would take anyone reaching
    # it over IPv6 for the home network. Published on an IPv4 address only,
    # the app sees each visitor's own address.
    root = ET.parse(ROOT / "deploy" / "unraid" / "twobells.xml").getroot()
    published = {
        "docker-compose.yml": _compose_ports(COMPOSE),
        "docker-compose.ghcr.yml": _compose_ports(GHCR_COMPOSE),
        # Unraid passes a Port setting to docker run as -p VALUE:TARGET/MODE.
        "twobells.xml": [f"{value}:{config.get('Target')}" for config in root.findall("Config")
                               if config.get("Type") == "Port" for value in (config.text, config.get("Default"))],
        "docker.md": re.findall(r"(?:-p |\"|`)([^\s\"`]*:\d+:\d+)", DOCKER_DOCS),
    }
    for where, ports in published.items():
        assert ports, where
        for port in ports:
            assert port.count(":") == 2, (where, port)  # an address, then the ports
            host, outside, inside = port.split(":")
            assert isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address), (where, port)
            assert (outside, inside) == ("8090", "8090"), (where, port)
    assert "-p 0.0.0.0:8090:8090" in DOCKER_DOCS  # the docker run command
    for text in (COMPOSE, GHCR_COMPOSE, DOCKER_DOCS):
        assert "IPv6" in text  # and why


# The desktop: started at login, on this computer only.


def _desktop_serve(argv: list[str]) -> None:
    from twobells.cli.main import _parser
    from twobells.settings.installs import install_named

    args = _parser().parse_args(argv)
    assert (args.command, args.install, args.lan, args.host, args.port, args.no_browser) == (
        "serve", "desktop", False, None, 8090, True)
    assert install_named("desktop").host == "127.0.0.1"  # no --host or --lan: this computer only


def test_the_mac_login_agent_runs_serve_on_this_computer_only():
    import plistlib

    path = ROOT / "deploy" / "desktop" / "uk.twobells.serve.plist"
    agent = plistlib.loads(path.read_bytes())
    assert agent["Label"] == path.stem == "uk.twobells.serve"
    program = agent["ProgramArguments"]
    assert program[0] == "/Users/YOU/.local/bin/twobells"
    _desktop_serve(program[1:])
    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] == {"SuccessfulExit": False}  # again if it fails, not when stopped
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert agent[key] == "/Users/YOU/Library/Logs/twobells.log"
    # The guide's sed puts the home folder in every path.
    assert set(re.findall(r"/Users/\w+", path.read_text())) == {"/Users/YOU"}
    assert 'sed "s|/Users/YOU|$HOME|g" deploy/desktop/uk.twobells.serve.plist' in DESKTOP_GUIDE
    assert 'launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/uk.twobells.serve.plist' in DESKTOP_GUIDE
    assert 'launchctl bootout "gui/$(id -u)/uk.twobells.serve"' in DESKTOP_GUIDE


def test_the_linux_login_service_runs_serve_on_this_computer_only():
    unit = _unit("twobells.service", ROOT / "deploy" / "desktop")
    [start] = unit["ExecStart"]
    words = shlex.split(start)
    assert words[0] == "%h/.local/bin/twobells"
    _desktop_serve(words[1:])
    assert unit["Restart"] == ["on-failure"] and unit["WantedBy"] == ["default.target"]
    assert "User" not in unit and "Group" not in unit  # a user service runs as that user
    for setting in ("NoNewPrivileges", "LockPersonality", "RestrictRealtime", "RestrictSUIDSGID"):
        assert unit[setting] == ["yes"], setting
    assert unit["UMask"] == ["0077"]
    for command in ("cp deploy/desktop/twobells.service ~/.config/systemd/user/",
                    "systemctl --user enable --now twobells.service",
                    "journalctl --user -u twobells -f"):
        assert command in DESKTOP_GUIDE, command


def test_the_desktop_needs_pillow_only():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    names = {re.split(r"[<>=!~ ;\[]", spec, maxsplit=1)[0].lower() for spec in project["dependencies"]}
    assert names == {"pillow"}
    assert any(spec.startswith("inky") for spec in project["optional-dependencies"]["pi"])  # the Pi's, as an extra
    assert "uv tool install twobells" in DESKTOP_GUIDE and "pipx install twobells" in DESKTOP_GUIDE
