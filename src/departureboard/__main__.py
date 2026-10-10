"""`python -m departureboard`: the command line, as `departureboard`."""

import sys

from .cli.main import main

if __name__ == "__main__":
    sys.exit(main())
