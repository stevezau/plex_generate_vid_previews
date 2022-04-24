# What is this script?
This script was created to speed up the Plex preview thumbnails process.
It will use multi threaded processes with a mixture of nvidia GPU and CPU for max throughput

# Questions or support?
Create github issue OR see thread https://forums.plex.tv/t/script-to-regenerate-video-previews-multi-threaded/788360

# STEP 1 - install python3 Deps
Requires python3
```
pip3 install loguru rich plexapi
```

# STEP 2 - Download script and edit the Vars
Download the script to your machine and edit the variables in the Python script

To get your plex url and token
1. Open plex web https://app.plex.tv/ and go to any video file
2. Open the ... menu and select "get info"
3. Click "view xml" on bottom left
4. Use the url (without the path) for the `PLEX_URL` var in the python script. For example `https://xxxx.plex.direct:32433/`
5. Find Plex-Token in the url and insert the value into `PLEX_TOKEN` var in the python script.

# STEP 3 - Run
You can specify an OPTIONAL cmd line arg which will tell the script to only work files which contain that text
`python3 plex_preview_thumbnail_generator.py <ONLY_PROCESS_FILES_IN_PATH>`
