
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
    lines = [line.strip() for line in _function("releases.sh", "build_release").splitlines()
             if line.strip().startswith('"$venv/bin/pip" install')]
    tools = next(i for i, line in enumerate(lines) if "requirements-build.txt" in line)
    assert "--require-hashes" in lines[tools] and "--no-deps" in lines[tools]
    code = [i for i, line in enumerate(lines) if '"$build"' in line]
    assert code and all(i > tools and "--no-build-isolation" in lines[i] for i in code)
    # install.sh and update.sh build each release with it, and run no pip of their own.
    for name in ("install.sh", "update.sh"):
        assert 'build_release "$release"\n' in _script(name) and "/bin/pip" not in _script(name), name


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


def test_python_in_the_container_never_imports_from_the_data_volume():
    # The entrypoint, the health check and docker exec run `python -m` or `-c` in
    # WORKDIR /data, which the app's user can write: without PYTHONSAFEPATH a
    # urllib.py or twobells/ there would run in place of the real one.
    env = DOCKERFILE[DOCKERFILE.index("\nENV "):DOCKERFILE.index("\n\n", DOCKERFILE.index("\nENV "))]
    assert re.search(r"^\s+PYTHONSAFEPATH=1 \\$", env, re.M)
    assert DOCKERFILE.index("PYTHONSAFEPATH=1") < DOCKERFILE.index("ENTRYPOINT")
    assert _entrypoint()[:2] == ["python", "-m"] and 'CMD ["python", "-c",' in DOCKERFILE


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
    # No name lookups first, to any address: the router's DNS, or a resolver
    # on the Pi itself (127.0.0.0/8 is home), would carry them out to the
    # internet. Plain DNS and DNS over TLS or QUIC, TCP and UDP. Not mDNS
    # (5353): its multicast addresses aren't home, so the last line refuses it.
    assert web_out == ["meta l4proto { tcp, udp } th dport { 53, 853 } reject with icmpx admin-prohibited",
                       "ip daddr @home4 accept", "ip6 daddr @home6 accept", "reject with icmpx admin-prohibited"]


def test_the_guide_says_what_the_guard_s_dns_rule_leaves_open():
    # So the docs don't claim more than the rule does.
    guide = (ROOT / "docs" / "security.md").read_text()
    for said in ("port 53", "853", "AF_UNIX", "systemd-resolved", "any other port", "systemd 252"):
        assert said in guide, said


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
    assert update.index(needed) < update.index(NEW_RELEASE)


def test_update_refuses_where_nothing_is_installed_before_changing_anything():
    # Run by mistake on a Pi without the app (upgrade.sh from a fresh download,
    # say), it must stop before it writes /etc/twobells or installs packages.
    update = _script("update.sh")
    needed = 'if [[ ! -x "$PREFIX/venv/bin/twobells" ]]; then\n'
    assert needed in update
    check = update.index(needed)
    for change in ('install -m 644 "$config"', "apt-get update", NEW_RELEASE):
        assert check < update.index(change), change
    block = update[check:update.index("fi\n", check)]
    assert "install.sh" in block and "exit 1" in block  # says what to run instead
    # After putting back the venv link a power cut part way through an update left out.
    assert update.index('source "$REPO/deploy/pi/releases.sh"\ntake_lock\nrecover\n') < check


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


def _import_checks(text: str) -> list[tuple[int, set[str]]]:
    """Where each import check in `text` is, and the modules it imports. Each
    is in isolated mode (-I), and stops, saying why, when an import fails."""
    stop = '    || { echo "A dependency is missing; see the error above" >&2; return 1; }'
    pattern = r'^  "\$venv/bin/python" -I -c "import ([\w., ]+)" \\\n' + re.escape(stop) + "$"
    return [(found.start(), set(found.group(1).replace(",", " ").split()))
            for found in re.finditer(pattern, text, re.M)]


def test_each_release_gets_the_pis_pypi_packages_and_is_checked_to_import_before_it_is_used():
    build = _function("releases.sh", "build_release")
    assert build.startswith('  local venv="$PREFIX/releases/$1/venv"\n')
    # Made as install.sh always made the venv: Debian's python3, seeing Debian's packages.
    assert build.index('python3 -I -m venv --system-site-packages "$venv"\n') == build.index("\n") + 3
    pinned = '--require-hashes --no-deps -r "$REPO/deploy/pi/requirements-pi.txt"'
    # A new release may pin another inky, so each release gets the one it names.
    pi = build.index(f'"$venv/bin/pip" install --quiet {pinned}')
    code = build.index('--no-build-isolation "$build"')
    # The code, the Pi's PyPI packages and Debian's they need, all in the new venv.
    [(checked_at, checked)] = _import_checks(build)
    assert checked == {"twobells.cli.main", "inky", "PIL", "numpy", "smbus2", "spidev", "gpiod"}
    assert pi < code < checked_at
    # Built, and checked, before anything points to it.
    for name in ("install.sh", "update.sh"):
        script = _script(name)
        assert script.index(NEW_RELEASE) < script.index('switch_to "$release"\n'), name


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
    assert update.index(needed) < update.index(NEW_RELEASE)


def test_a_screen_gets_espeak_ng_for_its_announcements_and_e_paper_does_not():
    # install.sh: with pygame, only for a screen.
    install = _script("install.sh")
    [block] = re.findall(r"^if \$screen; then\n(.*?)^fi$", install, re.M | re.S)
    assert block == "  apt-get install -y python3-pygame espeak-ng libegl1 libegl-mesa0 libgles2\n"
    assert len(re.findall(r"^[^#\n]*espeak-ng", install, re.M)) == 1  # nowhere else
    # update.sh: a screen installed before announcements gets it, before the new code goes in.
    update = _script("update.sh")
    needed = ('if [[ "$display" == hdmi-* && ! -x /usr/bin/espeak-ng ]]; then\n'
              "  apt-get update\n  apt-get install -y espeak-ng\nfi\n")
    assert update.index('display="$(query display)" || display=""') < update.index(needed)
    assert update.index(needed) < update.index(NEW_RELEASE)


def test_a_screen_gets_egl_and_gles_and_e_paper_does_not():
    # SDL opens the screen through EGL and GLES, which python3-pygame doesn't pull in:
    # without them set_mode fails with "EGL not initialized" on Pi OS Lite (Trixie).
    packages = "libegl1 libegl-mesa0 libgles2"
    install = _script("install.sh")
    [block] = re.findall(r"^if \$screen; then\n(.*?)^fi$", install, re.M | re.S)
    assert block.rstrip().endswith(packages)
    assert len(re.findall(rf"^[^#\n]*{packages}", install, re.M)) == 1  # only for a screen
    # update.sh: a screen installed before this gets them, before the new code goes in,
    # looked for in the linker's cache rather than asked of dpkg.
    update = _script("update.sh")
    needed = ('libraries="$(ldconfig -p)"\n'
              'if [[ "$display" == hdmi-* && ( "$libraries" != *libEGL.so.1* || "$libraries" != *libEGL_mesa.so.0* \\\n'
              '    || "$libraries" != *libGLESv2.so.2* ) ]]; then\n'
              f"  apt-get update\n  apt-get install -y {packages}\nfi\n")
    assert update.index('display="$(query display)" || display=""') < update.index(needed)
    assert update.index(needed) < update.index(NEW_RELEASE)


def test_the_screen_draws_through_gles():
    # pygame's window surface reaches the screen through an SDL renderer. Left to
    # choose, SDL 2.32 on a Pi 4 (Trixie) draws a black screen; with GLES it shows.
    screen = _unit("twobells-screen.service")
    assert {"SDL_VIDEODRIVER=kmsdrm", "SDL_FRAMEBUFFER_ACCELERATION=opengles2"} <= set(screen["Environment"])


def test_the_screen_keeps_mesa_s_shader_cache_in_a_folder_of_its_own():
    # Mesa looked for one under /home/twobells, which there isn't, and said so at every start.
    screen = _unit("twobells-screen.service")
    assert screen["CacheDirectory"] == ["twobells-screen"] and screen["CacheDirectoryMode"] == ["0700"]
    assert screen["Environment"] == ["SDL_VIDEODRIVER=kmsdrm", "SDL_FRAMEBUFFER_ACCELERATION=opengles2",
                                     "MESA_SHADER_CACHE_DIR=%C/twobells-screen"]
    # systemd makes the folder, the unit's user's, and writable however strict the rest of the system is.
    assert screen["ProtectSystem"] == ["strict"] and screen["User"] == ["twobells"]
    assert "ReadWritePaths" not in screen


def test_the_screen_s_service_may_use_the_sound_devices_and_nothing_more():
    screen = _unit("twobells-screen.service")
    # audio for /dev/snd (espeak-ng), beside video and render for the screen; not input.
    assert screen["SupplementaryGroups"] == ["video render audio"]
    # Nothing else is opened up for sound: no device list, capabilities or realtime.
    assert "DeviceAllow" not in screen and screen["CapabilityBoundingSet"] == [""]
    assert screen["RestrictRealtime"] == ["yes"]
    # AF_NETLINK for SDL's udev: SDL 2.32 crashes in display.init() without it.
    assert screen["RestrictAddressFamilies"] == ["AF_INET AF_INET6 AF_UNIX AF_NETLINK"]
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
    ones, up to half that (txc.inbound_limit), each taking up to
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
    from twobells.timetable import build, routesetup, txc

    limits = routesetup.document_limits(install)
    journeys = (limits.journeys + txc.inbound_limit(limits)) * build.JOURNEY_BYTES
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
    from twobells.timetable import routesetup, txc

    limits = routesetup.document_limits(install)
    return (limits.journeys + txc.inbound_limit(limits)) * LOADED_JOURNEY_BYTES


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
    # Both read untrusted downloads (the timetables, up to 61 MB): the same lock-down.
    hardening = ("NoNewPrivileges", "CapabilityBoundingSet", "ProtectSystem", "ProtectHome", "PrivateTmp",
                 "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups",
                 "RestrictAddressFamilies", "RestrictNamespaces", "RestrictSUIDSGID", "LockPersonality",
                 "MemoryDenyWriteExecute", "SystemCallArchitectures", "SystemCallFilter", "SystemCallErrorNumber",
                 "ProtectHostname", "ProtectKernelLogs", "RestrictRealtime", "PrivateIPC", "ProtectClock",
                 "ProcSubset", "ProtectProc", "MemoryMax", "TasksMax", "UMask")
    for setting in hardening:
        assert setting in unit and unit[setting] == timetable.get(setting), setting
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


def test_the_e_paper_run_keeps_its_last_redraw_in_memory_of_its_own_only():
    # A folder in /run for the timer's runs alone, kept between them, which
    # only the service user can open (board/unsaved.py refuses any other).
    kept = {"RuntimeDirectory": ["twobells"], "RuntimeDirectoryMode": ["0700"], "RuntimeDirectoryPreserve": ["yes"]}
    unit = _unit("twobells.service")
    assert {key: unit.get(key) for key in kept} == kept and unit["User"] == ["twobells"]
    for name in SERVICES:
        if name != "twobells.service":
            assert "twobells" not in _unit(name).get("RuntimeDirectory", []), name


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
    assert "twobells-fetch.service" in _units("update.sh")
    assert 'for unit in "${UNITS[@]}"; do\n  install -m 644 "$REPO/deploy/pi/$unit" /etc/systemd/system/\n' in update


SCRIPTS = ("install.sh", "update.sh")
NEW_RELEASE = 'build_release "$release"\n'  # where install.sh and update.sh build the new code


