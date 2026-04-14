#!/usr/bin/env python3
"""
shuffle_diff.py — Randomize file order in a unified diff for multi-pass review diversity.

CLI usage:
    python3 shuffle_diff.py [--input-file FILE] [--output-file FILE] [--seed N]

Python API:
    from shuffle_diff import shuffle_diff
    shuffled = shuffle_diff(diff_text, seed=42)
"""

import argparse
import random
import sys


def _parse_diff(diff_text: str) -> tuple[str, list[str]]:
    """
    Parse a unified diff into a preamble and a list of per-file blocks.

    The preamble is any content before the first ``diff --git`` (or ``diff -``)
    line. Each block starts with a diff header and includes all subsequent
    lines until the next diff header (exclusive). Binary file markers are
    included in their respective block.

    Returns:
        ``(preamble, blocks)`` where *preamble* may be an empty string and
        *blocks* may be an empty list when the diff has no file-level headers.
    """
    preamble_lines: list[str] = []
    blocks: list[str] = []
    current_lines: list[str] = []
    in_block = False

    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git ") or line.startswith("diff -"):
            if in_block:
                blocks.append("".join(current_lines))
                current_lines = []
            else:
                preamble_lines = list(current_lines)
                current_lines = []
            in_block = True
        current_lines.append(line)

    # Flush the last block (or preamble-only content when there are no blocks)
    if current_lines:
        if in_block:
            blocks.append("".join(current_lines))
        else:
            preamble_lines = list(current_lines)

    return "".join(preamble_lines), blocks


def shuffle_diff(diff_text: str, seed: int | None = None) -> str:
    """
    Randomize the file-level block order in a unified diff.

    Preserves header + hunk integrity within each block. Any preamble content
    before the first diff header is kept at the top, unchanged. Binary file
    markers (``Binary files differ``) are kept with their block.

    Args:
        diff_text: A unified diff string (e.g., output of ``git diff``).
        seed:      Optional integer seed for reproducible shuffling.

    Returns:
        A new diff string with file blocks in randomized order, or the
        original string unchanged when there are zero or one file blocks.
    """
    if not diff_text or not diff_text.strip():
        return diff_text

    preamble, blocks = _parse_diff(diff_text)

    if len(blocks) <= 1:
        return diff_text

    rng = random.Random(seed)
    rng.shuffle(blocks)

    output = preamble + "".join(blocks)

    # Preserve trailing newline
    if diff_text.endswith("\n") and not output.endswith("\n"):
        output += "\n"

    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Randomize file order in a unified diff for multi-pass review diversity."
        )
    )
    parser.add_argument(
        "--input-file",
        metavar="FILE",
        help="Path to input diff file (default: stdin)",
    )
    parser.add_argument(
        "--output-file",
        metavar="FILE",
        help="Path to output file (default: stdout)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help="Random seed for reproducible shuffling",
    )
    args = parser.parse_args()

    if args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as fh:
            diff_text = fh.read()
    else:
        diff_text = sys.stdin.read()

    result = shuffle_diff(diff_text, seed=args.seed)

    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as fh:
            fh.write(result)
    else:
        sys.stdout.write(result)


if __name__ == "__main__":
    main()
