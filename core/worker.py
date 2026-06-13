"""Verified stream worker with strict browser ownership."""

import json
import os
import threading
import time
from urllib.parse import urlsplit

from selenium.webdriver.common.by import By

from utils.helpers import (
    debug_print,
    domain_from_url,
    _kick_username_from_url,
    cookie_file_for_domain,
)
from .api import fetch_viewer_token, kick_channel_data_by_api
from .browser import CookieManager, close_chrome_driver, make_chrome_driver


class StreamWorker(threading.Thread):
    """Watch one stream and count time only after live video verification."""

    def __init__(
        self,
        url,
        minutes_target,
        on_update=None,
        on_finish=None,
        stop_event=None,
        driver_path=None,
        extension_path=None,
        hide_player=False,
        mute=True,
        mini_player=False,
        force_160p=False,
        offline_fresh_checks_to_switch=2,
        required_category_id=None,
        campaign_id=None,
        progress_check_interval=60,
        progress_stall_timeout=480,
        verification_timeout=90,
        playback_recovery_timeout=45,
        playback_recovery_interval=10,
        startup_wait=5,
        loop_interval=1,
        cookie_path=None,
        profile_prefix="",
        initial_elapsed_seconds=0,
        initial_drop_progress=None,
        initial_drop_verified=False,
    ):
        super().__init__(daemon=True)
        self.url = url
        self.minutes_target = minutes_target
        self.on_update = on_update
        self.on_finish = on_finish
        self.stop_event = stop_event or threading.Event()
        self.driver_path = driver_path
        self.extension_path = extension_path
        self.hide_player = hide_player
        self.mute = mute
        self.mini_player = mini_player
        self.force_160p = force_160p
        self.required_category_id = required_category_id
        self.campaign_id = campaign_id
        self.cookie_path = cookie_path or cookie_file_for_domain("kick.com")
        self.profile_prefix = profile_prefix

        self.driver = None
        self.elapsed_seconds = max(0, int(initial_elapsed_seconds or 0))
        self.completed = False
        self.finish_reason = None
        self.error_message = None
        self.ended_because_offline = False
        self.ended_because_wrong_category = False
        self.ended_because_claimed = False

        self._requested_stop_reason = "user_stopped"
        self._offline_fresh_checks = 0
        self.offline_fresh_checks_to_switch = max(
            0, int(offline_fresh_checks_to_switch or 0)
        )
        self._last_live_check = 0.0
        self._last_live_value = None
        self._last_live_source = "unknown"
        self._live_check_interval = 8
        self._last_category_check = 0.0
        self._category_check_interval = 30
        self._last_video_time = None
        self._last_video_health = {}
        self._playback_url = None
        self._channel_id = None
        self._livestream_id = None
        self._vod_id = None
        self._hls_playback_url = None
        self._verified_elapsed = float(self.elapsed_seconds)
        self._verification_started_at = None
        self._has_verified_playback = False
        self._unverified_since = None
        self._last_playback_recovery = 0.0
        self._last_viewer_health_check = 0.0

        self.progress_check_interval = max(1, int(progress_check_interval))
        self.progress_stall_timeout = max(1, int(progress_stall_timeout))
        self.verification_timeout = max(1, int(verification_timeout))
        self.playback_recovery_timeout = max(
            1, int(playback_recovery_timeout)
        )
        self.playback_recovery_interval = max(
            1, int(playback_recovery_interval)
        )
        self.startup_wait = max(0, float(startup_wait))
        self.loop_interval = max(0.05, float(loop_interval))
        self._last_progress_check = 0.0
        self._last_progress_value = None
        self._last_progress_change_at = None
        self.drop_progress = initial_drop_progress
        self.drop_verified = bool(initial_drop_verified)
        self.viewer_session = {
            "checked": False,
            "ok": False,
            "status": None,
            "authenticated": None,
            "tracking_started": False,
            "error": None,
        }

    def run(self):
        domain = domain_from_url(self.url)
        self._emit_update("starting", live=None, video_ok=False, video_advanced=False)
        try:
            use_headless = bool(self.hide_player)
            if self.mini_player or (
                self.extension_path and self.extension_path.lower().endswith(".crx")
            ):
                use_headless = False
            if os.environ.get("KDM_FORCE_HEADFUL") == "1":
                use_headless = False

            self.driver = make_chrome_driver(
                headless=use_headless,
                driver_path=self.driver_path,
                extension_path=self.extension_path,
                profile_dir_name=f"{self.profile_prefix}worker_{id(self)}",
                role="worker",
            )
            if not use_headless:
                try:
                    if self.mini_player:
                        self.driver.set_window_size(360, 360)
                        self.driver.set_window_position(20, 20)
                    else:
                        self.driver.set_window_position(60, 60)
                except Exception:
                    pass

            if domain:
                self.driver.get(f"https://{domain}")
                CookieManager.load_cookies(
                    self.driver,
                    domain,
                    cookie_path=self.cookie_path,
                )
                if self.force_160p:
                    try:
                        self.driver.execute_script(
                            "sessionStorage.setItem('stream_quality', '160');"
                        )
                    except Exception:
                        pass
            self.driver.get(self.url)
            self._verification_started_at = time.monotonic()
            if self.stop_event.wait(self.startup_wait):
                self.finish_reason = self._requested_stop_reason
                return

            last_loop = time.monotonic()
            while not self.stop_event.is_set():
                now = time.monotonic()
                loop_delta = min(2.0, max(0.0, now - last_loop))
                last_loop = now

                previous_live_check = self._last_live_check
                live = self.is_stream_live()
                fresh_live_check = self._last_live_check != previous_live_check
                self.ensure_player_state()
                video = self.get_video_health()
                video_ok = bool(video.get("ok"))
                video_advanced = self._video_advanced(video)

                if fresh_live_check:
                    if live is True:
                        self._offline_fresh_checks = 0
                    elif live is False:
                        self._offline_fresh_checks += 1

                if (
                    live is False
                    and self.offline_fresh_checks_to_switch
                    and self._offline_fresh_checks >= self.offline_fresh_checks_to_switch
                ):
                    self.ended_because_offline = True
                    self.finish_reason = "offline"
                    self._emit_update(
                        "offline",
                        live=False,
                        video_ok=video_ok,
                        video_advanced=video_advanced,
                    )
                    break

                verified = live is True and video_ok and video_advanced
                if verified:
                    self._has_verified_playback = True
                    self._unverified_since = None
                    self._verified_elapsed += loop_delta
                    self.elapsed_seconds = int(self._verified_elapsed)
                    self._check_viewer_session()
                    self._refresh_viewer_tracking_status(now)
                elif live is True:
                    if self._unverified_since is None:
                        self._unverified_since = now
                    self._recover_playback_if_needed(now)

                if self.required_category_id and live is True:
                    if now - self._last_category_check >= self._category_check_interval:
                        self._last_category_check = now
                        category_id = self.get_streamer_category_id()
                        if (
                            category_id is not None
                            and str(category_id) != str(self.required_category_id)
                        ):
                            self.ended_because_wrong_category = True
                            self.finish_reason = "wrong_category"
                            break

                if self.campaign_id and verified:
                    progress_result = self._check_drop_progress(now)
                    if progress_result == "completed":
                        break
                    if progress_result == "error":
                        break
                    if (
                        self._last_progress_change_at is not None
                        and now - self._last_progress_change_at
                        >= self.progress_stall_timeout
                    ):
                        self.finish_reason = "no_progress"
                        break

                if self._verification_expired(now, verified):
                    self.finish_reason = "verification_failed"
                    diagnostic = str(
                        self._last_video_health.get("diagnostic") or ""
                    ).strip()
                    self.error_message = "Yayın ve video oynatımı doğrulanamadı."
                    if diagnostic:
                        self.error_message += f" Sayfa durumu: {diagnostic}"
                    self._emit_update(
                        "verification_failed",
                        live=live,
                        video_ok=video_ok,
                        video_advanced=video_advanced,
                        error=self.error_message,
                    )
                    break

                if not self.campaign_id and self.minutes_target:
                    if self.elapsed_seconds >= self.minutes_target * 60:
                        self.completed = True
                        self.finish_reason = "completed"
                        break

                state = self._state_for(live, verified)
                self._emit_update(
                    state,
                    live=live,
                    video_ok=video_ok,
                    video_advanced=video_advanced,
                )
                if self.stop_event.wait(self.loop_interval):
                    self.finish_reason = self._requested_stop_reason
                    break
        except Exception as error:
            self.finish_reason = "browser_error"
            self.error_message = str(error)
            debug_print(f"StreamWorker error: {error}")
            self._emit_update(
                "error",
                live=None,
                video_ok=False,
                video_advanced=False,
                error=str(error),
            )
        finally:
            if self.finish_reason is None:
                self.finish_reason = (
                    self._requested_stop_reason
                    if self.stop_event.is_set()
                    else "browser_error"
                )
            driver = self.driver
            self.driver = None
            if driver is not None:
                close_chrome_driver(driver)
            self._emit_finish()

    def _state_for(self, live, verified):
        if live is False:
            return "offline"
        if not verified:
            return "verifying"
        if not self.campaign_id:
            return "watch_verified"
        if self.drop_verified:
            return "drop_verified"
        return "drop_waiting"

    def _check_drop_progress(self, now):
        if now - self._last_progress_check < self.progress_check_interval:
            return None
        self._last_progress_check = now
        from .api import campaign_progress, fetch_drops_progress

        result = fetch_drops_progress(
            driver=self.driver,
            cookie_path=self.cookie_path,
            profile_prefix=self.profile_prefix,
        )
        if not result.get("ok"):
            self.finish_reason = "progress_error"
            self.error_message = result.get("error") or "Kick ilerlemesi okunamadı."
            return "error"

        progress_record = next(
            (
                item
                for item in result.get("progress", [])
                if isinstance(item, dict)
                and str(item.get("id") or item.get("campaign_id"))
                == str(self.campaign_id)
            ),
            None,
        )
        percent, claimed = campaign_progress(progress_record)
        self.drop_progress = percent
        if self._last_progress_value is None:
            self._last_progress_value = percent
            self._last_progress_change_at = now
        elif percent > self._last_progress_value + 0.01:
            self._last_progress_value = percent
            self._last_progress_change_at = now
            self.drop_verified = True

        if claimed or percent >= 100:
            self.drop_verified = True
            self.completed = True
            self.ended_because_claimed = True
            self.finish_reason = "completed"
            self._emit_update(
                "drop_verified",
                live=True,
                video_ok=True,
                video_advanced=True,
            )
            return "completed"
        return None

    def _check_viewer_session(self):
        if self.viewer_session["checked"]:
            return
        self.viewer_session["checked"] = True
        if not hasattr(self.driver, "start_viewer_tracking"):
            self.viewer_session["error"] = "Tarayıcı izleyici doğrulamasını desteklemiyor."
            return
        try:
            session_token = self._session_token()
            response = fetch_viewer_token(cookie_path=self.cookie_path)
            status = int(response.get("status") or 0)
            self.viewer_session["status"] = status
            self.viewer_session["ok"] = bool(response.get("ok"))
            token = response.get("token")
            self.viewer_session["authenticated"] = bool(
                session_token and token and status == 200
            )
            if status != 200:
                self.viewer_session["error"] = str(
                    response.get("error")
                    or f"Kick izleyici oturumu HTTP {status} döndürdü."
                )
            elif not token:
                self.viewer_session["error"] = (
                    "Kick izleyici yanıtında token bulunamadı."
                )
            else:
                started = self.driver.start_viewer_tracking(
                    token,
                    self._channel_id,
                    self._livestream_id,
                    self._vod_id,
                )
                self.viewer_session["tracking_started"] = False
                if not started:
                    self.viewer_session["error"] = (
                        "Kick izleyici takip bağlantısı başlatılamadı."
                    )
        except Exception as error:
            self.viewer_session["error"] = str(error)

    def _refresh_viewer_tracking_status(self, now):
        if now - self._last_viewer_health_check < 5:
            return
        self._last_viewer_health_check = now
        if not hasattr(self.driver, "viewer_tracking_status"):
            return
        try:
            status = self.driver.viewer_tracking_status()
        except Exception as error:
            self.viewer_session["tracking_started"] = False
            self.viewer_session["error"] = str(error)
            return
        if not isinstance(status, dict):
            return
        self.viewer_session["tracking_started"] = bool(status.get("open"))
        if status.get("error"):
            self.viewer_session["error"] = str(status["error"])

    def _session_token(self):
        try:
            with open(
                self.cookie_path,
                "r",
                encoding="utf-8",
            ) as cookie_file:
                cookies = json.load(cookie_file)
            return next(
                (
                    str(cookie.get("value") or "")
                    for cookie in cookies
                    if isinstance(cookie, dict)
                    and cookie.get("name") == "session_token"
                    and cookie.get("value")
                ),
                "",
            )
        except Exception:
            return ""

    def _emit_update(
        self,
        state,
        live,
        video_ok,
        video_advanced,
        error=None,
    ):
        if not self.on_update:
            return
        payload = {
            "state": state,
            "elapsed_seconds": self.elapsed_seconds,
            "live": live,
            "video_ok": bool(video_ok),
            "video_advanced": bool(video_advanced),
            "drop_progress": self.drop_progress,
            "drop_verified": self.drop_verified,
            "viewer_session": dict(self.viewer_session),
            "live_source": self._last_live_source,
            "error": error or self.error_message,
        }
        try:
            self.on_update(payload)
        except TypeError:
            self.on_update(self.elapsed_seconds, live is True)

    def _emit_finish(self):
        if not self.on_finish:
            return
        try:
            self.on_finish(self.elapsed_seconds, self.completed, self.finish_reason)
        except TypeError:
            self.on_finish(self.elapsed_seconds, self.completed)

    def stop(self, reason="user_stopped"):
        self._requested_stop_reason = reason
        self.stop_event.set()

    def force_close_driver(self):
        driver = self.driver
        if driver is not None:
            close_chrome_driver(driver)

    def get_streamer_category_id(self):
        if not self.driver:
            return None
        try:
            data = kick_channel_data_by_api(self.url)
            livestream = data.get("livestream") if isinstance(data, dict) else None
            if livestream and livestream.get("is_live"):
                categories = livestream.get("categories") or []
                if categories:
                    return categories[0].get("id")
        except Exception:
            pass
        return None

    def _fetch_channel_data(self, username):
        script = """
        const done = arguments[arguments.length - 1];
        fetch(arguments[0], {
          credentials: 'include',
          cache: 'no-store',
          headers: {'Accept': 'application/json'}
        }).then(async response => {
          done(JSON.stringify({
            ok: response.ok,
            status: response.status,
            text: await response.text()
          }));
        }).catch(error => {
          done(JSON.stringify({ok: false, error: String(error)}));
        });
        """
        try:
            self.driver.set_script_timeout(12)
        except Exception:
            pass
        raw = self.driver.execute_async_script(
            script, f"https://kick.com/api/v2/channels/{username}"
        )
        envelope = json.loads(raw or "{}")
        if not envelope.get("ok"):
            return None
        return envelope.get("text")

    def is_stream_live(self):
        now = time.monotonic()
        if now - self._last_live_check < self._live_check_interval:
            return self._last_live_value
        self._last_live_check = now
        username = _kick_username_from_url(self.url)
        if username:
            data = kick_channel_data_by_api(self.url)
            if isinstance(data, dict):
                livestream = data.get("livestream")
                if isinstance(livestream, dict):
                    self._channel_id = (
                        livestream.get("channel_id")
                        or data.get("id")
                    )
                    self._livestream_id = livestream.get("id")
                    self._vod_id = (
                        livestream.get("vod_id")
                        or (livestream.get("vod") or {}).get("id")
                    )
                self._playback_url = (
                    data.get("playback_url")
                    or (
                        livestream.get("playback_url")
                        if isinstance(livestream, dict)
                        else None
                    )
                )
                self._last_live_value = bool(
                    livestream and livestream.get("is_live")
                )
                self._last_live_source = "direct_api"
                return self._last_live_value
            try:
                text = self._fetch_channel_data(username)
                data = json.loads(text) if text else None
                if isinstance(data, dict):
                    livestream = data.get("livestream")
                    self._last_live_value = bool(
                        livestream and livestream.get("is_live")
                    )
                    self._last_live_source = "browser_api"
                    return self._last_live_value
            except Exception:
                pass
            try:
                state_text = self.driver.execute_script(
                    """
                    try {
                      const next = document.getElementById('__NEXT_DATA__');
                      if (next && next.textContent) return next.textContent;
                      if (window.__NUXT__) return JSON.stringify(window.__NUXT__);
                    } catch (e) {}
                    return null;
                    """
                )
                if isinstance(state_text, str):
                    lowered = state_text.lower()
                    if '"is_live":true' in lowered:
                        self._last_live_value = True
                        self._last_live_source = "page_state"
                        return True
                    if '"is_live":false' in lowered:
                        self._last_live_value = False
                        self._last_live_source = "page_state"
                        return False
            except Exception:
                pass
        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text.upper()
            offline_markers = (
                "OFFLINE",
                "IS OFFLINE",
                "CHANNEL IS OFFLINE",
                "NOT LIVE",
                "ÇEVRİMDIŞI",
            )
            if any(marker in body for marker in offline_markers):
                self._last_live_value = False
                self._last_live_source = "dom_offline"
                return False
        except Exception:
            pass
        self._last_live_value = None
        self._last_live_source = "unknown"
        return None

    def get_video_health(self):
        try:
            result = self.driver.execute_script(
                """
                const video = document.querySelector('video');
                const diagnostic = (document.body?.innerText || '')
                  .replace(/\\s+/g, ' ').trim().slice(0, 180);
                if (!video) return {
                  exists: false,
                  diagnostic,
                  contentType: document.contentType || ''
                };
                return {
                  exists: true,
                  currentTime: Number(video.currentTime || 0),
                  paused: Boolean(video.paused),
                  ended: Boolean(video.ended),
                  readyState: Number(video.readyState || 0),
                  error: video.error ? Number(video.error.code || 1) : null,
                  hasSource: Boolean(video.currentSrc || video.src),
                  diagnostic,
                  contentType: document.contentType || ''
                };
                """
            )
        except Exception:
            result = None
        if not isinstance(result, dict):
            health = {
                "ok": False,
                "current_time": None,
                "diagnostic": "Video durumu tarayıcıdan okunamadı.",
            }
            self._last_video_health = health
            return health
        error_code = result.get("error")
        diagnostic = str(result.get("diagnostic") or "").strip()
        if error_code:
            diagnostic = f"Video hata kodu {error_code}. {diagnostic}".strip()
        elif result.get("exists") and int(result.get("readyState") or 0) < 2:
            diagnostic = (
                "Video hazır değil "
                f"(readyState={int(result.get('readyState') or 0)}, "
                f"paused={bool(result.get('paused'))}, "
                f"source={bool(result.get('hasSource'))}). "
                f"{diagnostic}"
            ).strip()
        health = {
            "ok": bool(
                result.get("exists")
                and not result.get("ended")
                and error_code is None
                and int(result.get("readyState") or 0) >= 2
            ),
            "current_time": result.get("currentTime"),
            "paused": bool(result.get("paused")),
            "diagnostic": diagnostic,
            "content_type": result.get("contentType"),
        }
        self._last_video_health = health
        return health

    def _video_advanced(self, video):
        current = video.get("current_time")
        if current is None:
            self._last_video_time = None
            return False
        try:
            current = float(current)
        except (TypeError, ValueError):
            return False
        previous = self._last_video_time
        self._last_video_time = current
        return previous is not None and current > previous + 0.05

    @staticmethod
    def _playback_identity(playback_url):
        if not playback_url:
            return None
        parsed = urlsplit(playback_url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _recover_playback_if_needed(self, now):
        if not hasattr(self.driver, "attach_hls"):
            return
        if self._unverified_since is None:
            return
        if now - self._unverified_since < self.playback_recovery_interval:
            return
        if now - self._last_playback_recovery < self.playback_recovery_interval:
            return
        self._last_playback_recovery = now
        self._last_live_check = 0.0
        self._hls_playback_url = None
        self._last_video_time = None

    def _verification_expired(self, now, verified):
        if verified:
            return False
        if not self._has_verified_playback:
            return (
                self._verification_started_at is not None
                and now - self._verification_started_at
                >= self.verification_timeout
            )
        return (
            self._unverified_since is not None
            and now - self._unverified_since
            >= self.playback_recovery_timeout
        )

    def ensure_player_state(self):
        if not self.driver:
            return
        current_identity = self._playback_identity(self._playback_url)
        attached_identity = self._playback_identity(self._hls_playback_url)
        if (
            self._playback_url
            and (
                self._hls_playback_url is None
                or current_identity != attached_identity
            )
            and hasattr(self.driver, "attach_hls")
        ):
            try:
                if self.driver.attach_hls(self._playback_url):
                    self._hls_playback_url = self._playback_url
            except Exception:
                pass
        hide = "true" if self.hide_player else "false"
        muted = "true" if self.mute else "false"
        volume = "0" if self.mute else "1"
        mini = "true" if (not self.hide_player and self.mini_player) else "false"
        playback_url = json.dumps(self._playback_url or "")
        direct_source = (
            "false" if hasattr(self.driver, "attach_hls") else "true"
        )
        try:
            self.driver.execute_script(
                f"""
                (() => {{
                  const video = document.querySelector('video');
                  if (!video) return;
                  try {{
                    const playbackUrl = {playback_url};
                    const playButton = [...document.querySelectorAll('button')].find(button => {{
                      const label = `${{button.getAttribute('aria-label') || ''}} ${{button.title || ''}}`;
                      return /(^|\\s)play(\\s|$)/i.test(label);
                    }});
                    if (playButton) playButton.click();
                    if ({direct_source} && !video.currentSrc && playbackUrl && video.src !== playbackUrl) {{
                      video.src = playbackUrl;
                      video.load();
                    }}
                    video.muted = {muted};
                    video.volume = {volume};
                    video.setAttribute('playsinline', '');
                    if (video.paused) video.play().catch(() => {{}});
                  }} catch (e) {{}}
                  if ({hide}) {{
                    video.style.opacity = '0';
                    video.style.width = '1px';
                    video.style.height = '1px';
                    video.style.position = 'fixed';
                    video.style.bottom = '0';
                    video.style.right = '0';
                    video.style.pointerEvents = 'none';
                  }} else if ({mini}) {{
                    video.style.opacity = '1';
                    video.style.width = '100px';
                    video.style.height = '100px';
                    video.style.position = 'fixed';
                    video.style.bottom = '6px';
                    video.style.right = '6px';
                    video.style.pointerEvents = 'none';
                    video.style.zIndex = '999999';
                  }}
                }})();
                """
            )
        except Exception:
            pass
