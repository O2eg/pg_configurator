import sys

from pg_configurator.configurator import run_pgc
from pg_configurator.orchestration import EXIT_CODES, envelope


def _machine_context(argv) -> tuple[bool, str | None]:
    arguments = list(sys.argv[1:] if argv is None else argv)
    machine = "--machine" in arguments
    request_id = None
    for index, argument in enumerate(arguments):
        if argument.startswith("--request-id="):
            request_id = argument.partition("=")[2]
        elif argument == "--request-id" and index + 1 < len(arguments):
            request_id = arguments[index + 1]
    return machine, request_id


def main(argv=None) -> int:
    machine, request_id = _machine_context(argv)
    try:
        run_pgc(argv)
    except (OSError, ValueError) as error:
        if machine:
            import json

            print(
                json.dumps(
                    envelope(
                        "generate",
                        "failed",
                        request_id=request_id,
                        error={"code": "validation_error", "message": str(error)},
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"pg-configurator: error: {error}", file=sys.stderr)
        return EXIT_CODES["validation_error"]
    return 0


def _cli_entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _cli_entrypoint()
