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

    # Show disk usage relevant to the bot (storage, backups, journal, etc.)
    coc-tracker disk
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .backup import BACKUP_DIR
from .storage import DonationStorage

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _human(num_bytes: int) -> str:
    """Format a byte count as a short human-readable string (1.2 MB)."""
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} PB"


def _dir_size(path: Path, exclude: tuple[str, ...] = ()) -> int:
    """Recursive size in bytes. Skips top-level entries whose name matches `exclude`."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        # Skip excluded subtrees (e.g. .git, .venv) — checked against any path component
        if any(part in exclude for part in p.relative_to(path).parts):
            continue
        with contextlib.suppress(OSError):
            total += p.stat().st_size
    return total


# ─── Commands ─────────────────────────────────────────────────────────────────


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


def _cmd_disk(args: argparse.Namespace) -> int:
    """Show disk footprint of everything the bot owns + journald usage.

    Designed for free-tier VMs where unbounded growth is the real risk.
    """
    storage_path = Path(config.STORAGE_FILE)
    last_season_path = Path(config.LAST_SEASON_FILE)
    repo_root = config.REPO_ROOT
    home = Path.home()

    rows: list[tuple[str, int, str]] = []

    rows.append(
        (
            "donation_storage.json",
            _dir_size(storage_path),
            str(storage_path) if storage_path.exists() else "(not yet created)",
        )
    )
    rows.append(
        (
            "last_season_storage.json",
            _dir_size(last_season_path),
            str(last_season_path) if last_season_path.exists() else "(no rollover yet)",
        )
    )

    snap_count = 0
    if BACKUP_DIR.exists():
        snap_count = sum(1 for p in BACKUP_DIR.iterdir() if p.is_dir())
    rows.append(
        (
            f"auto-backups ({snap_count} snapshots)",
            _dir_size(BACKUP_DIR),
            str(BACKUP_DIR) if BACKUP_DIR.exists() else "(not yet — first run 6h after start)",
        )
    )

    pre_deploy = sorted(home.glob("coc-tracker-pre-deploy-backup-*"))
    rows.append(
        (
            f"pre-deploy backups ({len(pre_deploy)})",
            sum(_dir_size(p) for p in pre_deploy),
            f"{home}/coc-tracker-pre-deploy-backup-*",
        )
    )

    rows.append(
        (
            "repo (code, excl .git/.venv)",
            _dir_size(repo_root, exclude=(".git", ".venv", "venv", "__pycache__")),
            str(repo_root),
        )
    )

    pip_cache = home / ".cache" / "pip"
    rows.append(("pip cache", _dir_size(pip_cache), str(pip_cache)))

    # systemd journal — call journalctl if available (try both with/without sudo)
    journal_size_str = "n/a"
    for cmd in (
        ["sudo", "-n", "journalctl", "--disk-usage"],
        ["journalctl", "--disk-usage"],
    ):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5).decode()
            journal_size_str = out.strip()
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            continue

    print("Disk footprint:")
    print(f"  {'thing':<35}  {'size':>10}  path")
    print(f"  {'-'*35}  {'-'*10}  {'-'*40}")
    for label, size, path in rows:
        print(f"  {label:<35}  {_human(size):>10}  {path}")

    total_owned = sum(size for _, size, _ in rows)
    print(f"  {'-'*35}  {'-'*10}")
    print(f"  {'TOTAL bot-owned':<35}  {_human(total_owned):>10}")
    print()
    print(f"systemd journal:  {journal_size_str}")

    # Filesystem free space
    if shutil.disk_usage:
        usage = shutil.disk_usage("/")
        free_gb = usage.free / (1024**3)
        used_pct = usage.used / usage.total * 100
        print(
            f"filesystem  /:  {_human(usage.used)} used / {_human(usage.total)} "
            f"({used_pct:.1f}%) — {free_gb:.1f} GB free"
        )

    # Warn if anything's clearly oversized for a free-tier VM
    warnings: list[str] = []
    if total_owned > 100 * 1024 * 1024:
        warnings.append(f"bot-owned files exceed 100 MB ({_human(total_owned)}) — investigate")
    if len(pre_deploy) > 10:
        warnings.append(
            f"{len(pre_deploy)} pre-deploy backups present — only the last 5 are kept by cron"
        )

    if warnings:
        print()
        for w in warnings:
            print(f"⚠️  {w}", file=sys.stderr)
        return 1

    return 0


# ─── Argparse wiring ──────────────────────────────────────────────────────────


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

    d = sub.add_parser("disk", help="Show disk usage of bot-owned files + journal")
    d.set_defaults(func=_cmd_disk)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
