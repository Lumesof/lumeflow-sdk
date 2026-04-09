import runpy
import sys

import os

def main():
    if len(sys.argv) < 2:
        print("Usage: run_module module_name [args...]", file=sys.stderr)
        sys.exit(1)

    module_name = sys.argv[1]
    args = sys.argv[1:]  # sys.argv[0] will become module_name

    # Adjust sys.argv to simulate -m module_name [args...]
    sys.argv = args

    # Run the module as if with -m
    runpy.run_module(module_name, run_name="__main__")

if __name__ == "__main__":
    main()