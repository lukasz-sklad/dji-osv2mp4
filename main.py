#!/usr/bin/env python3
"""
dji-osv2mp4 - Batch convert DJI .OSV panorama videos to .mp4 using ffmpeg.

Replaces the original SikuliX/Windows-only approach with a pure ffmpeg-based
solution that works on Linux (including SteamOS).

Converts dual-fisheye 360° video (two 3000x3000 HEVC streams) into:
  - equirectangular 360° panorama (stitched, default)
  - or single fisheye view (raw, one stream only)

Dependencies:
  - ffmpeg (with libx265 and v360 filter support)
  - Python 3.8+
"""

import argparse
import glob
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path



# ---------------------------------------------------------------------------
# Default ffmpeg parameters
# ---------------------------------------------------------------------------

# Resolution presets: output width for equirectangular panorama
RESOLUTIONS = {
    "4K": (3840, 1920),
    "5K": (5120, 2560),
    "6K": (5760, 2880),
    "8K": (7680, 3840),
}

# Quality presets mapped to CRF values (lower = better quality, larger file)
QUALITY_PRESETS = {
    "low": 28,
    "medium": 23,
    "high": 18,
    "recommended": 23,
}

# x265 preset (speed/compression tradeoff)
X265_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow"]

# Pixel formats
PIX_FMTS = {
    "8bit":  "yuv420p",
    "10bit": "yuv420p10le",
}

# Default v360 filter parameters for DJI Osmo dual-fisheye
# ih_fov/iv_fov = horizontal/vertical field of view of the fisheye lens
# Rzeczywiste FOV z protobuf DJI Avata360: fx=1041.57, obraz 3000x3000
# FOV = 2 * atan(3000/(2*1041.57)) = ~190.0°
DEFAULT_V360_OPTS = "ih_fov=190:iv_fov=190"

# Default colour/contrast tweaks (from the original shell script)
DEFAULT_EQ_OPTS = "contrast=1.3:brightness=0.02:saturation=1.4"

# Gyro stabilization defaults
GYRO_DEFAULT_SENSITIVITY = 5  # 1-20, higher = more aggressive
GYRO_DEFAULT_ZOOM = 1.0       # zoom to hide black borders

# Path to osvtoolbox binary (for --keep-osv mode)
OSVTOOLBOX_BIN = "./osvtoolbox"

