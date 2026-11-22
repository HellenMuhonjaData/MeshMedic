"""Manage MeshMedic clinician accounts (clinicians.json, git-ignored).

    uv run python manage_clinicians.py add dr.rivera --name "Dr. A. Rivera"
    uv run python manage_clinicians.py list
    uv run python manage_clinicians.py deactivate dr.rivera
    uv run python manage_clinicians.py activate dr.rivera

`add` asks for the password and signing PIN without echoing them. Clinic
hours (used for end-of-day reporting in Phase 8) default to 08:00-17:00.
"""

import argparse
import getpass
import sys

import clinician_registry


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("clinician_id")
    add.add_argument("--name", required=True)
    add.add_argument("--role", default="physician")
    add.add_argument("--clinic-start", default="08:00")
    add.add_argument("--clinic-end", default="17:00")
    add.add_argument("--utc-offset-minutes", type=int, default=0)
    sub.add_parser("list")
    for verb in ("activate", "deactivate"):
        sub.add_parser(verb).add_argument("clinician_id")
    args = parser.parse_args(argv)

    try:
        if args.command == "add":
            password = getpass.getpass("Password (10+ characters): ")
            if password != getpass.getpass("Repeat password: "):
                print("Passwords do not match.", file=sys.stderr)
                return 1
            pin = getpass.getpass("Signing PIN (4-8 digits): ")
            hours = {"start": args.clinic_start, "end": args.clinic_end, "utc_offset_minutes": args.utc_offset_minutes}
            c = clinician_registry.add_clinician(args.clinician_id, args.name, password, pin, args.role, hours)
            print(f"Added {c.display_name} ({c.clinician_id}).")
        elif args.command == "list":
            for c, active in clinician_registry.list_clinicians():
                print(f"{c.clinician_id:20s} {c.display_name:30s} {c.role:12s} {'active' if active else 'inactive'}")
        else:
            clinician_registry.set_active(args.clinician_id, args.command == "activate")
            print(f"{args.clinician_id} is now {'active' if args.command == 'activate' else 'inactive'}.")
    except clinician_registry.RegistryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
