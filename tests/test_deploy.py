"""The dashboard container's security settings, so a later edit can't quietly drop one."""

import re
from pathlib import Path

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


def test_the_documented_password_commands_fit_the_container():
    # Run with docker exec as the image's user, in the one writable place: /data.
    assert "container_name: departureboard-dashboard" in COMPOSE and "dashboard-data:/data" in COMPOSE
    assert '"python", "-m", "departureboard.cli", "dashboard"' in DOCKERFILE and "WORKDIR /data" in DOCKERFILE
    assert "chown 10001:10001 /data" in DOCKERFILE
    docs = (ROOT / "docs" / "USER_GUIDE.md").read_text() + (ROOT / "README.md").read_text()
    for flag in ("--set-password", "--reset-code"):
        command = f"docker exec -it departureboard-dashboard python -m departureboard.cli dashboard {flag} --state-dir /data"
        assert docs.count(command) == 2, flag  # once in each


def test_keys_file_is_git_ignored():
    assert "dashboard.env" in (ROOT / ".gitignore").read_text().splitlines()
