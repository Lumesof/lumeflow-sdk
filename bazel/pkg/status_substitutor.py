import json
import logging
import re
import sys
import traceback

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def substitute_vars(text, status_dict):
    def replacer(match):
        key = match.group(1)
        return status_dict.get(key, match.group(0))  # leave unchanged if not found

    return re.sub(r"\{([^}]+)\}", replacer, text)

def main():
    stable_status_file = sys.argv[1]
    output_file = sys.argv[2]
    input_lines_concat = sys.argv[3]
    with open(stable_status_file, "r") as f:
        try:
            status_file_lines = f.read().splitlines()
            status_dict = dict(
                line.split(" ", 1) for line in status_file_lines if " " in line
            )
            output_lines_concat = substitute_vars(input_lines_concat, status_dict)

        except Exception as e:
            logging.error(f"Encountered error while stamping: {str(e)}")
            traceback.print_exc()
            sys.exit(1)

    if not output_lines_concat.endswith("\n"):
        output_lines_concat += "\n"

    with open(output_file, "w") as f:
        f.write(output_lines_concat)

if __name__ == "__main__":
    main()
