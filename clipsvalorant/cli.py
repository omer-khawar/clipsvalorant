"""Command line interface.

    python -m clipsvalorant init
    python -m clipsvalorant serve              # the web UI, with the player
    python -m clipsvalorant ingest --limit 20
    python -m clipsvalorant ingest-file tests/fixtures/match_v4_sample.json
    python -m clipsvalorant matches
    python -m clipsvalorant highlights
"""

from __future__ import annotations

import logging
import datetime as _dt
import time
import sys
from datetime import datetime, timezone
from typing import Annotated, Optional

import typer

from clipsvalorant import db
from clipsvalorant.config import Config, ConfigError, load_config

app = typer.Typer(
    add_completion=False,
    help="Find your own Valorant plays in other people's Twitch VODs. Phase 1: match ingest.",
)


def _force_utf8_output() -> None:
    """Windows consoles default to cp1252, which cannot encode most Riot IDs.

    Without this, printing a roster containing non-ASCII names raises
    UnicodeEncodeError instead of showing the name.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - exotic stream
                pass


_force_utf8_output()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _config(require_api_key: bool = True) -> Config:
    try:
        return load_config(require_api_key=require_api_key)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)


def _fmt_ms(ms: int) -> str:
    total = ms // 1000
    return f"{total // 60:>2d}:{total % 60:02d}"


def _fmt_started(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


@app.command()
def init() -> None:
    """Create the SQLite database and apply the schema."""
    cfg = _config(require_api_key=False)
    with db.session(cfg.db_path) as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    typer.echo(f"Database ready at {cfg.db_path}")
    typer.echo(f"Tables: {', '.join(t for t in tables if not t.startswith('sqlite_'))}")


@app.command()
def ingest(
    limit: Annotated[int, typer.Option(help="How many recent matches to list.")] = 20,
    mode: Annotated[
        Optional[str],
        typer.Option(help="Queue filter, e.g. competitive or unrated. Omit for all."),
    ] = "competitive",
    refetch: Annotated[
        bool, typer.Option(help="Re-download matches already in the database.")
    ] = False,
    days: Annotated[
        int,
        typer.Option(
            help="Skip matches older than this many days without fetching them. "
                 "0 fetches everything listed.",
        ),
    ] = 0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Pull recent matches from the HenrikDev API and store them."""
    _setup_logging(verbose)
    cfg = _config()
    from clipsvalorant.ingest import ingest as run_ingest

    typer.echo(f"Fetching up to {limit} {mode or 'any-mode'} matches for {cfg.riot_id} ({cfg.region})...")
    report = run_ingest(
        cfg, limit=limit, mode=mode, refetch=refetch,
        since_epoch=(int(time.time()) - days * 86400) if days > 0 else None,
    )
    typer.echo(report.summary())

    for err in report.errors:
        typer.secho(f"  ! {err}", fg=typer.colors.YELLOW, err=True)

    if report.stored:
        typer.echo("\nRun 'python -m clipsvalorant highlights' to see what it found.")
    elif not report.seen:
        typer.secho(
            "\nNo matches listed. HenrikDev's stored-matches only contains matches that\n"
            "someone has previously requested - try --mode unrated, or play a game and\n"
            "run this again.",
            fg=typer.colors.YELLOW,
        )


