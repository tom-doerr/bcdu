import argparse
from pathlib import Path

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description="bcdu — keyboard-first bcachefs storage explorer")
    parser.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--demo", action="store_true", help="Synthetic interactive demo; no disk changes")
    parser.add_argument("--read-only", action="store_true", help="Disable policy editing")
    parser.add_argument("--bcachefs-bin", help="Path to the bcachefs executable")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    if not args.demo and not args.path.is_dir():
        parser.error("path must be an accessible directory")
    from .app import BcduApp

    BcduApp(args.path.resolve(), demo=args.demo, read_only=args.read_only,
            binary=args.bcachefs_bin).run()


if __name__ == "__main__":
    main()
