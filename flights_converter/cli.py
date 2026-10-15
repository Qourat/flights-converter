"""CLI: validate to a sanitised spool, then publish a complete conversion."""

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from datetime import date
from importlib.resources import files
from pathlib import Path

from .airports import load_airports
from .errors import DataRejected, rejection_code
from .reducer import Reducer
from .trace import JoinedParts, ordered_part_paths, unique_object

MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_CONFIG_BYTES = 8 * 1024 * 1024


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "flights-convert: invalid arguments (see --help)\n")


def read_config(path):
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise DataRejected("configuration")
        return json.loads(raw, object_pairs_hook=unique_object)
    except (OSError, ValueError, TypeError, RecursionError):
        raise DataRejected("configuration") from None


def main(argv=None):
    parser = Parser(description="Convert local adsb.lol prod-0 tar parts; never fetches data.")
    parser.add_argument("--day", required=True)
    parser.add_argument("--airports", required=True, type=Path)
    parser.add_argument("--operators", type=Path)
    parser.add_argument("--opt-outs", type=Path, help="JSON array of hex: / reg: deny keys")
    parser.add_argument("--method", default="p4-v1")
    parser.add_argument(
        "--input", nargs="+", default=["-"], help="stdin (-), or named prod-0 parts"
    )
    parser.add_argument("--output", default="-", help="destination, or stdout (-)")
    parser.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    args = parser.parse_args(argv)
    temporary = None
    try:
        try:
            day = date.fromisoformat(args.day)
            if day.isoformat() != args.day:
                raise ValueError
        except ValueError:
            raise DataRejected("day_argument", date_error=True) from None
        airports = load_airports(read_config(args.airports))
        operators = read_config(
            args.operators or files("flights_converter").joinpath("operators.json")
        )
        opt_outs = read_config(args.opt_outs) if args.opt_outs else []
        if not isinstance(opt_outs, list):
            raise DataRejected("opt_out_config")
        with ExitStack() as stack:
            reducer = Reducer(airports, operators, args.method, opt_outs=opt_outs)
            stack.callback(reducer.close)
            if args.input == ["-"]:
                source = sys.stdin.buffer
            elif "-" in args.input:
                raise DataRejected("part_names")
            else:
                paths = ordered_part_paths([Path(path) for path in args.input], day)
                if args.output != "-" and any(
                    Path(args.output).resolve() == p.resolve() for p in paths
                ):
                    raise DataRejected("output_is_input")
                source = JoinedParts(stack.enter_context(path.open("rb")) for path in paths)
            # This file holds only already-sanitised compact output, never trace bodies.
            spool = stack.enter_context(
                tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="")
            )
            writer = csv.writer(spool) if args.format == "csv" else None
            if writer:
                writer.writerow(("record_type", "record"))

            def emit(record):
                encoded = json.dumps(record, separators=(",", ":"), allow_nan=False)
                if spool.tell() + len(encoded) * 2 + 64 > MAX_OUTPUT_BYTES:
                    raise DataRejected("output_limit")
                if writer:
                    writer.writerow((record["record_type"], encoded))
                else:
                    spool.write(encoded + "\n")

            reducer.consume(day, source, emit)
            spool.seek(0)
            if args.output == "-":
                shutil.copyfileobj(spool, sys.stdout)
            else:
                destination = Path(args.output)
                fd, temporary = tempfile.mkstemp(prefix=".flights-", dir=destination.parent)
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                    shutil.copyfileobj(spool, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                temporary = None
        return 0
    except DataRejected as error:
        print(
            json.dumps(
                {
                    "record_type": "diagnostics",
                    "code": rejection_code(error.code),
                    "discard_generation": True,
                }
            ),
            file=sys.stderr,
        )
        return 65
    except OSError:
        print('{"record_type":"diagnostics","code":"io_failure"}', file=sys.stderr)
        return 74
    except KeyboardInterrupt:
        print('{"record_type":"diagnostics","code":"interrupted"}', file=sys.stderr)
        return 130
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
