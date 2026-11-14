"""Compatibility entry; supply current context, note, kind and evidence explicitly."""
from maintenance.submit_note import main


if __name__ == "__main__":
    raise SystemExit(main())
