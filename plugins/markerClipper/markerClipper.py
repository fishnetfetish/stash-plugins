import stashapi.log as log
from stashapi.stashapp import StashInterface
import sys
import json
import os
import subprocess
import time
import re
from pathlib import Path

def get_ffmpeg_path(stash, settings=None):
    """Get ffmpeg path: override from settings if set, else from systemStatus"""
    if settings and settings.get("ffmpegPathOverride"):
        override = settings["ffmpegPathOverride"].strip()
        if override and os.path.exists(override):
            return override
    query = """
    query {
        systemStatus {
            ffmpegPath
        }
    }
    """
    result = stash.call_GQL(query)
    return result["systemStatus"]["ffmpegPath"]

def get_generated_path(stash):
    """Get Stash's generated path from configuration"""
    config = stash.get_configuration()
    return config["general"]["generatedPath"]

def get_video_resolution(video_path, ffmpeg_path):
    ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
    cmd = [ffprobe_path, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", video_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
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

def clip_marker(scene_id, marker, settings, ffmpeg_path, stash):
    """Extract video clip from marker using ffmpeg"""
    try:
        # Get scene details
        scene_query = """
        query FindScene($id: ID!) {
            findScene(id: $id) {
                id
                title
                files {
                    path
                }
            }
        }
        """
        scene_result = stash.call_GQL(scene_query, {"id": scene_id})
        scene = scene_result["findScene"]

        if not scene or not scene["files"]:
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
        # VBR bitrate mode (active)
        if vcodec == "libx264":
            cmd.extend(["-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k", "-b:a", "128k", "-profile:v", "main", "-pix_fmt", "yuv420p"])
        elif vcodec in ("h264_nvenc", "av1_nvenc"):
            cmd.extend(["-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k", "-b:a", "128k", "-rc", "vbr", "-profile:v", "main", "-pix_fmt", "yuv420p"])

        resolution = settings.get("resolution", "")
        if resolution:
            source_res = get_video_resolution(video_path, ffmpeg_path)
            if source_res:
                try:
                    sw, sh = map(int, source_res.split("x"))
                    lw, lh = map(int, resolution.split("x"))
                    if sw > lw or sh > lh:
                        cmd.extend(["-vf", f"scale={resolution}"])
                except:
                    pass

        cmd.append(output_path)

        log.debug(f"Extracting clip: {filename}")
        log.debug("ffmpeg command: " + " ".join(cmd))

        # Execute ffmpeg (force overwrite)
        cmd.insert(1, "-y")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            stderr = result.stderr or result.stdout
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                log.info(f"Successfully created clip: {output_path}")
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
            "resolution": "original",
            "paddingBefore": 0,
            "paddingAfter": 0,
            "defaultDuration": 10,
            "filenameTemplate": "clip_{scene_id}_{timestamp}_{marker_title}",
            "outputDir": None,
            "ffmpegPathOverride": ""
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

        # Get ffmpeg path
        ffmpeg_path = get_ffmpeg_path(stash, settings)
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            log.error(f"ffmpeg not found at: {ffmpeg_path}")
            return

        log.progress(20)

        # Clip the marker
        success = clip_marker(scene_id, marker, settings, ffmpeg_path, stash)
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
            # Check if this is a background task execution (has settings_json) or UI submission
            if "settings_json" in json_input["args"]:
                log.debug("Calling clip_marker_task - background task execution")
                clip_marker_task()
            elif "scene_id" in json_input["args"] and "marker_id" in json_input["args"]:
                log.debug("Calling submit_clip_task - UI submission")
                submit_clip_task()
            else:
                log.error("Invalid arguments for clip_marker mode")
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
