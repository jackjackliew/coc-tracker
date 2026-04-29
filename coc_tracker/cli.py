"""Read-only CLI for inspecting the live storage files.

Examples
--------

    # Print top donators
    coc-tracker stats

    # Export the current season to CSV (one row per player)
    coc-tracker export --format csv > current.csv

    # Verify storage integrity (round-trip JSON load + schema check)
    coc-tracker verify

    # List backup snapshots
    coc-tracker backups
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from .backup import BACKUP_DIR
from .storage import DonationStorage


def _cmd_stats(args: argparse.Namespace) -> int:
    storage = DonationStorage()
    players = storage.get_all_players_sorted()
    if not players:
        print("⚠️  No donation data — storage is empty.", file=sys.stderr)
        return 0

    print(f"Season key:  {storage.data.get('season_key', '(unset)')}")
    print(f"Last sync:   {storage.data.get('last_sync', '(never)')}")
    print(f"Tracked clans: {len(storage.data.get('clan_tags', []))}")
    print(f"Total players: {len(players)}")
    print()
    print(f"Top {min(args.top, len(players))} donators:")
    for i, p in enumerate(players[: args.top], 1):
        print(f"  {i:>3}. {p['name']:<20} {p['donations']:>8,}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    storage = DonationStorage()
    players = storage.get_all_players_sorted()

    if args.format == "json":
        json.dump(
            {
                "season_key": storage.data.get("season_key"),
                "last_sync": storage.data.get("last_sync"),
                "players": players,
            },
            sys.stdout,
            indent=2,
        )
        print()
    elif args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(["rank", "name", "donations"])
        for i, p in enumerate(players, 1):
            writer.writerow([i, p["name"], p["donations"]])
    else:  # pragma: no cover — argparse blocks other values
        print(f"Unknown format: {args.format}", file=sys.stderr)
        return 2
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    storage = DonationStorage()
    required = {"season_key", "clan_tags", "clan_names", "last_sync", "players"}
    missing = required - set(storage.data.keys())
    if missing:
        print(f"❌ Storage missing required keys: {missing}", file=sys.stderr)
        return 1

    bad_players = []
    for tag, p in storage.data.get("players", {}).items():
        for field in ("name", "bonus", "last_clan", "last_donations"):
            if field not in p:
                bad_players.append((tag, field))

    if bad_players:
        print(f"❌ {len(bad_players)} player records missing fields:", file=sys.stderr)
        for tag, field in bad_players[:10]:
            print(f"   {tag}: missing '{field}'", file=sys.stderr)
        return 1

    print(
        f"✅ Storage OK — {len(storage.data.get('players', {}))} players, "
        f"{len(storage.data.get('clan_tags', []))} clans, "
        f"season {storage.data.get('season_key', '(unset)')}"
    )
    return 0


def _cmd_backups(args: argparse.Namespace) -> int:
    if not BACKUP_DIR.exists():
        print(f"No backups yet (would land in {BACKUP_DIR})", file=sys.stderr)
        return 0
    snaps = sorted([p for p in BACKUP_DIR.iterdir() if p.is_dir()], reverse=True)
    if not snaps:
        print(f"No backups in {BACKUP_DIR}", file=sys.stderr)
        return 0
    print(f"Backups in {BACKUP_DIR}:")
    for snap in snaps[: args.limit]:
        files = sorted(p.name for p in snap.iterdir())
        print(f"  {snap.name}  →  {', '.join(files)}")
    if len(snaps) > args.limit:
        print(f"  ... and {len(snaps) - args.limit} older snapshot(s)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coc-tracker", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="Print top donators from live storage")
    s.add_argument("--top", type=int, default=20, help="How many players to show (default: 20)")
    s.set_defaults(func=_cmd_stats)

    e = sub.add_parser("export", help="Export current standings as JSON or CSV")
    e.add_argument("--format", choices=["json", "csv"], default="csv")
    e.set_defaults(func=_cmd_export)

    v = sub.add_parser("verify", help="Validate storage schema integrity")
    v.set_defaults(func=_cmd_verify)

    b = sub.add_parser("backups", help="List available backup snapshots")
    b.add_argument("--limit", type=int, default=10)
    b.set_defaults(func=_cmd_backups)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
