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


def test_the_pi_builds_the_code_with_hashed_tools_and_fetches_nothing_else():
    # Build isolation would fetch whatever setuptools is newest, unpinned, and
    # run it as root; the build instead uses the hashed one, put in the venv first.
    build_tools = (ROOT / "deploy" / "requirements-build.txt").read_text()
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
    assert allowed == ["!src/", "!pyproject.toml", "!deploy/requirements-docker.txt"]
    assert "secrets" not in DOCKERFILE.replace("--secrets", "")


def test_compose_locks_the_container_down():
    for setting in ("read_only: true", "- ALL", "no-new-privileges:true", "mem_limit:", "pids_limit:"):
        assert setting in COMPOSE, setting
    for risky in ("privileged", "network_mode", "docker.sock", "cap_add"):
        assert risky not in COMPOSE, risky


def test_compose_runs_departureboard_on_the_dashboards_old_volume():
    lines = [line.split("#")[0].strip() for line in COMPOSE.splitlines()]
    for line in ("departureboard:", "image: departureboard:local", "container_name: departureboard",
                 "- secrets.env", "- dashboard-data:/data", "dashboard-data:", "- \"8090:8090\""):
        assert line in lines, line
    assert lines[lines.index("env_file:") + 1] == "- secrets.env"
    # The volume keeps its name, or a NAS would start on a new, empty one.
    assert re.search(r"^volumes:\n  dashboard-data:\n", COMPOSE, re.M)
    # config.toml only through the optional override, which stays out of git.
    assert "config.toml:" not in COMPOSE and "dashboard.env" not in COMPOSE
    override = (ROOT / "docker-compose.override.yml.example").read_text()
    assert "services:\n  departureboard:\n    volumes:\n      - ./config.toml:/config/config.toml:ro\n" in override
    assert "docker-compose.override.yml" in (ROOT / ".gitignore").read_text().splitlines()
    assert "/config/config.toml" in _entrypoint()  # where the override puts it


def test_the_image_knows_its_version_without_installing_the_package():
    # Nothing is installed, so version.py reads pyproject.toml beside src/; the build fails without it.
    assert "PYTHONPATH=/app/src" in DOCKERFILE
    assert re.search(r"^COPY pyproject\.toml /app/pyproject\.toml$", DOCKERFILE, re.M)
    assert "assert v() != 'unknown'" in DOCKERFILE


def test_the_image_runs_as_departureboard_with_the_volumes_uid():
    assert re.search(r"useradd --system --uid 10001 .* departureboard \\$", DOCKERFILE, re.M)
    assert "chown 10001:10001 /data" in DOCKERFILE


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

    assert "container_name: departureboard\n" in COMPOSE and "dashboard-data:/data" in COMPOSE
    assert "WORKDIR /data" in DOCKERFILE and "chown 10001:10001 /data" in DOCKERFILE
    docs = (ROOT / "docs" / "USER_GUIDE.md").read_text() + (ROOT / "README.md").read_text()
    for flag in ("--set-password", "--reset-code"):
        command = f"docker exec -it departureboard python -m departureboard.cli serve {flag} --state-dir /data"
        assert docs.count(command) == 2, flag  # once in each
        words = shlex.split(command)
        assert words[:7] == ["docker", "exec", "-it", "departureboard", "python", "-m", "departureboard.cli"]
        args = _parser().parse_args(words[7:])
        assert (args.command, args.state_dir, getattr(args, flag[2:].replace("-", "_"))) == ("serve", "/data", True)
    from departureboard.settings.installs import install_named

    docker = install_named("docker")
    for flag, command in (("--set-password", docker.password_command), ("--reset-code", docker.reset_command)):
        assert docs.count(command) == 2, flag  # what the settings pages tell the owner to run
    assert "docker logs departureboard<" in docker.log_help and "Container, departureboard, Log" in docker.log_help


def test_a_desktop_says_where_its_log_is_however_it_runs():
    from departureboard.settings.installs import install_named

    desktop = install_named("desktop").log_help
    # In a terminal, or started at login by the files in deploy/desktop.
    plist = (ROOT / "deploy" / "desktop" / "uk.departureboard.serve.plist").read_text()
    assert "Terminal" in desktop and "~/Library/Logs/departureboard.log" in desktop
    assert "/Library/Logs/departureboard.log" in plist
    assert "journalctl --user -u departureboard" in desktop


