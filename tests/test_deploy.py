"""Deployment security settings (the dashboard container, the Pi's units), so an edit can't quietly drop one."""

import json
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "docker-compose.yml").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text()
REQUIREMENTS = (ROOT / "deploy" / "requirements-docker.txt").read_text()


def test_base_image_is_pinned_by_digest():
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", DOCKERFILE, re.M)


def test_dependencies_come_from_hashed_wheels_only():
    assert "--require-hashes" in DOCKERFILE and "--only-binary=:all:" in DOCKERFILE
    pins = re.findall(r"^([a-z0-9-]+)==\S+ \\$", REQUIREMENTS, re.M)
    assert pins == ["pillow", "tzdata"]
    for block in re.split(r"\n(?=[a-z])", REQUIREMENTS.split("\n", 2)[2]):
        if "==" in block:
            assert re.search(r"--hash=sha256:[0-9a-f]{64}", block)


def test_it_runs_as_a_non_root_user_and_checks_its_health():
    assert re.search(r"^USER 10001:10001$", DOCKERFILE, re.M)
    assert "HEALTHCHECK" in DOCKERFILE and "/healthz" in DOCKERFILE
    assert "ADD " not in DOCKERFILE  # COPY only: nothing fetched by URL


def test_the_image_never_carries_keys_or_state():
    assert DOCKERIGNORE.splitlines()[1] == "*"  # everything out, then only what's needed back in
    allowed = [line for line in DOCKERIGNORE.splitlines() if line.startswith("!")]
    assert allowed == ["!src/", "!deploy/requirements-docker.txt"]
    assert "secrets" not in DOCKERFILE.replace("--secrets", "")


def test_compose_locks_the_container_down():
    for setting in ("read_only: true", "- ALL", "no-new-privileges:true", "mem_limit:", "pids_limit:"):
        assert setting in COMPOSE, setting
    assert "./config.toml:/config/config.toml:ro" in COMPOSE
    for risky in ("privileged", "network_mode", "docker.sock", "cap_add"):
        assert risky not in COMPOSE, risky


def _entrypoint() -> list[str]:
    found = re.search(r"^ENTRYPOINT (\[.*?\])$", DOCKERFILE, re.M | re.S).group(1)
    return json.loads(found.replace("\\\n", ""))


def test_the_container_serves_as_a_docker_install_in_its_one_writable_folder():
    from departureboard.cli import _parser

    entrypoint = _entrypoint()
    assert entrypoint[:3] == ["python", "-m", "departureboard.cli"]
    args = _parser().parse_args(entrypoint[3:])
    assert (args.command, args.install, args.state_dir, args.host, args.port) == ("serve", "docker", "/data",
                                                                                  "0.0.0.0", 8090)
    assert (args.config, args.secrets) == ("/config/config.toml", "/nonexistent")
    assert "EXPOSE 8090" in DOCKERFILE and "127.0.0.1:8090/healthz" in DOCKERFILE


def test_the_documented_password_commands_fit_the_container():
    # Run with docker exec as the image's user, in the one writable place: /data.
    from departureboard.cli import _parser

    assert "container_name: departureboard-dashboard" in COMPOSE and "dashboard-data:/data" in COMPOSE
    assert "WORKDIR /data" in DOCKERFILE and "chown 10001:10001 /data" in DOCKERFILE
    docs = (ROOT / "docs" / "USER_GUIDE.md").read_text() + (ROOT / "README.md").read_text()
    for flag in ("--set-password", "--reset-code"):
        command = f"docker exec -it departureboard-dashboard python -m departureboard.cli dashboard {flag} --state-dir /data"
        assert docs.count(command) == 2, flag  # once in each
        # The old command, kept as a hidden alias of serve, in the container's store.
        args = _parser().parse_args(shlex.split(command)[7:])
        assert (args.state_dir, getattr(args, flag[2:].replace("-", "_"))) == ("/data", True)
    from departureboard.installs import install_named

    for flag, command in (("--set-password", install_named("docker").password_command),
                          ("--reset-code", install_named("docker").reset_command)):
        assert docs.count(command) == 2, flag  # what the settings pages tell the owner to run


def test_keys_file_is_git_ignored():
    assert "dashboard.env" in (ROOT / ".gitignore").read_text().splitlines()


# The Pi's units: the settings page stays off the internet; the setup service goes out for it.


def _unit(name: str) -> dict[str, list[str]]:
    settings: dict[str, list[str]] = {}
    for line in (ROOT / "deploy" / name).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            settings.setdefault(key, []).append(value)
    return settings


def test_the_settings_page_stays_on_the_home_network_and_hands_setup_on():
    web = _unit("departureboard-web.service")
    assert web["IPAddressDeny"] == ["any"]
    assert web["ExecStart"][0].endswith(" --setup-via-spool")
    assert "departureboard-setup.path" in web["Wants"]


