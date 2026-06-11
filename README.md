# YouTube Shorts Pipeline

An AI-powered Python pipeline that finds YouTube videos from a list of channels, selects the best candidates with AI, and automatically turns them into vertical short-form videos for YouTube Shorts, Instagram Reels, and TikTok.

> ⚠️ Important: use this project only with content that you are legally allowed to download, edit, and republish. Always respect copyright, platform terms of service, and privacy rules.

## What it does

The project runs in two main stages:

1. **Find**: reads YouTube channels from `config.json`, fetches videos with `yt-dlp`, filters them by duration, applies a local ranking score, and uses Gemini to choose the best videos for short-form content.
2. **Edit**: downloads the selected video, extracts the audio, generates subtitles with Whisper, detects scene cuts, uses Groq/Gemini to plan short segments, and renders vertical 1080x1920 clips with `ffmpeg`.

Main output:

```text
out/<id>/
├── *_reel_part_001.mp4
├── *_reel_part_002.mp4
├── segments_plan.json
└── reelsinfo.json     # optional, if present
```

## Requirements

- Python **3.11+** recommended
- Git
- FFmpeg and FFprobe installed and available in your PATH
- A Groq API key
- A Gemini API key
- Enough free disk space for temporary videos and rendered outputs

### Install FFmpeg

Windows:

```powershell
winget install Gyan.FFmpeg
```

macOS:

```bash
brew install ffmpeg
```

Linux Debian/Ubuntu:

```bash
sudo apt update
sudo apt install ffmpeg
```

Check that FFmpeg is installed correctly:

```bash
ffmpeg -version
ffprobe -version
```

## Installation

Clone the repository:

```bash
git clone https://github.com/USERNAME/youtube-shorts-pipeline.git
cd youtube-shorts-pipeline
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it.

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Install the dependencies:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

## Configuration

Copy the example files:

```bash
cp .env.example .env
cp config.example.json config.json
cp reelsinfo.example.json reelsinfo.json
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
Copy-Item config.example.json config.json
Copy-Item reelsinfo.example.json reelsinfo.json
```

Open `.env` and add your API keys:

```env
GROQ_API_KEY=your_groq_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
```

Never commit `.env` to GitHub. It is already included in `.gitignore`.

## Configure YouTube channels

Edit `config.json`:

```json
{
  "channels": [
    "@examplechannel",
    "https://www.youtube.com/@anotherchannel/videos"
  ],
  "filters": {
    "min_duration_sec": 240,
    "max_duration_sec": 3600
  },
  "search": {
    "per_channel_results": 40,
    "candidate_pool_size": 100,
    "final_target": 10,
    "random_seed": null,
    "max_pool_per_channel": 25,
    "max_final_per_channel": 4
  },
  "ranking": {
    "preferred_duration_min_sec": 480,
    "preferred_duration_max_sec": 1800
  },
  "gemini": {
    "enabled": true,
    "model": "gemini-2.5-flash"
  }
}
```

Main parameters:

| Field | Meaning |
|---|---|
| `channels` | List of YouTube channels to analyze |
| `min_duration_sec` | Minimum duration for source videos |
| `max_duration_sec` | Maximum duration for source videos |
| `per_channel_results` | Maximum number of videos fetched per channel |
| `candidate_pool_size` | Maximum number of candidates sent to the AI selection step |
| `final_target` | Final number of videos to process |
| `max_final_per_channel` | Maximum number of selected videos from the same channel |
| `gemini.model` | Gemini model used for candidate selection |

## Usage

### 1. Run the full pipeline: find + edit

```bash
python pipeline.py all
```

Or simply:

```bash
python pipeline.py
```

### 2. Only find videos

```bash
python pipeline.py find
```

This creates:

```text
selected_videos.json
raw_candidates.json
channel_cache.json
```

### 3. Edit already selected videos

```bash
python pipeline.py edit-selected
```

### 4. Edit a single YouTube URL

```bash
python pipeline.py edit-url --url "https://www.youtube.com/watch?v=VIDEO_ID" --title "My Short Title"
```

Without subtitles:

```bash
python pipeline.py edit-url --url "https://www.youtube.com/watch?v=VIDEO_ID" --title "My Short Title" --no-subs
```

## Visual customization

### Logo

Replace this file with your own logo:

```text
assets/logo.png
```

The logo is automatically added to the final video.

### Fonts

The script looks for these optional font files:

```text
assets/fonts/Poppins-SemiBold.ttf
assets/fonts/BebasNeue-Regular.ttf
```

If they are not available, the script falls back to system fonts. Do not include font files in your repository unless their license allows redistribution.

### Video parameters

You can customize the main rendering settings inside `pipeline.py`:

```python
MIN_SHORT_SECONDS = 60
MAX_SHORT_SECONDS = 120
MAX_SHORTS = 14
MAX_TOTAL_SEGMENTS = 100
CENTER_H = 1100
TITLE_FONT_SIZE = 86
SUB_FONT_SIZE = 64
LOGO_SIZE = 140
```

## Generated files

| File/folder | Description |
|---|---|
| `out/` | Final rendered clips |
| `tmp/` | Temporary files, deleted after processing if `CLEAN_TMP_AFTER=True` |
| `selected_videos.json` | Videos selected by Gemini |
| `raw_candidates.json` | Candidate videos before final selection |
| `used_videos.json` | URLs that were processed successfully |
| `channel_cache.json` | Temporary cache for fetched channels |
| `last_edit_result.json` | Summary of the latest editing run |

## Optional browser cookies

By default, the project does not read browser cookies. If you need to download a video that is accessible only through your own account, you can set:

```env
YTDLP_COOKIE_BROWSER=chrome
```

Possible values include `chrome`, `firefox`, and `edge`, depending on your `yt-dlp` installation. Use this only for content you are allowed to access and process.

## Common issues

### `ffmpeg not found`

Install FFmpeg and reopen your terminal. Then check:

```bash
ffmpeg -version
```

### `whisper: command not found`

Make sure your virtual environment is active, then reinstall Whisper:

```bash
pip install openai-whisper
```

### `Missing Gemini API key` or `GROQ_API_KEY not set`

Check your `.env` file:

```env
GROQ_API_KEY=...
GEMINI_API_KEY=...
```

### Chrome cookie error

Remove or clear this line in `.env`:

```env
YTDLP_COOKIE_BROWSER=
```

### Subtitles are generated in the wrong language

Edit this value inside `pipeline.py`:

```python
WHISPER_LANG = "en"
```

Examples: `it`, `en`, `de`, `es`.

## License

MIT. Always check the licenses of any external assets, fonts, logos, or video content you use with this project.