def test_keys_files_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "secrets.env" in ignored  # anywhere: the Pi's copy, Docker's next to docker-compose.yml
    assert "dashboard.env" in ignored  # Docker's old name, until every NAS has moved it


def test_one_key_template_for_the_pi_and_docker_in_groups():
    assert not (ROOT / "deploy" / "dashboard.env.example").exists()
    template = (ROOT / "deploy" / "secrets.env.example").read_text()
    names = re.findall(r"^([A-Z_]+)=$", template, re.M)  # every value empty
    assert names == ["RDM_API_KEY", "BODS_API_KEY", "TAPI_APP_ID", "TAPI_APP_KEY", "RDM_DETAILS_API_KEY",
                     "RDM_KB_API_KEY"]
    groups = [template.index(heading) for heading in ("# General:", "# Board:", "# Dashboard:")]
    places = [template.index(f"\n{name}=") for name in names]
    assert groups[0] < places[0] < places[1] < groups[1] < places[2] < places[3] < groups[2] < places[4]
    assert "/etc/departureboard/secrets.env" in template and "next to docker-compose.yml" in template
    install = (ROOT / "deploy" / "install.sh").read_text()
    assert '"$REPO/deploy/secrets.env.example" "$CONF/secrets.env"' in install


# The Pi's units: the web server stays off the internet; the fetch service goes out for it.


def _unit(name: str) -> dict[str, list[str]]:
    settings: dict[str, list[str]] = {}
    for line in (ROOT / "deploy" / name).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            settings.setdefault(key, []).append(value)
    return settings


def test_the_settings_page_stays_on_the_home_network_and_hands_its_internet_work_on():
    web = _unit("departureboard-web.service")
    assert web["IPAddressDeny"] == ["any"]  # with the dashboard view on too: its feeds come from the fetch service
    assert web["IPAddressAllow"] == ["localhost link-local 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 fc00::/7"]
    assert "departureboard-fetch.path" in web["Wants"]


def test_the_pi_serves_its_settings_at_8090_and_nothing_at_8080():
    from departureboard.cli import WEB_PORT, _parser, setup_url
    from departureboard.settings.installs import install_named

    web = _unit("departureboard-web.service")
    assert web["ExecStart"] == ["/opt/departureboard/venv/bin/departureboard serve --install pi "
                                "--config /etc/departureboard/config.toml --port 8090"]
    args = _parser().parse_args(shlex.split(web["ExecStart"][0])[1:])
    assert (args.command, args.install, args.config, args.port, args.host, args.lan) == (
        "serve", "pi", "/etc/departureboard/config.toml", 8090, None, False)
    assert install_named("pi").host == "0.0.0.0"  # every address, kept to the home network above
    assert WEB_PORT == 8090 and setup_url("kitchen") == "http://kitchen.local:8090/settings"  # what the board shows
    # No second listener on the old port: bookmarks to it break, on purpose.
    assert "8080" not in (ROOT / "deploy" / "departureboard-web.service").read_text()
    assert _parser().parse_args(["web"]).port == 8090  # the old command, hidden, on the new port


def test_the_settings_service_has_room_for_every_connection_the_server_takes():
    # A thread per connection, and a few of the server's own; past TasksMax a
    # connection can't be answered at all.
    from departureboard.web import server as dashserver

    web = _unit("departureboard-web.service")
    assert int(web["TasksMax"][0]) >= max(64, dashserver.MAX_CONNECTIONS + 8)
    assert web["MemoryMax"] == ["128M"]


