import importlib.util
import pathlib
import sys

HAMMER = pathlib.Path(__file__).resolve().parent.parent / "fshammer.py"

# the real module, loaded by path: the tool is a single script, not an installed package
_spec = importlib.util.spec_from_file_location("fshammer", HAMMER)
assert _spec and _spec.loader, f"cannot load {HAMMER}"
fshammer = importlib.util.module_from_spec(_spec)
sys.modules["fshammer"] = fshammer
_spec.loader.exec_module(fshammer)
