import json
import time
import unittest
from unittest.mock import patch

import core.api as api
import core.worker as worker_module


class _FakeElement:
    text = ""


class _FakeDriver:
    def __init__(self, live=True, progress=None, malformed=False):
        self.live = live
        self.progress = list(progress or [])
        self.malformed = malformed
        self.video_time = 0.0
        self.browser_pid = None
        self.closed = False

    def get(self, *_args):
        pass

    def refresh(self):
        pass

    def add_cookie(self, *_args):
        pass

    def get_cookies(self):
        return [{"name": "session_token", "value": "test"}]

    def set_script_timeout(self, *_args):
        pass

    def execute_async_script(self, _script, url, *_args):
        if self.malformed:
            return json.dumps({"ok": True, "status": 200, "text": "not-json"})
        if "/api/v2/channels/" in url:
            if self.live is None:
                return json.dumps({"ok": False, "status": 403, "error": "blocked"})
            body = json.dumps(
                {
                    "livestream": {
                        "is_live": self.live,
                        "categories": [{"id": 13}],
                    }
                }
            )
            return json.dumps({"ok": True, "status": 200, "text": body})
        if url.endswith("/campaigns"):
            body = json.dumps(
                {
                    "data": [
                        {
                            "id": "camp",
                            "name": "Test",
                            "status": "active",
                            "category": {"id": 13, "name": "Rust"},
                            "channels": [],
                        }
                    ]
                }
            )
            return json.dumps({"ok": True, "status": 200, "text": body})
        if "/drops/progress" in url:
            value = (
                self.progress.pop(0)
                if len(self.progress) > 1
                else (self.progress[0] if self.progress else 0)
            )
            body = json.dumps(
                {
                    "data": [
                        {
                            "id": "camp",
                            "rewards": [
                                {"progress": value, "claimed": value >= 100}
                            ],
                        }
                    ]
                }
            )
            return json.dumps({"ok": True, "status": 200, "text": body})
        raise AssertionError(url)

    def execute_script(self, script, *_args):
        if "currentTime" in script and "querySelector('video')" in script:
            self.video_time += 0.2
            return {
                "exists": True,
                "currentTime": self.video_time,
                "paused": False,
                "ended": False,
                "readyState": 4,
                "error": None,
            }
        if "__NEXT_DATA__" in script:
            return None
        return None

    def find_element(self, *_args):
        return _FakeElement()

    def quit(self):
        self.closed = True


class _HlsFakeDriver(_FakeDriver):
    def __init__(self):
        super().__init__()
        self.hls_sources = []
        self.viewer_tracking = None

    def attach_hls(self, source):
        self.hls_sources.append(source)
        return True

    def start_viewer_tracking(self, token, channel_id, livestream_id, vod_id=None):
        self.viewer_tracking = (token, channel_id, livestream_id, vod_id)
        return True

    def viewer_tracking_status(self):
        return {"exists": True, "open": True, "readyState": 1, "error": None}


