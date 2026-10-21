"""
Example demonstrating how developers can integrate Qivaryn
programmatically into their own custom Python applications.
"""

from qivaryn import QivarynEngine


def main():
    # Instagram: the default platform
    engine = QivarynEngine(
        platform="instagram",
        output_dir="./Downloads/Qivaryn",  # Platform/account folders live inside
        db_path="./my_state.db",
        sessionid="YOUR_INSTAGRAM_SESSION_ID_HERE",  # Optional for public content
        consecutive_threshold=3,
    )

    target_account = "natgeo"
    print(f"Starting programmatic download for @{target_account}...")

    # 1. Bulk download image posts and carousels with a count limit
    engine.download_account(target_account, media_type_filter="posts", count=10)

    # 2. Download only carousel albums
    # engine.download_account(target_account, media_type_filter="carousels", count=5)

    # 3. Download using a profile, Reels, or post URL directly
    # engine.download_url(f"https://www.instagram.com/{target_account}/", count=10, media_type="posts")

    # --- Other platforms -------------------------------------------------
    # Each platform uses the same engine API; only the client differs.
    # Every platform stores its media in "<output_dir>/<Platform>/<account>".

    # YouTube channel Shorts
    # youtube = QivarynEngine(platform="youtube", output_dir="./Downloads/Qivaryn")
    # youtube.download_url("https://www.youtube.com/@CHANNEL/shorts", count=10)
    # youtube.download_playlist("PLxxxxxxxxxxxxxxxx")

    # TikTok profile feed
    # tiktok = QivarynEngine(platform="tiktok", output_dir="./Downloads/Qivaryn")
    # tiktok.download_account("someuser", count=20)

    # Facebook page videos (cookies are usually required)
    # facebook = QivarynEngine(
    #     platform="facebook",
    #     output_dir="./Downloads/Qivaryn",
    #     cookies_file="./cookies.txt",
    # )
    # facebook.download_account("somepage")


if __name__ == "__main__":
    main()