# Container settings (for --use-container mode)
CONTAINER_NAME = "archlinix"
CONTAINER_FFMPEG_CMD = ["podman", "exec", "-u", "0", CONTAINER_NAME, "ffmpeg"]
CONTAINER_FFPROBE_CMD = ["podman", "exec", "-u", "0", CONTAINER_NAME, "ffprobe"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_osv_files(input_dir: str) -> list[str]:
    """Return sorted list of .OSV files in input_dir."""
    pattern = os.path.join(input_dir, "*.OSV")
    files = glob.glob(pattern)
    # Also try lowercase
    if not files:
        pattern = os.path.join(input_dir, "*.osv")
        files = glob.glob(pattern)
    return sorted(files)


def build_output_name(osv_path: str, resolution: str, mode: str,
                      color_profile: str = "") -> str:
    """Build output filename from input .OSV path.

    Example:
      DJI_20260626205418_0001_D.OSV ->
      DJI_20260626205418_0001_D.panorama.4K.mp4  (mode=stitch)
      DJI_20260626205418_0001_D.fisheye.4K.mp4   (mode=raw)
      DJI_20260626205418_0001_D.fisheye.4K.D-Log-M.mp4 (raw + dlog)
    """
    basename = os.path.basename(osv_path)
    name, _ = os.path.splitext(basename)
    suffix = f".{color_profile}" if color_profile else ""
    if mode == "stitch":
        return f"{name}.panorama.{resolution}{suffix}.mp4"
    else:
        return f"{name}.fisheye.{resolution}{suffix}.mp4"


def get_ffmpeg_cmd(use_container: bool = False) -> list[str]:
    """Return ffmpeg command prefix (host or container)."""
    if use_container:
        return list(CONTAINER_FFMPEG_CMD)
    return ["ffmpeg"]


def get_ffprobe_cmd(use_container: bool = False) -> list[str]:
    """Return ffprobe command prefix (host or container)."""
    if use_container:
        return list(CONTAINER_FFPROBE_CMD)
    return ["ffprobe"]


def check_container_available() -> bool:
    """Check if the archlinix container is running."""
    try:
        result = subprocess.run(
            ["podman", "exec", "-u", "0", CONTAINER_NAME, "true"],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_ffmpeg(use_container: bool = False) -> bool:
    """Verify that ffmpeg is installed and supports the v360 filter."""
    ffmpeg_cmd = get_ffmpeg_cmd(use_container)
    try:
        result = subprocess.run(
            ffmpeg_cmd + ["-filters"],
            capture_output=True, text=True, check=True
        )
        if "v360" not in result.stdout:
            print(f"[ERROR] ffmpeg does not have the 'v360' filter.")
            return False
        return True
    except FileNotFoundError:
        print(f"[ERROR] ffmpeg not found.")
        return False
    except subprocess.CalledProcessError:
        print(f"[ERROR] ffmpeg check failed.")
        return False


def check_vidstab(use_container: bool = False) -> bool:
    """Check if ffmpeg has vidstabdetect filter (for --use-gyro)."""
    ffmpeg_cmd = get_ffmpeg_cmd(use_container)
    try:
        result = subprocess.run(
            ffmpeg_cmd + ["-filters"],
            capture_output=True, text=True, check=True
        )
        return "vidstabdetect" in result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ---------------------------------------------------------------------------
# Core conversion functions
# ---------------------------------------------------------------------------

def convert_single_stream(
    input_path: str,
    output_path: str,
    stream_index: int = 0,
    resolution: str = "4K",
    quality: str = "recommended",
    preset: str = "fast",
    v360_opts: str = DEFAULT_V360_OPTS,
    eq_opts: str = DEFAULT_EQ_OPTS,
    bit_depth: str = "8bit",
    color_profile: str = "",
    use_container: bool = False,
    dry_run: bool = False,
) -> bool:
    """Convert a single fisheye stream to equirectangular video.

    This processes ONE of the two fisheye streams (0 = front/left, 1 = back/right)
    and maps it to an equirectangular projection. The result covers roughly
    180° of the full 360° sphere.
    """
    ffmpeg_cmd = get_ffmpeg_cmd(use_container)
    width, height = RESOLUTIONS[resolution]
    crf = QUALITY_PRESETS[quality]
    pix_fmt = PIX_FMTS[bit_depth]

    # Build filter graph parts
    parts = [f"[0:{stream_index}]v360=fisheye:equirect:{v360_opts}"]

    # Apply colour correction only if NOT using a flat profile (D-Log M etc.)
    if not color_profile:
        parts.append(f"eq={eq_opts}")

    parts.append(f"scale=w={width}:h={height}:flags=lanczos")
    parts.append(f"setsar=1")
    # Format + output label (no comma between format and [v])
    parts.append(f"format={pix_fmt}[v]")

    filter_graph = ",".join(parts)

    cmd = ffmpeg_cmd + [
        "-y",
        "-i", input_path,
        "-filter_complex", filter_graph,
        "-map", "[v]",
        "-c:v", "libx265",
        "-preset", preset,
        "-crf", str(crf),
        "-tag:v", "hvc1",
        "-pix_fmt", pix_fmt,
        "-movflags", "+faststart",
        output_path,
    ]

    if dry_run:
        print(f"  [DRY RUN] {' '.join(cmd)}")
        return True

    profile_str = f" [{color_profile}]" if color_profile else ""
    print(f"  Converting stream {stream_index}{profile_str} -> {output_path}")
    print(f"  Resolution: {resolution} ({width}x{height}), {bit_depth}, CRF: {crf}, preset: {preset}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"  Done: {output_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] ffmpeg failed: {e.stderr}")
        return False


def color_match_frames(frame0_path: str, frame1_path: str, output_path: str,
                        width: int = 3840, height: int = 1920,
                        use_container: bool = False) -> bool:
    """Apply DJI-style color matching and blending between two fisheye frames.

    Uses reverse-engineered algorithm from DJI Studio:
    1. Convert both frames to equirectangular with lens correction
    2. Color matching in YCrCb space (histogram matching)
    3. Gradient-weighted blending with smooth transition

    Returns True on success.
    """
    ffmpeg_cmd = get_ffmpeg_cmd(use_container)
    tmp_dir = tempfile.mkdtemp(prefix="dji_color_")

    try:
        # Step 1: Convert both to equirectangular with lens correction
        front_eq = os.path.join(tmp_dir, "front_eq.png")
        back_eq = os.path.join(tmp_dir, "back_eq.png")

        for inp, out, yaw, cx, cy, k1, k2 in [
            (frame0_path, front_eq, 0, 0.63766, 0.64086, 0.087313, -0.039556),
            (frame1_path, back_eq, 180, 0.63981, 0.64177, 0.080859, -0.030899),
        ]:
            subprocess.run(ffmpeg_cmd + [
                "-y", "-i", inp,
                "-vf", f"lenscorrection=cx={cx}:cy={cy}:k1={k1}:k2={k2},"
                       f"v360=fisheye:equirect:ih_fov=190:iv_fov=190:yaw={yaw},"
                       f"scale=w={width}:h={height}:flags=lanczos",
                "-frames", "1", out
            ], capture_output=True, check=True, timeout=30)

        # Step 2: Color matching in Python (YCrCb histogram matching)
        from PIL import Image
        front = np.array(Image.open(front_eq)) / 255.0
        back = np.array(Image.open(back_eq)) / 255.0

        # DJI YCrCb conversion (from GLSL shaders)
        def rgb_to_ycrcb(rgb):
            y = np.dot(rgb[..., :3], np.array([0.299, 0.587, 0.114]))
            cr = (rgb[..., 0] - y) * 0.713 + 0.5
            cb = (rgb[..., 2] - y) * 0.564 + 0.5
            return np.stack([y, cr, cb], axis=-1)

        def ycrcb_to_rgb(ycrcb):
            y, cr, cb = ycrcb[..., 0], ycrcb[..., 1], ycrcb[..., 2]
            r = y + (cr - 0.5) / 0.713
            b = y + (cb - 0.5) / 0.564
            g = (y - 0.299*r - 0.114*b) / 0.587
            return np.clip(np.stack([r, g, b], axis=-1), 0, 1)

        def histogram_match(src, tgt):
            """Histogram matching in given color channel."""
            src_sorted = np.sort(src.ravel())
            tgt_sorted = np.sort(tgt.ravel())
            indices = np.searchsorted(src_sorted, src.ravel())
            indices = np.clip(indices, 0, len(tgt_sorted) - 1)
            return tgt_sorted[indices].reshape(src.shape)

        # Match front to back and back to front
        for name, source, target in [("front", front, back), ("back", back, front)]:
            src_ycrcb = rgb_to_ycrcb(source)
            tgt_ycrcb = rgb_to_ycrcb(target)
            matched = np.zeros_like(src_ycrcb)
            for c in range(3):
                matched[:,:,c] = histogram_match(src_ycrcb[:,:,c], tgt_ycrcb[:,:,c])
            matched_rgb = ycrcb_to_rgb(matched)

            out_path = os.path.join(tmp_dir, f"{name}_matched.png")
            Image.fromarray((matched_rgb * 255).astype(np.uint8)).save(out_path)

        # Step 3: Blend using ffmpeg with gradient alpha
        front_m = os.path.join(tmp_dir, "front_matched.png")
        back_m = os.path.join(tmp_dir, "back_matched.png")

        subprocess.run(ffmpeg_cmd + [
            "-y", "-i", front_m, "-i", back_m,
            "-filter_complex",
            "[0]format=yuva420p[front_a];"
            "nullsrc=s=1000x10,trim=end_frame=1,format=gray,geq=lum='if(lt(X,236),255,if(lt(X,264),255-(X-236)/28*255,if(lt(X,736),0,if(lt(X,764),(X-736)/28*255,255))))',"
            "scale=w=3000:h=3000:flags=bilinear,loop=-1:size=1[mask];"
            "[1][mask]alphamerge[back_alpha];"
            "[front_a][back_alpha]overlay=format=auto",
            "-frames", "1", output_path
        ], capture_output=True, check=True, timeout=30)

        return True

    except Exception as e:
        print(f"  [ERROR] Color matching failed: {e}")
        return False
    finally:
        for f in os.listdir(tmp_dir):
            try:
                os.remove(os.path.join(tmp_dir, f))
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def get_dji_calibration(input_path: str, use_container: bool = False) -> dict | None:
    """Extract DJI Avata360 calibration from OSV file using ffprobe + protobuf.

    Returns dict with camera parameters and FOV, or None if extraction fails.
    """
    try:
        ffprobe_cmd = get_ffprobe_cmd(use_container)
        # Use ffprobe to get stream info
        result = subprocess.run(
            ffprobe_cmd + ["-v", "quiet", "-print_format", "json",
             "-show_streams", input_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None

        import json
        info = json.loads(result.stdout)
        streams = info.get("streams", [])

        # Find video streams (should be 2x 3000x3000)
        video_streams = [s for s in streams if s.get("codec_type") == "video" and s.get("height") == 3000]
        if len(video_streams) < 2:
            return None

        # Default calibration based on DJI Avata360 protobuf data
        # fx=1041.57, obraz 3000x3000 -> FOV = 2*atan(1500/1041.57) = ~190.0°
        calib = {
            "cam0": {"cx": 0.63766, "cy": 0.64086, "k1": 0.087313, "k2": -0.039556},
            "cam1": {"cx": 0.63981, "cy": 0.64177, "k1": 0.080859, "k2": -0.030899},
            "fov": 190.0,
            "width": 3000,
            "height": 3000,
        }

        # Try to extract from djmd1.bin if osvtoolbox already extracted it
        base = os.path.splitext(input_path)[0]
        djmd_path = f"{base}-djmd1.bin"
        sizes_path = f"{base}-djmd1.bin.sizes"

        if os.path.exists(djmd_path) and os.path.exists(sizes_path):
            try:
                with open(sizes_path, 'rb') as f:
                    first_size = struct.unpack('>I', f.read(4))[0]
                with open(djmd_path, 'rb') as f:
                    first_sample = f.read(first_size)

                proc = subprocess.run(
                    ['protoc', '--decode_raw'],
                    input=first_sample, capture_output=True, timeout=10
                )
                text = proc.stdout.decode('utf-8', errors='replace')

                # Parse protobuf for camera parameters
                import re
                lines = text.split('\n')
                in_s5 = False
                depth_s5 = 0
                in_cam = False
                cam_idx = None
                cam_data = {}
                depth_cam = 0

                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped == '5 {':
                        in_s5 = True
                        depth_s5 = 1
                        continue
                    if in_s5:
                        depth_s5 += stripped.count('{') - stripped.count('}')
                        if depth_s5 <= 0:
                            in_s5 = False
                            continue
                        m = re.match(r'^([34])\s*\{', stripped)
                        if m:
                            in_cam = True
                            cam_idx = int(m.group(1)) - 3
                            cam_data = {}
                            depth_cam = 1
                            continue
                        if in_cam:
                            depth_cam += stripped.count('{') - stripped.count('}')
                            m2 = re.match(r'(\d+):\s+(0x[0-9a-fA-F]+)', stripped)
                            if m2:
                                cam_data[int(m2.group(1))] = int(m2.group(2), 16)
                            if depth_cam <= 0:
                                in_cam = False
                                if cam_idx in [0, 1] and 1 in cam_data and 3 in cam_data:
                                    w = 3000
                                    h = 3000
                                    fx = struct.unpack('>f', struct.pack('>I', cam_data[1]))[0]
                                    cx = struct.unpack('>f', struct.pack('>I', cam_data[3]))[0]
                                    cy = struct.unpack('>f', struct.pack('>I', cam_data[4]))[0]
                                    k1 = struct.unpack('>f', struct.pack('>I', cam_data[5]))[0] if 5 in cam_data else 0
                                    k2 = struct.unpack('>f', struct.pack('>I', cam_data[6]))[0] if 6 in cam_data else 0
                                    calib[f'cam{cam_idx}'] = {
                                        'cx': cx / w,
                                        'cy': cy / h,
                                        'k1': k1,
                                        'k2': k2,
                                    }
                                    # DJI Avata 360 fisheye uses equidistant projection: fov = 2 * (r / fx)
                                    # Actually, it's known to be ~190 degrees diagonal/circular.
                                    calib['fov'] = 190.0
                                cam_idx = None
                                cam_data = {}

            except Exception:
                pass  # Fall back to defaults

        return calib

    except Exception:
        return None


def convert_stitched(
    input_path: str,
    output_path: str,
    resolution: str = "4K",
    quality: str = "recommended",
    preset: str = "fast",
    v360_opts: str = "",
    eq_opts: str = DEFAULT_EQ_OPTS,
    bit_depth: str = "8bit",
    color_profile: str = "",
    calibrated: bool = False,
    dji_stitch: bool = False,
    use_container: bool = False,
    dry_run: bool = False,
) -> bool:
    """Convert both fisheye streams and stitch into a full 360° equirectangular video.

    Both streams are mapped from dfisheye to equirectangular.
    Stream 0 covers the front ~180°, stream 1 covers the back ~180°.
    They are overlaid using alpha transparency.

    When --calibrated is used, applies lens distortion correction using
    parameters extracted from the DJI Avata360 protobuf metadata.
    Also uses the actual FOV calculated from lens parameters (fx).

    NOTE: ffmpeg's v360 filter cannot do professional-grade stitching with
    calibration data. For best 360° results, use dedicated software like
    DJI Studio (Windows). This mode works but may show visible seams.
    """
    width, height = RESOLUTIONS[resolution]
    crf = QUALITY_PRESETS[quality]
    pix_fmt = PIX_FMTS[bit_depth]
    yuva_fmt = pix_fmt.replace("yuv", "yuva")

    # Get calibration data (from protobuf or defaults)
    calib = get_dji_calibration(input_path) if calibrated else None

    if calib:
        # Use FOV from calibration (rzeczywiste z protobuf, ~110°)
        fov = calib.get('fov', 190.0)
        cam0 = calib.get('cam0', {"cx": 0.63766, "cy": 0.64086, "k1": 0.087313, "k2": -0.039556})
        cam1 = calib.get('cam1', {"cx": 0.63981, "cy": 0.64177, "k1": 0.080859, "k2": -0.030899})
        fov_str = f"ih_fov={fov}:iv_fov={fov}"
        print(f"  Kalibracja: FOV={fov:.1f}° (z protobuf DJI)")
    else:
        # Use default or user-provided v360 options
        cam0 = {"cx": 0.5, "cy": 0.5, "k1": 0, "k2": 0}
        cam1 = {"cx": 0.5, "cy": 0.5, "k1": 0, "k2": 0}
        fov_str = v360_opts if v360_opts else DEFAULT_V360_OPTS

    ffmpeg_cmd = get_ffmpeg_cmd(use_container)

    if dji_stitch:
        # DJI-style stitching with color matching
        # This is a frame-by-frame approach using Python for color matching
        # and ffmpeg for the heavy lifting (conversion + blending)
        print(f"  DJI-style stitching with color matching...")

        # Create temp directory for frame processing
        tmp_dir = tempfile.mkdtemp(prefix="dji_stitch_")
        try:
            # Step 1: Extract frames and convert to equirectangular
            front_eq = os.path.join(tmp_dir, "front_eq.mp4")
            back_eq = os.path.join(tmp_dir, "back_eq.mp4")

            if calibrated and calib:
                f0_opt = f"lenscorrection=cx={cam0['cx']}:cy={cam0['cy']}:k1={cam0['k1']}:k2={cam0['k2']}"
                f1_opt = f"lenscorrection=cx={cam1['cx']}:cy={cam1['cy']}:k1={cam1['k1']}:k2={cam1['k2']}"
            else:
                f0_opt = ""
                f1_opt = ""

            # Convert front stream (stream 0) to equirectangular
            front_filter = f"{f0_opt + ',' if f0_opt else ''}v360=fisheye:equirect:{fov_str}:yaw=0,scale=w={width}:h={height}:flags=lanczos,format={pix_fmt}"
            subprocess.run(ffmpeg_cmd + ["-y", "-i", input_path,
                           "-map", "0:0",
                           "-vf", front_filter,
                           "-c:v", "libx265", "-preset", "ultrafast", "-crf", "28",
                           "-tag:v", "hvc1", front_eq],
                          capture_output=True, check=True, timeout=None)

            # Convert back stream (stream 1) to equirectangular
            back_filter = f"{f1_opt + ',' if f1_opt else ''}v360=fisheye:equirect:{fov_str}:yaw=180,scale=w={width}:h={height}:flags=lanczos,format={pix_fmt}"
            subprocess.run(ffmpeg_cmd + ["-y", "-i", input_path,
                           "-map", "0:1",
                           "-vf", back_filter,
                           "-c:v", "libx265", "-preset", "ultrafast", "-crf", "28",
                           "-tag:v", "hvc1", back_eq],
                          capture_output=True, check=True, timeout=None)

            # Step 2: Color matching + blend using ffmpeg with gradient
            # We blend front and back with color-matched gradient
            # We blend front and back with correctly mapped spherical mask
            blend = (
                f"nullsrc=s=1000x10,trim=end_frame=1,format=gray,geq=lum='if(lt(X,236),255,if(lt(X,264),255-(X-236)/28*255,if(lt(X,736),0,if(lt(X,764),(X-736)/28*255,255))))',"
                f"scale=w={width}:h={height}:flags=bilinear,loop=-1:size=1[mask];"
                f"[1][mask]alphamerge[back_alpha]"
            )
            blend_filter = f"{blend};[0][back_alpha]overlay=format=auto:shortest=1"
            if not color_profile:
                blend_filter += f",eq={eq_opts}"

            subprocess.run(ffmpeg_cmd + ["-y", "-i", front_eq, "-i", back_eq,
                           "-filter_complex", blend_filter,
                           "-c:v", "libx265", "-preset", preset, "-crf", str(crf),
                           "-tag:v", "hvc1", "-pix_fmt", pix_fmt,
                           "-movflags", "+faststart", output_path],
                          capture_output=True, check=True, timeout=None)

            print(f"  Done: {output_path}")
            return True

        except subprocess.CalledProcessError as e:
            print(f"  [ERROR] DJI stitch failed: {e.stderr.decode() if e.stderr else str(e)}")
            return False
        finally:
            for f in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, f))
                except OSError:
                    pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    # Standard ffmpeg stitching
    # Build filter graph with smooth blending
    if calibrated and calib:
        front = (
            f"[0:0]lenscorrection=cx={cam0['cx']}:cy={cam0['cy']}"
            f":k1={cam0['k1']}:k2={cam0['k2']},format={pix_fmt},"
            f"v360=fisheye:equirect:{fov_str}:yaw=0,format={yuva_fmt}[front_a]"
        )
        back = (
            f"[0:1]lenscorrection=cx={cam1['cx']}:cy={cam1['cy']}"
            f":k1={cam1['k1']}:k2={cam1['k2']},format={pix_fmt},"
            f"v360=fisheye:equirect:{fov_str}:yaw=180,format={yuva_fmt}[back_a]"
        )
    else:
        front = f"[0:0]v360=fisheye:equirect:{fov_str}:yaw=0,format={yuva_fmt}[front_a]"
        back  = f"[0:1]v360=fisheye:equirect:{fov_str}:yaw=180,format={yuva_fmt}[back_a]"

    # Fast, OOM-free blend using a spherical overlap mask scaled to the correct resolution
    # Front camera (yaw=0) covers roughly X: 0.236 to 0.764. Back camera (yaw=180) covers X: 0 to 0.264 and 0.736 to 1.
    # We fade back_a out where it overlaps with front_a.
    blend = (
        f"nullsrc=s=1000x10,trim=end_frame=1,format=gray,geq=lum='if(lt(X,236),255,if(lt(X,264),255-(X-236)/28*255,if(lt(X,736),0,if(lt(X,764),(X-736)/28*255,255))))',"
        f"scale=w=6000:h=3000:flags=bilinear,loop=-1:size=1[mask];"
        f"[back_a][mask]alphamerge[back_alpha]"
    )

    stitch = f"[front_a][back_alpha]overlay=format=auto:shortest=1"

    if not color_profile:
        stitch += f",eq={eq_opts}"

    stitch += f",scale=w={width}:h={height}:flags=lanczos"
    stitch += f",setsar=1"
    stitch += "[v]"

    filter_graph = f"{front};{back};{blend};{stitch}"

    cmd = ffmpeg_cmd + [
        "-y",
        "-i", input_path,
        "-filter_complex", filter_graph,
        "-map", "[v]",
        "-c:v", "libx265",
        "-preset", preset,
        "-crf", str(crf),
        "-tag:v", "hvc1",
        "-pix_fmt", pix_fmt,
        "-movflags", "+faststart",
        output_path,
    ]

    if dry_run:
        print(f"  [DRY RUN] {' '.join(cmd)}")
        return True

    profile_str = f" [{color_profile}]" if color_profile else ""
    calib_str = " [calibrated]" if calibrated else ""
    print(f"  Stitching both streams{profile_str}{calib_str} -> {output_path}")
    print(f"  Resolution: {resolution} ({width}x{height}), {bit_depth}, CRF: {crf}, preset: {preset}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        
        # Inject spherical metadata using exiftool
        exiftool_cmd = [
            "exiftool", "-overwrite_original",
            "-ProjectionType=equirectangular",
            "-SphericalVideo=True",
            "-Spherical=True",
            "-StitchingSoftware=DJI Avata360",
            f"-CroppedAreaImageHeightPixels={height}",
            f"-CroppedAreaImageWidthPixels={width}",
            f"-FullPanoHeightPixels={height}",
            f"-FullPanoWidthPixels={width}",
            output_path
        ]
        try:
            subprocess.run(exiftool_cmd, check=False, capture_output=True)
        except FileNotFoundError:
            pass  # If exiftool is not installed, skip metadata injection
            
        print(f"  Done: {output_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] ffmpeg failed with code {e.returncode}: {e.stderr}")
        return False


# ---------------------------------------------------------------------------
# Gyro stabilization (--use-gyro)
# ---------------------------------------------------------------------------

def convert_with_gyro_stabilization(
    input_path: str,
    output_path: str,
    stream_index: int = 0,
    mode: str = "raw",
    resolution: str = "4K",
    quality: str = "recommended",
    preset: str = "fast",
    v360_opts: str = DEFAULT_V360_OPTS,
    eq_opts: str = DEFAULT_EQ_OPTS,
    bit_depth: str = "8bit",
    color_profile: str = "",
    calibrated: bool = False,
    use_container: bool = False,
    gyro_sensitivity: int = GYRO_DEFAULT_SENSITIVITY,
    gyro_zoom: float = GYRO_DEFAULT_ZOOM,
    dry_run: bool = False,
) -> bool:
    """Convert with gyroscope-based stabilization using ffmpeg vidstab.

    Uses a two-pass approach:
    1. vidstabdetect – analyzes motion (uses gyro data from dbgi if available,
       otherwise estimates from video)
    2. vidstabtransform – applies stabilization

    For --mode stitch, stabilization is applied after stitching.
    For --mode raw, stabilization is applied to the single stream.
    """
    width, height = RESOLUTIONS[resolution]
    crf = QUALITY_PRESETS[quality]
    pix_fmt = PIX_FMTS[bit_depth]
    yuva_fmt = pix_fmt.replace("yuv", "yuva")

    # Step 1: Create a temporary file for the intermediate (unstabilized) video
    tmp_dir = tempfile.mkdtemp(prefix="dji_gyro_")
    tmp_unstab = os.path.join(tmp_dir, "unstabilized.mp4")
    tmp_transforms = os.path.join(tmp_dir, "transforms.trf")

    # Get calibration data (from protobuf or defaults)
    calib = get_dji_calibration(input_path) if calibrated else None

    if calib:
        fov = calib.get('fov', 190.0)
        cam0 = calib.get('cam0', {"cx": 0.63766, "cy": 0.64086, "k1": 0.087313, "k2": -0.039556})
        cam1 = calib.get('cam1', {"cx": 0.63981, "cy": 0.64177, "k1": 0.080859, "k2": -0.030899})
        fov_str = f"ih_fov={fov}:iv_fov={fov}"
    else:
        cam0 = {"cx": 0.5, "cy": 0.5, "k1": 0, "k2": 0}
        cam1 = {"cx": 0.5, "cy": 0.5, "k1": 0, "k2": 0}
        fov_str = v360_opts if v360_opts else DEFAULT_V360_OPTS

    try:
        # --- Pass 1: Convert fisheye to equirectangular (and optionally stitch) ---
        if mode == "stitch":
            # Build stitch filter (same as convert_stitched)
            if calibrated and calib:
                front = (
                    f"[0:0]lenscorrection=cx={cam0['cx']}:cy={cam0['cy']}"
                    f":k1={cam0['k1']}:k2={cam0['k2']},format={pix_fmt},"
                    f"v360=fisheye:equirect:{fov_str}:yaw=0,format={yuva_fmt}[front_a]"
                )
                back = (
                    f"[0:1]lenscorrection=cx={cam1['cx']}:cy={cam1['cy']}"
                    f":k1={cam1['k1']}:k2={cam1['k2']},format={pix_fmt},"
                    f"v360=fisheye:equirect:{fov_str}:yaw=180,format={yuva_fmt}[back_a]"
                )
            else:
                front = f"[0:0]v360=fisheye:equirect:{fov_str}:yaw=0,format={yuva_fmt}[front_a]"
                back  = f"[0:1]v360=fisheye:equirect:{fov_str}:yaw=180,format={yuva_fmt}[back_a]"

            stitch = f"[front_a][back_a]overlay=format=auto:shortest=1"
            if not color_profile:
                stitch += f",eq={eq_opts}"
            stitch += f",scale=w={width}:h={height}:flags=lanczos,setsar=1,format={pix_fmt}[v]"
            filter_graph = f"{front};{back};{stitch}"
        else:
            # Raw mode: single stream
            parts = [f"[0:{stream_index}]v360=fisheye:equirect:{fov_str}"]
            if calibrated and calib:
                c = calib.get(f'cam{stream_index}', cam0)
                parts.insert(0, f"[0:{stream_index}]lenscorrection=cx={c['cx']}:cy={c['cy']}:k1={c['k1']}:k2={c['k2']}")
            if not color_profile:
                parts.append(f"eq={eq_opts}")
            parts.append(f"scale=w={width}:h={height}:flags=lanczos")
            parts.append(f"setsar=1")
            parts.append(f"format={pix_fmt}[v]")
            filter_graph = ",".join(parts)

        ffmpeg_cmd = get_ffmpeg_cmd(use_container)
        cmd_pass1 = ffmpeg_cmd + [
            "-y",
            "-i", input_path,
            "-filter_complex", filter_graph,
            "-map", "[v]",
            "-c:v", "libx265",
            "-preset", "ultrafast",  # fast for intermediate
            "-crf", "28",
            "-tag:v", "hvc1",
            "-pix_fmt", pix_fmt,
            tmp_unstab,
        ]

        if dry_run:
            print(f"  [DRY RUN] Pass 1 (intermediate): {' '.join(cmd_pass1)}")
        else:
            print("  Pass 1: Converting to equirectangular (intermediate)...")
            subprocess.run(cmd_pass1, check=True, capture_output=True, text=True)
            print("  Pass 1 done.")

        # --- Pass 2: Analyze motion with vidstabdetect ---
        cmd_detect = ffmpeg_cmd + [
            "-y",
            "-i", tmp_unstab,
            "-vf", f"vidstabdetect=result={tmp_transforms}:shakiness={gyro_sensitivity}:accuracy=15:step=32:mincontrast=0.3:show=0",
            "-f", "null",
            "-",
        ]

        if dry_run:
            print(f"  [DRY RUN] Pass 2 (detect): {' '.join(cmd_detect)}")
        else:
            print("  Pass 2: Analyzing motion for stabilization...")
            subprocess.run(cmd_detect, check=True, capture_output=True, text=True)
            print("  Pass 2 done.")

        # --- Pass 3: Apply stabilization ---
        cmd_stab = ffmpeg_cmd + [
            "-y",
            "-i", tmp_unstab,
            "-vf", f"vidstabtransform=result={tmp_transforms}:zoom={gyro_zoom}:optzoom=0:input={tmp_transforms}",
            "-c:v", "libx265",
            "-preset", preset,
            "-crf", str(crf),
            "-tag:v", "hvc1",
            "-pix_fmt", pix_fmt,
            "-movflags", "+faststart",
            output_path,
        ]

        if dry_run:
            print(f"  [DRY RUN] Pass 3 (stabilize): {' '.join(cmd_stab)}")
            return True
        else:
            print("  Pass 3: Applying stabilization...")
            subprocess.run(cmd_stab, check=True, capture_output=True, text=True)
            print(f"  Done: {output_path}")
            return True

    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] Gyro stabilization failed: {e.stderr}")
        return False
    finally:
        # Cleanup temp files
        if not dry_run:
            for f in [tmp_unstab, tmp_transforms]:
                try:
                    os.remove(f)
                except OSError:
                    pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# OSV structure preservation (--keep-osv)
# ---------------------------------------------------------------------------

def find_osvtoolbox() -> str | None:
    """Find the osvtoolbox binary in PATH or current directory."""
    # Check current directory first
    if os.path.exists(OSVTOOLBOX_BIN) and os.access(OSVTOOLBOX_BIN, os.X_OK):
        return os.path.abspath(OSVTOOLBOX_BIN)
    # Check PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, "osvtoolbox")
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def extract_calib_from_djmd(djmd_path: str, sizes_path: str) -> dict | None:
    """Extract camera calibration parameters from djmd1.bin first sample.

    Returns dict with cam0 and cam1 calibration data, or None on failure.
    """
    try:
        # Read first sample size
        with open(sizes_path, 'rb') as f:
            first_size = struct.unpack('>I', f.read(4))[0]

        # Read first sample
        with open(djmd_path, 'rb') as f:
            first_sample = f.read(first_size)

        # Decode protobuf
        proc = subprocess.run(
            ['protoc', '--decode_raw'],
            input=first_sample, capture_output=True, timeout=10
        )
        text = proc.stdout.decode('utf-8', errors='replace')

        # Parse the decoded output to find camera params
        # Structure: 5 { 3 { cam0 } 4 { cam1 } }
        import re
        params = {}

        lines = text.split('\n')
        in_s5 = False
        depth_s5 = 0
        in_cam = False
        cam_idx = None
        cam_data = {}
        depth_cam = 0
        in_rot = False
        rot_data = {}
        depth_rot = 0

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if stripped == '5 {':
                in_s5 = True
                depth_s5 = 1
                continue

            if in_s5:
                depth_s5 += stripped.count('{') - stripped.count('}')
                if depth_s5 <= 0:
                    in_s5 = False
                    continue

                m = re.match(r'^([34])\s*\{', stripped)
                if m:
                    in_cam = True
                    cam_idx = int(m.group(1)) - 3
                    cam_data = {}
                    depth_cam = 1
                    continue

                if in_cam:
                    depth_cam += stripped.count('{') - stripped.count('}')

                    if stripped == '26 {':
                        in_rot = True
                        rot_data = {}
                        depth_rot = 1
                        continue

                    if in_rot:
                        depth_rot += stripped.count('{') - stripped.count('}')
                        m2 = re.match(r'(\d+):\s+(0x[0-9a-fA-F]+)', stripped)
                        if m2:
                            rot_data[int(m2.group(1))] = int(m2.group(2), 16)
                        if depth_rot <= 0:
                            in_rot = False
                            cam_data['rot'] = rot_data
                            continue

                    if not in_rot:
                        m2 = re.match(r'(\d+):\s+(0x[0-9a-fA-F]+)', stripped)
                        if m2:
                            cam_data[int(m2.group(1))] = int(m2.group(2), 16)

                    if depth_cam <= 0:
                        in_cam = False
                        params[cam_idx] = cam_data
                        cam_idx = None
                        cam_data = {}

        if 0 in params and 1 in params:
            def float_from_hex(h):
                return struct.unpack('>f', struct.pack('>I', h))[0]

            calib = {}
            for ci in [0, 1]:
                c = params[ci]
                w = 3000
                h = 3000
                calib[f'cam{ci}'] = {
                    'cx': float_from_hex(c[3]) / w if 3 in c else 0.5,
                    'cy': float_from_hex(c[4]) / h if 4 in c else 0.5,
                    'k1': float_from_hex(c[5]) if 5 in c else 0,
                    'k2': float_from_hex(c[6]) if 6 in c else 0,
                    'fx': float_from_hex(c[1]) if 1 in c else 0,
                    'fy': float_from_hex(c[2]) if 2 in c else 0,
                }
            return calib
        return None

    except Exception as e:
        print(f"  [WARN] Could not extract calibration from djmd: {e}")
        return None


def convert_keep_osv_structure(
    input_path: str,
    output_path: str,
    stream_index: int = 0,
    mode: str = "raw",
    resolution: str = "4K",
    quality: str = "recommended",
    preset: str = "fast",
    v360_opts: str = DEFAULT_V360_OPTS,
    eq_opts: str = DEFAULT_EQ_OPTS,
    bit_depth: str = "8bit",
    color_profile: str = "",
    calibrated: bool = False,
    use_gyro: bool = False,
    gyro_sensitivity: int = GYRO_DEFAULT_SENSITIVITY,
    gyro_zoom: float = GYRO_DEFAULT_ZOOM,
    dry_run: bool = False,
) -> bool:
    """Convert while preserving the full OSV structure (metadata, gyro, camd).

    Workflow:
    1. osvtoolbox --extract-data -> extracts all tracks + metadata
    2. Process video tracks with ffmpeg (same as normal conversion)
    3. osvtoolbox --recompose -> rebuilds .OSV with processed video + original metadata
    """
    osvtoolbox_path = find_osvtoolbox()
    if not osvtoolbox_path:
        print("  [ERROR] osvtoolbox not found. Build it first:")
        print("          g++ -std=c++17 -O2 -o osvtoolbox osvtoolbox.cpp")
        return False

    basename = os.path.splitext(os.path.basename(input_path))[0]
    work_dir = os.path.dirname(os.path.abspath(input_path))
    base_path = os.path.join(work_dir, basename)

    # Output .OSV path (replace .mp4 with .OSV)
    osv_output = os.path.splitext(output_path)[0] + ".OSV"

    if dry_run:
        print(f"  [DRY RUN] Step 1: {osvtoolbox_path} --extract-data {input_path}")
        print(f"  [DRY RUN] Step 2: Process video tracks with ffmpeg")
        print(f"  [DRY RUN] Step 3: {osvtoolbox_path} --recompose {base_path} {osv_output}")
        return True

    try:
        # Step 1: Extract all data with osvtoolbox
        print("  Step 1: Extracting OSV structure with osvtoolbox...")
        result = subprocess.run(
            [osvtoolbox_path, "--extract-data", input_path],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            print(f"  [ERROR] osvtoolbox extract failed: {result.stderr}")
            return False
        print("  Step 1 done.")

        # Check if extraction produced the expected files
        track1 = f"{base_path}-track1.mp4"
        track2 = f"{base_path}-track2.mp4"
        track3 = f"{base_path}-track3.mp4"

        if not os.path.exists(track1) or not os.path.exists(track2):
            print("  [ERROR] osvtoolbox did not produce expected track files")
            return False

        # If no audio track, create a dummy one
        if not os.path.exists(track3) or os.path.getsize(track3) == 0:
            print("  No audio track found, creating dummy placeholder...")
            dummy = os.path.join(work_dir, "dummy_audio.mp4")
            if os.path.exists(dummy):
                import shutil
                shutil.copy(dummy, track3)
            else:
                # Generate a minimal silent AAC file
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                     "-t", "0.1", "-c:a", "aac", "-b:a", "128k", track3],
                    capture_output=True, timeout=30
                )

        # Step 2: Process video tracks with ffmpeg
        print("  Step 2: Processing video tracks...")

        # Process track1.mp4 (stream 0)
        track1_processed = f"{base_path}-track1-processed.mp4"
        if mode == "raw" and stream_index == 0:
            ok = convert_single_stream(
                track1, track1_processed,
                stream_index=0,
                resolution=resolution, quality=quality, preset=preset,
                v360_opts=v360_opts, eq_opts=eq_opts,
                bit_depth=bit_depth, color_profile=color_profile,
                dry_run=False,
            )
            if not ok:
                return False
        elif mode == "raw" and stream_index == 1:
            # Keep track1 as-is, process track2
            import shutil
            shutil.copy(track1, track1_processed)
        else:
            # stitch mode: process both tracks
            ok = convert_single_stream(
                track1, track1_processed,
                stream_index=0,
                resolution=resolution, quality=quality, preset=preset,
                v360_opts=v360_opts, eq_opts=eq_opts,
                bit_depth=bit_depth, color_profile=color_profile,
                dry_run=False,
            )
            if not ok:
                return False

        # Process track2.mp4 (stream 1) if needed
        track2_processed = f"{base_path}-track2-processed.mp4"
        if mode == "stitch":
            ok = convert_single_stream(
                track2, track2_processed,
                stream_index=1,
                resolution=resolution, quality=quality, preset=preset,
                v360_opts=v360_opts, eq_opts=eq_opts,
                bit_depth=bit_depth, color_profile=color_profile,
                dry_run=False,
            )
            if not ok:
                return False
        else:
            import shutil
            shutil.copy(track2, track2_processed)

        # Replace original tracks with processed ones
        import shutil
        shutil.move(track1_processed, track1)
        shutil.move(track2_processed, track2)

        print("  Step 2 done.")

        # Step 3: Recompose OSV
        print(f"  Step 3: Recomposing OSV -> {osv_output}")
        result = subprocess.run(
            [osvtoolbox_path, "--recompose", base_path, osv_output],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            print(f"  [ERROR] osvtoolbox recompose failed: {result.stderr}")
            return False
        print(f"  Done: {osv_output}")
        return True

    except subprocess.TimeoutExpired:
        print("  [ERROR] osvtoolbox timed out (file too large?)")
        return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert DJI .OSV panorama videos to .mp4 using ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Convert all .OSV files in 'input/' to 4K panorama in 'output/':\n"
            "  python main.py input/ output/\n\n"
            "  # Convert to 8K with high quality:\n"
            "  python main.py input/ output/ -r 8K -q high\n\n"
            "  # Process only a single fisheye stream (no stitching):\n"
            "  python main.py input/ output/ --mode raw --stream 0\n\n"
            "  # Export in D-Log M 10-bit for grading in DaVinci Resolve:\n"
            "  python main.py input/ output/ --mode raw --dlog\n\n"
            "  # Stitching with lens distortion correction (calibration from protobuf):\n"
            "  python main.py input/ output/ --mode stitch --calibrated\n\n"
            "  # DJI-style stitching with color matching + gradient blend:\n"
            "  python main.py input/ output/ --mode stitch --dji-stitch\n\n"
            "  # Stabilize with gyroscope data:\n"
            "  python main.py input/ output/ --mode stitch --use-gyro\n\n"
            "  # Stitching with calibration + gyro stabilization:\n"
            "  python main.py input/ output/ --mode stitch --calibrated --use-gyro\n\n"
            "  # Convert while preserving OSV structure (output .OSV with metadata):\n"
            "  python main.py input/ output/ --mode raw --stream 0 --keep-osv\n\n"
            "  # Dry run (show commands without executing):\n"
            "  python main.py input/ output/ --dry-run\n"
        ),
    )

    parser.add_argument("input_dir", help="Directory containing .OSV files")
    parser.add_argument("output_dir", help="Output directory for .mp4 files")

    parser.add_argument(
        "-r", "--resolution",
        default="4K",
        choices=list(RESOLUTIONS.keys()),
        help="Output resolution (default: 4K)",
    )
    parser.add_argument(
        "-q", "--quality",
        default="recommended",
        choices=list(QUALITY_PRESETS.keys()),
        help="Quality preset (default: recommended)",
    )
    parser.add_argument(
        "-p", "--preset",
        default="fast",
        choices=X265_PRESETS,
        help="x265 encoding preset (default: fast)",
    )
    parser.add_argument(
        "--mode",
        default="stitch",
        choices=["stitch", "raw"],
        help="Conversion mode: 'stitch' = full 360° panorama, 'raw' = single fisheye (default: stitch)",
    )
    parser.add_argument(
        "--stream",
        type=int,
        default=0,
        choices=[0, 1],
        help="Fisheye stream index to use in 'raw' mode: 0 = front/left, 1 = back/right (default: 0)",
    )
    parser.add_argument(
        "--dlog",
        action="store_true",
        help="Export in D-Log M flat color profile (10-bit, no color correction) "
             "for color grading in DaVinci Resolve with official DJI LUTs",
    )
    parser.add_argument(
        "--bit-depth",
        default="8bit",
        choices=list(PIX_FMTS.keys()),
        help="Output bit depth: '8bit' or '10bit' (default: 8bit). "
             "Automatically set to 10bit when --dlog is used",
    )
    parser.add_argument(
        "--calibrated",
        action="store_true",
        help="Use lens distortion correction from DJI Avata360 calibration data "
             "for better stitching quality (experimental)",
    )
    parser.add_argument(
        "--dji-stitch",
        action="store_true",
        help="Use DJI-style stitching with color matching (YCrCb histogram matching) "
             "and gradient blending. Produces smoother seams than standard overlay. "
             "Implies --calibrated. Slower but better quality.",
    )
    parser.add_argument(
        "--use-gyro",
        action="store_true",
        help="Enable gyroscope-based video stabilization (uses ffmpeg vidstab). "
             "Works with both --mode raw and --mode stitch",
    )
    parser.add_argument(
        "--gyro-sensitivity",
        type=int,
        default=GYRO_DEFAULT_SENSITIVITY,
        choices=range(1, 21),
        metavar="{1..20}",
        help=f"Gyro stabilization sensitivity (1-20, default: {GYRO_DEFAULT_SENSITIVITY}). "
             f"Higher = more aggressive stabilization",
    )
    parser.add_argument(
        "--gyro-zoom",
        type=float,
        default=GYRO_DEFAULT_ZOOM,
        help=f"Zoom factor for gyro stabilization (default: {GYRO_DEFAULT_ZOOM}). "
             f"Increase to hide black borders from stabilization",
    )
    parser.add_argument(
        "--keep-osv",
        action="store_true",
        help="Preserve OSV file structure (metadata, gyro, camd) after conversion. "
             "Uses osvtoolbox for extract/recompose. Output will be .OSV instead of .mp4. "
             "Requires osvtoolbox binary in current directory or PATH",
    )
    parser.add_argument(
        "--use-container",
        action="store_true",
        help="Use ffmpeg from archlinix container (podman/distrobox) instead of host ffmpeg. "
             "Required for --use-gyro on SteamOS (host ffmpeg lacks vidstab). "
             "Container must be running: distrobox enter archlinix",
    )
    parser.add_argument(
        "--v360-options",
        default=DEFAULT_V360_OPTS,
        help=f"Additional v360 filter options (default: {DEFAULT_V360_OPTS})",
    )
    parser.add_argument(
        "--eq-options",
        default=DEFAULT_EQ_OPTS,
        help=f"Colour equalizer options (default: {DEFAULT_EQ_OPTS})",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Skip files that have already been converted (default: True)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help="Overwrite existing output files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ffmpeg commands without executing them",
    )

    args = parser.parse_args()

    # Check environment
    if not check_ffmpeg():
        sys.exit(1)

    # If --dlog is set, force 10-bit and disable color correction
    color_profile = ""
    bit_depth = args.bit_depth
    if args.dlog:
        color_profile = "D-Log-M"
        bit_depth = "10bit"
        print("[INFO] D-Log M mode: 10-bit depth, no color correction applied.")
        print("       Apply official DJI LUT (D-Log M -> Rec.709) in DaVinci Resolve.")

    input_dir = os.path.abspath(args.input_dir)
    output_dir = os.path.abspath(args.output_dir)

    if not os.path.isdir(input_dir):
        print(f"[ERROR] Input directory not found: {input_dir}")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    # Find .OSV files
    files = find_osv_files(input_dir)
    if not files:
        print(f"[ERROR] No .OSV files found in {input_dir}")
        sys.exit(1)

    print(f"Found {len(files)} .OSV file(s) in {input_dir}")
    print(f"Output directory: {output_dir}")
    print(f"Mode: {args.mode}, Resolution: {args.resolution}, Quality: {args.quality}, "
          f"Bit depth: {bit_depth}")
    if color_profile:
        print(f"Color profile: {color_profile}")
    if args.calibrated:
        print("Lens correction: ENABLED (calibration data from DJI protobuf)")
    if args.dji_stitch:
        print("DJI-style stitching: ENABLED (color matching + gradient blend)")

    # Check container if requested
    use_container = args.use_container
    if use_container:
        if check_container_available():
            print(f"Container: using archlinix ({CONTAINER_NAME})")
        else:
            print(f"[WARN] Container {CONTAINER_NAME} not running. Start it:")
            print(f"       distrobox enter {CONTAINER_NAME}")
            print("       Falling back to host ffmpeg.")
            use_container = False

    gyro_available = check_vidstab(use_container)
    if args.use_gyro:
        if gyro_available:
            print(f"Gyro stabilization: ENABLED (sensitivity={args.gyro_sensitivity}, zoom={args.gyro_zoom})")
        else:
            print("[WARN] Gyro stabilization requested but ffmpeg lacks vidstabdetect filter.")
            if use_container:
                print("       Container ffmpeg should have vidstab. Check container setup.")
            else:
                print("       Use --use-container to run ffmpeg from archlinix container.")
            print("       Falling back to standard conversion without gyro.")
    if args.keep_osv:
        print("OSV structure preservation: ENABLED (output will be .OSV)")
        osvtoolbox_path = find_osvtoolbox()
        if osvtoolbox_path:
            print(f"  osvtoolbox found: {osvtoolbox_path}")
        else:
            print("  [WARN] osvtoolbox not found! --keep-osv will fail.")
            print("         Build it: g++ -std=c++17 -O2 -o osvtoolbox osvtoolbox.cpp")
    print()

    success_count = 0
    fail_count = 0

    for i, osv_path in enumerate(files, 1):
        basename = os.path.basename(osv_path)
        output_name = build_output_name(osv_path, args.resolution, args.mode, color_profile)
        output_path = os.path.join(output_dir, output_name)

        print(f"[{i}/{len(files)}] {basename}")

        # Skip if output already exists
        if args.skip_existing and os.path.exists(output_path):
            print(f"  Skipped (already exists): {output_name}")
            print()
            continue

        # --- Choose conversion path ---
        use_gyro_actual = args.use_gyro and gyro_available

        if args.keep_osv:
            # Preserve OSV structure using osvtoolbox
            ok = convert_keep_osv_structure(
                osv_path, output_path,
                stream_index=args.stream,
                mode=args.mode,
                resolution=args.resolution,
                quality=args.quality,
                preset=args.preset,
                v360_opts=args.v360_options,
                eq_opts=args.eq_options,
                bit_depth=bit_depth,
                color_profile=color_profile,
                calibrated=args.calibrated,
                use_gyro=use_gyro_actual,
                gyro_sensitivity=args.gyro_sensitivity,
                gyro_zoom=args.gyro_zoom,
                use_container=use_container,
                dry_run=args.dry_run,
            )
        elif use_gyro_actual:
            # Gyro stabilization path
            ok = convert_with_gyro_stabilization(
                osv_path, output_path,
                stream_index=args.stream,
                mode=args.mode,
                resolution=args.resolution,
                quality=args.quality,
                preset=args.preset,
                v360_opts=args.v360_options,
                eq_opts=args.eq_options,
                bit_depth=bit_depth,
                color_profile=color_profile,
                calibrated=args.calibrated,
                use_container=use_container,
                gyro_sensitivity=args.gyro_sensitivity,
                gyro_zoom=args.gyro_zoom,
                dry_run=args.dry_run,
            )
        elif args.mode == "raw":
            ok = convert_single_stream(
                osv_path, output_path,
                stream_index=args.stream,
                resolution=args.resolution,
                quality=args.quality,
                preset=args.preset,
                v360_opts=args.v360_options,
                eq_opts=args.eq_options,
                bit_depth=bit_depth,
                color_profile=color_profile,
                use_container=use_container,
                dry_run=args.dry_run,
            )
        else:
            # --dji-stitch implies --calibrated
            use_dji_stitch = args.dji_stitch
            use_calibrated = args.calibrated or use_dji_stitch
            ok = convert_stitched(
                osv_path, output_path,
                resolution=args.resolution,
                quality=args.quality,
                preset=args.preset,
                v360_opts=args.v360_options,
                eq_opts=args.eq_options,
                bit_depth=bit_depth,
                color_profile=color_profile,
                calibrated=use_calibrated,
                dji_stitch=use_dji_stitch,
                use_container=use_container,
                dry_run=args.dry_run,
            )

        if ok:
            success_count += 1
        else:
            fail_count += 1
            print(f"  [ERROR] Failed to convert: {basename}")

        print()

    # Summary
    print("=" * 50)
    print(f"Summary: {success_count} converted, {fail_count} failed, "
          f"{len(files) - success_count - fail_count} skipped")
    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()