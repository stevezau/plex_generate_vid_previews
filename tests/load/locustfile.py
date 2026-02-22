"""Locust load test for webhook endpoints.

Run with:
    locust -f tests/load/locustfile.py
    # Then open http://localhost:8089 in your browser

Or headless:
    locust -f tests/load/locustfile.py --headless -u 50 -r 10 -t 60s
"""

from locust import HttpUser, between, task


class WebhookUser(HttpUser):
    host = "http://localhost:8080"
    wait_time = between(0.5, 2)

    def on_start(self) -> None:
        self.client.headers.update({"X-Auth-Token": "your-token-here"})

    @task(3)
    def radarr_download(self) -> None:
        payload = {
            "eventType": "Download",
            "movie": {
                "id": 1,
                "title": "Inception",
                "year": 2010,
                "folderPath": "/movies/Inception (2010)",
                "imdbId": "tt1375666",
                "tmdbId": 27205,
            },
            "movieFile": {
                "relativePath": "Inception (2010).mkv",
                "quality": "Bluray-1080p",
            },
            "isUpgrade": False,
        }
        with self.client.post(
            "/api/webhooks/radarr",
            json=payload,
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"Expected 202, got {response.status_code}")

    @task(3)
    def sonarr_download(self) -> None:
        payload = {
            "eventType": "Download",
            "series": {
                "id": 1,
                "title": "Breaking Bad",
                "path": "/tv/Breaking Bad",
                "tvdbId": 81189,
            },
            "episodes": [
                {
                    "seasonNumber": 1,
                    "episodeNumber": 1,
                    "title": "Pilot",
                }
            ],
            "episodeFile": {
                "relativePath": "Season 01/Breaking Bad - S01E01 - Pilot.mkv",
                "quality": "HDTV-720p",
            },
            "isUpgrade": False,
        }
        with self.client.post(
            "/api/webhooks/sonarr",
            json=payload,
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"Expected 202, got {response.status_code}")

    @task(1)
    def radarr_test(self) -> None:
        payload = {"eventType": "Test"}
        with self.client.post(
            "/api/webhooks/radarr",
            json=payload,
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"Expected 200, got {response.status_code}")

    @task(1)
    def webhook_history(self) -> None:
        with self.client.get(
            "/api/webhooks/history",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"Expected 200, got {response.status_code}")
