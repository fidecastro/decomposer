import sys
from pathlib import Path

# Tests import the fakes by module name; make the tests directory importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
