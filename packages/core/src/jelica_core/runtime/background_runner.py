from __future__ import annotations


def main() -> None:
    from jelica_core.events import run_service_runtime

    result = run_service_runtime()
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
