# What is this script?
This script was created to speed up the Plex preview thumbnails process.
It will use multi threaded processes with a mixture of nvidia GPU and CPU for max throughput

# Questions or support?
Create github issue OR see thread https://forums.plex.tv/t/script-to-regenerate-video-previews-multi-threaded/788360

# STEP 1 - Install dependencies (make sure they are in your path)
1. FFmpeg: https://www.ffmpeg.org/download.html
2. MediaInfo: https://mediaarea.net/fr/MediaInfo/Download

# STEP 2 - Install Python 3 dependencies
```
pip3 install loguru rich plexapi gpustat requests pymediainfo
```

# STEP 3 - Download script and edit the Vars
Download the [plex_generate_previews.py](https://raw.githubusercontent.com/stevezau/plex_generate_vid_previews/main/plex_generate_previews.py) script to your machine. Open it in your fav editor and update variables.

To get your plex url and token
1. Open plex web https://app.plex.tv/ and go to any video file
2. Open the ... menu and select "get info"
3. Click "view xml" on bottom left
4. Use the url (without the path) for the `PLEX_URL` var in the python script. For example `https://xxxx.plex.direct:32400/` or `https://127.0.0.1:32400/` if running locally.
5. Find Plex-Token in the url and insert the value into `PLEX_TOKEN` var in the python script.

# STEP 4 - Run
You can specify an OPTIONAL cmd line arg which will tell the script to only work files which contain that text
`python3 plex_preview_thumbnail_generator.py <ONLY_PROCESS_FILES_IN_PATH>`
