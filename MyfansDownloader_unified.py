#!/usr/bin/env python3
"""
Unified MyfansDownloader - Single Program
Combines CLI and Web Interface
"""
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Add current directory to path to allow imports
sys.path.insert(0, str(Path(__file__).parent))

from helpers.deps import install_requirements, check_python_version, check_ffmpeg_installed

def run_cli_mode():
    """Run the CLI downloader"""
    try:
        print("\n" + "="*50)
        print("MyfansDownloader - CLI Mode")
        print("="*50 + "\n")
        
        print("Choose an option:")
        print("1. Download videos")
        print("2. Download images")
        
        choice = input("\nEnter your choice (1 or 2): ").strip()
        
        if choice == "1":
            if not check_ffmpeg_installed():
                print("FFmpeg is not installed. Please install FFmpeg to continue.")
                return
            from scripts.myfans_dl import main as video_main
            print("\nRunning Download videos...\n")
            _run_cli_entry(video_main)
        elif choice == "2":
            from scripts.myfans_image_dl import main as image_main
            print("\nRunning Download images...\n")
            _run_cli_entry(image_main)
        else:
            print("Invalid choice. Please enter 1 or 2")
    except Exception as e:
        print(f"Error in CLI mode: {e}")


def _run_cli_entry(fn):
    try:
        fn()
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(f"Stopped with exit code {exc.code}")

def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def run_web_mode():
    """Run the local web app and open it in the browser once it is listening."""
    try:
        from app import app

        port = 5000
        url = f"http://127.0.0.1:{port}"
        if _port_open(port):
            print(f"MyFans Downloader is already open at {url}")
            webbrowser.open(url)
            return

        def open_when_ready():
            for _ in range(80):
                if _port_open(port):
                    try:
                        webbrowser.open(url)
                    except Exception as exc:
                        print(f"Could not open the browser: {exc}")
                        print(f"Visit {url}")
                    return
                time.sleep(0.1)
            print(f"Visit {url}")

        threading.Thread(target=open_when_ready, daemon=True).start()
        print(f"MyFans Downloader is at {url}")
        print("Close this window to stop it.")
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except OSError as exc:
        print(f"Could not start the server: {exc}")
    except Exception as e:
        print(f"Error in web mode: {e}")
        import traceback
        traceback.print_exc()

def _web_requested(argv=None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    return any(arg in {"web", "--web"} for arg in args)


def main():
    """Main entry point"""
    # Check Python version
    if not check_python_version():
        print("Python version check failed")
        sys.exit(1)
    
    # Install requirements
    if not install_requirements():
        print("Failed to install requirements")
        sys.exit(1)

    from scripts.settings_loader import migrate_token_out_of_config

    migrate_token_out_of_config()

    # The browser page opens even when FFmpeg is missing. Video downloads
    # still ask to install it; the page offers that choice itself.
    if _web_requested():
        run_web_mode()
        return

    # Main menu
    while True:
        print("\n" + "="*50)
        print("MyfansDownloader - Main Menu")
        print("="*50)
        print("\nSelect mode:")
        print("1. CLI Mode (Download videos/images)")
        print("2. Web Mode (Web interface)")
        print("3. Exit")
        
        choice = input("\nEnter your choice (1, 2, or 3): ").strip()
        
        if choice == "1":
            run_cli_mode()
        elif choice == "2":
            run_web_mode()
        elif choice == "3":
            print("\nExiting MyfansDownloader. Goodbye!")
            sys.exit(0)
        else:
            print("Invalid choice. Please enter 1, 2, or 3")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nProgram interrupted. Exiting...")
        sys.exit(0)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
