"""Run the Combi Export test suite in headless Blender.

    python tests/run.py [-k PATTERN] [-v] [-x]
    blender -b --factory-startup --python-exit-code 1 -P tests/run.py -- [-k PATTERN] [-v] [-x]

From plain Python the script finds Blender (the BLENDER environment variable, else the newest default install, else
`blender` on PATH) and runs itself in it. -k runs the tests whose name contains PATTERN, -v lists every test, -x stops
at the first failure. The exit code is 0 when every test passes.

Tests marked `expectedFailure` reproduce known bugs (see the "BUG:" comment above each). Once a bug is fixed its test
reports an unexpected success, which fails the run: remove the marker then.
"""
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    import bpy  # noqa: F401
except ImportError:
    bpy = None


def find_blender():
    if os.environ.get("BLENDER"): return os.environ["BLENDER"]
    candidates = sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe"),
                        key=lambda p: [int(n) for n in os.path.basename(os.path.dirname(p)).split()[-1].split(".") if n.isdigit()])
    candidates += ["/Applications/Blender.app/Contents/MacOS/Blender"]
    for path in reversed(candidates):
        if os.path.isfile(path): return path
    return shutil.which("blender")


def launch():
    blender = find_blender()
    if not blender: sys.exit("Blender not found: set the BLENDER environment variable to its executable")
    sys.exit(subprocess.call([blender, "-b", "--factory-startup", "--python-exit-code", "1", "-P", os.path.abspath(__file__),
                              "--", *sys.argv[1:]]))


def main():
    import unittest
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    pattern = args[args.index("-k") + 1] if "-k" in args else None
    sys.path[:0] = [ROOT, HERE]
    import combi_export
    combi_export.register()

    loader = unittest.TestLoader()
    if pattern: loader.testNamePatterns = [f"*{pattern}*"]
    suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2 if "-v" in args else 1, failfast="-x" in args)
    result = runner.run(suite)
    sys.stdout.flush()
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    launch() if bpy is None else main()