class WorkerVerificationTests(unittest.TestCase):
    def test_rotating_playback_token_does_not_reattach_hls(self):
        worker = worker_module.StreamWorker("https://kick.com/example", 1)
        worker.driver = _HlsFakeDriver()
        first = (
            "https://video.example/channel.m3u8?token=first"
        )
        second = (
            "https://video.example/channel.m3u8?token=second"
        )

        worker._playback_url = first
        worker.ensure_player_state()
        worker._playback_url = second
        worker.ensure_player_state()

        self.assertEqual(worker.driver.hls_sources, [first])

    def test_verified_stream_uses_recovery_timeout_not_startup_timeout(self):
        worker = worker_module.StreamWorker(
            "https://kick.com/example",
            1,
            verification_timeout=1,
            playback_recovery_timeout=45,
        )
        worker._verification_started_at = 1.0
        worker._has_verified_playback = True
        worker._unverified_since = 100.0

        self.assertFalse(worker._verification_expired(110.0, verified=False))
        self.assertTrue(worker._verification_expired(146.0, verified=False))

    def test_viewer_token_is_summarized_without_exposing_token(self):
        worker = worker_module.StreamWorker("https://kick.com/example", 1)
        worker.driver = _HlsFakeDriver()
        worker._channel_id = 12
        worker._livestream_id = 34

        with patch.object(worker, "_session_token", return_value="session"), patch.object(
            worker_module,
            "fetch_viewer_token",
            return_value={
                "ok": True,
                "status": 200,
                "token": "viewer-token",
                "error": None,
            },
        ):
            worker._check_viewer_session()
            worker._refresh_viewer_tracking_status(10)

        self.assertTrue(worker.viewer_session["ok"])
        self.assertTrue(worker.viewer_session["authenticated"])
        self.assertTrue(worker.viewer_session["tracking_started"])
        self.assertNotIn("token", worker.viewer_session)
        self.assertEqual(worker.driver.viewer_tracking[1:3], (12, 34))

    def _start_worker(self, driver, **kwargs):
        updates = []
        finishes = []
        worker = worker_module.StreamWorker(
            "https://kick.com/test",
            0,
            on_update=updates.append,
            on_finish=lambda elapsed, completed, reason: finishes.append(
                (elapsed, completed, reason)
            ),
            hide_player=True,
            startup_wait=0,
            loop_interval=0.05,
            verification_timeout=kwargs.pop("verification_timeout", 1),
            **kwargs,
        )
        patcher = patch.object(
            worker_module, "make_chrome_driver", return_value=driver
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        api_patcher = patch.object(
            api,
            "_http_fetch_text",
            side_effect=api.BrowserRequestError("browser fallback"),
        )
        api_patcher.start()
        self.addCleanup(api_patcher.stop)
        worker.start()
        return worker, updates, finishes

    def test_unknown_live_status_never_counts_watch_time(self):
        driver = _FakeDriver(live=None)
        worker, _updates, finishes = self._start_worker(driver)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.elapsed_seconds, 0)
        self.assertEqual(finishes[-1][2], "verification_failed")
        self.assertTrue(driver.closed)

    def test_live_and_advancing_video_counts_time(self):
        driver = _FakeDriver(live=True)
        worker, updates, _finishes = self._start_worker(
            driver, verification_timeout=3
        )
        time.sleep(1.2)
        worker.stop()
        worker.join(3)
        self.assertGreaterEqual(worker.elapsed_seconds, 1)
        self.assertTrue(
            any(update["state"] == "watch_verified" for update in updates)
        )
        self.assertTrue(driver.closed)

    def test_campaign_completes_only_from_server_progress(self):
        driver = _FakeDriver(live=True, progress=[10, 100])
        worker, updates, finishes = self._start_worker(
            driver,
            campaign_id="camp",
            progress_check_interval=1,
            progress_stall_timeout=5,
            verification_timeout=3,
        )
        worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(finishes[-1][1:], (True, "completed"))
        self.assertTrue(any(update["drop_verified"] for update in updates))


class ApiBrowserOwnershipTests(unittest.TestCase):
    def test_expired_campaigns_are_not_returned_to_inventory(self):
        result = api._parse_campaigns(
            [
                {
                    "id": "old",
                    "name": "Eski",
                    "status": "expired",
                    "channels": [{"slug": "old-channel"}],
                },
                {
                    "id": "active",
                    "name": "Aktif",
                    "status": "active",
                    "channels": [{"slug": "live-channel"}],
                },
            ]
        )

        self.assertEqual([item["id"] for item in result], ["active"])

    def test_owned_browser_closes_on_success_and_parse_error(self):
        for malformed, expected_ok in ((False, True), (True, False)):
            driver = _FakeDriver(malformed=malformed)
            closed = []
            with patch.object(
                api,
                "_http_fetch_text",
                side_effect=api.BrowserRequestError("browser fallback"),
            ):
                with patch.object(api, "make_chrome_driver", return_value=driver):
                    with patch.object(
                        api,
                        "close_chrome_driver",
                        side_effect=lambda value: closed.append(value),
                    ):
                        result = api.fetch_drops_campaigns_and_progress()
            self.assertEqual(result["ok"], expected_ok)
            self.assertEqual(closed, [driver])
            self.assertIsNone(result["driver"])

    def test_external_worker_browser_is_not_closed_by_progress_fetch(self):
        driver = _FakeDriver(progress=[20])
        with patch.object(api, "close_chrome_driver") as close:
            result = api.fetch_drops_progress(driver=driver)
        self.assertTrue(result["ok"])
        close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
