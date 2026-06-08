import json
import os
import re
import subprocess
import sys
import time

import stashapi.log as log
from stashapi.stashapp import StashInterface

def get_paths(stash, settings=None):
    """Get ffmpeg/ffprobe paths: override from settings if set, else from systemStatus"""
    ffmpeg_override = settings.get("ffmpegPathOverride", "").strip() if settings else ""
    ffprobe_override = settings.get("ffprobePathOverride", "").strip() if settings else ""
    query = """
    query {
        systemStatus {
            ffmpegPath
            ffprobePath
        }
    }
    """
    result = stash.call_GQL(query)
    ffmpeg_path = ffmpeg_override if ffmpeg_override and os.path.exists(ffmpeg_override) else result["systemStatus"]["ffmpegPath"]
    ffprobe_path = ffprobe_override if ffprobe_override and os.path.exists(ffprobe_override) else result["systemStatus"]["ffprobePath"]
    return ffmpeg_path, ffprobe_path

def get_generated_path(stash):
    """Get Stash's generated path from configuration"""
    config = stash.get_configuration()
    return config["general"]["generatedPath"]

def get_video_resolution(video_path, ffprobe_path):
    """Return video resolution 'WxH' via ffprobe, or None on error."""
    cmd = [ffprobe_path, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", video_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return None

def get_source_bitrate(video_path, ffprobe_path):
    """Return video bitrate in kbps (int) via ffprobe, or None on error."""
    cmd = [ffprobe_path, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        try:
            br = int(result.stdout.strip())
            return max(500, br // 1000)  # kbps, floor at 500
        except Exception:
            return None
    return None

def format_timestamp(seconds):
    """Convert seconds to HH:MM:SS format for ffmpeg"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"

def sanitize_filename(name):
    """Sanitize string for use in filename"""
    return re.sub(r'[^\w\-_.]', '_', str(name))[:100]

def get_marker_details(stash, scene_id, marker_id):
    """Get marker details including scene and timing info"""
    query = """
    query FindScene($id: ID!) {
        findScene(id: $id) {
            id
            title
            files {
                path
            }
            scene_markers {
                id
                title
                seconds
                end_seconds
                primary_tag {
                    id
                    name
                }
            }
        }
    }
    """
    variables = {"id": scene_id}
    result = stash.call_GQL(query, variables)
    scene = result["findScene"]

    # Find the specific marker
    for marker in scene["scene_markers"]:
        if marker["id"] == marker_id:
            # Combine marker and scene info
            marker["scene"] = {
                "id": scene["id"],
                "title": scene["title"],
                "files": scene["files"]
            }
            return marker

    return None

def clip_marker(scene_id, marker, settings, ffmpeg_path, ffprobe_path, stash):
    """Extract video clip from marker using ffmpeg"""
    try:
        scene = marker.get("scene")
        if not scene or not scene.get("files"):
            log.error(f"No video file found for scene {scene_id}")
            return False

        video_path = scene["files"][0]["path"]
        if not os.path.exists(video_path):
            log.error(f"Video file not found: {video_path}")
            return False

        # Calculate timing with padding
        padding_before = settings.get("paddingBefore", 0)
        padding_after = settings.get("paddingAfter", 0)
        start_time = max(0, marker["seconds"] - padding_before)
        log.debug(f"Marker timing: seconds={marker['seconds']}, end_seconds={marker.get('end_seconds')}, start_time={start_time}")

        # Handle markers without end_seconds (use default duration)
        if marker.get("end_seconds") is not None:
            duration = (marker["end_seconds"] - marker["seconds"]) + padding_before + padding_after
        else:
            # Default duration for markers without end time
            default_duration = settings.get("defaultDuration", 10)
            duration = default_duration + padding_before + padding_after

        log.debug(f"Clip parameters: start_time={start_time}, duration={duration}")

        # Prepare output filename
        template = settings.get("filenameTemplate", "clip_{scene_id}_{timestamp}_{marker_title}")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe_scene = sanitize_filename(scene["title"])
        safe_marker = sanitize_filename(marker.get("title", "marker"))
        filename = template.format(
            scene_title=safe_scene,
            scene_id=scene["id"],
            marker_title=safe_marker,
            timestamp=timestamp
        ) + ".mp4"

        # Get default output directory (generated path + clips subdir)
        if "outputDir" not in settings or not settings.get("outputDir"):
            generated_path = get_generated_path(stash)
            output_dir = os.path.join(generated_path, "clips")
        else:
            output_dir = settings["outputDir"]

        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, filename)

        # Build ffmpeg command (fast seek: -ss before -i)
        vcodec = settings.get("vcodec") or "libx264"
        cmd = [
            ffmpeg_path,
            "-ss", str(start_time),
            "-i", video_path,
            "-t", str(duration),
            "-c:v", vcodec,
            "-c:a", settings.get("acodec") or "aac",
            "-preset", settings.get("preset") or "medium",
            "-movflags", "faststart",
            "-loglevel", "error"
        ]
        # Bitrate handling
        match_bitrate = settings.get("matchBitrate", False)
        if match_bitrate:
            br = get_source_bitrate(video_path, ffprobe_path) or 2500
            maxr = f"{int(br * 1.1)}k"
            bufs = f"{int(br * 2.2)}k"
            if vcodec == "libx264":
                cmd.extend(["-crf", "20", "-maxrate", maxr, "-bufsize", bufs, "-b:a", "128k", "-profile:v", "main", "-pix_fmt", "yuv420p"])
            elif vcodec in ("h264_nvenc", "av1_nvenc"):
                cmd.extend(["-crf", "20", "-maxrate", maxr, "-bufsize", bufs, "-b:a", "128k", "-rc", "vbr", "-profile:v", "main", "-pix_fmt", "yuv420p"])
        else:
            if vcodec == "libx264":
                cmd.extend(["-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k", "-b:a", "128k", "-profile:v", "main", "-pix_fmt", "yuv420p"])
            elif vcodec in ("h264_nvenc", "av1_nvenc"):
                cmd.extend(["-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k", "-b:a", "128k", "-rc", "vbr", "-profile:v", "main", "-pix_fmt", "yuv420p"])

        resolution = settings.get("resolution") or "original"
        if resolution != "original":
            source_res = get_video_resolution(video_path, ffprobe_path)
            if source_res:
                try:
                    sw, sh = map(int, source_res.split("x"))
                    lw, lh = map(int, resolution.split("x"))
                    if sw > lw or sh > lh:
                        cmd.extend(["-vf", f"scale={resolution}"])
                except Exception:
                    pass

        cmd.append(output_path)

        log.debug(f"Extracting clip: {filename}")
        log.debug("ffmpeg command: " + " ".join(cmd))

        # Execute ffmpeg (force overwrite)
        cmd.insert(1, "-y")
        try:
            encode_start = time.time()
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            elapsed = time.time() - encode_start
            stderr = result.stderr or result.stdout
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                mins, secs = divmod(int(elapsed), 60)
                took = f"{mins}m{secs}s" if mins else f"{secs}s"
                log.info(f"Successfully created clip (took {took}): {output_path}")
                return True
            else:
                log.error(f"ffmpeg failed (code {result.returncode}): {stderr}")
                if os.path.exists(output_path):
                    os.remove(output_path)
                return False
        except subprocess.TimeoutExpired:
            log.error("ffmpeg timed out after 300s")
            if os.path.exists(output_path):
                os.remove(output_path)
            return False
        except Exception as e:
            log.error(f"ffmpeg subprocess error: {str(e)}")
            return False

    except Exception as e:
        log.error(f"Error clipping marker: {str(e)}")
        return False

def submit_clip_task():
    """Submit marker clipping as a background task"""
    args = json_input["args"]

    log.debug("submit_clip_task: Starting marker clip submission")
    try:
        scene_id = args.get("scene_id")
        marker_id = args.get("marker_id")

        if not scene_id:
            log.error("scene_id is required")
            response = {
                "success": False,
                "message": "Missing required parameter: scene_id"
            }
            print(json.dumps(response))
            return

        if not marker_id:
            log.error("marker_id is required")
            response = {
                "success": False,
                "message": "Missing required parameter: marker_id"
            }
            print(json.dumps(response))
            return

        # Validate that the marker exists in the scene
        marker = get_marker_details(stash, scene_id, marker_id)
        if not marker:
            log.error(f"Marker {marker_id} not found in scene {scene_id}")
            response = {
                "success": False,
                "message": f"Marker {marker_id} not found in scene {scene_id}"
            }
            print(json.dumps(response))
            return

        log.debug(f"Validated marker {marker_id} in scene {scene_id}")

        # Get plugin settings and merge with defaults
        plugin_config = stash.get_configuration().get("plugins", {}).get("markerClipper", {})
        default_settings = {
            "vcodec": "libx264",
            "acodec": "aac",
            "preset": "medium",
            "resolution": "",
            "paddingBefore": 0,
            "paddingAfter": 0,
            "defaultDuration": 10,
            "filenameTemplate": "clip_{scene_id}_{timestamp}_{marker_title}",
            "outputDir": None,
            "ffmpegPathOverride": "",
            "ffprobePathOverride": "",
            "matchBitrate": False
        }

        # Merge plugin settings with defaults (only known keys)
        settings = {**default_settings, **{k: v for k, v in plugin_config.items() if k in default_settings}}

        # Submit as a background task using stash.run_plugin_task (async)
        task_args = {
            "scene_id": scene_id,
            "marker_id": marker_id,
            "settings_json": json.dumps(settings)
        }

        task_id = stash.run_plugin_task("markerClipper", "clip_marker", args=task_args)
        response = {
            "success": True,
            "message": f"Clip task submitted for marker '{marker.get('title', 'Untitled')}' in scene '{marker['scene']['title']}'",
            "task_id": task_id
        }
        print(json.dumps(response))
        log.debug(f"submit_clip_task: Background task submitted successfully with ID {task_id}")

    except Exception as e:
        log.error(f"Error in submit_clip_task: {str(e)}")
        response = {
            "success": False,
            "message": f"Error submitting clip task: {str(e)}"
        }
        print(json.dumps(response))


def clip_marker_task():
    """Background task to handle marker clipping"""
    args = json_input["args"]

    try:
        scene_id = args.get("scene_id")
        marker_id = args.get("marker_id")
        settings_json = args.get("settings_json")

        if not scene_id or not marker_id:
            log.error("scene_id and marker_id are required for clip task")
            return

        # Parse settings
        settings = json.loads(settings_json) if settings_json else {}

        # Get marker details
        marker = get_marker_details(stash, scene_id, marker_id)
        if not marker:
            log.error(f"Could not find marker {marker_id} in scene {scene_id}")
            return

        log.info(f"Processing clip task for marker {marker_id} in scene {scene_id}")
        log.progress(10)

        # Get ffmpeg/ffprobe paths
        ffmpeg_path, ffprobe_path = get_paths(stash, settings)
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            log.error(f"ffmpeg not found at: {ffmpeg_path}")
            return
        if not ffprobe_path or not os.path.exists(ffprobe_path):
            log.error(f"ffprobe not found at: {ffprobe_path}")
            return

        log.progress(20)

        # Clip the marker
        success = clip_marker(scene_id, marker, settings, ffmpeg_path, ffprobe_path, stash)
        if success:
            log.progress(100)
            log.debug("Marker clipping completed successfully")
        else:
            log.error("Marker clipping failed")

    except Exception as e:
        log.error(f"Error in clip_marker_task: {str(e)}")
    log.debug("clip_marker_task finished")

json_input = json.loads(sys.stdin.read())
FRAGMENT_SERVER = json_input["server_connection"]
stash = StashInterface(FRAGMENT_SERVER)

log.debug(f"Plugin called with args: {json_input['args']}")
log.debug("Plugin execution started")

try:
    if "mode" in json_input["args"]:
        PLUGIN_ARGS = json_input["args"]["mode"]
        log.debug(f"Plugin mode: {PLUGIN_ARGS}")
        if "clip_marker" == PLUGIN_ARGS:
            # Stash plugins use a single entrypoint (runPluginOperation).
            # We therefore receive TWO distinct calls with the same mode="clip_marker":
            #
            # 1. UI submission (from JS callPluginAPI): contains scene_id + marker_id.
            #    → submit_clip_task() validates, builds settings, then calls
            #      stash.run_plugin_task(...) to enqueue the real work.
            #
            # 2. Background execution (later, when the queued task runs):
            #    contains the same ids PLUS settings_json.
            #    → clip_marker_task() performs the actual ffmpeg clip extraction.
            #
            # The presence/absence of settings_json is the discriminator between the two phases.
            if "settings_json" in json_input["args"]:
                log.debug("clip_marker mode: settings_json present → executing background clip_marker_task()")
                clip_marker_task()
            elif "scene_id" in json_input["args"] and "marker_id" in json_input["args"]:
                log.debug("clip_marker mode: scene_id+marker_id present → handling UI submit_clip_task()")
                submit_clip_task()
            else:
                log.error("clip_marker mode: invalid argument combination")
                print(json.dumps({"success": False, "message": "Invalid arguments for clip_marker mode"}))
        else:
            log.error(f"Unknown mode: {PLUGIN_ARGS}")
            print(json.dumps({"success": False, "message": f"Unknown mode: {PLUGIN_ARGS}"}))
    else:
        log.error("No mode specified in args")
        print(json.dumps({"success": False, "message": "No mode specified in args"}))
except Exception as e:
    log.error(f"Plugin execution failed: {str(e)}")
    print(json.dumps({"success": False, "message": f"Plugin execution failed: {str(e)}"}))
