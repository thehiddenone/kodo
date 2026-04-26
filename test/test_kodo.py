import kodo


def test_version() -> None:
    import re
    from importlib.metadata import version

    base = re.sub(r"b\d+$", "", kodo.__version__)
    assert base == version("kodo")