def _script(name: str) -> str:
    return (PI / name).read_text()


def _units(name: str) -> list[str]:
    """The units the script `name` installs: its UNITS=(...) list, or its `for unit in` one."""
    script = _script(name)
    found = re.search(r"^UNITS=\((.*?)\)$", script, re.M | re.S) or re.search(r"for unit in (.*?); do", script, re.S)
    return found.group(1).replace("\\", " ").split()


def _function(name: str, function: str) -> str:
    """The body of the shell function `function` in the script `name`."""
    [body] = re.findall(rf"^ *{function}\(\) \{{\n(.*?)^ *\}}$", _script(name), re.M | re.S)
    return body


def _run_lines(name: str) -> list[str]:
    """The script's systemctl commands: those it runs, not those it prints."""
    return [line.strip() for line in _script(name).splitlines()
            if line.strip().startswith("systemctl ") and not line.startswith("       ")]


def _query(name: str, what: str, conf: Path) -> list[str]:
    """The twobells command line `query WHAT` runs in the script
    `name`, as it would with its config folder at `conf`."""
    [body] = re.findall(r"^query\(\) \{\n  (.*)\n\}$", _script(name), re.M)
    words = shlex.split(body)
    # Asked as the services' user, not as root, and not handed the lock (releases.sh's take_lock).
    assert words[:4] == ["runuser", "-u", "twobells", "--"], words
    assert words.pop() == "9<&-", words
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


