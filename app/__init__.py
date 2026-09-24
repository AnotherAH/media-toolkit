"""Media Toolkit.

``__version__`` is the one place the version lives. MediaToolkit.spec writes it
into the exe's version resource, tools/build.py passes it to the installer,
the release workflow checks the git tag against it, and the API reports it.
tests/test_packaging.py fails if any of those drift apart.
"""

__version__ = "1.2.0"
