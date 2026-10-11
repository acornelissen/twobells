from departureboard.settings.installs import install_named
from departureboard.timetable import routesetup


def test_a_pi_reads_100_mb_of_each_timetable_file_to_fit_its_memory_and_parses_what_it_reads():
    # Its units' MemoryMax=256M: see tests/test_deploy.py.
    assert routesetup.document_limits(install_named("pi")) == routesetup.DocumentLimits(read=100_000_000,
                                                                                        parse=100_000_000)


def test_docker_reads_100_mb_of_each_timetable_file_to_fit_its_container_and_parses_what_it_reads():
    # Its compose files' and Unraid template's 512 MB: see tests/test_deploy.py.
    assert routesetup.document_limits(install_named("docker")) == routesetup.DocumentLimits(read=100_000_000,
                                                                                            parse=100_000_000)


def test_a_desktop_reads_200_mb_of_each_and_parses_what_it_reads():
    # No memory limit of its own.
    assert routesetup.document_limits(install_named("desktop")) == routesetup.DocumentLimits(read=200_000_000,
                                                                                             parse=200_000_000)
