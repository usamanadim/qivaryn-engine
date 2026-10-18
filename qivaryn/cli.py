import click
from .downloader import QivarynEngine


PLATFORM_ENGINES = {
    "instagram": QivarynEngine,
}

class URLFallbackGroup(click.Group):
    def resolve_command(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            return "url", self.commands["url"], args
        return super().resolve_command(ctx, args)


def _create_engine(platform="instagram", **options):
    try:
        engine_class = PLATFORM_ENGINES[platform]
    except KeyError as exc:
        supported = ", ".join(PLATFORM_ENGINES)
        raise click.ClickException(
            f"Unsupported platform '{platform}'. Available platforms: {supported}."
        ) from exc

    try:
        return engine_class(**options)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


def _platform_for_url(url):
    for platform, engine_class in PLATFORM_ENGINES.items():
        try:
            detected = engine_class.detect_url(url)
        except ValueError as exc:
            raise click.ClickException(f"Invalid URL: {exc}") from exc
        if detected is not None:
            return platform
    raise click.ClickException(
        "Could not identify a supported platform or content type from this URL."
    )


@click.group(cls=URLFallbackGroup)
def cli():
    """Qivaryn media downloader. Pass a URL directly for automatic detection."""
    pass

@cli.command()
@click.argument("url_or_shortcode")
@click.option("--platform", type=click.Choice(tuple(PLATFORM_ENGINES)), default="instagram", show_default=True, help="Media platform to use.")
@click.option("--cookies", "--cookies-file", "cookies_file", type=click.Path(dir_okay=False), help="Path to Netscape cookies.txt file exported from browser.")
@click.option("--sessionid", help="Direct sessionid cookie string.")
@click.option("--output", "-o", default="./downloads", help="Directory to save downloaded media.")
@click.option("--db", default="qivaryn_state.db", help="Path to SQLite state tracking database.")
@click.option("--force", "-f", is_flag=True, default=False, help="Force re-download even if already downloaded.")
def single(url_or_shortcode, platform, cookies_file, sessionid, output, db, force):
    """Download a single Reel, Post, or Carousel by URL or shortcode."""
    engine = _create_engine(platform=platform, output_dir=output, db_path=db, sessionid=sessionid, cookies_file=cookies_file)
    engine.download_single(url_or_shortcode, force=force)

@cli.command()
@click.argument("username")
@click.option("--platform", type=click.Choice(tuple(PLATFORM_ENGINES)), default="instagram", show_default=True, help="Media platform to use.")
@click.option("--cookies", "--cookies-file", "cookies_file", type=click.Path(dir_okay=False), help="Path to Netscape cookies.txt file exported from browser.")
@click.option("--sessionid", help="Direct sessionid cookie string.")
@click.option("--type", "media_type", type=click.Choice(["all", "posts", "reels", "videos", "carousels"], case_sensitive=False), default="all", help="Filter media: 'all', 'posts', 'reels', 'videos', or 'carousels'.")
@click.option("--count", "-n", "count", type=int, default=None, help="Number of new items to download (e.g. 100). Automatically skips existing content and stops once this number of new items is saved.")
@click.option("--limit", "-l", "limit", type=int, default=None, help="Alias for --count.")
@click.option("--output", "-o", default="./downloads", help="Directory to save downloaded media.")
@click.option("--db", default="qivaryn_state.db", help="Path to SQLite state tracking database.")
@click.option("--threshold", default=3, help="Consecutive known posts before early-exit halt (only active when --count is omitted).")
@click.option("--max-pages", type=int, default=None, help="Maximum pagination pages to scan.")
@click.option("--force", "-f", is_flag=True, default=False, help="Force re-download even if already downloaded.")
def download(username, platform, cookies_file, sessionid, media_type, count, limit, output, db, threshold, max_pages, force):
    """Download account content with media filtering, batch count limits, and fast resumption."""
    target_count = count if count is not None else limit
    if target_count is not None and target_count <= 0:
        raise click.BadParameter("Count must be a positive integer greater than 0.", param_hint="--count")

    engine = _create_engine(
        platform=platform,
        output_dir=output,
        db_path=db,
        sessionid=sessionid,
        cookies_file=cookies_file,
        consecutive_threshold=threshold
    )
    engine.download_account(username, media_type_filter=media_type, max_pages=max_pages, force=force, count=target_count)

@cli.command("url", hidden=True)
@click.argument("url")
@click.option("--type", "media_type", type=click.Choice(["all", "posts", "reels", "videos", "carousels"], case_sensitive=False), default=None, help="Filter media: 'all', 'posts', 'reels', 'videos', or 'carousels'.")
@click.option("--cookies", "--cookies-file", "cookies_file", type=click.Path(dir_okay=False), help="Path to Netscape cookies.txt file exported from browser.")
@click.option("--sessionid", help="Direct sessionid cookie string.")
@click.option("--count", "-n", "count", type=int, default=None, help="Number of new items to download from a profile or Reels tab.")
@click.option("--limit", "-l", "limit", type=int, default=None, help="Alias for --count.")
@click.option("--output", "-o", default="./downloads", help="Directory to save downloaded media.")
@click.option("--db", default="qivaryn_state.db", help="Path to SQLite state tracking database.")
@click.option("--max-pages", type=int, default=None, help="Maximum pagination pages to scan per feed.")
@click.option("--force", "-f", is_flag=True, default=False, help="Force re-download even if already downloaded.")
def download_url(url, media_type, cookies_file, sessionid, count, limit, output, db, max_pages, force):
    """Automatically download content based on an Instagram URL."""
    target_count = count if count is not None else limit
    if target_count is not None and target_count <= 0:
        raise click.BadParameter("Count must be a positive integer greater than 0.", param_hint="--count")

    platform = _platform_for_url(url)
    engine = _create_engine(
        platform=platform,
        output_dir=output,
        db_path=db,
        sessionid=sessionid,
        cookies_file=cookies_file,
    )
    if media_type:
        engine.download_url(url, max_pages=max_pages, force=force, count=target_count, media_type=media_type)
    else:
        engine.download_url(url, max_pages=max_pages, force=force, count=target_count)

@cli.command("clear-db")
@click.argument("item", required=False, default=None)
@click.option("--platform", type=click.Choice(tuple(PLATFORM_ENGINES)), default="instagram", show_default=True, help="Media platform whose history to manage.")
@click.option("--item", "-i", "item_opt", help="Specific shortcode or post URL to remove from database.")
@click.option("--all", "-a", "clear_all_flag", is_flag=True, default=False, help="Clear entire download history from database.")
@click.option("--missing", "-m", "--prune", is_flag=True, default=False, help="Remove records whose media files were deleted from disk.")
@click.option("--output", "-o", default="./downloads", help="Directory where media files are stored.")
@click.option("--db", default="qivaryn_state.db", help="Path to SQLite state tracking database.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Confirm clearing all records without prompting.")
def clear_db(item, platform, item_opt, clear_all_flag, missing, output, db, yes):
    """Remove download records from database without deleting media files."""
    target_item = item or item_opt
    engine = _create_engine(platform=platform, output_dir=output, db_path=db)

    if target_item:
        engine.clear_database(item=target_item)
    elif missing:
        engine.clear_database(prune_missing=True)
    elif clear_all_flag or yes:
        engine.clear_database(clear_all=True)
    else:
        record_count = engine.db.get_record_count()
        if record_count == 0:
            click.echo("[*] Database is already empty (0 records).")
            return
        if click.confirm(f"Are you sure you want to clear all {record_count} download records? (Existing files will NOT be deleted)"):
            engine.clear_database(clear_all=True)
        else:
            click.echo("[-] Operation cancelled.")

if __name__ == "__main__":
    cli()