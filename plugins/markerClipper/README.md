# MarkerClipper Plugin

A Stash plugin that adds clip buttons (✂️) next to scene markers, allowing users to extract video segments from markers with a single click.
By default, files will be exported to the stash/generated/clips directory, but this can be changed in the plugin's settings.

## Features

- One-Click Clipping: Click the ✂️ button next to any marker to extract that video segment
- Background Processing: Submits clip jobs to Stash's background task system
- FFmpeg Integration: Uses FFmpeg for high-quality video extraction

## Installation

1. Copy plugin files to your Stash plugins directory (or install via CommunityScripts)
2. Install Python dependencies: `pip install -r requirements.txt`
3. Reload Plugins
4. Configure settings via Stash Settings > Plugins

## Known Limitations
- Requires ffmpeg available via Stash or override path.
- Markers without end_seconds default to 10s duration.
- Filename sanitization may alter special characters.

## Configuration

Settings available in the plugin UI (outputDir, vcodec, acodec, preset, resolution, paddingBefore/After, filenameTemplate).

## Usage

1. Navigate to a scene with markers.
2. Click the "Markers" tab.
3. Click ✂️ buttons next to markers to queue clips.
4. Monitor progress in Stash's Tasks section.

## License

This plugin is provided as-is for Stash users. See Stash documentation for plugin development guidelines.
