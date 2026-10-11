from departureboard.settings.installs import install_named
from departureboard.timetable import routesetup

# The largest timetable file seen: First South Yorkshire's, BODS data set 2100 (2026).
LARGEST_FILE = 36_904_485


def test_a_pi_reads_50_mb_of_each_timetable_file_and_holds_7000_journeys_to_fit_its_memory():
    # Its units' MemoryMax=256M: see tests/test_deploy.py.
    assert routesetup.document_limits(install_named("pi")) == routesetup.DocumentLimits(
        read=50_000_000, parse=50_000_000, journeys=7_000)


def test_a_pi_holds_the_journeys_at_sheffield_s_busiest_stops():
    # 5,816 from First South Yorkshire alone (BODS data set 2100, 2026).
    assert routesetup.document_limits(install_named("pi")).journeys >= 5_816


def test_docker_reads_100_mb_of_each_timetable_file_and_holds_12000_journeys_to_fit_its_container():
    # Its compose files' and Unraid template's 512 MB: see tests/test_deploy.py.
    assert routesetup.document_limits(install_named("docker")) == routesetup.DocumentLimits(
        read=100_000_000, parse=100_000_000, journeys=12_000)


def test_a_desktop_reads_200_mb_of_each_and_holds_20000_journeys():
    # No memory limit of its own.
    assert routesetup.document_limits(install_named("desktop")) == routesetup.DocumentLimits(
        read=200_000_000, parse=200_000_000, journeys=20_000)


def test_every_install_parses_the_largest_timetable_file_seen_and_a_busy_stop_s_journeys():
    # A stop's week runs to hundreds of journeys, low thousands where many
    # lines meet; BODS can have two versions of each.
    for name in ("pi", "docker", "desktop"):
        limits = routesetup.document_limits(install_named(name))
        assert limits.parse >= LARGEST_FILE * 1.25, name
        assert limits.journeys >= 5_000, name
