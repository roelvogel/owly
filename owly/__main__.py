"""Allow `python -m owly` to show usage."""

import sys


def main() -> None:
    print("Owly — use `python -m owly.run` to generate editions.")
    print("       use `python -m owly.dashboard` to start the dashboard.")
    sys.exit(0)


if __name__ == "__main__":
    main()
