"""palctl — REST-native Palworld dedicated server control."""

# Version comes from the git tag via setuptools-scm, which writes _version.py at
# build/install time. Fallback order: the generated file (present in any built
# or `pip install`-ed copy, including the frozen exe), then installed package
# metadata, then a dev placeholder for a bare source checkout that was never
# built. This never raises, so importing palctl can't fail on versioning.
try:
    from ._version import __version__
except ImportError:  # not built yet (fresh `git clone`, no install)
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("palctl")
    except (ImportError, PackageNotFoundError):
        __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
