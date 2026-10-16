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

    # Run bulk download with early-exit resumption
    engine.download_account(target_account, max_pages=5)

if __name__ == "__main__":
    main()