
import argparse
import os
import itertools

parser = argparse.ArgumentParser()
parser.add_argument("-data_file", type=str)

args = parser.parse_args()

def _is_divider(line: str) -> bool:
    empty_line = line.strip() == ''
    if empty_line:
        return True
    else:
        first_token = line.split()[0]
        if first_token == "-DOCSTART-":  # pylint: disable=simplifiable-if-statement
            return True
        else:
            return False


file_path = os.path.abspath(args.raw_path)
with open(file_path, "r") as data_file:

    # Group into alternative divider / sentence chunks.
    for is_divider, lines in itertools.groupby(data_file, _is_divider):
        # Ignore the divider chunks, so that `lines` corresponds to the words
        # of a single sentence.
        if not is_divider:
            fields = [line.strip().split() for line in lines]
            for val in fields:
                if len(val) != 4:
                    print('\n\n\n\n\n\n\nTOO LONG')
                    print(val)
                    print(file_path)
                    print('\n\n\n\n\n\n')
            fields = [
                val if len(val) == 4 else [" ".join(val[:-3]), val[-3], val[-2], val[-1]]
                for val in fields
            ]
            # unzipping trick returns tuples, but our Fields need lists
            fields = [list(field) for field in zip(*fields)]
            tokens_, _, _, pico_tags = fields