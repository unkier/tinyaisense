from importlib.metadata import version

import tinyaisense_wire


def test_wire_package_reports_its_distribution_version() -> None:
    assert tinyaisense_wire.__version__ == version("tinyaisense-wire")
