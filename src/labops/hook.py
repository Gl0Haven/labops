import sys

from .spool import write_event


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "ingest" or args[1] != "--client":
        sys.stderr.write("labops hook: expected ingest --client codex|claude\n")
        return 0
    client = args[2]
    try:
        write_event(client, sys.stdin.buffer.read())
    except BaseException as exc:
        message = str(exc).replace("\r", " ").replace("\n", " ")
        sys.stderr.write(f"labops hook: {type(exc).__name__}: {message}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
