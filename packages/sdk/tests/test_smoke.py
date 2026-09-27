from importlib.metadata import version

import tinyaisense


def test_sdk_package_reports_its_distribution_version() -> None:
    assert tinyaisense.__version__ == version("tinyaisense")
