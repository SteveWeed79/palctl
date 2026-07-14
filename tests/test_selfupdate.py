"""Version comparison decides whether we nag the user about an update, so the
ordering and the tolerance for 'v' prefixes / suffixes are pinned. The network
fetch is best-effort and not tested."""

from palctl import selfupdate


def test_is_newer_ordering():
    assert selfupdate.is_newer("0.1.0", "0.2.0")
    assert selfupdate.is_newer("0.1.0", "v0.1.1")
    assert selfupdate.is_newer("1.9.9", "1.10.0")  # numeric, not lexical
    assert not selfupdate.is_newer("1.0.0", "1.0.0")
    assert not selfupdate.is_newer("2.0.0", "1.9.9")


def test_is_newer_ignores_trailing_zeros():
    # "1.2" and "1.2.0" are the same version — differing tuple lengths must not
    # make one look newer than the other.
    assert not selfupdate.is_newer("1.2", "1.2.0")
    assert not selfupdate.is_newer("1.2.0", "1.2")
    assert selfupdate.is_newer("1.2", "1.2.1")
    assert not selfupdate.is_newer("1.2.1", "1.2")


def test_parse_tolerates_prefix_and_suffix():
    assert selfupdate._parse_version("v1.2.3") == (1, 2, 3)
    assert selfupdate._parse_version("1.2.3-rc1") == (1, 2, 3)
    assert selfupdate._parse_version("") == (0,)