def test_the_fetch_service_goes_out_and_listens_on_nothing():
    from departureboard.setup import spool as setupspool

    unit = _unit("departureboard-fetch.service")
    assert "IPAddressDeny" not in unit and "IPAddressAllow" not in unit
    assert unit["SocketBindDeny"] == ["any"]
    assert unit["ExecStart"] == [
        "/opt/departureboard/venv/bin/departureboard fetch --config /etc/departureboard/config.toml"]
    assert unit["Type"] == ["simple"] and unit["User"] == ["departureboard"]
    assert unit["Restart"] == ["on-failure"] and unit["RestartSec"] == ["10"]
    assert unit["StateDirectory"] == ["departureboard"]
    assert unit["EnvironmentFile"] == ["/etc/departureboard/secrets.env"]
    assert unit["WantedBy"] == ["multi-user.target"]  # at boot, so the dashboard's feeds start with the Pi
    assert int(unit["TasksMax"][0]) >= 32
    timetable = _unit("departureboard-timetable.service")
    hardening = ("NoNewPrivileges", "CapabilityBoundingSet", "ProtectSystem", "ProtectHome", "PrivateTmp",
                 "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups",
                 "RestrictAddressFamilies", "RestrictNamespaces", "RestrictSUIDSGID", "LockPersonality",
                 "MemoryDenyWriteExecute", "SystemCallFilter", "MemoryMax", "UMask")
    for setting in hardening:
        assert unit[setting] == timetable[setting], setting
    path = _unit("departureboard-fetch.path")
    assert path["DirectoryNotEmpty"] == [f"{setupspool.RUNTIME}/requests"]
    assert path["Unit"] == ["departureboard-fetch.service"]


def test_the_setup_units_are_gone():
    for name in ("departureboard-setup.service", "departureboard-setup.path"):
        assert not (ROOT / "deploy" / name).exists(), name


def test_the_spool_is_in_memory_and_shared_by_both_units():
    from departureboard.setup import spool as setupspool

    spool = {"RuntimeDirectory": ["departureboard-fetch"], "RuntimeDirectoryMode": ["0700"],
             "RuntimeDirectoryPreserve": ["yes"]}  # kept when either stops
    web, fetch = _unit("departureboard-web.service"), _unit("departureboard-fetch.service")
    for unit in (web, fetch):
        assert {key: unit.get(key) for key in spool} == spool
    assert web["User"] == fetch["User"] == ["departureboard"]  # so both can open the 0700 folder
    assert Path("/run") / spool["RuntimeDirectory"][0] == setupspool.RUNTIME
    watched = _unit("departureboard-fetch.path")["DirectoryNotEmpty"][0]
    assert watched.startswith("/run/departureboard-fetch/") and "/var/lib" not in watched


def test_a_fetch_service_that_keeps_stopping_at_once_is_given_up_on_not_looped():
    # With the dashboard off, each run waits a minute for more, so ten starts in two minutes is a loop.
    for name in ("departureboard-fetch.service", "departureboard-fetch.path"):
        unit = _unit(name)
        assert unit["StartLimitIntervalSec"] == ["2min"] and unit["StartLimitBurst"] == ["10"], name
    guide = (ROOT / "docs" / "USER_GUIDE.md").read_text()
    assert "systemctl reset-failed departureboard-fetch.service departureboard-fetch.path" in guide


def test_install_and_update_set_up_the_fetch_service():
    install = (ROOT / "deploy" / "install.sh").read_text()
    update = (ROOT / "deploy" / "update.sh").read_text()
    for text in (install, update):
        assert "departureboard-fetch.service" in text and "departureboard-fetch.path" in text
    assert '"$REPO/deploy/departureboard-setup' not in install
    assert "systemctl enable --now departureboard-fetch.path departureboard-fetch.service" in install
    # The setup units go, the fetch units come in their place, then the old spools go.
    order = [
        "systemctl disable --now departureboard-setup.path departureboard-setup.service",
        "rm -f /etc/systemd/system/departureboard-setup.service /etc/systemd/system/departureboard-setup.path\n",
        "systemctl enable departureboard-fetch.path departureboard-fetch.service\n",
        "systemctl restart departureboard-fetch.path departureboard-fetch.service\n",
        "systemctl try-restart departureboard-web.service\n",
        "rm -rf /run/departureboard-setup /var/lib/departureboard/setup-spool\n",
    ]
    places = [update.index(step) for step in order]
    assert places == sorted(places)
    units = re.search(r"for unit in (.*?); do", update, re.S).group(1)
    assert "departureboard-fetch.service" in units and "departureboard-setup" not in units


SCRIPTS = ("install.sh", "update.sh")


