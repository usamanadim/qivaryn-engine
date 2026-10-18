"""
Example demonstrating how developers can integrate Qivaryn
programmatically into their own custom Python applications.
"""

from qivaryn import QivarynEngine

def main():
    # Initialize the engine
    engine = QivarynEngine(
        output_dir="./my_custom_downloads",
        db_path="./my_state.db",
        sessionid="YOUR_INSTAGRAM_SESSION_ID_HERE", # Optional for public, recommended for 1080p Reels
        consecutive_threshold=3
    )

    target_account = "natgeo"
    print(f"Starting programmatic download for @{target_account}...")

    # Example 1: Run bulk download for posts (photos & carousels) with count limit
    engine.download_account(target_account, media_type_filter="posts", count=10)

    # Example 2: Download only carousel albums
    # engine.download_account(target_account, media_type_filter="carousels", count=5)

    # Example 3: Download using profile or post URL directly
    # engine.download_url(f"https://www.instagram.com/{target_account}/", count=10, media_type="posts")

if __name__ == "__main__":
    main()