def test_the_setup_service_goes_out_and_nothing_more():
    from departureboard import setupspool

    unit = _unit("departureboard-setup.service")
    assert "IPAddressDeny" not in unit and "IPAddressAllow" not in unit
    assert unit["ExecStart"] == [
        "/opt/departureboard/venv/bin/departureboard setup serve --config /etc/departureboard/config.toml"]
    assert unit["Type"] == ["oneshot"] and unit["User"] == ["departureboard"] and "TimeoutStartSec" in unit
    assert unit["StateDirectory"] == ["departureboard"]
    assert unit["EnvironmentFile"] == ["/etc/departureboard/secrets.env"]
    timetable = _unit("departureboard-timetable.service")
    hardening = ("NoNewPrivileges", "CapabilityBoundingSet", "ProtectSystem", "ProtectHome", "PrivateTmp",
                 "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups",
                 "RestrictAddressFamilies", "RestrictNamespaces", "RestrictSUIDSGID", "LockPersonality",
                 "MemoryDenyWriteExecute", "SystemCallFilter", "MemoryMax", "UMask")
    for setting in hardening:
        assert unit[setting] == timetable[setting], setting
    path = _unit("departureboard-setup.path")
    assert path["DirectoryNotEmpty"] == [f"{setupspool.RUNTIME}/requests"]
    assert path["Unit"] == ["departureboard-setup.service"]


def test_the_setup_spool_is_in_memory_and_shared_by_both_units():
    from departureboard import setupspool

    spool = {"RuntimeDirectory": ["departureboard-setup"], "RuntimeDirectoryMode": ["0700"],
             "RuntimeDirectoryPreserve": ["yes"]}  # kept when either stops
    web, setup = _unit("departureboard-web.service"), _unit("departureboard-setup.service")
    for unit in (web, setup):
        assert {key: unit.get(key) for key in spool} == spool
    assert web["User"] == setup["User"] == ["departureboard"]  # so both can open the 0700 folder
    assert setupspool.RUNTIME == Path("/run") / spool["RuntimeDirectory"][0]
    watched = _unit("departureboard-setup.path")["DirectoryNotEmpty"][0]
    assert watched.startswith("/run/departureboard-setup/") and "/var/lib" not in watched


def test_a_setup_service_that_keeps_stopping_at_once_is_given_up_on_not_looped():
    # Each run waits a minute for more, so ten starts in two minutes is a loop.
    for name in ("departureboard-setup.service", "departureboard-setup.path"):
        unit = _unit(name)
        assert unit["StartLimitIntervalSec"] == ["2min"] and unit["StartLimitBurst"] == ["10"], name
    guide = (ROOT / "docs" / "USER_GUIDE.md").read_text()
    assert "systemctl reset-failed departureboard-setup.service departureboard-setup.path" in guide


def test_install_and_update_set_up_the_setup_service():
    for script in ("install.sh", "update.sh"):
        text = (ROOT / "deploy" / script).read_text()
        assert "departureboard-setup.service" in text and "departureboard-setup.path" in text, script
    assert "systemctl enable --now departureboard-setup.path" in (ROOT / "deploy" / "install.sh").read_text()
    update = (ROOT / "deploy" / "update.sh").read_text()
    # Onto the folder in memory, and the old spool on the SD card removed.
    assert "systemctl enable departureboard-setup.path\nsystemctl restart departureboard-setup.path" in update
    assert "rm -rf /var/lib/departureboard/setup-spool\n" in update


def _timetable_question(conf: Path) -> list[str]:
    """The departureboard command update.sh asks whether to build the bus
    timetable, as it would run with its config folder at `conf`."""
    update = (ROOT / "deploy" / "update.sh").read_text()
    [line] = [line for line in update.splitlines() if line.startswith("if ") and " timetable " in line]
    words = shlex.split(line.removeprefix("if ").removesuffix("; then"))
    # Asked as the service user, not as root.
    assert words[:4] == ["runuser", "-u", "departureboard", "--"], words
    assert words[4] == "$PREFIX/venv/bin/departureboard"
    return [word.replace("$CONF", str(conf)) for word in words[5:]]


@pytest.mark.parametrize("config, saved, wanted", [
    ('[bus]\nsource = "bods"\n', "", True),
    ("[bus]\n", '[bus]\nsource = "bods"\n', True),  # chosen on the settings page or in the wizard
    ('[bus]\nsource = "bods"\n', "[bus]\nenabled = false\n", False),  # buses switched off since
    ("[bus]\n", "", False),  # TransportAPI
    ('[train]\ncrs = "GLO"\n', "", False),  # no buses at all
])
def test_update_builds_the_bus_timetable_only_when_the_settings_want_it(tmp_path, config, saved, wanted):
    from departureboard.cli import main

    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "config.toml").write_text(f'state_file = "{state / "state.json"}"\n{config}')
    if saved:
        (state / "settings.toml").write_text(saved)
    update = (ROOT / "deploy" / "update.sh").read_text()
    assert 'source[[:space:]]*=' not in update  # not by reading config.toml alone
    assert (main(_timetable_question(tmp_path)) == 0) == wanted


SERVICES = ("departureboard.service", "departureboard-screen.service", "departureboard-web.service",
            "departureboard-timetable.service", "departureboard-setup.service")


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
    assert unit.get("ProcSubset") == (None if name == "departureboard.service" else ["pid"])