def _script(name: str) -> str:
    return (ROOT / "deploy" / name).read_text()


def _run_lines(name: str) -> list[str]:
    """The script's systemctl commands: those it runs, not those it prints."""
    return [line.strip() for line in _script(name).splitlines()
            if line.strip().startswith("systemctl ") and not line.startswith("       ")]


def _query(name: str, what: str, conf: Path) -> list[str]:
    """The departureboard command line `query WHAT` runs in the script
    `name`, as it would with its config folder at `conf`."""
    [body] = re.findall(r"^query\(\) \{\n  (.*)\n\}$", _script(name), re.M)
    words = shlex.split(body)
    # Asked as the services' user, not as root.
    assert words[:4] == ["runuser", "-u", "departureboard", "--"], words
    assert words[4] == "$PREFIX/venv/bin/departureboard"
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
    timer = "if query needs-timetable >/dev/null; then\n  systemctl enable --now departureboard-timetable.timer\n"
    assert timer in script
    assert '"$(query display)"' in script


@pytest.mark.parametrize("name", SCRIPTS)
@pytest.mark.parametrize("config, saved, wanted", [
    ('[bus]\nsource = "bods"\n', "", True),
    ("[bus]\n", '[bus]\nsource = "bods"\n', True),  # chosen on the settings page or in the wizard
    ('[bus]\nsource = "bods"\n', "[bus]\nenabled = false\n", False),  # buses switched off since
    ("[bus]\n", "", False),  # TransportAPI
    ('[train]\ncrs = "GLO"\n', "", False),  # no buses at all
])
def test_install_and_update_keep_the_bus_timetable_only_when_the_settings_want_it(tmp_path, name, config, saved,
                                                                                    wanted):
    from departureboard.cli import main

    _settings(tmp_path, config, saved)
    assert (main(_query(name, "needs-timetable", tmp_path)) == 0) == wanted


def test_install_starts_the_settings_and_the_fetch_service_and_installs_every_unit():
    install = _script("install.sh")
    started = " ".join(line for line in _run_lines("install.sh") if line.startswith("systemctl enable --now "))
    for unit in ("departureboard-web.service", "departureboard-fetch.path", "departureboard-fetch.service",
                 "departureboard-timetable.timer"):
        assert unit in started, unit
    units = re.search(r"for unit in (.*?); do", install, re.S).group(1).replace("\\", " ").split()
    shipped = [path.name for path in (ROOT / "deploy").iterdir() if path.suffix in (".service", ".timer", ".path")]
    assert sorted(units) == sorted(shipped)


@pytest.mark.parametrize("saved, board", [("", True), ('[app]\nviews = ["dashboard"]\n', False)])
def test_install_names_the_boards_unit_only_while_its_view_is_on(tmp_path, capsys, saved, board):
    from departureboard.cli import main

    _settings(tmp_path, saved=saved)
    assert main(_query("install.sh", "views", tmp_path)) == 0
    assert ("board" in capsys.readouterr().out.splitlines()) == board
    install = _script("install.sh")
    assert 'if grep -qx board <<<"$views"; then\n  cat <<EOF\n  1. Start the board:\n' in install


@pytest.mark.parametrize("display, renderer", [
    ("", "departureboard.timer"),  # an Inky, unless config.toml says
    ('display = "waveshare-10in85g"\n', "departureboard.timer"),
    ('display = "hdmi-1280x400"\n', "departureboard-screen.service"),
])
def test_install_names_the_boards_unit_for_the_display_and_never_starts_it(tmp_path, capsys, display, renderer):
    import subprocess

    from departureboard.cli import main

    _settings(tmp_path, display)
    assert main(_query("install.sh", "display", tmp_path)) == 0
    said = capsys.readouterr().out.strip()
    [choice] = re.findall(r'^if \[\[ "\$display" == hdmi-\* \]\]; then\n  renderer=.*?\nfi$',
                          _script("install.sh"), re.M | re.S)
    shown = subprocess.run(["bash", "-c", f'display="$1"\n{choice}\necho "$renderer"', "bash", said],
                           capture_output=True, text=True, check=True).stdout
    assert shown == f"{renderer}\n"
    for line in _run_lines("install.sh"):  # only printed, for the owner to run
        assert "$renderer" not in line and "departureboard.timer" not in line and "screen" not in line, line


