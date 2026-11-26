"""Import this BEFORE pandas.

Some locked-down Windows setups (Application Control / Smart App Control) block pyarrow's compiled DLLs, and pandas
then crashes at import time because it auto-loads pyarrow. If pyarrow cannot be fully imported, hide it so pandas and
Streamlit fall back to non-Arrow code paths (the app then shows plain HTML tables instead of st.dataframe).

Hiding does not work with every pandas build. If pandas still insists on pyarrow, this raises a clear message; the
reliable fix is then:   .venv\\Scripts\\python.exe -m pip uninstall -y pyarrow   (the app does not need it).
"""
import sys


def _purge(prefix):
    for name in [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]:
        del sys.modules[name]


try:
    import pyarrow  # noqa: F401
    import pyarrow.compute  # noqa: F401
    import pyarrow.lib  # noqa: F401
    ARROW_OK = True
except ImportError:  # includes "DLL load failed ... Application Control policy has blocked this file"
    _purge("pyarrow")
    sys.modules["pyarrow"] = None  # later "import pyarrow" raises ImportError, which most pandas builds handle
    ARROW_OK = False
    try:
        import pandas  # noqa: F401
    except ImportError as e:
        if "pyarrow" not in str(e):
            raise
        _purge("pandas")
        raise RuntimeError(
            "Windows is blocking pyarrow and this pandas build cannot run with it hidden.\n"
            "Fix: run   .venv\\Scripts\\python.exe -m pip uninstall -y pyarrow   and restart the app. "
            "The app does not need pyarrow (tables fall back to plain HTML).\n"
            f"Original error: {e}"
        ) from None