def test_saving_the_settings_starts_a_timetable_check():
    # Trams or BODS buses set up in the wizard are built straight away, not at the
    # timer's next hour: systemd watches the saved settings (replaced whole, which
    # PathChanged sees too) and starts the check, which builds only what is due.
    from twobells.settings.store import OVERLAY_NAME

    path = _unit("twobells-timetable.path")
    assert path["PathChanged"] == [f"/var/lib/twobells/{OVERLAY_NAME}"]
    assert path["Unit"] == ["twobells-timetable.service"]
    # Saves come in runs (the settings pages save each change), and a build that fails
    # exits 1: no start limit, which would fail the .path unit and the timer with it.
    assert _unit("twobells-timetable.service")["StartLimitIntervalSec"] == ["0"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_install_and_update_watch_the_settings_for_a_timetable_check(name):
    assert "\nsystemctl enable --now twobells-timetable.path\n" in _script(name)


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


@pytest.mark.parametrize("name", ["install.sh", "update.sh", "releases.sh"])
def test_nothing_is_installed_into_the_venv_in_use(name):
    # pip into the live venv uninstalled the old code before installing the
    # new: a power cut then left no twobells command, and a failed check left
    # the new code in. Each release is built in its own folder instead.
    script = _script(name)
    assert not re.search(r"\$PREFIX/venv/bin/(pip|python)", script)
    assert "--force-reinstall" not in script


def test_update_switches_to_the_new_release_only_once_it_imports_and_restarts_after():
    update = _script("update.sh")
    steps = ["read_version\nprune\nnew_release\n", NEW_RELEASE, 'switch_to "$release"\n',
             'install -m 644 "$REPO/deploy/pi/$unit" /etc/systemd/system/', "\nsystemctl daemon-reload\n",
             "\nsystemctl restart twobells-fetch.path twobells-fetch.service\n",
             "\nsystemctl try-restart twobells-web.service\n", "\nsystemctl try-restart twobells-screen.service\n",
             'if ! stay_up "${watched[@]}"; then\n  go_back\n  exit 1\nfi\n', "\nsay_if_unguarded\n",
             "\nprune\necho\n"]
    places = [update.index(step) for step in steps]
    assert places == sorted(places)
    # The board's timer carries on throughout: the venv it runs isn't touched until
    # the link moves, in one step. Only going back stops anything (a run of the board on the new code).
    assert "systemctl stop" not in update.replace(_function("update.sh", "go_back"), "")
    # A run that stops before switching removes the release it made, and nothing else.
    assert "trap finish EXIT\n" in update and "discard \"$release\"" in _function("update.sh", "finish")


def test_install_lays_out_the_app_as_update_does():
    # A fresh install the same way, so update.sh has one case to update.
    install = _script("install.sh")
    steps = ['source "$REPO/deploy/pi/releases.sh"\ntake_lock\nrecover\n', "trap finish EXIT\n",
             "read_version\nprune\nnew_release\n", NEW_RELEASE, 'switch_to "$release"\nprune\n',
             'install -d -m 755 "$CONF"', '"$(query display)"']
    places = [install.index(step) for step in steps]
    assert places == sorted(places)
    assert "python3 -m venv" not in install  # only build_release makes one


# The release folders (releases.sh), run for real on folders in tmp_path.


def _gnu() -> None:
    """Skips unless mv and ln are GNU's, as on the Pi (-T is GNU's own)."""
    import subprocess

    try:
        said = subprocess.run(["mv", "--version"], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin"}).stdout
    except OSError:
        said = ""
    if "GNU" not in said:
        pytest.skip("needs GNU mv and ln, as on the Pi")


def _fake_venv(folder: Path, version: str) -> None:
    """A stand-in venv: a twobells command that prints `version`, and its metadata folder."""
    (folder / "bin").mkdir(parents=True)
    command = folder / "bin" / "twobells"
    command.write_text(f"#!/bin/sh\necho {version}\n")
    command.chmod(0o755)
    (folder / "lib" / "python3.11" / "site-packages" / f"twobells-{version}.dist-info").mkdir(parents=True)


def _releases(prefix: Path, calls: str, cut: int | None = None):
    """Runs `calls` in bash with releases.sh sourced and PREFIX at `prefix`.
    With `cut`, the power goes before the change to the folders (an ln, mv,
    install or rm) after the first `cut`: nothing more runs. sync is stood
    in for, and install leaves owners alone (this isn't root)."""
    import shutil
    import subprocess

    stubs = ("sync() { :; }\n"
             "install() { local a=(); while (( $# )); do case \"$1\" in -o|-g) shift 2 ;; *) a+=(\"$1\"); shift ;;"
             " esac; done; change; command install \"${a[@]}\"; }\n"
             "changes=0\nchange() { changes=$((changes + 1)); if [[ -n \"$CUT\" ]] && (( changes > CUT )); then"
             " exit 99; fi; }\n"
             "ln() { change; command ln \"$@\"; }\nmv() { change; command mv \"$@\"; }\n"
             "rm() { change; command rm \"$@\"; }\n")
    script = f'set -euo pipefail\nPREFIX="$1"\nsource "$2"\n{stubs}{calls}\n'
    return subprocess.run([shutil.which("bash") or "bash", "-c", script, "bash", str(prefix), str(PI / "releases.sh")],
                          capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "CUT": "" if cut is None
                                                               else str(cut)})


def _layout(prefix: Path) -> dict:
    """Where each link points, the release folders, and the version venv runs."""
    import os
    import subprocess

    links = {name: os.readlink(prefix / name) if (prefix / name).is_symlink() else None
             for name in ("venv", "previous")}
    command = prefix / "venv" / "bin" / "twobells"
    runs = subprocess.run([str(command)], capture_output=True, text=True).stdout.strip() if command.exists() else None
    releases = sorted(path.name for path in (prefix / "releases").iterdir()) if (prefix / "releases").is_dir() else []
    return {**links, "releases": releases, "runs": runs}


def test_a_fresh_install_links_the_venv_to_its_release(tmp_path):
    _gnu()
    _fake_venv(tmp_path / "releases" / "0.6.0-aaaaaa" / "venv", "0.6.0")
    done = _releases(tmp_path, "switch_to 0.6.0-aaaaaa\nprune")
    assert done.returncode == 0, done.stderr
    assert _layout(tmp_path) == {"venv": "releases/0.6.0-aaaaaa/venv", "previous": None,
                                 "releases": ["0.6.0-aaaaaa"], "runs": "0.6.0"}


def test_an_update_keeps_the_release_before_and_removes_older_ones(tmp_path):
    _gnu()
    for name in ("0.5.0-aaaaaa", "0.5.3-bbbbbb", "0.6.0-cccccc", "0.6.0-dddddd"):  # the last, half built
        _fake_venv(tmp_path / "releases" / name / "venv", name[:5])
    (tmp_path / "venv").symlink_to("releases/0.5.3-bbbbbb/venv")
    (tmp_path / "previous").symlink_to("releases/0.5.0-aaaaaa/venv")
    done = _releases(tmp_path, "switch_to 0.6.0-cccccc\nprune")
    assert done.returncode == 0, done.stderr
    assert _layout(tmp_path) == {"venv": "releases/0.6.0-cccccc/venv", "previous": "releases/0.5.3-bbbbbb/venv",
                                 "releases": ["0.5.3-bbbbbb", "0.6.0-cccccc"], "runs": "0.6.0"}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["previous", "releases", "venv"]


def test_an_install_from_before_release_folders_moves_into_them_on_its_first_update(tmp_path):
    _gnu()
    _fake_venv(tmp_path / "venv", "0.5.3")
    _fake_venv(tmp_path / "releases" / "0.6.0-aaaaaa" / "venv", "0.6.0")
    done = _releases(tmp_path, "prune\nswitch_to 0.6.0-aaaaaa\nprune")
    assert done.returncode == 0, done.stderr
    assert _layout(tmp_path) == {"venv": "releases/0.6.0-aaaaaa/venv", "previous": "releases/0.5.3-inplace/venv",
                                 "releases": ["0.5.3-inplace", "0.6.0-aaaaaa"], "runs": "0.6.0"}
    assert (tmp_path / "releases/0.5.3-inplace/venv/lib/python3.11/site-packages/twobells-0.5.3.dist-info").is_dir()


@pytest.mark.parametrize("before", ["inplace", "linked"])
@pytest.mark.parametrize("cut", range(7))
def test_a_power_cut_at_any_step_of_a_switch_leaves_a_working_install_that_updates_again(tmp_path, before, cut):
    # Each step of switch_to that changes the folders, in turn, is the last
    # before the power goes. At the next start a working release runs (once
    # recover has looked, as update.sh and install.sh do first), and the
    # update run again ends as one that was never cut.
    _gnu()
    if before == "inplace":
        _fake_venv(tmp_path / "venv", "0.5.3")
    else:
        _fake_venv(tmp_path / "releases" / "0.5.3-aaaaaa" / "venv", "0.5.3")
        (tmp_path / "venv").symlink_to("releases/0.5.3-aaaaaa/venv")
    _fake_venv(tmp_path / "releases" / "0.6.0-bbbbbb" / "venv", "0.6.0")
    _releases(tmp_path, "switch_to 0.6.0-bbbbbb", cut=cut)
    assert _releases(tmp_path, "recover").returncode == 0
    assert _layout(tmp_path)["runs"] in ("0.5.3", "0.6.0")
    # Run again, it prunes, then builds another folder for the same release and switches to it.
    assert _releases(tmp_path, "recover\nprune").returncode == 0
    _fake_venv(tmp_path / "releases" / "0.6.0-cccccc" / "venv", "0.6.0")
    done = _releases(tmp_path, "switch_to 0.6.0-cccccc\nprune")
    assert done.returncode == 0, done.stderr
    layout = _layout(tmp_path)
    assert (layout["venv"], layout["runs"]) == ("releases/0.6.0-cccccc/venv", "0.6.0")
    assert layout["previous"] in ("releases/0.5.3-inplace/venv", "releases/0.5.3-aaaaaa/venv",
                                  "releases/0.6.0-bbbbbb/venv")
    assert sorted(layout["releases"]) == sorted({layout["previous"].split("/")[1], "0.6.0-cccccc"})
    assert sorted(path.name for path in tmp_path.iterdir()) == ["previous", "releases", "venv"]


@pytest.mark.parametrize("before, previous", [("0.5.0-zzzzzz", "releases/0.5.0-zzzzzz/venv"), ("", None)])
def test_going_back_makes_the_release_before_current_and_removes_the_failed_one(tmp_path, before, previous):
    _gnu()
    for name in ("0.5.0-zzzzzz", "0.5.3-aaaaaa", "0.6.0-bbbbbb"):
        _fake_venv(tmp_path / "releases" / name / "venv", name[:5])
    (tmp_path / "venv").symlink_to("releases/0.6.0-bbbbbb/venv")
    (tmp_path / "previous").symlink_to("releases/0.5.3-aaaaaa/venv")
    done = _releases(tmp_path, f'switch_back 0.6.0-bbbbbb "{before}"')
    assert done.returncode == 0, done.stderr
    layout = _layout(tmp_path)
    assert (layout["venv"], layout["previous"], layout["runs"]) == ("releases/0.5.3-aaaaaa/venv", previous, "0.5.3")
    assert "0.6.0-bbbbbb" not in layout["releases"]


def test_going_back_with_no_release_before_changes_nothing(tmp_path):
    _gnu()
    _fake_venv(tmp_path / "releases" / "0.6.0-bbbbbb" / "venv", "0.6.0")
    (tmp_path / "venv").symlink_to("releases/0.6.0-bbbbbb/venv")
    assert _releases(tmp_path, 'switch_back 0.6.0-bbbbbb ""').returncode == 1
    assert _layout(tmp_path) == {"venv": "releases/0.6.0-bbbbbb/venv", "previous": None,
                                 "releases": ["0.6.0-bbbbbb"], "runs": "0.6.0"}


@pytest.mark.parametrize("target, name", [
    ("releases/0.6.0-aaaaaa/venv", "0.6.0-aaaaaa"), ("{prefix}/releases/0.6.0-aaaaaa/venv", "0.6.0-aaaaaa"),
    ("releases/../venv", ""), ("releases/./venv", ""), ("/elsewhere/releases/x/venv", ""),
    ("releases/0.6.0-aaaaaa", ""), ("releases/a/b/venv", ""),
])
def test_only_a_link_to_a_release_folders_venv_names_a_release(tmp_path, target, name):
    (tmp_path / "venv").symlink_to(target.format(prefix=tmp_path))
    assert _releases(tmp_path, "linked venv").stdout == (f"{name}\n" if name else "")


def test_nothing_is_pruned_or_discarded_that_a_link_points_to_or_without_a_venv_link(tmp_path):
    _gnu()
    _fake_venv(tmp_path / "venv", "0.5.3")  # a folder, not a link: which to keep isn't known
    _fake_venv(tmp_path / "releases" / "0.5.3-inplace" / "venv", "0.5.3")
    assert _releases(tmp_path, "prune").returncode == 0
    assert _layout(tmp_path)["releases"] == ["0.5.3-inplace"]
    (tmp_path / "previous").symlink_to("releases/0.5.3-inplace/venv")
    assert _releases(tmp_path, "discard 0.5.3-inplace\ndiscard ''").returncode == 0
    assert _layout(tmp_path)["releases"] == ["0.5.3-inplace"]


@pytest.mark.parametrize("version", ["0.6.0", "10.20.300", "", "0.6", "v0.6.0", "0.6.0rc1", "0.6.0/../x", "0.6.0 "])
def test_a_release_folder_is_named_only_by_a_plain_release_version(tmp_path, version):
    import shutil
    import subprocess
    import sys

    # As upgrade.sh takes them, read from pyproject.toml by Debian's python3 in isolated mode.
    assert ((_releases(tmp_path, f'valid_release_version "{version}"').returncode == 0)
            == (_sourced('valid_version "$1"', version).returncode == 0))
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "twobells"\nversion = "{version}"\n')
    python = tmp_path / "bin"
    python.mkdir()
    (python / "python3").symlink_to(sys.executable)
    done = subprocess.run([shutil.which("bash") or "bash", "-c",
                           'set -euo pipefail\nREPO="$1"\nsource "$2"\nread_version\necho "$version"', "bash",
                           str(tmp_path), str(PI / "releases.sh")],
                          capture_output=True, text=True, env={"PATH": f"{python}:/usr/bin:/bin"})
    valid = _sourced('valid_version "$1"', version).returncode == 0
    assert (done.returncode == 0) == valid, done.stderr
    assert done.stdout == (f"{version}\n" if valid else "")


def test_a_new_release_folder_is_root_s_and_readable_by_the_services():
    new = _function("releases.sh", "new_release")
    assert 'install -d -o root -g root -m 755 "$PREFIX" "$PREFIX/releases"\n' in new
    # mktemp makes it 0700, whatever the umask: opened up for the services' user.
    assert new.index('mktemp -d "$PREFIX/releases/$version-XXXXXX"') < new.index('chmod 755 "$PREFIX/releases/$release"')


def test_the_guide_s_way_back_without_the_internet_puts_the_release_before_in_use(tmp_path):
    import shutil
    import subprocess

    _gnu()
    guide = PI_GUIDE[PI_GUIDE.index("### Going back to the release before"):]
    block = re.search(r"```bash\n(.*?)```", guide, re.S).group(1)
    assert block.startswith("cd /opt/twobells\n")
    for name in ("0.5.3-aaaaaa", "0.6.0-bbbbbb"):
        _fake_venv(tmp_path / "releases" / name / "venv", name[:5])
    (tmp_path / "venv").symlink_to("releases/0.6.0-bbbbbb/venv")
    (tmp_path / "previous").symlink_to("releases/0.5.3-aaaaaa/venv")
    restarted = tmp_path / "restarted"
    stubs = f'sudo() {{ "$@"; }}\nsystemctl() {{ echo "$*" >> "{restarted}"; }}\n'
    script = "set -euo pipefail\n" + stubs + block.replace("/opt/twobells", str(tmp_path))
    done = subprocess.run([shutil.which("bash") or "bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stderr
    assert _layout(tmp_path) == {"venv": "releases/0.5.3-aaaaaa/venv", "previous": "releases/0.5.3-aaaaaa/venv",
                                 "releases": ["0.5.3-aaaaaa", "0.6.0-bbbbbb"], "runs": "0.5.3"}
    assert restarted.read_text().splitlines() == ["restart twobells-fetch.service",
                                                  "try-restart twobells-web.service twobells-screen.service"]
    # The next update keeps it as the one before, and removes the newer one.
    _fake_venv(tmp_path / "releases" / "0.6.1-cccccc" / "venv", "0.6.1")
    assert _releases(tmp_path, "switch_to 0.6.1-cccccc\nprune").returncode == 0
    assert _layout(tmp_path)["releases"] == ["0.5.3-aaaaaa", "0.6.1-cccccc"]


def test_the_guide_says_where_each_release_is_as_the_scripts_lay_them_out():
    releases = _script("releases.sh")
    for path in ("/opt/twobells/releases/<version>-<6 letters>/venv", "/opt/twobells/venv", "/opt/twobells/previous",
                 "/opt/twobells/releases/<version>-inplace/venv", GUARD_FILE):
        assert f"`{path}`" in PI_GUIDE, path
    assert 'mktemp -d "$PREFIX/releases/$version-XXXXXX"' in releases and 'echo "$version-inplace"' in releases
    assert 'ln -sfnT "releases/$2/venv" "$PREFIX/$1.new"' in releases
    assert "#where-the-app-is" in PI_GUIDE and "about 30 MB" in PI_GUIDE


# One install.sh or update.sh at a time (releases.sh's take_lock), run for real with flock.


def _flock() -> None:
    """Skips unless flock is there, as on the Pi (util-linux's)."""
    import shutil

    if shutil.which("flock", path="/usr/bin:/bin") is None:
        pytest.skip("needs flock (util-linux), as on the Pi")


def _locking(lock: Path, calls: str, path: str = "/usr/bin:/bin"):
    """A bash running `calls` with releases.sh sourced and its LOCK at `lock`, not waited for."""
    import shutil
    import subprocess

    script = f'set -euo pipefail\nsource "$1"\nLOCK="$2"\n{calls}\n'
    return subprocess.Popen([shutil.which("bash") or "/bin/bash", "-c", script, "bash", str(PI / "releases.sh"),
                             str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={"PATH": path})


def _locked(lock: Path, calls: str = "take_lock\necho took it", path: str = "/usr/bin:/bin"):
    """`calls` run to the end, as _locking: (exit code, stdout, stderr)."""
    run = _locking(lock, calls, path)
    out, err = run.communicate(timeout=10)
    return run.returncode, out, err


def test_a_second_install_or_update_stops_at_once_while_one_runs(tmp_path):
    _flock()
    lock = tmp_path / "twobells-deploy.lock"
    first = _locking(lock, "take_lock\necho held\nread -r _ || true\necho done")
    try:
        assert first.stdout.readline() == "held\n"
        code, out, err = _locked(lock)
        # Not waiting for it: it stops, saying why, before anything else.
        assert (code, out) == (1, "")
        assert "Another install.sh or update.sh is running" in err
    finally:
        first.communicate("\n", timeout=10)
    assert first.returncode == 0
    # Once the first has ended, the next takes it.
    assert _locked(lock)[:2] == (0, "took it\n")


@pytest.mark.parametrize("ending", ["exit 3", "kill -KILL $$", "false"])
def test_the_lock_is_let_go_however_a_run_ends(tmp_path, ending):
    # A check that fails (set -e), a power cut's kill: nothing is left to clear by hand.
    _flock()
    lock = tmp_path / "twobells-deploy.lock"
    code, _, _ = _locked(lock, f"take_lock\n{ending}\necho never")
    assert code != 0
    assert _locked(lock)[:2] == (0, "took it\n")


def test_the_lock_is_held_to_the_end_of_the_run_and_its_file_never_replaced(tmp_path):
    # Held through everything after, going back included; the file kept, as
    # a run holding the old one would run beside one holding a new one.
    _flock()
    lock = tmp_path / "twobells-deploy.lock"
    lock.write_text("")
    inode = lock.stat().st_ino
    first = _locking(lock, "take_lock\necho held\nread -r _ || true")
    try:
        assert first.stdout.readline() == "held\n"
        assert _locked(lock)[0] == 1
        assert lock.stat().st_ino == inode
    finally:
        first.communicate("\n", timeout=10)
    assert _locked(lock)[0] == 0 and lock.stat().st_ino == inode


def test_the_lock_is_root_s_alone_to_open(tmp_path):
    # Anyone who could open it could hold it (flock works on a file open only
    # to read) and keep every install.sh and update.sh out until the Pi restarts.
    import stat

    _flock()
    lock = tmp_path / "twobells-deploy.lock"
    assert _locked(lock, "umask 022\ntake_lock\necho took it")[:2] == (0, "took it\n")
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    # One made open to all by an earlier release is closed up, in place.
    lock.chmod(0o644)
    inode = lock.stat().st_ino
    assert _locked(lock, "umask 022\ntake_lock\necho took it")[0] == 0
    assert (stat.S_IMODE(lock.stat().st_mode), lock.stat().st_ino) == (0o600, inode)


@pytest.mark.parametrize("kind", ["link", "dangling link", "folder"])
def test_a_lock_that_isn_t_a_plain_file_is_refused(tmp_path, kind):
    # /run is root's alone, so nobody else can put one there: belt and braces.
    _flock()
    lock, target = tmp_path / "twobells-deploy.lock", tmp_path / "elsewhere"
    if kind == "link":
        target.write_text("")
        target.chmod(0o644)
    if kind == "folder":
        lock.mkdir()
    else:
        lock.symlink_to(target)
    code, out, err = _locked(lock)
    assert (code, out) == (1, "")
    assert f"{lock} isn't a plain file of root's" in err
    # Nothing followed: no file made, nor one's mode changed, where a link points.
    assert (oct(target.stat().st_mode & 0o777) if target.exists() else None) == ("0o644" if kind == "link" else None)


def test_no_other_user_can_open_the_lock_and_one_of_theirs_is_refused():
    # As install.sh and update.sh run: as root (the Docker run). nobody stands
    # for the services' user, which a compromised web server runs as.
    import os
    import pwd
    import shutil
    import subprocess
    import tempfile

    _flock()
    runuser = shutil.which("runuser", path="/usr/sbin:/sbin:/usr/bin:/bin")
    if os.geteuid() != 0 or runuser is None:
        pytest.skip("needs root and runuser, as install.sh and update.sh run")
    folder = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        folder.chmod(0o755)  # others may look in, as in /run
        lock = folder / "twobells-deploy.lock"
        assert _locked(lock, "umask 022\ntake_lock\necho took it")[0] == 0
        held = subprocess.run([runuser, "-u", "nobody", "--", "flock", "--nonblock", "--shared", str(lock), "true"],
                              capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        assert held.returncode != 0 and "Permission denied" in held.stderr
        # A lock file of another user's isn't trusted.
        os.chown(lock, pwd.getpwnam("nobody").pw_uid, -1)
        code, _, err = _locked(lock)
        assert code == 1 and f"{lock} isn't a plain file of root's" in err
    finally:
        shutil.rmtree(folder)


def test_without_flock_install_and_update_stop_saying_how_to_get_it(tmp_path):
    lock = tmp_path / "twobells-deploy.lock"
    code, out, err = _locked(lock, path=str(tmp_path / "nothing"))
    assert (code, out) == (1, "")
    assert "util-linux" in err and "sudo apt-get install util-linux" in err
    assert not lock.exists()  # stopped before making anything


def test_the_lock_is_where_only_root_can_make_it():
    # Not /run/lock or /tmp, which anyone may write to: a link put there
    # first would have root open whatever it points to.
    [path] = re.findall(r"^LOCK=(\S+)$", _script("releases.sh"), re.M)
    assert Path(path).parent == Path("/run")
    lock = _function("releases.sh", "take_lock")
    # Made if missing, root's alone, never replaced or removed; opened only to
    # read, and held on until the shell exits.
    assert ('(umask 077 && : >>"$LOCK")\n  chmod 600 "$LOCK"\n  exec 9<"$LOCK"\n'
            "  if ! flock --nonblock 9; then\n") in lock
    for script in ("install.sh", "update.sh", "releases.sh"):
        text = _script(script)
        assert not re.search(r'(rm|install|mv|ln)\b[^\n]*"\$LOCK"', text), script
        # Never let go before the end, nor closed but for the services' user's commands (_query).
        assert not re.search(r"flock (-u|--unlock)|exec 9[<>]&-", text), script


@pytest.mark.parametrize("name", SCRIPTS)
def test_install_and_update_take_the_lock_before_changing_anything(name):
    script = _script(name)
    taken = script.index('source "$REPO/deploy/pi/releases.sh"\ntake_lock\nrecover\n')
    for change in ("apt-get update", 'install -m 644 "$config"', "useradd", "trap finish EXIT", NEW_RELEASE):
        if change in script:
            assert taken < script.index(change), change
    # Only the root check before it, which changes nothing.
    assert script.index("if [[ $EUID -ne 0 ]]; then") < taken


STAY_UP = re.compile(r"^stay_up\(\) \{\n.*?^\}$", re.M | re.S)


def _stay_up(states: dict[str, str], settle: int = 5):
    """Runs update.sh's stay_up on the units in `states`, systemctl and sleep
    stood in for: each unit's ActiveState:SubState, a word a second, the last
    kept up from there."""
    import shutil
    import subprocess

    [function] = STAY_UP.findall(_script("update.sh"))
    stubs = ('tick=0\nsleep() { tick=$((tick + 1)); }\n'
             'systemctl() {\n  local name="${2#--property=}" unit="$4" var states state\n'
             '  var="STATES_${unit//[-.]/_}"\n  read -r -a states <<< "${!var}"\n'
             '  state="${states[tick - 1]:-${states[${#states[@]} - 1]}}"\n'
             '  if [[ "$name" == ActiveState ]]; then echo "${state%%:*}"; else echo "${state#*:}"; fi\n}\n')
    env = {"PATH": "/usr/bin:/bin", **{"STATES_" + re.sub(r"[-.]", "_", unit): said for unit, said in states.items()}}
    script = (f"set -euo pipefail\nSETTLE={settle}\n{stubs}{function}\n"
              f'if stay_up {" ".join(states)}; then echo "up $tick"; else echo "down $tick"; fi')
    return subprocess.run([shutil.which("bash") or "bash", "-c", script], capture_output=True, text=True, env=env,
                          check=True)


def test_update_sees_the_services_staying_up_on_the_new_release():
    done = _stay_up({"twobells-fetch.service": "active:running", "twobells-web.service": "active:running"})
    assert (done.stdout, done.stderr) == ("up 5\n", "")
    # The fetch service stopping by itself (the dashboard off) isn't a crash.
    assert _stay_up({"twobells-fetch.service": "active:running active:running inactive:dead"}).stdout == "up 5\n"


@pytest.mark.parametrize("states", ["active:running active:running activating:auto-restart",
                                    "active:running active:running activating:auto-restart-queued",
                                    "active:running failed:failed"])
def test_update_sees_a_service_crash_on_the_new_release(states):
    done = _stay_up({"twobells-fetch.service": "active:running", "twobells-web.service": states})
    assert done.stdout == f"down {len(states.split())}\n"
    assert "twobells-web.service" in done.stderr and "journalctl -u twobells-web" in done.stderr


def test_update_goes_back_to_what_ran_before_when_the_services_dont_stay_up():
    update = _script("update.sh")
    switched = update.index('switch_to "$release"\n')
    # Taken before the switch: the units and guard's rules in use, the release
    # before, and which long-running services were up.
    for taken in ('saved="$(mktemp -d)"\n', 'cp -p "/etc/systemd/system/$unit" "$saved/"',
                  'cp -p "$PREFIX/twobells-web.nft" "$saved/"', 'before="$(linked previous)"\n',
                  "watched=(twobells-fetch.service)\n", 'if systemctl is-active --quiet "$unit"; then\n'):
        assert update.index(taken) < switched, taken
    back = _function("update.sh", "go_back")
    steps = ['if ! switch_back "$release" "$before"; then\n', 'install -m 644 "$saved/$unit" /etc/systemd/system/',
             'install -m 644 -o root -g root "$saved/twobells-web.nft" "$PREFIX/twobells-web.nft"',
             "  systemctl daemon-reload\n", "  systemctl restart twobells-guard.service || true\n",
             '  systemctl restart twobells-fetch.path "${watched[@]}"\n']
    places = [back.index(step) for step in steps]
    assert places == sorted(places)
    assert _units("update.sh") == _units("install.sh")  # puts back each unit it put in
    assert "SETTLE=30\n" in update


# The e-paper board's run on the new release, which goes back as a crash does if it fails.
DRAWS = re.compile(r"^draws\(\) \{\n.*?^\}$", re.M | re.S)
EPAPER = re.compile(r"^epaper=false\nif .*?^fi$", re.M | re.S)
GO_BACK = re.compile(r"^go_back\(\) \{\n.*?^\}$", re.M | re.S)


def _bash(script: str, *args: str, **env: str):
    import shutil
    import subprocess

    return subprocess.run([shutil.which("bash") or "bash", "-c", script, "bash", *args], capture_output=True,
                          text=True, env={"PATH": "/usr/bin:/bin", **env})


def _draws(states: str, start: int = 0, wait: int = 5, hang: bool = False) -> list[str]:
    """Runs update.sh's draws with systemctl, sleep and timeout stood in for:
    twobells.service's ActiveState a word a second (the last kept up from
    there), and its start exiting `start`, or `hang`ing past timeout's
    limit. Each timeout and start it ran, with the second it started, then
    whether it drew."""
    [function] = DRAWS.findall(_script("update.sh"))
    stubs = ('tick=0\nsleep() { tick=$((tick + 1)); }\n'
             'systemctl() {\n  local states\n  read -r -a states <<< "$STATES"\n  case "$1" in\n'
             '    show) [[ "$*" == "show --property=ActiveState --value twobells.service" ]] || return 9\n'
             '      echo "${states[tick]:-${states[${#states[@]} - 1]}}" ;;\n'
             '    start) echo "start $2 at $tick"; return "$START" ;;\n    *) return 9 ;;\n  esac\n}\n'
             'timeout() { echo "timeout $1"; shift; if [[ -n "$HANG" ]]; then return 124; fi; "$@"; }\n')
    done = _bash(f"set -euo pipefail\nRENDER_WAIT={wait}\n{stubs}{function}\n"
                 "if draws; then echo drew; else echo failed; fi",
                 STATES=states, START=str(start), HANG="yes" if hang else "")
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


@pytest.mark.parametrize("states", ["inactive", "failed", "active"])
def test_update_runs_the_e_paper_board_once_on_the_new_release_and_waits_for_it(states):
    # failed: an earlier tick's, which a start clears.
    assert _draws(states) == ["timeout 5", "start twobells.service at 0", "drew"]


@pytest.mark.parametrize("states", ["activating activating inactive", "activating deactivating failed"])
def test_update_lets_a_run_of_the_old_code_finish_before_its_own(states):
    # A tick the timer started before the switch runs the old code, and a
    # start while it goes would wait for it and take its result for the new release's.
    assert _draws(states) == ["timeout 5", "start twobells.service at 2", "drew"]


def test_update_gives_up_on_a_run_of_the_old_code_that_never_ends():
    assert _draws("activating", wait=5) == ["failed"]  # and never starts one of its own to judge by it


def test_update_sees_the_e_paper_board_fail_or_hang_on_the_new_release():
    assert _draws("inactive", start=1) == ["timeout 5", "start twobells.service at 0", "failed"]
    assert _draws("inactive", hang=True) == ["timeout 5", "failed"]


def test_update_waits_longer_for_the_e_paper_board_than_its_unit_does():
    # The unit stops a hung run itself, and says so in its log; update.sh's
    # own limit is for a unit that doesn't, with room for a slow first run.
    [wait] = re.findall(r"^RENDER_WAIT=(\d+)$", _script("update.sh"), re.M)
    [unit] = _unit("twobells.service")["TimeoutStartSec"]
    assert int(wait) >= 2 * int(unit)


@pytest.mark.parametrize("display, enabled, epaper", [
    ("", True, True),  # an Inky, unless the settings say; or ones that can't be read
    ("waveshare-10in85g", True, True),
    ("hdmi-1280x400", True, False),  # a screen's service is watched with the others instead
    ("waveshare-10in85g", False, False),  # the board is yours to turn on
])
def test_update_checks_the_e_paper_board_only_where_its_timer_runs_it(display, enabled, epaper):
    [block] = EPAPER.findall(_script("update.sh"))
    stub = 'systemctl() { [[ "$*" == "is-enabled --quiet twobells.timer" && "$ENABLED" == yes ]]; }\n'
    done = _bash(f'set -euo pipefail\ndisplay="$1"\n{stub}{block}\necho "$epaper"', display,
                 ENABLED="yes" if enabled else "no")
    assert (done.returncode, done.stdout) == (0, f"{str(epaper).lower()}\n")


def test_update_goes_back_when_the_e_paper_board_fails_on_the_new_release():
    update = _script("update.sh")
    # Decided before the switch, as the services watched are.
    assert update.index(EPAPER.findall(update)[0]) < update.index('switch_to "$release"\n')
    # Once the services stay up, and before anything else is turned on.
    check = ('if $epaper; then\n  echo "Drawing the e-paper board on the new release"\n'
             "  if ! draws; then\n")
    steps = ['if ! stay_up "${watched[@]}"; then\n  go_back\n  exit 1\nfi\n', check, "      go_back\n      exit 1\n",
             "\nsay_if_unguarded\n", "\nsystemctl enable --now twobells-timetable.timer\n"]
    places = [update.index(step) for step in steps]
    assert places == sorted(places)
    said = update[places[1]:places[2]]
    # Which service failed, where to see why, and that it may be the panel.
    assert "twobells.service" in said and "journalctl -u twobells -n 30" in said and "panel" in said
    # But not when the card wouldn't take the state of a board that drew: the new release stays.
    kept = said.index("    if card_refused_save; then\n")
    assert kept < said.index("    else\n") < said.index("twobells.service, the e-paper board, failed")
    assert "go_back" not in said[kept:said.index("    else\n")]
    # That run is the only one, but going back's: none at the end, whatever it does, as before.
    assert update.count("systemctl start twobells.service") == 2
    for function in ("draws", "go_back"):
        assert _function("update.sh", function).count('timeout "$RENDER_WAIT" systemctl start twobells.service') == 1


REFUSED = re.compile(r"^card_refused_save\(\) \{\n.*?^\}$", re.M | re.S)
FAILED_74 = "ActiveState=failed\nResult=exit-code\nExecMainStatus=74\n"


REFUSED_BY_THE_CARD = 3  # the probe's exit when the card itself refused the file


def _card_refused_save(shown: str | None, probe: int = REFUSED_BY_THE_CARD, hang: bool = False):
    """Runs update.sh's card_refused_save with systemctl, timeout and runuser
    stood in for: systemctl show saying `shown` (None: it fails), the probe
    run as the services' user exiting `probe`, or timeout giving up on it if
    `hang`. What it found, and each command it ran."""
    [function] = REFUSED.findall(_script("update.sh"))
    stubs = ('systemctl() { echo "systemctl $*" >&2; if [[ -z "$SHOWN" ]]; then return 1; fi; printf "%s" "$SHOWN"; }\n'
             'timeout() { echo "timeout $1 $2" >&2; if [[ -n "$HANG" ]]; then return 124; fi; shift 2; "$@"; }\n'
             # Its words but the script, then where the script writes.
             'runuser() { echo "runuser ${*:1:7}" >&2; if [[ "$8" == *" /var/lib/twobells/"* ]]; then\n'
             '  echo "in the state folder" >&2; fi; return "$PROBE"; }\n')
    done = _bash(f"set -euo pipefail\nPROBE_WAIT=30\n{stubs}{function}\n"
                 "if card_refused_save; then echo card; else echo release; fi",
                 SHOWN=shown or "", PROBE=str(probe), HANG="yes" if hang else "")
    assert done.returncode == 0, done.stderr
    return done.stdout.strip(), done.stderr.splitlines()


def test_a_board_that_drew_but_couldn_t_save_on_an_unwritable_card_isn_t_the_release_s_fault():
    from twobells.cli.run import UNSAVED

    assert FAILED_74.endswith(f"={UNSAVED}\n")  # run.py's exit for a run that drew but couldn't save
    found, ran = _card_refused_save(FAILED_74)
    assert found == "card"
    assert ran[0] == ("systemctl show --property=ActiveState --property=Result --property=ExecMainStatus "
                      "twobells.service")
    # Bounded, with a kill for one that ignores being asked to stop.
    assert ran[1] == "timeout --kill-after=5 30"
    # Tried as the services' user, in the C locale its errors are read in, in the state folder, not handed the lock.
    assert ran[2:] == ["runuser -u twobells -- env LC_ALL=C sh -c", "in the state folder"]
    assert re.search(r"^PROBE_WAIT=30$", _script("update.sh"), re.M)


@pytest.mark.parametrize("probe, hang", [
    (0, False),  # the card took the file: a release whose saving is broken exits 74 too
    (1, False),  # refused for another reason (the folder not the services' user's, say), or runuser failed
    (126, False), (127, False), (137, False),  # runuser couldn't run it, or it was killed
    (0, True),  # it hung (a FIFO swapped in for its file, say): timeout gave up on it
])
def test_a_probe_that_doesn_t_say_the_card_refused_is_the_release_s_fault(probe, hang):
    # Nothing but the card's own refusal keeps the new release: not knowing goes back.
    assert _card_refused_save(FAILED_74, probe, hang)[0] == "release"


def _probe_script() -> str:
    [function] = REFUSED.findall(_script("update.sh"))
    [probe] = re.findall(r"runuser -u twobells -- env LC_ALL=C sh -c '([^']*)' 9<&-", function)
    return probe


def _run_probe(folder: Path, fakes: dict[str, str] | None = None, path: Path | None = None):
    """The probe's sh -c, run for real with `folder` for /var/lib/twobells,
    and the commands in `fakes` (name: script) put first on PATH in `path`."""
    import shutil
    import subprocess

    bins = ""
    if fakes:
        assert path is not None
        path.mkdir()
        for name, body in fakes.items():
            (path / name).write_text(f"#!/bin/sh\n{body}\n")
            (path / name).chmod(0o755)
        bins = f"{path}:"
    return subprocess.run([shutil.which("sh") or "/bin/sh", "-c", _probe_script().replace("/var/lib/twobells",
                                                                                          str(folder))],
                          capture_output=True, text=True, env={"PATH": f"{bins}/usr/bin:/bin", "LC_ALL": "C"})


@pytest.mark.parametrize("shown", [
    "ActiveState=failed\nResult=exit-code\nExecMainStatus=1\n",  # any other exit
    "ActiveState=failed\nResult=timeout\nExecMainStatus=74\n",  # its unit stopped it, hung
    "ActiveState=activating\nResult=exit-code\nExecMainStatus=74\n",  # still going: an earlier run's result
    "ActiveState=failed\nResult=signal\nExecMainStatus=74\n",
    "ActiveState=failed\nResult=exit-code\nExecMainStatus=074\n",  # read strictly
    "ActiveState=failed\nResult=exit-code\nExecMainStatus=74 \n",
    "ActiveState=failed\nResult=exit-code\n",  # no status
    "Result=exit-code\nExecMainStatus=74\n",
    None,  # systemctl failed
])
def test_any_other_failure_of_the_board_is_the_release_s(shown):
    found, ran = _card_refused_save(shown)
    assert found == "release"
    assert not [line for line in ran if line.startswith(("runuser", "timeout"))]  # nothing tried unless it was 74


def test_the_card_s_probe_writes_a_states_worth_and_always_cleans_up(tmp_path):
    # Its sh -c, run for real in a folder standing in for /var/lib/twobells.
    done = _run_probe(tmp_path)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert list(tmp_path.iterdir()) == []


def test_the_probe_doesn_t_blame_the_card_for_a_folder_it_may_not_write_to(tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root writes into a folder whatever its mode")
    (tmp_path / "theirs").mkdir(mode=0o500)  # as a state folder of root's would be to the services' user
    done = _run_probe(tmp_path / "theirs")
    assert done.returncode not in (0, REFUSED_BY_THE_CARD)
    assert list((tmp_path / "theirs").iterdir()) == []


# What mktemp and head say (in the C locale) when the card refuses, as the probe reads them.
FULL = "No space left on device"  # out of room, or out of files
READ_ONLY = "Read-only file system"


@pytest.mark.parametrize("said, refused", [
    (FULL, True), (READ_ONLY, True), ("Disk quota exceeded", True),
    ("Permission denied", False), ("Input/output error", False),
])
def test_the_probe_blames_the_card_only_for_its_own_refusals(tmp_path, said, refused):
    expected = REFUSED_BY_THE_CARD if refused else 1
    # mktemp refused...
    made = _run_probe(tmp_path / "state", {"mktemp": f'echo "mktemp: failed to create file via template: {said}" >&2'
                                                    "\nexit 1"}, tmp_path / "bin1")
    assert made.returncode == expected, made.stderr
    # ...or the write did, and the file was still removed.
    (tmp_path / "state").mkdir()
    wrote = _run_probe(tmp_path / "state", {"head": f"echo \"head: error writing 'standard output': {said}\" >&2"
                                                    "\nexit 1"}, tmp_path / "bin2")
    assert wrote.returncode == expected, wrote.stderr
    assert list((tmp_path / "state").iterdir()) == []


def test_a_probe_that_hangs_and_ignores_being_asked_to_stop_is_killed(tmp_path):
    # A compromised services' user owns the state folder, and could swap the
    # probe's file for a FIFO, so the write never opens: run for real, the
    # probe is given up on after PROBE_WAIT and killed 5 seconds later.
    import shutil
    import subprocess
    import time

    timeout = shutil.which("timeout", path="/usr/bin:/bin")
    if timeout is None:
        pytest.skip("needs GNU timeout, as on the Pi")
    fakes = tmp_path / "bin"
    fakes.mkdir()
    runuser = fakes / "runuser"
    runuser.write_text('#!/bin/sh\ntrap "" TERM\nwhile :; do sleep 1; done\n')
    runuser.chmod(0o755)
    [function] = REFUSED.findall(_script("update.sh"))
    stub = 'systemctl() { printf "%s" "$SHOWN"; }\n'
    started = time.monotonic()
    done = subprocess.run([shutil.which("bash") or "bash", "-c", f"set -euo pipefail\nPROBE_WAIT=1\n{stub}{function}\n"
                           "if card_refused_save; then echo card; else echo release; fi"],
                          capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{fakes}:/usr/bin:/bin", "SHOWN": FAILED_74})
    assert (done.returncode, done.stdout) == (0, "release\n"), done.stderr
    assert time.monotonic() - started < 15


def _go_back(epaper: bool, fails: bool, tmp_path: Path):
    """Runs update.sh's go_back with everything it changes stood in for, each
    call said in order: `epaper` as update.sh found it, and the release
    from before's run of the board failing if `fails`."""
    [function] = GO_BACK.findall(_script("update.sh"))
    stubs = ('switch_back() { echo "switch_back $*"; }\nlinked() { echo 0.5.3-aaaaaa; }\n'
             'install() { echo "install $*"; }\ntimeout() { echo "timeout $1"; shift; "$@"; }\n'
             'systemctl() { echo "systemctl $*"; if [[ "$1" == start && -n "$FAILS" ]]; then return 1; fi; }\n')
    setting = (f'PREFIX=/opt/twobells release=0.6.0-bbbbbb before=0.5.0-zzzzzz saved="{tmp_path}"\n'
               f"UNITS=(twobells.service twobells.timer)\nwatched=(twobells-fetch.service)\n"
               f"epaper={str(epaper).lower()}\nRENDER_WAIT=300\n")
    return _bash(f"set -euo pipefail\n{setting}{stubs}{function}\ngo_back\necho went back", FAILS="yes" if fails else "")


@pytest.mark.parametrize("fails", [False, True])
def test_going_back_draws_the_e_paper_board_again_on_the_release_from_before(tmp_path, fails):
    done = _go_back(True, fails, tmp_path)
    # As best it can: if that fails too it says so, and the rest stands.
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "switch_back 0.6.0-bbbbbb 0.5.0-zzzzzz", "systemctl daemon-reload", "systemctl restart twobells-guard.service",
        "systemctl restart twobells-fetch.path twobells-fetch.service",
        # A run still going (a hung one, or the timer's on the new code) is stopped first, or the start would join it.
        "systemctl stop twobells.service", "timeout 300", "systemctl start twobells.service", "went back"]
    assert "back on the release from before (0.5.3-aaaaaa)" in done.stderr
    assert ("journalctl -u twobells -n 30" in done.stderr) == fails


def test_going_back_leaves_the_board_alone_without_the_e_paper_timer(tmp_path):
    done = _go_back(False, False, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "twobells.service" not in done.stdout


def test_update_never_stops_or_starts_the_boards_timer():
    # Whatever happens, the timer is left as it was: on if it was, and off if not.
    for line in _script("update.sh").splitlines():
        if "systemctl" in line and "twobells.timer" in line and not line.lstrip().startswith("#"):
            assert "systemctl is-enabled --quiet twobells.timer" in line, line


# Room on the SD card for the board to save its state, looked at before the switch.
ROOM = re.compile(r"^room_to_save\(\) \{\n.*?^\}$", re.M | re.S)


@pytest.mark.parametrize("df, room", [
    ("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 30000000 20000000 512000 98% /\n", True),
    ("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 30000000 29990000 10240 100% /\n", True),
    ("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 30000000 29990000 10239 100% /\n", False),
    ("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 30000000 30000000 0 100% /\n", False),
    ("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 1 1 - - /\n", False),  # can't be told
    (None, False),  # df failed
])
def test_update_needs_room_on_the_card_for_the_board_to_save_its_state(df, room):
    assert _room(df, INODES) == room


INODES = "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 1900000 100000 1800000 6% /\n"
ROOMY = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 30000000 20000000 512000 98% /\n"


def _room(df: str | None, inodes: str | None) -> bool:
    """update.sh's room_to_save, df -Pk saying `df` and df -Pi `inodes` (None: it fails)."""
    [function] = ROOM.findall(_script("update.sh"))
    stub = ('df() { case "$*" in\n'
            '  "-Pk /var/lib/twobells") [[ -n "$SAID" ]] && printf "%s" "$SAID" ;;\n'
            '  "-Pi /var/lib/twobells") [[ -n "$INODES" ]] && printf "%s" "$INODES" ;;\n'
            '  *) return 9 ;;\nesac; }\n')
    done = _bash(f"set -euo pipefail\nMIN_FREE_KB=10240\nMIN_FREE_INODES=1000\n{stub}{function}\n"
                 "if room_to_save; then echo room; else echo none; fi", SAID=df or "", INODES=inodes or "")
    assert done.returncode == 0, done.stderr
    return done.stdout == "room\n"


@pytest.mark.parametrize("inodes, room", [
    (INODES, True),
    ("Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 1900000 1899000 1000 100% /\n", True),
    # A card out of files has room to spare in KB, but can't take a new state file.
    ("Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 1900000 1899001 999 100% /\n", False),
    ("Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 0 0 0 - /\n", True),  # none counted: no limit
    ("Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root - - - - /\n", False),  # can't be told
    (None, False),  # df failed
])
def test_update_needs_room_for_a_file_on_the_card_too(inodes, room):
    assert _room(ROOMY, inodes) == room


def test_update_stops_before_the_switch_on_a_card_too_full_to_save_the_boards_state():
    # A run that can't save its state fails, and would be blamed on the new release.
    update = _script("update.sh")
    stop = 'if ! room_to_save; then\n'
    assert update.index(NEW_RELEASE) < update.index(stop) < update.index('switch_to "$release"\n')
    block = update[update.index(stop):update.index("fi\n", update.index(stop))]
    assert "exit 1" in block and "/var/lib/twobells" in block
    assert re.search(r"^MIN_FREE_KB=10240$", update, re.M) and re.search(r"^MIN_FREE_INODES=1000$", update, re.M)


@pytest.mark.parametrize("name", ["install.sh", "update.sh", "upgrade.sh", "releases.sh"])
def test_python_run_as_root_never_imports_from_the_current_folder(name):
    # python -c and python -m put the current folder first on sys.path: run
    # from /tmp, a planted inky.py, numpy.py or venv.py would be imported as
    # root. -I leaves it out (and PYTHON* variables), and the system site
    # packages still load.
    runs = re.findall(r"python3?\"?((?: -[A-Za-z]+)*) -[cm] ", _script(name))
    assert all("-I" in flags.split() for flags in runs), (name, runs)
    # The version check, making the venv and the import check.
    assert len(runs) == (3 if name == "releases.sh" else 0), (name, runs)


@pytest.mark.parametrize("name", ["install.sh", "update.sh"])
def test_install_and_update_set_their_own_umask(name):
    # sudo may pass on a umask of 077, and the venv would be unreadable to the
    # services' user. Every file that must be private is given its mode outright.
    script = _script(name)
    assert "set -euo pipefail\n# " in script
    after = script[script.index("set -euo pipefail\n"):].splitlines()
    assert after[2] == "umask 022", after[:3]
    for line in script.splitlines():
        if "secrets.env" in line and line.lstrip().startswith("install "):
            assert "-m 600" in line, line


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
    # Neither touches the hardware: only native system calls, and no other user's processes.
    assert unit.get("SystemCallArchitectures") == ["native"] and unit.get("ProtectProc") == ["invisible"]
    # Each of the services' own has a ceiling on its threads and processes.
    if name != GUARD_UNIT:
        assert int(unit["TasksMax"][0]) >= 32, name
    # Where a lock-down setting is left out, the unit says why (it is untried with the hardware).
    text = (PI / name).read_text()
    assert unit.get("MemoryDenyWriteExecute") == ["yes"] or "# No MemoryDenyWriteExecute:" in text, name


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
    for name in ("install.sh", "update.sh", "releases.sh"):
        script = _script(name)
        needed |= {path for path in re.findall(r"\$REPO/([\w./-]+)", script) if not path.endswith(("/", "."))}
    for name in ("install.sh", "update.sh"):
        needed |= {f"deploy/pi/{unit}" for unit in _units(name)}
    needed |= {"deploy/pi/install.sh", "deploy/pi/update.sh", "deploy/pi/upgrade.sh"}  # upgrade.sh runs the release's update.sh
    return needed


def _jobs(workflow: str = RELEASE) -> dict[str, str]:
    """A workflow's jobs (release.yml's unless given), by name, each as its text."""
    body = workflow[workflow.index("\njobs:\n") + len("\njobs:\n"):]
    parts = re.split(r"\n(?=  [a-z][\w-]*:\n)", body)
    return {part.split(":", 1)[0].strip(): part for part in parts}


def _needs(job: str) -> set[str]:
    found = re.search(r"^    needs: (\[[^\]]*\]|\S+)$", job, re.M).group(1)
    return {name.strip() for name in found.strip("[]").split(",")}


def test_the_image_is_tagged_and_signed_only_after_the_release_is_approved():
    jobs = _jobs()
    assert list(jobs) == ["verify", "test", "dist", "image", "tarball", "pypi", "image-tags", "sign-sums", "release"]
    # pypi's environment is the one review: nothing before it tags, signs or publishes the image.
    assert [name for name, job in jobs.items() if "environment:" in job] == ["pypi"]
    # Only pypi and the two jobs after it that sign may ask for an OIDC token.
    assert [name for name, job in jobs.items() if "id-token" in job] == ["pypi", "image-tags", "sign-sums"]
    assert [name for name, job in jobs.items() if "cosign sign" in job] == ["image-tags", "sign-sums"]
    image = jobs["image"]
    assert "push-by-digest=true" in image and "push: true" not in image
    assert "tags: ${{ steps.meta.outputs.tags }}" not in image and "type=raw,value=latest" not in image
    assert "id-token" not in image and "cosign" not in image
    assert "sbom: true" in image and "provenance: mode=max" in image
    assert "digest: ${{ steps.build.outputs.digest }}" in image
    # After the review: signed by that digest, then tagged, then checked as a user would.
    tags = jobs["image-tags"]
    assert {"image", "pypi"} <= _needs(tags) and "id-token: write" in tags
    assert "DIGEST: ${{ needs.image.outputs.digest }}" in tags
    signed = tags.index('cosign sign --yes "$IMAGE@$DIGEST"')
    tagged = tags.index('imagetools create --tag "$IMAGE:$VERSION" "$IMAGE@$DIGEST"')
    # latest only for the highest vX.Y.Z tag: an older release approved last mustn't take it back.
    highest = tags.index("sort -V")
    latest = tags.index('imagetools create --tag "$IMAGE:latest" "$IMAGE@$DIGEST"')
    assert 'if [[ "$highest" == "$VERSION" ]]' in tags[highest:latest]
    # Each tag is checked to name the digest signed, then verified as a user verifies it.
    inspected = tags.index('imagetools inspect "$IMAGE:$tag"')
    compared = tags.index('if [[ "$got" != "$DIGEST" ]]')
    verified = tags.index('cosign verify "$IMAGE:$tag"')
    assert tags.index("^sha256:[0-9a-f]{64}$") < signed < tagged < highest < latest < inspected < compared < verified
    signer = re.search(r'signer="(\S+)"', tags).group(1)
    assert signer.replace("$GITHUB_REPOSITORY", REPO).replace("$GITHUB_REF", "refs/tags/v0.2.0") == _identity("v0.2.0")
    # PyPI's upload stays last of what is permanent, after the image is pushed; the notes name its tag.
    assert {"dist", "image", "tarball"} <= _needs(jobs["pypi"])
    assert {"image-tags", "sign-sums"} <= _needs(jobs["release"])


def test_the_pi_tarball_s_sums_are_signed_only_after_the_release_is_approved():
    jobs = _jobs()
    assert "cosign" not in jobs["tarball"] and "sha256sum -- * > SHA256SUMS" in jobs["tarball"]
    sign = jobs["sign-sums"]
    assert {"tarball", "pypi"} <= _needs(sign)
    assert re.search(r"^    permissions:\n      id-token: write #[^\n]*\n    [a-z]", sign, re.M)  # nothing else
    # It signs only sums that still match the files they name, then hands the release the bundle.
    checked = sign.index("sha256sum --check SHA256SUMS")
    assert checked < sign.index("cosign sign-blob --yes --bundle SHA256SUMS.sigstore.json SHA256SUMS")
    assert "path: out/SHA256SUMS.sigstore.json" in sign and "name: sums-signature" in jobs["release"]


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
            "examples/glossop.toml", "deploy/pi/twobells-fetch.path", "LICENSE", "deploy/pi/releases.sh"} <= needed
    for path in sorted(needed):
        assert (ROOT / path).is_file(), path
        assert _in_tarball(path), path
    # git archive also leaves out whatever .gitattributes marks export-ignore.
    attributes = ROOT / ".gitattributes"
    assert not attributes.exists() or "export-ignore" not in attributes.read_text()
    # And the scripts never need git: a tarball has no .git.
    for name in ("install.sh", "update.sh", "upgrade.sh", "releases.sh"):
        assert not re.search(r"(^|[\s;&|(])git\s", _script(name), re.M), name
    # The release checks the scripts are in the tarball it built.
    assert "for script in install.sh update.sh upgrade.sh releases.sh; do" in RELEASE


# The workflows' toolchain and gates: what CI, the release and the data refresh install, and who holds a write token.

WORKFLOWS = ROOT / ".github" / "workflows"
CI = (WORKFLOWS / "ci.yml").read_text()
DATA_REFRESH = (WORKFLOWS / "data.yml").read_text()
LOCK_PLATFORMS = {"linux-x64", "linux-arm64", "macos-arm64"}  # CI's runners and the owner's Mac


def _matrix_pythons(job: str) -> list[str]:
    return json.loads(re.search(r"^        python: (\[.*\])$", job, re.M).group(1))


def _permissions(job: str) -> list[str]:
    """A job's permissions, as `scope: level` lines, comments dropped."""
    block = re.search(r"^    permissions:\n((?:      .*\n)+)", job, re.M).group(1)
    return [line.split("#")[0].strip() for line in block.splitlines()]


def _run_block(job: str, step: str) -> str:
    """The shell script of the step named `step` in a job, dedented."""
    import textwrap

    after = job[job.index(f"- name: {step}\n"):]
    run = re.search(r"^( +)run: \|\n", after, re.M)
    lines = []
    for line in after[run.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= len(run.group(1)):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines)) + "\n"


def test_no_release_job_builds_or_publishes_before_the_tests_pass_on_every_python():
    jobs = _jobs()
    # The release tests on each Python CI tests, which are the ones the package says it runs on.
    assert _matrix_pythons(jobs["test"]) == _matrix_pythons(_jobs(CI)["test"])
    assert "MISE_PYTHON_VERSION: ${{ matrix.python }}" in jobs["test"]
    assert jobs["test"].index("Check the venv runs the Python under test") < jobs["test"].index("- run: mise run test")
    assert "mise run test" not in jobs["verify"]
    assert "- run: mise run lint" in jobs["verify"] and "- run: mise run typecheck" in jobs["verify"]
    # Every job after the gate needs it outright, so one failing Python stops all of them.
    for name, job in jobs.items():
        if name not in ("verify", "test"):
            assert {"verify", "test"} <= _needs(job), name


def test_the_release_dates_the_wheel_by_the_tagged_commit():
    build = _run_block(_jobs()["dist"], "Build")
    dated = build.index('export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct "$GITHUB_SHA")"')
    assert dated < build.index("-m build --no-isolation")


def test_ci_and_the_release_install_only_the_tools_mise_lock_pins():
    import tomllib

    tools = tomllib.loads((ROOT / "mise.toml").read_text())["tools"]
    lock = tomllib.loads((ROOT / "mise.lock").read_text())
    # Every tool, and every Python mise.toml lists, pinned by checksum on each platform.
    for tool, wanted in tools.items():
        for request in [wanted] if isinstance(wanted, str) else wanted:
            entries = [entry for entry in lock["tools"][tool] if request in entry["specifiers"]]
            assert len(entries) == 1, (tool, request)
            platforms = {key.removeprefix("platforms."): value
                         for key, value in entries[0].items() if key.startswith("platforms.")}
            assert set(platforms) >= LOCK_PLATFORMS, (tool, request)
            for platform in LOCK_PLATFORMS:
                assert re.fullmatch(r"sha256:[0-9a-f]{64}", platforms[platform]["checksum"]), (tool, platform)
                assert platforms[platform]["url"].startswith("https://"), (tool, platform)
    # The first Python is the venv's, the Dockerfile's minor version; CI's matrices name mise.toml's Pythons.
    assert tools["python"][0].startswith(re.search(r"^FROM python:(\d+\.\d+)-slim", DOCKERFILE, re.M).group(1) + ".")
    assert sorted(_matrix_pythons(_jobs(CI)["test"])) == sorted(tools["python"])
    # Each workflow that sets up mise does it locked, with the one mise version the lock was made for.
    versions = set()
    for workflow in (CI, RELEASE, DATA_REFRESH):
        for job in _jobs(workflow).values():
            if "jdx/mise-action@" in job:
                assert 'MISE_LOCKED: "1"' in workflow, job.split(":")[0]
                versions |= set(re.findall(r"^          version: (\S+)$", job, re.M))
    assert len(versions) == 1


def test_the_data_refresh_holds_its_write_token_only_where_nothing_but_the_pull_request_runs():
    jobs = _jobs(DATA_REFRESH)
    assert list(jobs) == ["build", "propose", "ci", "report"]
    assert "\npermissions: {}\n" in DATA_REFRESH
    # build downloads NaPTAN, the toolchain and the dev tools, and runs the tests: it may only read.
    build, propose = jobs["build"], jobs["propose"]
    assert _permissions(build) == ["contents: read"]
    assert "write" not in build
    assert "changed: ${{ steps.diff.outputs.changed }}" in build
    # propose runs only for a change, after build, and runs nothing of the project's.
    assert _needs(propose) == {"build"}
    assert "    if: needs.build.outputs.changed == 'true'\n" in propose
    assert _permissions(propose) == ["contents: write", "pull-requests: write"]
    steps = propose[:propose.index("peter-evans/create-pull-request@")]  # the PR's own text may name the builder
    for word in ("mise", "pip", "python", "uv ", "uv\n", "tools/", "make ", "npm", "node", "bash ", "./"):
        assert word not in steps, word
    assert "\nenv:" not in DATA_REFRESH  # no workflow-wide setting reaches propose
    actions = re.findall(r"uses: ([\w-]+/[\w-]+)@", propose)
    assert actions == ["actions/checkout", "actions/download-artifact", "peter-evans/create-pull-request"]
    # It checks the artefact before the files reach the checkout, and the PR takes only the CSV files.
    downloaded = propose.index("actions/download-artifact@")
    checked = propose.index("- name: Check the artefact holds only the expected data files")
    placed = propose.index('cp -- "$RUNNER_TEMP"/setup-data/*.csv src/twobells/data/')
    assert downloaded < checked < placed < propose.index("peter-evans/create-pull-request@")
    assert "add-paths: src/twobells/data/*.csv" in propose
    assert "name: setup-data" in build and "name: setup-data" in propose
    assert "persist-credentials: false" in build and "persist-credentials: false" in propose


DATA_FILES = sorted(path.name for path in (ROOT / "src" / "twobells" / "data").glob("*.csv"))
ARTEFACT_CHECK = "Check the artefact holds only the expected data files"
MIB = 1024 * 1024


def _check_artefact(folder: Path):
    """Runs the propose job's check of the artefact in `folder`, as the runner's bash -e would."""
    import shutil
    import subprocess

    script = _run_block(_jobs(DATA_REFRESH)["propose"], ARTEFACT_CHECK)
    return subprocess.run([shutil.which("bash") or "bash", "-e", "-c", script], cwd=folder,
                          capture_output=True, text=True)


def test_the_data_refresh_takes_the_data_files_it_builds_and_no_others():
    script = _run_block(_jobs(DATA_REFRESH)["propose"], ARTEFACT_CHECK)
    allowed = re.search(r"^ +(\S+(?: \| \S+)*)\) ;;$", script, re.M).group(1).split(" | ")
    assert sorted(allowed) == DATA_FILES
    assert len(DATA_FILES) == 4  # read from the data folder, not empty


@pytest.mark.parametrize("names", [DATA_FILES, DATA_FILES[:1]], ids=["all", "one"])
def test_the_data_refresh_lets_through_the_files_that_changed(tmp_path, names):
    for name in names:
        (tmp_path / name).write_bytes((ROOT / "src" / "twobells" / "data" / name).read_bytes())
    (tmp_path / names[0]).write_bytes(b"x" * MIB)  # the largest it takes
    result = _check_artefact(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name, kind", [
    ("evil.sh", "file"), (".gb_stations.csv", "file"), ("gb_stations.csv.bak", "file"),
    ("sub/gb_stations.csv", "file"), ("gb_stations.csv", "symlink"), ("london_stops.csv", "folder"),
    ("metrolink_stops.csv", "empty"), ("naptan_area_grid.csv", "too big"), ("$(id)\n::warning::x.csv", "file"),
])
def test_the_data_refresh_refuses_an_artefact_holding_anything_else(tmp_path, name, kind):
    target = tmp_path / name
    target.parent.mkdir(exist_ok=True)
    if kind == "file":
        target.write_text("name,crs\n")
    elif kind == "symlink":
        target.symlink_to("/etc/hostname")
    elif kind == "folder":
        target.mkdir()
    else:
        target.write_bytes(b"" if kind == "empty" else b"x" * (MIB + 1))
    # Beside a good file, so it is this one that is refused.
    good = next(other for other in DATA_FILES if other != name)
    (tmp_path / good).write_text("name,crs\n")
    result = _check_artefact(tmp_path)
    assert result.returncode != 0
    assert result.stdout.startswith("::error::") and result.stdout.count("\n") == 1  # one line, the name escaped


def test_the_data_refresh_refuses_an_empty_artefact(tmp_path):
    result = _check_artefact(tmp_path)
    assert result.returncode != 0 and result.stdout.startswith("::error::")


def test_ci_called_tests_the_commit_it_is_given_in_every_job_with_a_read_only_token():
    on = CI[CI.index("\non:\n"):CI.index("\npermissions:")]
    # Pushes to main, pull requests and the weekly run start it as before; data.yml calls it with a commit.
    assert on.startswith("\non:\n  push:\n    branches: [main]\n  pull_request:\n  schedule:\n")
    called = on[on.index("\n  workflow_call:\n"):]
    assert "\n      ref:\n" in called and "\n        type: string\n" in called and "\n        required: true\n" in called
    jobs = _jobs(CI)
    assert list(jobs) == ["lint", "test", "package", "docker"]
    for name, job in jobs.items():
        # Each job checks out the commit given (none given: the one the run started on), keeping no credentials.
        assert job.count("actions/checkout@") == 1, name
        checkout = re.search(r"- uses: actions/checkout@[0-9a-f]{40} # v\S+\n        with:\n((?:          .*\n)+)", job)
        assert [line.strip() for line in checkout.group(1).splitlines()] == [
            "ref: ${{ inputs.ref }}", "persist-credentials: false"], name
        assert _permissions(job) == ["contents: read"], name
    assert "\npermissions: {}\n" in CI and not re.search(r": write\b", CI) and "secrets." not in CI
    # The input reaches checkout and the concurrency group, never a script. Called, github.ref is the
    # caller's (main), so the commit names the group: main's own runs neither wait for it nor cancel it.
    assert [line.strip() for line in CI.splitlines() if "inputs." in line] == [
        "group: ci-${{ inputs.ref || github.ref }}", *["ref: ${{ inputs.ref }}"] * 4]
    # Nothing is published, however it starts: the image is built, not pushed, and nothing signs in.
    assert "push: false" in jobs["docker"] and "push: true" not in CI and "login" not in CI
    assert "upload-artifact" not in CI


def test_the_data_refresh_runs_ci_on_its_pull_request_read_only_and_without_secrets():
    jobs = _jobs(DATA_REFRESH)
    propose, ci, report = jobs["propose"], jobs["ci"], jobs["report"]
    # propose hands on the pull request it opened and the commit it pushed, as the action gives them.
    assert re.search(r"- name: Open a pull request\n        id: pr\n        uses: peter-evans/create-pull-request@", propose)
    assert "      pull-request: ${{ steps.pr.outputs.pull-request-number }}\n" in propose
    assert "      sha: ${{ steps.pr.outputs.pull-request-head-sha }}\n" in propose
    # ci: only while a pull request is open, on its commit, from the workflow in the commit this run is on.
    assert _needs(ci) == {"propose"}
    assert "    if: needs.propose.outputs.pull-request != ''\n" in ci
    assert re.search(r"^    uses: [.$]/\.github/workflows/ci\.yml\n", ci, re.M)
    assert "      ref: ${{ needs.propose.outputs.sha }}\n" in ci
    # A called workflow's jobs get no more than the calling job's token, so this one may only read.
    assert _permissions(ci) == ["contents: read"]
    assert "secrets:" not in DATA_REFRESH and "secrets." not in DATA_REFRESH  # nothing passed on, not even inherit
    # report: may set statuses and nothing else; every outcome, on the commit ci was given.
    assert _needs(report) == {"propose", "ci"}
    assert "    if: always() && needs.propose.outputs.pull-request != ''\n" in report
    assert _permissions(report) == ["statuses: write"]
    assert "          SHA: ${{ needs.propose.outputs.sha }}\n" in report
    assert "          RESULT: ${{ needs.ci.result }}\n" in report
    # It checks nothing out, uses no action and runs nothing of the project's; what it is given comes in by env.
    assert not re.search(r"^ +(- )?uses:", report, re.M)
    for word in ("mise", "pip", "python", "uv ", "tools/", "npm", "node", "./", "git "):
        assert word not in report, word
    assert "${{" not in _run_block(report, CI_REPORT)


CI_REPORT = "Set the CI status on the pull request's commit"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _report_ci(tmp_path: Path, sha: str, result: str):
    """Runs the report job's script as the runner would, with a gh that writes down what it was asked."""
    import shutil
    import subprocess

    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "gh").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_ARGS"\n')
    (folder / "gh").chmod(0o755)
    asked = tmp_path / "asked"
    env = {"PATH": f"{folder}:/usr/bin:/bin", "GH_ARGS": str(asked), "GH_TOKEN": "token", "SHA": sha, "RESULT": result,
           "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "42"}
    script = _run_block(_jobs(DATA_REFRESH)["report"], CI_REPORT)
    run = subprocess.run([shutil.which("bash") or "bash", "-e", "-o", "pipefail", "-c", script], env=env,
                         capture_output=True, text=True)
    return run, asked.read_text().splitlines() if asked.exists() else None


@pytest.mark.parametrize("result, state", [
    ("success", "success"), ("failure", "failure"), ("cancelled", "error"), ("skipped", "error"),
])
def test_the_data_refresh_puts_ci_s_result_on_the_commit_ci_tested(tmp_path, result, state):
    run, asked = _report_ci(tmp_path, COMMIT, result)
    assert run.returncode == 0, run.stdout + run.stderr
    assert asked[:4] == ["api", "--method", "POST", f"repos/owner/repo/statuses/{COMMIT}"]
    fields = dict(field.split("=", 1) for flag, field in zip(asked[4::2], asked[5::2], strict=True) if flag == "-f")
    assert fields.pop("description")
    assert fields == {"state": state, "context": "CI", "target_url": "https://github.com/owner/repo/actions/runs/42"}


@pytest.mark.parametrize("sha", [
    "", "main", COMMIT[:-1], COMMIT + "0", COMMIT.upper(), f"{COMMIT}\n", f"{COMMIT}/statuses/x", "../../../user/repos",
])
def test_the_data_refresh_reports_nothing_for_anything_but_a_full_commit_sha(tmp_path, sha):
    run, asked = _report_ci(tmp_path, sha, "success")
    assert run.returncode != 0 and run.stdout.startswith("::error::")
    assert asked is None


def test_dependabot_updates_uv_lock_but_leaves_ruff_in_step_with_mise():
    import tomllib

    entries = (ROOT / ".github" / "dependabot.yml").read_text().split("\n  - package-ecosystem: ")
    uv = next(entry for entry in entries if entry.startswith("uv\n"))
    assert "\n    directory: /\n" in uv and "\n      interval: weekly\n" in uv and "\n      default-days: 7\n" in uv
    assert "\n    ignore:\n      - dependency-name: ruff\n" in uv
    # The one pin Dependabot can't move alone: pyproject's and uv.lock's ruff are mise.toml's.
    ruff = tomllib.loads((ROOT / "mise.toml").read_text())["tools"]["ruff"]
    assert f"ruff=={ruff}" in tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    assert [package["version"] for package in lock["package"] if package["name"] == "ruff"] == [ruff]


def _upgrade(*args: str, path: Path | None = None):
    """Runs upgrade.sh, with only the folder `path` on PATH if given."""
    import os
    import shutil
    import subprocess

    env = None if path is None else {**os.environ, "PATH": str(path)}
    return subprocess.run([shutil.which("bash") or "bash", str(PI / "upgrade.sh"), *args],
                          capture_output=True, text=True, env=env)


def _sourced(call: str, *args: str, path: Path | None = None):
    """Runs `call` in bash with upgrade.sh sourced (so its main doesn't run), with only `path` on PATH if given."""
    import os
    import shutil
    import subprocess

    env = None if path is None else {**os.environ, "PATH": str(path)}
    return subprocess.run([shutil.which("bash") or "bash", "-c", f'source "$1"; shift; {call}', "bash",
                           str(PI / "upgrade.sh"), *args], capture_output=True, text=True, env=env)


def _fake_cosign(tmp_path: Path, status: int = 0) -> Path:
    """A folder holding a cosign that writes its arguments, one a line, to `args` beside it and exits `status`."""
    folder = tmp_path / "bin"
    folder.mkdir()
    cosign = folder / "cosign"
    cosign.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "${{0%/*}}/args"\nexit {status}\n')
    cosign.chmod(0o755)
    return folder


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


def test_upgrade_needs_root_after_the_version(tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("run as root, it would go on to download")
    done = _upgrade("0.2.0", path=_fake_cosign(tmp_path))
    assert (done.returncode, done.stderr) == (1, "Run with sudo\n")


def test_upgrade_refuses_without_cosign_before_doing_anything(tmp_path):
    from tests.test_docs import anchors

    (tmp_path / "bin").mkdir()
    done = _upgrade("0.2.0", path=tmp_path / "bin")
    assert done.returncode == 1 and done.stdout == ""
    assert "Run with sudo" not in done.stderr  # stopped before going on
    assert "--no-signature-check" in done.stderr and "install-pi.md#install-cosign" in done.stderr
    # The link is to this project's guide, at a heading it has.
    assert f"DOCS=https://github.com/{REPO}/blob/main/docs\n" in _script("upgrade.sh")
    assert "install-cosign" in anchors(ROOT / "docs" / "install-pi.md")


@pytest.mark.parametrize("args", [["--no-signature-check"], ["--config", "x", "--no-signature-check"],
                                  ["--no-signature-check", "--secrets", "y"]])
def test_upgrade_without_cosign_goes_on_when_told_and_says_so(tmp_path, args):
    import os

    if os.geteuid() == 0:
        pytest.skip("run as root, it would go on to download")
    (tmp_path / "bin").mkdir()
    done = _upgrade("0.2.0", *args, path=tmp_path / "bin")
    assert (done.returncode, done.stderr) == (1, "Run with sudo\n")  # past the cosign check
    assert "--no-signature-check" in done.stdout and "SHA-256" in done.stdout


def test_upgrade_checks_the_signature_as_the_release_workflow_made_it(tmp_path):
    done = _sourced('check_signature "$1" 0.2.0', "/work", path=_fake_cosign(tmp_path))
    assert done.returncode == 0
    args = (tmp_path / "bin" / "args").read_text().splitlines()
    assert args == ["verify-blob", "/work/SHA256SUMS", "--bundle", "/work/SHA256SUMS.sigstore.json",
                    "--certificate-identity", _identity("v0.2.0"),
                    "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com"]


def test_upgrade_refuses_a_signature_cosign_doesnt_accept(tmp_path):
    done = _sourced('check_signature "$1" 0.2.0', "/work", path=_fake_cosign(tmp_path, status=1))
    assert done.returncode == 1 and "signature doesn't check out" in done.stderr


def test_upgrade_checks_the_signature_before_unpacking_and_keeps_its_own_option_from_update():
    script = _script("upgrade.sh")
    main = script[script.index("main() {"):script.index("\n}\n", script.index("main() {"))]
    # Without cosign, it stops before downloading; with it, the signature is checked before the sum.
    assert main.index('signature_check_ready "$signature"') < main.index('fetch "$base/$name"')
    assert main.index('fetch "$base/SHA256SUMS.sigstore.json"') < main.index('check_signature "$work"')
    assert main.index('check_signature "$work"') < main.index('check_tarball "$work"') < main.index("tar -xzf")
    # update.sh gets the other options, not --no-signature-check, which it would refuse.
    assert 'bash "$unpacked/deploy/pi/update.sh" "${options[@]}"' in main
    assert main.count('"$@"') == 1 and 'for option in "$@"; do' in main


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
    assert 'bash "$unpacked/deploy/pi/update.sh" "${options[@]}"' in script
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
    # And not unless cosign says the release workflow signed SHA256SUMS first, in the same block.
    signature = block[block.index("cosign verify-blob "):block.rindex("sha256sum ")]
    assert signature.startswith("cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \\\n")
    assert signature.rstrip().endswith(" &&") and signature.count("&&") == 1
    assert block.index('curl -fLO "$base/SHA256SUMS.sigstore.json"') < block.index("cosign verify-blob ")
    assert f'--certificate-identity "{_identity("v$VERSION")}"' in signature
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in signature
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in PI_GUIDE
    assert "sudo ./deploy/pi/upgrade.sh 0.2.0" in PI_GUIDE
    assert "sudo ./deploy/pi/upgrade.sh 0.2.0 --no-signature-check" in PI_GUIDE
    assert "| sudo bash" not in PI_GUIDE


def test_the_release_notes_check_the_signature_before_the_sum():
    notes = RELEASE[RELEASE.index('cat > "$RUNNER_TEMP/notes.md" <<EOF'):RELEASE.index("\n          EOF\n")]
    pi = notes[notes.index("## Install on a Raspberry Pi"):notes.index("## Container")]
    for name in ("twobells-$VERSION.tar.gz", "SHA256SUMS", "SHA256SUMS.sigstore.json"):
        assert f"curl -fLO $base/{name}\n" in pi
    chain = pi[pi.index("cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json"):]
    steps = [step.strip() for step in chain[:chain.index("\\`\\`\\`")].split("&&")]
    assert steps[0].startswith("cosign verify-blob") and "--certificate-identity $identity" in steps[0]
    assert steps[1:] == ["sha256sum --check --ignore-missing SHA256SUMS", "tar -xzf twobells-$VERSION.tar.gz",
                         "cd twobells-$VERSION", "sudo ./deploy/pi/install.sh"]
    assert "docs/install-pi.md#install-cosign" in pi


def test_the_guide_installs_the_cosign_the_release_signs_with_checked_against_its_sums():
    signing = set(re.findall(r"cosign-release: (v\S+)", RELEASE))
    block = re.search(r"```bash\nCOSIGN=(\S+)\n(.*?)```", PI_GUIDE, re.S)
    assert signing == {f"v{block.group(1)}"}
    steps = [step.strip() for step in block.group(2)[block.group(2).index("sha256sum "):].split("&&")]
    assert steps[0] == "sha256sum --check --ignore-missing cosign_checksums.txt"
    assert steps[1].startswith('sudo apt-get install -y "./$deb"')
    for url in re.findall(r'curl -fLO "(\S+)"', block.group(2)):
        assert url.startswith("https://github.com/sigstore/cosign/releases/download/v$COSIGN/"), url


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