def test_update_never_enables_or_disables_the_board():
    for line in _run_lines("update.sh"):
        if line.startswith(("systemctl enable", "systemctl disable")):
            assert "departureboard.timer" not in line and "departureboard-screen" not in line, line


def test_update_restarts_a_running_screen_onto_the_new_code():
    # The screen's service runs for good; without a restart it keeps the old
    # code. try-restart leaves a stopped one stopped, so the board stays yours.
    assert "systemctl try-restart departureboard-screen.service" in _run_lines("update.sh")


SERVICES = ("departureboard.service", "departureboard-screen.service", "departureboard-web.service",
            "departureboard-timetable.service", "departureboard-fetch.service")


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


def test_install_and_update_give_the_settings_new_address():
    from departureboard.settings.installs import install_named

    install = (ROOT / "deploy" / "install.sh").read_text()
    update = (ROOT / "deploy" / "update.sh").read_text()
    assert "http://$(hostname).local:8090/settings" in install
    assert "The settings page moved to :8090/settings" in update
    for text in (install, update):
        assert ":8080" not in text and "departureboard web " not in text
    # The command the settings page gives for the password, as install.sh gives it.
    assert install_named("pi").password_command.replace("/opt/departureboard", "$PREFIX") in install


# Installing from a release: the Pi's tarball, upgrade.sh, the published image and the desktop files.

RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text()
IMAGE = re.search(r"^  IMAGE: (\S+)$", RELEASE, re.M).group(1)
REPO = IMAGE.removeprefix("ghcr.io/")
GUIDE = (ROOT / "docs" / "USER_GUIDE.md").read_text()
DOCKER_DOCS = (ROOT / "docs" / "DOCKER.md").read_text()
GHCR_COMPOSE = (ROOT / "docker-compose.ghcr.yml").read_text()


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
    needed |= {str(path.relative_to(ROOT)) for path in (ROOT / "src" / "departureboard").rglob("*")
               if path.is_file() and "__pycache__" not in path.parts}
    for name in ("install.sh", "update.sh"):
        script = _script(name)
        needed |= {path for path in re.findall(r"\$REPO/([\w./-]+)", script) if not path.endswith(("/", "."))}
        units = re.search(r"for unit in (.*?); do", script, re.S).group(1).replace("\\", " ").split()
        needed |= {f"deploy/{unit}" for unit in units}
    needed |= {"deploy/install.sh", "deploy/update.sh", "deploy/upgrade.sh"}  # upgrade.sh runs the release's update.sh
    return needed


def test_the_release_tarball_holds_everything_the_pi_scripts_use():
    excludes = _tarball_excludes()
    assert {"tests", ".github", ".beads", ".claude"} <= set(excludes)  # read from release.yml, not empty
    needed = _needed_on_the_pi()
    assert {"config.example.toml", "deploy/secrets.env.example", "deploy/requirements-pi.txt",
            "examples/glossop.toml", "deploy/departureboard-fetch.path", "LICENSE"} <= needed
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

    return subprocess.run(["bash", str(ROOT / "deploy" / "upgrade.sh"), *args], capture_output=True, text=True)


def _sourced(call: str, *args: str):
    """Runs `call` in bash with upgrade.sh sourced (so its main doesn't run)."""
    import subprocess

    return subprocess.run(["bash", "-c", f'source "$1"; shift; {call}', "bash", str(ROOT / "deploy" / "upgrade.sh"),
                           *args], capture_output=True, text=True)


@pytest.mark.parametrize("version, valid", [
    ("0.1.0", True), ("10.20.300", True),
    ("", False), ("0.1", False), ("v0.1.0", False), ("0.1.0-rc1", False), ("0.1.0 ", False), ("0.1.0\n", False),
    ("0.1.0;id", False), ("../0.1.0", False), ("0.1.0/../../x", False), ("0..1", False), ("a.b.c", False),
])
def test_upgrade_takes_only_a_plain_release_version(version, valid):
    assert (_sourced('valid_version "$1"', version).returncode == 0) == valid


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
    name = "inky-departures-0.2.0.tar.gz"
    (folder / name).write_bytes(b"the release")
    digest = hashlib.sha256(b"the release").hexdigest()
    wheel = hashlib.sha256(b"a wheel").hexdigest()
    lines = f"{wheel}  departureboard-0.2.0-py3-none-any.whl\n{digest}  {name}\n"  # as release.yml writes them
    (folder / "SHA256SUMS").write_text(lines if sums is None else sums.format(digest=digest, name=name))
    return folder


