#!/usr/bin/env python3
"""
dji-osv2mp4 - Metadata injection for converted .mp4 files.

Copies metadata (timestamps, camera model, etc.) from the original .OSV file
to the converted .mp4 file using ExifTool.

Works on Linux (Debian, SteamOS, etc.).

Dependencies:
  - ExifTool (libimage-exiftool-perl on Debian/Ubuntu,
              perl-image-exiftool on Arch/SteamOS)
  - Python 3.8+
"""

from glob import glob
import subprocess
from datetime import datetime, timedelta
import argparse
import sys
import re
import os


def get_time_from_filename(filename: str) -> str | None:
    """
    Extract timestamp from DJI filename.

    Format: CAM_20250128212939_0256_D.MP4 -> 2025:01:28 21:29:39
    Also handles: DJI_20260626205418_0001_D.OSV
    """
    match = re.search(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', filename)
    if match:
        return (f"{match.group(1)}:{match.group(2)}:{match.group(3)} "
                f"{match.group(4)}:{match.group(5)}:{match.group(6)}")
    return None


def has_description(filepath: str, tag: str) -> bool:
    """Check if an ExifTool tag exists and has content."""
    if not os.path.exists(filepath):
        return False

    cmd = ["exiftool", "-s3", tag, filepath]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return len(result.stdout.strip()) > 0
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def has_xmp_description(filepath: str) -> bool:
    """Check if XMP:Description tag exists in the file."""
    return has_description(filepath, "-XMP:Description")


def get_createdate(filepath: str) -> str | None:
    """Get CreateDate from file metadata."""
    cmd = ["exiftool", "-s3", "-CreateDate", filepath]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        out = result.stdout.strip()
        return out if out else None
    except FileNotFoundError:
        return None


def find_matching_osv(mp4_path: str, osv_dir: str | None = None) -> str | None:
    """
    Find the matching .OSV file for a given .mp4 file.

    The .mp4 filename is derived from the .OSV filename:
      DJI_20260626205418_0001_D.OSV ->
      DJI_20260626205418_0001_D.panorama.4K.mp4

    So we strip everything after the second-to-last dot and add .OSV.
    """
    basename = os.path.basename(mp4_path)
    name_no_ext, _ = os.path.splitext(basename)

    # Try stripping known suffixes: .panorama.4K, .fisheye.4K, etc.
    # Pattern: anything.panorama.RESOLUTION or anything.fisheye.RESOLUTION
    match = re.match(r'^(.+?)\.(panorama|fisheye)\.\w+$', name_no_ext)
    if match:
        osv_name = match.group(1) + ".OSV"
    else:
        # Fallback: just take the first part before any dot
        osv_name = name_no_ext.split('.')[0] + ".OSV"

    # Look in the same directory as the mp4, or in specified osv_dir
    search_dirs = [os.path.dirname(mp4_path)]
    if osv_dir:
        search_dirs.append(osv_dir)

    for d in search_dirs:
        candidate = os.path.join(d, osv_name)
        if os.path.isfile(candidate):
            return candidate

        # Also try lowercase
        candidate_lower = os.path.join(d, osv_name.lower())
        if os.path.isfile(candidate_lower):
            return candidate_lower

    return None


def copy_exif(
    src: str,
    dst: str,
    new_tz: int = 8,
    default_tz: int = 8,
    model: str = "DJI Osmo360",
    debug: bool = False,
) -> bool:
    """
    Copy metadata from .OSV file to .mp4 file, adjusting timezone.

    Args:
        src: Source .OSV file path.
        dst: Destination .mp4 file path.
        new_tz: Target timezone offset from UTC (e.g., 9 for UTC+9).
        default_tz: Default timezone of the camera (e.g., 8 for UTC+8).
        model: Camera model string to set.
        debug: Print exiftool command.

    Returns:
        True on success, False on failure.
    """
    tz_str = f"{new_tz:+03d}:00"
    shift = new_tz  # shift relative to UTC
    shift_str = f"{shift:+d}"

    # Get creation date from source
    create_date = get_createdate(src)
    if create_date is None:
        create_date = get_time_from_filename(os.path.basename(src))
        if not create_date:
            print("Failed (No time info from filename)")
            return False

        create_date_ts = datetime.strptime(create_date, "%Y:%m:%d %H:%M:%S") + timedelta(hours=-default_tz)
        create_date = create_date_ts.strftime("%Y:%m:%d %H:%M:%S")
    else:
        create_date_ts = datetime.strptime(create_date, "%Y:%m:%d %H:%M:%S")

    local_date_ts = create_date_ts + timedelta(hours=shift)
    local_date = local_date_ts.strftime("%Y:%m:%d %H:%M:%S")

    cmd = [
        "exiftool",
        "-overwrite_original",
        "-api", "LargeFileSupport=1",
        "-tagsFromFile", src,
        "-all:all",                          # Copy all metadata
        f"-Model={model}",                   # Override camera model
        "-globalTimeShift", shift_str,
        f"-XMP:description={create_date}",
        f"-Keys:CreationDate={local_date}{tz_str}",
        dst,
    ]

    if debug:
        print("  command:", " ".join(cmd))

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed ({e.stderr})")
        return False
    except FileNotFoundError:
        print("Failed (exiftool not found. Install with: sudo apt install libimage-exiftool-perl)")
        return False

    # Sync file timestamps
    src_stat = os.stat(src)
    os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Inject metadata from .OSV files into converted .mp4 files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Fix metadata for all .mp4 files in current directory (UTC+2):\n"
            "  python inject.py \"*.mp4\" 2\n\n"
            "  # Fix metadata with custom camera timezone:\n"
            "  python inject.py \"./*.mp4\" 9 -t 8\n\n"
            "  # Force overwrite existing metadata:\n"
            "  python inject.py \"./*.mp4\" 2 -f\n"
        ),
    )

    parser.add_argument("dir", help="Glob pattern for .mp4 files (e.g., '*.mp4' or './*.mp4')")
    parser.add_argument(
        "correct_tz",
        type=int,
        nargs="?",
        default=8,
        help="Target timezone offset from UTC (e.g., 2 for UTC+2, -5 for UTC-5). Default: 8",
    )
    parser.add_argument(
        "-c", "--correct-tz",
        type=int,
        dest="correct_tz_opt",
        default=None,
        help="Alternative way to specify target timezone",
    )
    parser.add_argument(
        "-t", "--default-tz",
        type=int,
        default=8,
        choices=range(-12, 15),
        metavar="[-12..14]",
        help="Default timezone of the camera timestamps (default: 8)",
    )
    parser.add_argument(
        "-o", "--osv-dir",
        default=None,
        help="Directory containing .OSV files (if different from .mp4 location)",
    )
    parser.add_argument(
        "-f", "--overwrite",
        action="store_true",
        help="Force overwrite existing metadata",
    )
    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="Print exiftool commands",
    )

    args = parser.parse_args()

    # Resolve timezone: positional arg takes precedence, then --correct-tz flag
    tz = args.correct_tz_opt if args.correct_tz_opt is not None else args.correct_tz

    # Check exiftool availability
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True)
    except FileNotFoundError:
        print("[ERROR] exiftool not found. Install it:")
        print("        Debian/Ubuntu: sudo apt install libimage-exiftool-perl")
        print("        SteamOS/Arch:  sudo pacman -S perl-image-exiftool")
        sys.exit(1)

    files = sorted(glob(args.dir))
    mp4_files = [f for f in files if f.lower().endswith(".mp4")]

    if not mp4_files:
        print(f"No .mp4 files found matching: {args.dir}")
        sys.exit(1)

    print(f"Found {len(mp4_files)} .mp4 file(s)")
    print(f"Target timezone: UTC{tz:+d}, Camera default: UTC{args.default_tz:+d}")
    print()

    success = 0
    skipped = 0
    failed = 0

    for mp4 in mp4_files:
        basename = os.path.basename(mp4)
        print(f"File: {basename} ...", end=" ", flush=True)

        # Check if already processed
        if not args.overwrite and has_xmp_description(mp4):
            print("Skipped (metadata already present)")
            skipped += 1
            continue

        # Find matching .OSV
        osv = find_matching_osv(mp4, osv_dir=args.osv_dir)
        if osv is None:
            print(f"Skipped (no matching .OSV found)")
            skipped += 1
            continue

        print(f"[{os.path.basename(osv)}] ...", end=" ", flush=True)

        if copy_exif(osv, mp4, new_tz=tz, default_tz=args.default_tz, debug=args.debug):
            print("Done")
            success += 1
        else:
            print("Failed")
            failed += 1

    print()
    print(f"Summary: {success} updated, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