@app.command(name="ingest-file")
def ingest_file_cmd(
    path: Annotated[str, typer.Argument(help="A saved v4 match JSON file.")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Load a match from a JSON file on disk. Needs no API key."""
    _setup_logging(verbose)
    cfg = _config(require_api_key=False)
    from clipsvalorant.ingest import ingest_file

    try:
        report = ingest_file(cfg, path)
    except LookupError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(report.summary())


@app.command()
def backfill(
    limit: Annotated[
        int, typer.Option(help="How many matches to re-fetch in one go.")
    ] = 25,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Re-fetch matches stored before kill coordinates were kept.

    Costs one API call per match, so it is yours to run rather than something
    a scan does quietly. Without it, matches ingested earlier have no map
    view: the payload that carried the coordinates was trimmed on the way to
    disk, and nothing local can bring them back.
    """
    _setup_logging(verbose)
    cfg = _config()
    from clipsvalorant.ingest import backfill as run_backfill

    report = run_backfill(cfg, limit=limit)
    if not report.seen:
        typer.echo("Every stored match already has its full detail.")
        return
    typer.echo(
        f"{report.seen} match(es) were missing detail; "
        f"re-fetched {report.stored}, failed {report.failed}."
    )
    if report.skipped:
        typer.echo(f"{report.skipped} left - run it again to continue.")
    for err in report.errors:
        typer.secho(f"  ! {err}", fg=typer.colors.YELLOW, err=True)


@app.command()
def recompute(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Re-derive kills and highlights from stored match JSON. No API calls."""
    _setup_logging(verbose)
    cfg = _config(require_api_key=False)
    from clipsvalorant.ingest import recompute as run_recompute

    report = run_recompute(cfg)
    typer.echo(report.summary())
    for err in report.errors:
        typer.secho(f"  ! {err}", fg=typer.colors.YELLOW, err=True)


@app.command(name="set-secret")
def set_secret(
    client_id: Annotated[
        Optional[str], typer.Option(help="Twitch client id. Public; safe to pass here.")
    ] = None,
) -> None:
    """Store the Twitch client secret in .env without it appearing anywhere.

    The secret is read with a hidden prompt, so it never reaches the shell history,
    the terminal scrollback, or a chat transcript. The client id is not a secret -
    Twitch exposes it in the app's own URL - so it can be passed as a flag.
    """
    import getpass

    from clipsvalorant.config import REPO_ROOT

    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        typer.secho(f"No .env at {env_path}. Copy .env.example first.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    secret = getpass.getpass("Twitch client secret (input hidden): ").strip()
    if not secret:
        typer.secho("Nothing entered; .env unchanged.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    lines = env_path.read_text(encoding="utf-8").splitlines()
    wanted = {"TWITCH_CLIENT_SECRET": secret}
    if client_id:
        wanted["TWITCH_CLIENT_ID"] = client_id

    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            out.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in wanted.items():
        if key not in seen:
            out.append(f"{key}={value}")

    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    typer.secho(
        f"Wrote {', '.join(sorted(wanted))} to {env_path} "
        f"(secret length {len(secret)}, not shown).",
        fg=typer.colors.GREEN,
    )



@app.command()
def resolve(
    match: Annotated[
        Optional[str], typer.Option(help="Resolve one match by id (prefix is enough).")
    ] = None,
    limit: Annotated[int, typer.Option(help="How many recent matches to check.")] = 5,
    lead: Annotated[int, typer.Option(help="Seconds of lead-in before each moment.")] = 8,
    watch_vods: Annotated[bool, typer.Option(
        "--watch/--no-watch",
        help="Open each covering VOD and read the screen to confirm the match.",
    )] = True,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Find players in your matches who were streaming, and print VOD links."""
    _setup_logging(verbose)
    cfg = _config(require_api_key=False)
    if not cfg.has_twitch:
        typer.secho(
            "Twitch credentials missing. To set them up:\n"
            "  1. Enable 2FA on the Twitch account - no app can be registered without\n"
            "     it: https://www.twitch.tv/settings/security\n"
            "  2. Register an app: https://dev.twitch.tv/console/apps/create\n"
            "     (redirect http://localhost:3000, category Application Integration,\n"
            "     client type Confidential)\n"
            "  3. Put the client id and secret in .env as TWITCH_CLIENT_ID and\n"
            "     TWITCH_CLIENT_SECRET",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=2)

    from clipsvalorant.resolve import resolve_recent
    from clipsvalorant.twitch import TwitchError

    try:
        report = resolve_recent(cfg, limit=limit, match_id=match, watch_vods=watch_vods)
    except TwitchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo(report.summary())
    for err in report.errors:
        typer.secho(f"  ! {err}", fg=typer.colors.YELLOW, err=True)

    if not report.confirmed and not report.candidates:
        typer.echo(
            "\nNo streamers found. Either nobody in those lobbies was live, their VODs\n"
            "have expired (Twitch keeps them roughly 7-14 days), or their Twitch name\n"
            "differs from their in-game name - the case klips.live says covers 72%\n"
            "of streamers."
        )
        return

    with db.session(cfg.db_path) as conn:
        if report.confirmed:
            typer.secho("\nCONFIRMED", fg=typer.colors.GREEN, bold=True)
            for cand in report.confirmed:
                _show(conn, cand, lead, colour=typer.colors.GREEN, marker="->")

        if report.candidates:
            typer.secho(
                "\nUNVERIFIED - these channels were broadcasting across your match, but\n"
                "nothing proves they were in it. A short Riot ID collides with an\n"
                "established streamer more often than not. Click a link before believing it.",
                fg=typer.colors.YELLOW,
                bold=True,
            )
            for cand in report.candidates:
                _show(conn, cand, lead, colour=typer.colors.YELLOW, marker="? ")

        if report.rejected and verbose:
            typer.secho("\nRULED OUT", fg=typer.colors.BRIGHT_BLACK, bold=True)
            for cand in report.rejected:
                typer.secho(
                    f"  {cand.player.name}#{cand.player.tag} x twitch.tv/{cand.channel_login}"
                    f"  - {'; '.join(cand.evidence.reasons())}",
                    fg=typer.colors.BRIGHT_BLACK,
                )


def _show(conn, cand, lead: int, *, colour: str, marker: str) -> None:
    """One candidate: who, where, on what evidence, and the timestamps."""
    from clipsvalorant import calibrate
    from clipsvalorant.resolve import vod_link

    # Keyed on the match this candidate came from. A channel that streams daily
    # turns up in several of them, and picking the newest row put highlights from
    # one match against the VOD of another - offsets like t=217h.
    row = conn.execute(
        """
        SELECT m.id, m.started_at_epoch, m.map, e.same_team
        FROM encounter e JOIN match m ON m.id = e.match_id
        WHERE e.match_id = ? AND e.channel_id = ? AND e.puuid = ?
        """,
        (cand.match_id, cand.channel_id, cand.player.puuid),
    ).fetchone()
    if row is None or cand.vod is None:
        return
    side = "your team" if row["same_team"] else "enemy team"
    typer.secho(
        f"\n  {cand.player.name}#{cand.player.tag} {marker} twitch.tv/{cand.channel_login}"
        f"  ({cand.source}, {row['map']}, {side})",
        fg=colour,
    )
    for reason in cand.evidence.reasons():
        typer.echo(f"      - {reason}")

    highlights = conn.execute(
        "SELECT round, label, time_in_match_ms FROM highlight "
        "WHERE match_id = ? ORDER BY time_in_match_ms",
        (row["id"],),
    ).fetchall()
    if not highlights:
        typer.echo("      (no highlights of yours in this match)")
    cal = calibrate.load(conn, row["id"], cand.vod.id)
    if not cal.measured and highlights:
        typer.secho(
            f"      (start of match not read off the broadcast; these use the "
            f"{cal.shift_s:.0f}s median and may be seconds out)",
            fg=typer.colors.BRIGHT_BLACK,
        )
    for h in highlights:
        offset = calibrate.moment_offset(
            match_start_epoch=row["started_at_epoch"],
            vod_created_epoch=cand.vod.created_epoch,
            time_in_match_ms=h["time_in_match_ms"],
            shift_s=cal.shift_s,
            lead_s=lead,
        )
        # A stream that started mid-match simply does not contain the earlier
        # moments. Say so rather than silently linking to t=0.
        if offset < 0:
            typer.echo(
                f"      round {h['round']:>2}  {h['label']:<13} "
                f"not in this VOD (stream started {abs(offset)}s after that moment)"
            )
            continue
        typer.echo(
            f"      round {h['round']:>2}  {h['label']:<13} {vod_link(cand.vod.id, offset)}"
        )


@app.command()
def sweep(
    limit: Annotated[int, typer.Option(help="How many recent matches to list.")] = 20,
    mode: Annotated[
        Optional[str],
        typer.Option(help="Queue filter. Omit for every mode, which is what a sweep wants."),
    ] = None,
    lead: Annotated[int, typer.Option(help="Seconds of lead-in before each moment.")] = 8,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Only print when something was found.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Say what would be resolved, fetch nothing.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Ingest new matches and resolve the ones still worth resolving.

    Built to run unattended on a schedule. Twitch deletes past broadcasts after
    7-14 days, so a match resolved a fortnight late usually has nothing left to
    link to however well the players are identified - running regularly is what
    actually finds streamers, not a cleverer search.
    """
    _setup_logging(verbose)
    cfg = _config()
    from clipsvalorant.sweep import sweep as run_sweep

    started = _dt.datetime.now(_dt.timezone.utc)
    report = run_sweep(cfg, limit=limit, mode=mode, dry_run=dry_run)

    found = bool(report.confirmed)
    if not quiet or found:
        typer.echo(f"[{started.strftime('%Y-%m-%d %H:%M:%S')}Z] {report.summary()}")
    for err in report.errors:
        typer.secho(f"  ! {err}", fg=typer.colors.YELLOW, err=True)

    if not found:
        if not quiet:
            typer.echo("No new streamers confirmed this pass.")
        return

    with db.session(cfg.db_path) as conn:
        typer.secho("\nCONFIRMED", fg=typer.colors.GREEN, bold=True)
        for cand in report.confirmed:
            _show(conn, cand, lead, colour=typer.colors.GREEN, marker="->")


@app.command()
def audit(
    limit: Annotated[int, typer.Option(help="How many rejections to re-examine.")] = 100,
    frames: Annotated[int, typer.Option(help="Frame budget per broadcast.")] = 20,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Re-open broadcasts we ruled out, and check whether we were wrong.

    False positives announce themselves - you click a link and a stranger is
    playing Deadlock. False negatives are silent: a streamer we quietly binned
    looks exactly like a match where nobody was streaming. This is the check
    for those.
    """
    _setup_logging(verbose)
    cfg = _config(require_api_key=False)
    from clipsvalorant.resolve import audit_verdicts

    flips, notes = audit_verdicts(cfg, limit=limit, frames=frames)
    for note in notes:
        typer.echo(note)
    if not flips:
        typer.secho(
            "\nNo rejection overturned. Every broadcast we ruled out stayed ruled out.",
            fg=typer.colors.GREEN,
        )
        return
    typer.secho(f"\n{len(flips)} rejection(s) were WRONG:", fg=typer.colors.RED, bold=True)
    for flip in flips:
        typer.secho(f"  {flip}", fg=typer.colors.RED)
    typer.echo("\nThese have been corrected to confirmed. Run 'highlights' for links.")


@app.command(name="calibrate")
def calibrate_cmd(
    limit: Annotated[int, typer.Option(help="How many encounters to measure.")] = 50,
    force: Annotated[
        bool, typer.Option("--force", help="Re-measure ones already measured.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Measure where each confirmed match really starts inside its broadcast.

    A match carries one wall-clock timestamp and one in-match clock, and they
    do not share a zero: the timestamp is the queue popping, the clock starts
    when the map loads. Agent select and loading sit in between - 101, 109 and
    125 seconds on the three broadcasts this was measured against - so adding
    the two together lands about a round and a half early.

    This reads the countdown off the screen and writes down the real gap. It
    costs frames, not API calls.
    """
    _setup_logging(verbose)
    cfg = _config(require_api_key=False)
    from clipsvalorant.calibrate import backfill

    notes = backfill(cfg, limit=limit, force=force)
    for note in notes:
        typer.echo(note)
    typer.secho("\nLinks will now point at the play rather than a round before it.",
                fg=typer.colors.GREEN)


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Port to listen on. Taken? the next free one.")] = 8765,
    host: Annotated[str, typer.Option(help="Interface to bind. Localhost by default.")] = "127.0.0.1",
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open a browser window.")
    ] = True,
) -> None:
    """Start the web UI: search for a Riot ID and watch what it finds.

    Bound to localhost by default, because the page has no authentication of
    its own and would otherwise hand anyone who reached it the ability to
    spend this machine's API quota. Bind it wider - `--host 0.0.0.0`, which is
    what a container needs - and set CLIPS_PASSWORD, which gates every route
    but /healthz behind one shared password.
    """
    cfg = _config(require_api_key=False)
    from clipsvalorant.web import serve as run_server

    run_server(cfg, host=host, port=port, open_browser=open_browser,
               say=lambda line: typer.echo(line))


@app.command()
def matches(
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 20,
) -> None:
    """List the matches currently in the database."""
    cfg = _config(require_api_key=False)
    with db.session(cfg.db_path) as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.started_at_epoch, m.map, m.mode, m.queue,
                   m.rounds_won, m.rounds_lost,
                   m.game_length_ms,
                   (SELECT COUNT(*) FROM kill k WHERE k.match_id = m.id) AS kills,
                   (SELECT COUNT(*) FROM kill k WHERE k.match_id = m.id AND k.is_mine) AS my_kills,
                   (SELECT COUNT(*) FROM highlight h WHERE h.match_id = m.id) AS highlights
            FROM match m
            ORDER BY m.started_at_epoch DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not rows:
        typer.echo("No matches stored yet. Run 'ingest' first.")
        return

    header = f"{'Started (UTC)':<17} {'Map':<10} {'Mode':<12} {'Score':>7} {'Len':>6} {'Kills':>6} {'Mine':>5} {'HL':>3}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in rows:
        score = (
            f"{r['rounds_won']}-{r['rounds_lost']}"
            if r["rounds_won"] is not None
            else "?"
        )
        length = f"{r['game_length_ms'] // 60000}m" if r["game_length_ms"] else "?"
        # v4 metadata carries 'queue'; older shapes carry 'mode'. Show whichever we got.
        mode = r["mode"] or r["queue"] or "?"
        typer.echo(
            f"{_fmt_started(r['started_at_epoch']):<17} {(r['map'] or '?'):<10} "
            f"{mode:<12} {score:>7} {length:>6} "
            f"{r['kills']:>6} {r['my_kills']:>5} {r['highlights']:>3}"
        )


@app.command()
def highlights(
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 50,
    kind: Annotated[
        Optional[str], typer.Option(help="Filter: ace, quadra, triple or clutch.")
    ] = None,
) -> None:
    """Show your aces, 4Ks, 3Ks and clutches with their millisecond offsets.

    The time_in_match_ms column is what Phase 2 turns into a Twitch VOD timestamp.
    """
    cfg = _config(require_api_key=False)
    sql = """
        SELECT h.*, m.map, m.mode, m.started_at_epoch, m.started_at
        FROM highlight h JOIN match m ON m.id = h.match_id
        {where}
        ORDER BY m.started_at_epoch DESC, h.time_in_match_ms ASC
        LIMIT ?
    """
    params: list[object] = []
    where = ""
    if kind:
        where = "WHERE h.kind = ?"
        params.append(kind.lower())
    params.append(limit)

    with db.session(cfg.db_path) as conn:
        rows = conn.execute(sql.format(where=where), params).fetchall()

    if not rows:
        typer.echo("No highlights stored yet. Run 'ingest' first.")
        return

    header = (
        f"{'Started (UTC)':<17} {'Map':<10} {'Rd':>3} {'Highlight':<14} "
        f"{'At':>6} {'time_in_match_ms':>17} {'Won':>4}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in rows:
        won = "-" if r["round_won"] is None else ("yes" if r["round_won"] else "no")
        typer.echo(
            f"{_fmt_started(r['started_at_epoch']):<17} {(r['map'] or '?'):<10} "
            f"{r['round']:>3} {r['label']:<14} {_fmt_ms(r['time_in_match_ms']):>6} "
            f"{r['time_in_match_ms']:>17} {won:>4}"
        )

    typer.echo(
        f"\n{len(rows)} highlight(s). The time_in_match_ms column is the offset "
        "Phase 2 adds to the match start to locate the moment in a streamer's VOD."
    )


if __name__ == "__main__":
    app()
