"""
Mock Plex Media Server for E2E testing.

Mimics the Plex Media Server API for testing purposes.
Run standalone: python -m tests.mocks.mock_plex_server
"""

import os
from flask import Flask, jsonify, request, Response


app = Flask(__name__)

# Mock data
MOCK_SERVER_NAME = "Test Plex Server"
MOCK_MACHINE_ID = "abc123def456"
MOCK_TOKEN = "test-token-12345"

MOCK_LIBRARIES = [
    {"key": "1", "title": "Movies", "type": "movie", "agent": "tv.plex.agents.movie"},
    {"key": "2", "title": "TV Shows", "type": "show", "agent": "tv.plex.agents.series"},
    {"key": "3", "title": "Music", "type": "artist", "agent": "tv.plex.agents.music"},
]

MOCK_MOVIES = [
    {"ratingKey": "101", "title": "Test Movie 1", "year": 2023, "duration": 7200000},
    {"ratingKey": "102", "title": "Test Movie 2", "year": 2024, "duration": 5400000},
]

MOCK_SHOWS = [
    {"ratingKey": "201", "title": "Test Show 1", "year": 2023},
    {"ratingKey": "202", "title": "Test Show 2", "year": 2024},
]


def check_token():
    """Verify the Plex token is provided."""
    token = request.headers.get("X-Plex-Token") or request.args.get("X-Plex-Token")
    if not token:
        return False, Response(
            '<?xml version="1.0"?><Response code="401" status="Unauthorized"/>',
            status=401,
            mimetype="application/xml",
        )
    return True, None


@app.route("/")
def server_identity():
    """Return server identity (root endpoint)."""
    valid, error = check_token()
    if not valid:
        return error

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="0" allowCameraUpload="1" allowChannelAccess="1"
    allowMediaDeletion="1" allowSharing="1" allowSync="1" allowTuners="1"
    backgroundProcessing="1" certificate="1" companionProxy="1"
    countryCode="us" diagnostics="logs,databases,streaminglogs"
    eventStream="1" friendlyName="{MOCK_SERVER_NAME}"
    hubSearch="1" itemClusters="1" livetv="7"
    machineIdentifier="{MOCK_MACHINE_ID}"
    mediaProviders="1" multiuser="1" musicAnalysis="2"
    myPlex="1" myPlexMappingState="mapped" myPlexSigninState="ok"
    myPlexSubscription="1" myPlexUsername="testuser"
    offlineTranscode="1" ownerFeatures="..."
    photoAutoTag="1" platform="Linux" platformVersion="5.15.0"
    pluginHost="1" pushNotifications="0" readOnlyLibraries="0"
    streamingBrainABRVersion="3" streamingBrainVersion="2"
    sync="1" transcoderActiveVideoSessions="0"
    transcoderAudio="1" transcoderLyrics="1" transcoderPhoto="1"
    transcoderSubtitles="1" transcoderVideo="1"
    transcoderVideoBitrates="64,96,208,320,720,1500,2000,3000,4000,8000,10000,12000,20000"
    transcoderVideoQualities="0,1,2,3,4,5,6,7,8,9,10,11,12"
    transcoderVideoResolutions="128,128,160,240,320,480,720,720,1080,1080,1080,1080,1080"
    updatedAt="1704067200" updater="1" version="1.40.0.7775"
    voiceSearch="1">
</MediaContainer>"""
    return Response(xml, mimetype="application/xml")


@app.route("/library/sections")
def library_sections():
    """Return list of library sections."""
    valid, error = check_token()
    if not valid:
        return error

    sections = ""
    for lib in MOCK_LIBRARIES:
        sections += f'''<Directory allowSync="1" art="/:/resources/movie-fanart.jpg"
            composite="/library/sections/{lib["key"]}/composite/1234"
            filters="1" refreshing="0" thumb="/:/resources/movie.png"
            key="{lib["key"]}" type="{lib["type"]}" title="{lib["title"]}"
            agent="{lib["agent"]}" scanner="Plex Movie" language="en-US"
            uuid="abc-{lib["key"]}" updatedAt="1704067200" createdAt="1704000000"
            scannedAt="1704067200" content="1" directory="1"
            contentChangedAt="1234" hidden="0">
            <Location id="{lib["key"]}" path="/media/{lib["title"].lower().replace(" ", "_")}"/>
        </Directory>'''

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="{len(MOCK_LIBRARIES)}" allowSync="1" title1="Plex Library">
{sections}
</MediaContainer>"""
    return Response(xml, mimetype="application/xml")


@app.route("/library/sections/<section_id>/all")
def library_section_all(section_id: str):
    """Return all items in a library section."""
    valid, error = check_token()
    if not valid:
        return error

    items = ""
    if section_id == "1":  # Movies
        for movie in MOCK_MOVIES:
            items += f'''<Video ratingKey="{movie["ratingKey"]}"
                key="/library/metadata/{movie["ratingKey"]}"
                type="movie" title="{movie["title"]}" year="{movie["year"]}"
                duration="{movie["duration"]}"
                addedAt="1704067200" updatedAt="1704067200">
                <Media id="1" duration="{movie["duration"]}"
                    bitrate="8000" width="1920" height="1080"
                    aspectRatio="1.78" audioChannels="6" audioCodec="ac3"
                    videoCodec="h264" videoResolution="1080" container="mkv">
                    <Part id="1" key="/library/parts/1/1234/file.mkv"
                        duration="{movie["duration"]}"
                        file="/media/movies/{movie["title"].lower().replace(" ", "_")}.mkv"
                        size="8000000000" container="mkv"
                        videoProfile="main"/>
                </Media>
            </Video>'''
    elif section_id == "2":  # TV Shows
        for show in MOCK_SHOWS:
            items += f'''<Directory ratingKey="{show["ratingKey"]}"
                key="/library/metadata/{show["ratingKey"]}/children"
                type="show" title="{show["title"]}" year="{show["year"]}"
                addedAt="1704067200" updatedAt="1704067200"
                leafCount="10" viewedLeafCount="0" childCount="1">
            </Directory>'''

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="2" allowSync="1" art="/:/resources/movie-fanart.jpg"
    identifier="com.plexapp.plugins.library"
    librarySectionID="{section_id}"
    librarySectionTitle="Library"
    librarySectionUUID="abc-{section_id}"
    mediaTagPrefix="/system/bundle/media/flags/"
    mediaTagVersion="1704067200">
{items}
</MediaContainer>"""
    return Response(xml, mimetype="application/xml")


@app.route("/library/metadata/<item_id>")
def library_metadata(item_id: str):
    """Return metadata for a specific item."""
    valid, error = check_token()
    if not valid:
        return error

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="1">
    <Video ratingKey="{item_id}" key="/library/metadata/{item_id}"
        type="movie" title="Test Item" year="2024" duration="7200000">
    </Video>
</MediaContainer>"""
    return Response(xml, mimetype="application/xml")


@app.route("/status/sessions")
def status_sessions():
    """Return active sessions (empty for testing)."""
    valid, error = check_token()
    if not valid:
        return error

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="0">
</MediaContainer>"""
    return Response(xml, mimetype="application/xml")


@app.route("/identity")
def identity():
    """Return server identity."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="0" claimed="1" machineIdentifier="{MOCK_MACHINE_ID}"
    version="1.40.0.7775">
</MediaContainer>"""
    return Response(xml, mimetype="application/xml")


# Health check for testing
@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "server": "mock_plex"})


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PLEX_PORT", 32401))
    print(f"Starting mock Plex server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
