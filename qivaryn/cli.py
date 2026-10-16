import click
from .downloader import QivarynEngine


PLATFORM_ENGINES = {
    "instagram": QivarynEngine,
}


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


@click.group()
def cli():
    """Qivaryn - media downloading engine with platform support."""
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
@click.option("--type", "media_type", type=click.Choice(["all", "posts", "reels"], case_sensitive=False), default="all", help="Filter media: 'posts', 'reels', or 'all'.")
@click.option("--output", "-o", default="./downloads", help="Directory to save downloaded media.")
@click.option("--db", default="qivaryn_state.db", help="Path to SQLite state tracking database.")
@click.option("--threshold", default=3, help="Consecutive known posts before early-exit halt.")
@click.option("--max-pages", type=int, default=None, help="Maximum pagination pages to scan.")
@click.option("--force", "-f", is_flag=True, default=False, help="Force re-download even if already downloaded.")
def download(username, platform, cookies_file, sessionid, media_type, output, db, threshold, max_pages, force):
    """Download account content with media filtering and fast bulk resumption."""
    engine = _create_engine(
        platform=platform,
        output_dir=output,
        db_path=db,
        sessionid=sessionid,
        cookies_file=cookies_file,
        consecutive_threshold=threshold
    )
    engine.download_account(username, media_type_filter=media_type, max_pages=max_pages, force=force)

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