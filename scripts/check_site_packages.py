"""
Fail if the Python environment carries an unexpected interpreter-startup hook.

Run INSIDE the built image (CI does this on every PR and before every publish):
    docker run --rm --entrypoint python -v "$PWD/scripts:/check:ro" <image> \
        -I -S /check/check_site_packages.py --pin litellm==1.106.2

`-S` matters: it stops Python's startup from executing .pth files, so a planted hook never runs
during the check that is looking for it (`import site` below does not run them when -S is set).
`--pin NAME==VERSION` (repeatable) also asserts installed versions, read from package metadata only.

Why: the compromised LiteLLM releases (1.82.7/1.82.8, 2026-03-24; 1.107.0, 2026-10-21) shipped a
credential stealer as a `.pth` / `sitecustomize` file — code that runs on EVERY Python start, before
any of ours. A clean image of this repo has no `.pth` files at all. Anything listed below is a
build failure until someone has looked at it and added it to EXPECTED_PTH on purpose.

Checks every site-packages directory (and the stdlib dir, where sitecustomize.py can also live)
WITHOUT importing any third-party package, so a planted hook cannot hide the evidence.
"""

from __future__ import annotations

import argparse
import os
import sys
import sysconfig
from importlib import metadata

# .pth files a clean image is allowed to contain. Empty on purpose — add one only after review.
EXPECTED_PTH: frozenset[str] = frozenset()
# Never acceptable, anywhere on the path.
FORBIDDEN = {"sitecustomize.py", "usercustomize.py", "cook.py"}


def scan_dirs() -> list[str]:
    paths = sysconfig.get_paths()
    dirs = {paths["purelib"], paths["platlib"], paths["stdlib"]}
    try:
        import site
        dirs.update(site.getsitepackages())
        dirs.add(site.getusersitepackages())
    except Exception:
        pass
    return sorted(d for d in dirs if d and os.path.isdir(d))


def problems(dirs: list[str]) -> list[str]:
    found = []
    for d in dirs:
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if name.endswith(".pth") and name not in EXPECTED_PTH:
                found.append(f"unexpected .pth: {path}")
            elif name in FORBIDDEN:
                found.append(f"forbidden file: {path}")
            elif name in {"sitecustomize", "usercustomize"} and os.path.isdir(path):
                found.append(f"forbidden package: {path}")
    return found


def pin_problems(pins: list[str], dirs: list[str]) -> list[str]:
    found = []
    for pin in pins:
        name, _, want = pin.partition("==")
        try:
            # path=dirs: read dist-info directly — works under -S, imports nothing third-party
            dists = [d for d in metadata.distributions(path=dirs)
                     if (d.metadata["Name"] or "").lower().replace("_", "-") == name.lower()]
            got = {d.version for d in dists}
        except Exception as exc:          # pragma: no cover — defensive
            found.append(f"cannot read {name} metadata: {exc}")
            continue
        if got != {want}:
            found.append(f"version pin: {name} is {sorted(got) or 'not installed'}, expected {want}")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pin", action="append", default=[], help="NAME==VERSION that must be installed")
    args = ap.parse_args(argv)
    dirs = scan_dirs()
    bad = problems(dirs) + pin_problems(args.pin, dirs)
    print("scanned:", ", ".join(dirs))
    for pin in args.pin:
        print("pinned:", pin)
    if bad:
        print("FAIL:")
        for b in bad:
            print("  " + b)
        return 1
    print("OK — no unexpected .pth, sitecustomize.py, usercustomize.py or cook.py"
          + ("; pins match" if args.pin else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
