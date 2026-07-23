"""Allow ``python -m rpi_streamer`` to run the CLI."""

from rpi_streamer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
