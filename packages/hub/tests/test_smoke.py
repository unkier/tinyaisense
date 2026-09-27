from importlib.metadata import version

import tinyaisense_hub


def test_hub_package_reports_its_distribution_version() -> None:
    assert tinyaisense_hub.__version__ == version("tinyaisense-hub")
