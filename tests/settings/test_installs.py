from twobells.settings.installs import INSTALLS, install_named
from twobells.timetable import build, routesetup

# The largest timetable file seen: First Bus's 75/76 in South Yorkshire, BODS data set 2100 (October 2026).
LARGEST_FILE = 48_094_105


def test_a_pi_reads_61_mb_of_each_timetable_file_and_holds_8500_journeys_to_fit_its_memory():
    # Its units' MemoryMax=256M: see tests/test_deploy.py.
    assert routesetup.document_limits(install_named("pi")) == routesetup.DocumentLimits(
        read=61_000_000, parse=61_000_000, journeys=8_500)


def test_a_pi_holds_the_journeys_at_sheffield_s_busiest_stops():
    # 5,816 from First South Yorkshire alone (BODS data set 2100, 2026).
    assert routesetup.document_limits(install_named("pi")).journeys >= 5_816


def test_a_pi_holds_two_of_sheffield_s_busiest_stops_while_it_reads():
    # Arundel Gate/Furnival Street and Wicker/Blonk Street, from BODS data set
    # 2100 (all of First Bus, October 2026): 5,581 journeys once built, but
    # more while each document's replaced versions are still held, refused
    # at 7,000 and built at 7,500.
    assert routesetup.document_limits(install_named("pi")).journeys >= 7_500


def test_docker_reads_100_mb_of_each_timetable_file_and_holds_8000_journeys_to_fit_its_container():
    # Its compose files' and Unraid template's 512 MB: see tests/test_deploy.py.
    assert routesetup.document_limits(install_named("docker")) == routesetup.DocumentLimits(
        read=100_000_000, parse=100_000_000, journeys=8_000)


def test_a_desktop_reads_200_mb_of_each_and_holds_20000_journeys():
    # No memory limit of its own.
    assert routesetup.document_limits(install_named("desktop")) == routesetup.DocumentLimits(
        read=200_000_000, parse=200_000_000, journeys=20_000)


def _largest_saved_timetable(name: str) -> int:
    """The largest timetable file a build on install `name` saves: its most
    journeys, inbound-only ones too, at build.SAVED_JOURNEY_BYTES each
    (tests/timetable/test_build.py measures it), and their stops and roads."""
    limits = routesetup.document_limits(install_named(name))
    return (limits.journeys + build.inbound_limit(limits)) * build.SAVED_JOURNEY_BYTES + build.STOPS_AND_ROADS_BYTES


def test_every_install_saves_the_largest_timetable_its_limits_allow():
    # One larger than load() reads isn't saved (TimetableTooBig): the build
    # would be wasted, and the timetable left to go stale.
    for name in INSTALLS:
        assert _largest_saved_timetable(name) <= build.MAX_FILE_BYTES, name


def test_no_timetable_file_larger_than_an_install_saves_is_read():
    # load() refuses anything bigger: no build here made it.
    assert max(_largest_saved_timetable(name) for name in INSTALLS) == build.MAX_FILE_BYTES


def test_every_install_parses_the_largest_timetable_file_seen_and_a_busy_stop_s_journeys():
    # A stop's week runs to hundreds of journeys, low thousands where many
    # lines meet; BODS can have two versions of each.
    for name in ("pi", "docker", "desktop"):
        limits = routesetup.document_limits(install_named(name))
        assert limits.parse >= LARGEST_FILE * 1.25, name
        assert limits.journeys >= 5_000, name
