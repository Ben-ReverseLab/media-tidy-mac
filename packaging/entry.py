"""Entry point of the downloadable MediaTidy.app: no arguments = GUI, otherwise the command line."""
import sys

from mediatidy.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
