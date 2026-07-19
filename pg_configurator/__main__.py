import sys

from pg_configurator.configurator import run_pgc


def main(argv=None) -> int:
    try:
        run_pgc(argv)
    except (OSError, ValueError) as error:
        print(f"pg-configurator: error: {error}", file=sys.stderr)
        return 2
    return 0


def _cli_entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _cli_entrypoint()
