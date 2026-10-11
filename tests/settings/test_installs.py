from departureboard.settings.installs import INSTALLS, install_named
from departureboard.timetable import routesetup


def test_a_pi_reads_and_parses_less_of_each_timetable_file_to_fit_its_memory():
    # Its units' MemoryMax=256M: see tests/test_deploy.py.
    assert routesetup.document_limits(install_named("pi")) == routesetup.DocumentLimits(read=100_000_000,
                                                                                        parse=25_000_000)


def test_docker_and_a_desktop_read_200_mb_of_each_and_parse_what_they_read():
    for name in ("docker", "desktop"):
        assert routesetup.document_limits(install_named(name)) == routesetup.DocumentLimits(read=200_000_000,
                                                                                            parse=200_000_000)


def test_where_the_dashboard_builds_the_timetable_it_has_the_install_s_limits():
    # Its upkeep builds with the timetable's own defaults, on the installs where
    # serve runs it in its own process; a Pi's dashboard never builds it.
    for install in INSTALLS.values():
        if install.fetch == "process":
            assert routesetup.document_limits(install) == routesetup.DocumentLimits(), install.name
