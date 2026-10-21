import click

from .downloader import DEFAULT_OUTPUT_DIR, QivarynEngine
from .platforms import (
    ALL_MEDIA_TYPES,
    PLATFORM_NAMES,
    get_client_class,
    supported_media_types,
)


#: Every platform is served by the same generic engine; the client class that
#: the engine instantiates is what makes each platform different.
PLATFORM_ENGINES = {name: QivarynEngine for name in PLATFORM_NAMES}

#: Platforms whose clients understand a raw ``--sessionid`` cookie value.
SESSIONID_PLATFORMS = ("instagram", "tiktok")


def _looks_like_url(value: str) -> bool:
    """Returns True when the argument should be treated as a URL rather than a command."""
    lowered = value.lower()
    return "://" in lowered or lowered.startswith("www.")


class URLFallbackGroup(click.Group):
    """Group that routes a bare URL argument to the hidden ``url`` command.

    Unknown arguments that do not look like URLs are left alone so Click can
    report "No such command" instead of a confusing platform error.
    """

    def resolve_command(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            if _looks_like_url(args[0]):
                return "url", self.commands["url"], args
        return super().resolve_command(ctx, args)


def _validate_sessionid(platform, sessionid):
    if sessionid and platform not in SESSIONID_PLATFORMS:
        raise click.ClickException(
            f"--sessionid is not supported by {platform}. "
            f"Use --cookies cookies.txt instead."
        )


def _validate_media_type(platform, media_type):
    if not media_type:
        return
    allowed = supported_media_types(platform)
    if media_type.lower() not in allowed:
        raise click.BadParameter(
            f"'{media_type}' is not supported by {platform}. "
            f"Available types: {', '.join(allowed)}.",
            param_hint="--type",
        )


def _confirm_quality_fallback(fallback):
    """Prompts before downloading at a lower resolution than requested."""
    requested = fallback.get("requested")
    selected = fallback.get("selected")
    if fallback.get("no_lower"):
        message = (
            f"[!] Requested {requested}p is unavailable and no lower resolution "
            f"exists. Continue at the lowest available ({selected}p)?"
        )
    else:
        message = (
            f"[!] Requested {requested}p is unavailable. "
            f"Closest lower resolution is {selected}p. Continue at {selected}p?"
        )
    return click.confirm(message, default=False)


def _quality_was_provided():
    context = click.get_current_context(silent=True)
    if context is None:
        return False
    return (
        context.get_parameter_source("quality")
        == click.core.ParameterSource.COMMANDLINE
    )


def _create_engine(platform="instagram", quality="best", quality_provided=False, **options):
    try:
        engine_class = PLATFORM_ENGINES[platform]
    except KeyError as exc:
        supported = ", ".join(PLATFORM_ENGINES)
        raise click.ClickException(
            f"Unsupported platform '{platform}'. Available platforms: {supported}."
        ) from exc

    _validate_sessionid(platform, options.get("sessionid"))

    if platform != "youtube":
        if quality_provided or (quality and quality != "best"):
            click.echo(
                f"[!] --quality is only supported for YouTube; ignoring it for {platform}."
            )
    elif quality and quality != "best":
        options["quality"] = quality
        options["quality_confirmer"] = _confirm_quality_fallback

    try:
        return engine_class(platform=platform, **options)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


def _platform_for_url(url):
    for platform in PLATFORM_NAMES:
        client_class = get_client_class(platform)
        try:
            detected = client_class.detect_url(url)
        except ValueError as exc:
            raise click.ClickException(f"Invalid URL: {exc}") from exc
        if detected is not None:
            return platform
    raise click.ClickException(
        "Could not identify a supported platform or content type from this URL."
    )


def _parse_quality(_ctx, _param, value):
    if value is None:
        return "best"
    text = str(value).strip().lower()
    if text in ("best", "worst"):
        return text
    if text.endswith("p"):
        text = text[:-1]
    if text.isdigit() and int(text) > 0:
        return str(int(text))
    raise click.BadParameter(
        "Use 'best', 'worst', or a maximum height such as 1080 or 720p."
    )


_PLATFORM_OPTION = click.option(
    "--platform",
    type=click.Choice(PLATFORM_NAMES),
    default="instagram",
    show_default=True,
    help="Media platform to use.",
)
_MEDIA_TYPE_OPTION = click.option(
    "--type",
    "media_type",
    type=click.Choice(ALL_MEDIA_TYPES, case_sensitive=False),
    default=None,
    help="Filter media. Available types depend on the platform.",
)
_COOKIES_OPTION = click.option(
    "--cookies",
    "--cookies-file",
    "cookies_file",
    type=click.Path(dir_okay=False),
    help="Path to a Netscape cookies.txt file exported from your browser.",
)
_SESSIONID_OPTION = click.option(
    "--sessionid",
    help="Direct sessionid cookie value (Instagram and TikTok only).",
)
_OUTPUT_OPTION = click.option(
    "--output", "-o", default=DEFAULT_OUTPUT_DIR,
    help="Root directory for the platform folders.",
)
_DB_OPTION = click.option(
    "--db", default="qivaryn_state.db", help="Path to SQLite state tracking database."
)
_FORCE_OPTION = click.option(
    "--force", "-f", is_flag=True, default=False,
    help="Force re-download even if already downloaded.",
)
_QUALITY_OPTION = click.option(
    "--quality", "-q", "quality",
    default="best",
    callback=_parse_quality,
    metavar="Q",
    help=(
        "YouTube only: best (default), worst, or a maximum height "
        "(for example 1080 or 720p)."
    ),
)
_COUNT_OPTION = click.option(
    "--count", "-n", "count", type=int, default=None,
    help="Number of new items to download. Existing content is skipped.",
)
_LIMIT_OPTION = click.option("--limit", "-l", "limit", type=int, default=None, help="Alias for --count.")
_MAX_PAGES_OPTION = click.option(
    "--max-pages", type=int, default=None, help="Maximum pagination pages to scan."
)


@click.group(cls=URLFallbackGroup)
def cli():
    """Qivaryn media downloader. Pass a URL directly for automatic detection."""
    pass


@cli.command()
@click.argument("url_or_id")
@_PLATFORM_OPTION
@_COOKIES_OPTION
@_SESSIONID_OPTION
@_OUTPUT_OPTION
@_DB_OPTION
@_FORCE_OPTION
@_QUALITY_OPTION
def single(url_or_id, platform, cookies_file, sessionid, output, db, force, quality):
    """Download a single item by URL or identifier."""
    engine = _create_engine(
        platform=platform,
        output_dir=output,
        db_path=db,
        sessionid=sessionid,
        cookies_file=cookies_file,
        quality=quality,
        quality_provided=_quality_was_provided(),
    )
    engine.download_single(url_or_id, force=force)


@cli.command()
@click.argument("account")
@_PLATFORM_OPTION
@_COOKIES_OPTION
@_SESSIONID_OPTION
@_MEDIA_TYPE_OPTION
@_COUNT_OPTION
@_LIMIT_OPTION
@_OUTPUT_OPTION
@_DB_OPTION
@click.option(
    "--threshold", default=3,
    help="Consecutive known items before early-exit halt (only active when --count is omitted).",
)
@_MAX_PAGES_OPTION
@_FORCE_OPTION
@_QUALITY_OPTION
def download(account, platform, cookies_file, sessionid, media_type, count, limit,
             output, db, threshold, max_pages, force, quality):
    """Download account content with media filtering, batch limits and resumption."""
    target_count = count if count is not None else limit
    if target_count is not None and target_count <= 0:
        raise click.BadParameter(
            "Count must be a positive integer greater than 0.", param_hint="--count"
        )

    _validate_media_type(platform, media_type)
    engine = _create_engine(
        platform=platform,
        output_dir=output,
        db_path=db,
        sessionid=sessionid,
        cookies_file=cookies_file,
        consecutive_threshold=threshold,
        quality=quality,
        quality_provided=_quality_was_provided(),
    )
    engine.download_account(
        account,
        media_type_filter=media_type or "all",
        max_pages=max_pages,
        force=force,
        count=target_count,
    )


@cli.command("url", hidden=True)
@click.argument("url")
@_MEDIA_TYPE_OPTION
@_COOKIES_OPTION
@_SESSIONID_OPTION
@_COUNT_OPTION
@_LIMIT_OPTION
@_OUTPUT_OPTION
@_DB_OPTION
@_MAX_PAGES_OPTION
@_FORCE_OPTION
@_QUALITY_OPTION
def download_url(url, media_type, cookies_file, sessionid, count, limit, output, db,
                 max_pages, force, quality):
    """Automatically download content based on a supported URL."""
    target_count = count if count is not None else limit
    if target_count is not None and target_count <= 0:
        raise click.BadParameter(
            "Count must be a positive integer greater than 0.", param_hint="--count"
        )

    platform = _platform_for_url(url)
    _validate_media_type(platform, media_type)
    engine = _create_engine(
        platform=platform,
        output_dir=output,
        db_path=db,
        sessionid=sessionid,
        cookies_file=cookies_file,
        quality=quality,
        quality_provided=_quality_was_provided(),
    )
    engine.download_url(
        url,
        max_pages=max_pages,
        force=force,
        count=target_count,
        media_type=media_type,
    )


@cli.command("clear-db")
@click.argument("item", required=False, default=None)
@_PLATFORM_OPTION
@click.option("--item", "-i", "item_opt", help="Specific identifier or item URL to remove from the database.")
@click.option("--all", "-a", "clear_all_flag", is_flag=True, default=False,
              help="Clear this platform's entire download history.")
@click.option("--missing", "-m", "--prune", is_flag=True, default=False,
              help="Remove records whose media files were deleted from disk.")
@_OUTPUT_OPTION
@_DB_OPTION
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Confirm clearing all records without prompting.")
def clear_db(item, platform, item_opt, clear_all_flag, missing, output, db, yes):
    """Remove download records from the database without deleting media files."""
    target_item = item or item_opt
    engine = _create_engine(platform=platform, output_dir=output, db_path=db)

    if target_item:
        engine.clear_database(item=target_item)
    elif missing:
        engine.clear_database(prune_missing=True)
    elif clear_all_flag or yes:
        engine.clear_database(clear_all=True)
    else:
        record_count = engine.db.get_record_count(platform)
        if record_count == 0:
            click.echo(f"[*] No {platform} records in the database (0 records).")
            return
        if click.confirm(
            f"Are you sure you want to clear all {record_count} {platform} download "
            "records? (Existing files will NOT be deleted)"
        ):
            engine.clear_database(clear_all=True)
        else:
            click.echo("[-] Operation cancelled.")


@cli.command("platforms")
def list_platforms():
    """List supported platforms and their media filters."""
    for name in PLATFORM_NAMES:
        types = ", ".join(supported_media_types(name))
        click.echo(f"{name:<10} {types}")


if __name__ == "__main__":
    cli()