def _check(folder: Path):
    import shutil

    if shutil.which("sha256sum") is None:
        pytest.skip("no sha256sum here")
    return _sourced('check_tarball "$1" 0.2.0', str(folder))


def test_upgrade_accepts_a_tarball_that_matches_its_sum(tmp_path):
    done = _check(_release_folder(tmp_path))
    assert (done.returncode, done.stdout) == (0, "inky-departures-0.2.0.tar.gz: OK\n")


def test_upgrade_refuses_a_tarball_that_doesnt_match(tmp_path):
    folder = _release_folder(tmp_path)
    (folder / "inky-departures-0.2.0.tar.gz").write_bytes(b"the release, changed")
    done = _check(folder)
    assert done.returncode == 1 and "does not match SHA256SUMS" in done.stderr


@pytest.mark.parametrize("sums", [
    "",  # nothing for it
    "{digest}  inky-departures-0.1.0.tar.gz\n",  # another release's
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
    assert 'bash "$unpacked/deploy/update.sh" "$@"' in script
    # The release's own names for the tarball, its folder and the signature, and its signing identity.
    assert '-o "out/inky-departures-$VERSION.tar.gz"' in RELEASE and '--prefix="inky-departures-$VERSION/"' in RELEASE
    assert "--bundle SHA256SUMS.sigstore.json SHA256SUMS" in RELEASE
    identity = re.search(r"^IDENTITY=(\S+)$", script, re.M).group(1)
    assert f"{identity}@refs/tags/v$version" == _identity("v$version")
    assert '--certificate-identity "$IDENTITY@refs/tags/v$version"' in script


def test_the_guide_installs_a_pi_from_a_checked_release():
    block = re.search(r"```bash\nVERSION=0\.1\.0\n(.*?)```", GUIDE, re.S).group(1)
    assert f'base="https://github.com/{REPO}/releases/download/v$VERSION"' in block
    for name in ("inky-departures-$VERSION.tar.gz", "SHA256SUMS", "SHA256SUMS.sigstore.json"):
        assert f'curl -fLO "$base/{name}"' in block
    # Nothing is unpacked or run unless the sum is right, even pasted as a block.
    assert block.endswith('sha256sum --check --ignore-missing SHA256SUMS &&\n'
                          '  tar -xzf "inky-departures-$VERSION.tar.gz" &&\n'
                          '  cd "inky-departures-$VERSION" &&\n  sudo ./deploy/install.sh\n')
    assert f'--certificate-identity "{_identity("v$VERSION")}"' in GUIDE
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in GUIDE
    assert "sudo ./deploy/upgrade.sh 0.2.0" in GUIDE
    assert "| sudo bash" not in GUIDE


# The published image: the same lock-down as docker-compose.yml, wherever it runs.


def _live(text: str) -> list[str]:
    return [line.split("#")[0].rstrip() for line in text.splitlines() if line.split("#")[0].strip()]


def test_the_published_image_compose_file_is_pinned_and_locked_down_as_the_built_one():
    built, published = _live(COMPOSE), _live(GHCR_COMPOSE)
    hardening = built[built.index("    read_only: true"):built.index("volumes:")]
    assert len(hardening) > 10
    for line in hardening:
        assert line in published, line
    for line in ("    container_name: departureboard", "    restart: unless-stopped", '      - "8090:8090"',
                 "      - dashboard-data:/data", "volumes:", "  dashboard-data:"):
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

    root = ET.parse(ROOT / "deploy" / "unraid" / "departureboard.xml").getroot()
    assert (root.tag, root.get("version")) == ("Container", "2")
    assert root.findtext("Name") == "departureboard"
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
    assert (data.get("Type"), data.get("Mode"), data.text) == ("Path", "rw", "/mnt/user/appdata/departureboard")
    # A folder, not a volume: the guide gives it to the image's user first.
    assert "chown 10001:10001 /mnt/user/appdata/departureboard" in DOCKER_DOCS
    assert re.search(r"^USER 10001:10001$", DOCKERFILE, re.M)
    assert 's/X\\.Y\\.Z/$VERSION/' in DOCKER_DOCS and "templates-user/my-departureboard.xml" in DOCKER_DOCS


def test_the_documented_docker_run_is_locked_down_as_compose():
    command = re.search(r"```bash\n(docker run .*?)\n```", DOCKER_DOCS, re.S).group(1)
    words = shlex.split(command.replace("\\\n", " "))
    assert words[:5] == ["docker", "run", "-d", "--name", "departureboard"]
    assert words[-1] == f"{IMAGE}:$VERSION@$DIGEST"
    rest = _docker_run_flags(words[5:-1])
    assert {key: rest.pop(key) for key in ("--restart", "-p", "-v")} == {
        "--restart": "unless-stopped", "-p": "8090:8090", "-v": "departureboard-data:/data"}
    assert rest == HARDENING_FLAGS
    assert f'--certificate-identity "{_identity("v$VERSION")}"' in DOCKER_DOCS
    assert f'cosign verify "{IMAGE}@$DIGEST"' in DOCKER_DOCS


# The desktop: started at login, on this computer only.


def _desktop_serve(argv: list[str]) -> None:
    from departureboard.cli import _parser
    from departureboard.settings.installs import install_named

    args = _parser().parse_args(argv)
    assert (args.command, args.install, args.lan, args.host, args.port, args.no_browser) == (
        "serve", "desktop", False, None, 8090, True)
    assert install_named("desktop").host == "127.0.0.1"  # no --host or --lan: this computer only


def test_the_mac_login_agent_runs_serve_on_this_computer_only():
    import plistlib

    path = ROOT / "deploy" / "desktop" / "uk.departureboard.serve.plist"
    agent = plistlib.loads(path.read_bytes())
    assert agent["Label"] == path.stem == "uk.departureboard.serve"
    program = agent["ProgramArguments"]
    assert program[0] == "/Users/YOU/.local/bin/departureboard"
    _desktop_serve(program[1:])
    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] == {"SuccessfulExit": False}  # again if it fails, not when stopped
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert agent[key] == "/Users/YOU/Library/Logs/departureboard.log"
    # The guide's sed puts the home folder in every path.
    assert set(re.findall(r"/Users/\w+", path.read_text())) == {"/Users/YOU"}
    assert 'sed "s|/Users/YOU|$HOME|g" deploy/desktop/uk.departureboard.serve.plist' in GUIDE
    assert 'launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/uk.departureboard.serve.plist' in GUIDE
    assert 'launchctl bootout "gui/$(id -u)/uk.departureboard.serve"' in GUIDE


def test_the_linux_login_service_runs_serve_on_this_computer_only():
    unit = _unit("desktop/departureboard.service")
    [start] = unit["ExecStart"]
    words = shlex.split(start)
    assert words[0] == "%h/.local/bin/departureboard"
    _desktop_serve(words[1:])
    assert unit["Restart"] == ["on-failure"] and unit["WantedBy"] == ["default.target"]
    assert "User" not in unit and "Group" not in unit  # a user service runs as that user
    for setting in ("NoNewPrivileges", "LockPersonality", "RestrictRealtime", "RestrictSUIDSGID"):
        assert unit[setting] == ["yes"], setting
    assert unit["UMask"] == ["0077"]
    for command in ("cp deploy/desktop/departureboard.service ~/.config/systemd/user/",
                    "systemctl --user enable --now departureboard.service",
                    "journalctl --user -u departureboard -f"):
        assert command in GUIDE, command


def test_the_desktop_needs_pillow_only():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    names = {re.split(r"[<>=!~ ;\[]", spec, maxsplit=1)[0].lower() for spec in project["dependencies"]}
    assert names == {"pillow"}
    assert any(spec.startswith("inky") for spec in project["optional-dependencies"]["pi"])  # the Pi's, as an extra
    assert "uv tool install departureboard" in GUIDE and "pipx install departureboard" in GUIDE
