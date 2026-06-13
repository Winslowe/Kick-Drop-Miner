"""Browser automation and cookie management"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import signal
from types import SimpleNamespace

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
import websocket

try:
    import undetected_chromedriver as uc
except ImportError:
    uc = None
from utils.helpers import cookie_file_for_domain, CHROME_DATA_DIR

# --- GHOST MODE PATCH ---
# We patch subprocess.Popen to force SW_HIDE on all Chrome processes created by undetected_chromedriver
# This completely prevents the window from ever appearing on screen or taskbar when headless=True
if os.name == "nt":
    _original_popen = subprocess.Popen
    def _patched_popen(*args, **kwargs):
        if getattr(subprocess, "_kdm_hide_chrome", False):
            # Check if this is a chrome or chromedriver process
            cmd = str(args) + str(kwargs)
            if (
                "chrome.exe" in cmd.lower()
                or "brave.exe" in cmd.lower()
                or "chromedriver" in cmd.lower()
            ):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                kwargs["startupinfo"] = startupinfo
        return _original_popen(*args, **kwargs)
    subprocess.Popen = _patched_popen
# ------------------------

class CookieManager:
    """Manages browser cookies for authentication"""

    WORKER_COOKIE_ALLOWLIST = {"session_token"}
    
    @staticmethod
    def save_cookies(driver, domain, cookie_path=None):
        """Save cookies from driver to file"""
        path = cookie_path or cookie_file_for_domain(domain)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cookies = driver.get_cookies()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
        return path

    @staticmethod
    def load_cookies(driver, domain, cookie_path=None):
        """Load cookies from file into driver"""
        path = cookie_path or cookie_file_for_domain(domain)
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        for c in cookies:
            if (
                getattr(driver, "_kdm_role", None) == "worker"
                and c.get("name") not in CookieManager.WORKER_COOKIE_ALLOWLIST
            ):
                continue
            # Fix certain fields that cause problems
            if "expiry" in c and c["expiry"] is None:
                del c["expiry"]
            try:
                driver.add_cookie(c)
            except Exception:
                # Firefox rejects some Chromium-exported sameSite values.
                retry_cookie = dict(c)
                retry_cookie.pop("sameSite", None)
                try:
                    driver.add_cookie(retry_cookie)
                except Exception:
                    pass
        return True

    @staticmethod
    def import_from_browser(domain: str) -> bool:
        """Attempts to import existing cookies from browsers (Chrome/Edge/Firefox)
        using browser_cookie3. Returns True if a file was written.
        """
        try:
            import browser_cookie3 as bc3  # type: ignore
        except Exception:
            return False

        try:
            cj = bc3.brave(domain_name=domain)
        except Exception:
            try:
                cj = bc3.load(domain_name=domain)
            except Exception:
                cj = None

        if not cj:
            return False

        cookies = []
        try:
            for c in cj:
                if not getattr(c, "name", None):
                    continue
                cookie = {
                    "name": c.name,
                    "value": c.value,
                    "domain": getattr(c, "domain", domain) or domain,
                    "path": getattr(c, "path", "/") or "/",
                    "secure": bool(getattr(c, "secure", False)),
                }
                exp = getattr(c, "expires", None)
                if exp is not None:
                    try:
                        cookie["expiry"] = int(exp)
                    except Exception:
                        pass
                cookies.append(cookie)
        except Exception:
            return False

        if not cookies:
            return False

        path = cookie_file_for_domain(domain)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2)
            return True
        except Exception:
            return False


class BrowserManager:
    """Tracks and reliably closes browser processes created by this app."""

    def __init__(self, profile_root):
        self.profile_root = os.path.abspath(profile_root)
        self._drivers = {}
        self._lock = threading.RLock()

    def _is_managed_path(self, path):
        if not path:
            return False
        try:
            return os.path.commonpath(
                (self.profile_root, os.path.abspath(path))
            ) == self.profile_root
        except (OSError, ValueError):
            return False

    def register(self, driver, profile_dir, role):
        service = getattr(driver, "service", None)
        service_process = getattr(service, "process", None)
        record = {
            "driver": driver,
            "pid": (
                getattr(driver, "browser_pid", None)
                or getattr(service_process, "pid", None)
            ),
            "profile_dir": os.path.abspath(profile_dir),
            "role": role or "browser",
        }
        with self._lock:
            self._drivers[id(driver)] = record
        try:
            driver._kdm_profile_dir = record["profile_dir"]
            driver._kdm_role = record["role"]
        except Exception:
            pass
        return driver

    def active_count(self, profile_prefix=None):
        with self._lock:
            if not profile_prefix:
                return len(self._drivers)
            return sum(
                1
                for record in self._drivers.values()
                if os.path.basename(record["profile_dir"]).startswith(profile_prefix)
            )

    def _pid_exists(self, pid):
        if not pid:
            return False
        if os.name != "nt":
            try:
                os.kill(int(pid), 0)
                return True
            except OSError:
                return False
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not process:
                return False
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        except Exception:
            return False

    def _kill_process_tree(self, pid):
        if not pid or not self._pid_exists(pid):
            return
        if os.name != "nt":
            descendants = []
            parent_map = {}
            proc_root = "/proc"
            try:
                for entry in os.scandir(proc_root):
                    if not entry.name.isdigit():
                        continue
                    try:
                        stat = open(
                            os.path.join(entry.path, "stat"),
                            "r",
                            encoding="utf-8",
                        ).read()
                        after_name = stat.rsplit(")", 1)[1].strip().split()
                        parent_map[int(entry.name)] = int(after_name[1])
                    except Exception:
                        continue
                pending = [int(pid)]
                while pending:
                    parent = pending.pop()
                    children = [
                        child
                        for child, process_parent in parent_map.items()
                        if process_parent == parent and child not in descendants
                    ]
                    descendants.extend(children)
                    pending.extend(children)
            except Exception:
                descendants = []
            for process_id in reversed(descendants + [int(pid)]):
                try:
                    os.kill(process_id, signal.SIGTERM)
                except OSError:
                    pass
            time.sleep(0.3)
            for process_id in reversed(descendants + [int(pid)]):
                if self._pid_exists(process_id):
                    try:
                        os.kill(process_id, signal.SIGKILL)
                    except OSError:
                        pass
            return
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                capture_output=True,
                timeout=8,
                creationflags=flags,
            )
        except Exception:
            pass

    def _kill_profile_processes(self, profile_dir=None):
        """Kill only Chrome-family processes using this app's profile root."""
        target = os.path.abspath(profile_dir or self.profile_root)
        if not self._is_managed_path(target):
            return
        if os.name != "nt":
            matching_pids = []
            try:
                for entry in os.scandir("/proc"):
                    if not entry.name.isdigit():
                        continue
                    try:
                        raw = open(
                            os.path.join(entry.path, "cmdline"),
                            "rb",
                        ).read()
                        command = raw.replace(b"\0", b" ").decode(
                            "utf-8", "ignore"
                        )
                    except Exception:
                        continue
                    if target in command and any(
                        name in command.lower()
                        for name in (
                            "chrome",
                            "chromium",
                            "chromedriver",
                            "firefox",
                            "geckodriver",
                        )
                    ):
                        matching_pids.append(int(entry.name))
            except Exception:
                matching_pids = []
            for pid in matching_pids:
                self._kill_process_tree(pid)
            return
        script = (
            "$root=$env:KDM_CHROME_PROFILE; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { "
            "$_.Name -match '^(chrome|chromedriver|brave)\\.exe$' -and "
            "$_.CommandLine -and $_.CommandLine.Contains($root) "
            "} | ForEach-Object { "
            "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue "
            "}"
        )
        env = os.environ.copy()
        env["KDM_CHROME_PROFILE"] = target
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                env=env,
                capture_output=True,
                timeout=15,
                creationflags=flags,
            )
        except Exception:
            pass

    def _remove_profile(self, profile_dir):
        if not self._is_managed_path(profile_dir):
            return
        for _ in range(3):
            try:
                shutil.rmtree(profile_dir)
                return
            except FileNotFoundError:
                return
            except Exception:
                time.sleep(0.25)
        shutil.rmtree(profile_dir, ignore_errors=True)

    def close(self, driver, cleanup_profile=True):
        if driver is None:
            return
        with self._lock:
            record = self._drivers.pop(id(driver), None)
        profile_dir = (
            record.get("profile_dir")
            if record
            else getattr(driver, "_kdm_profile_dir", None)
        )
        pid = record.get("pid") if record else getattr(driver, "browser_pid", None)
        try:
            driver.quit()
        except Exception:
            pass
        if self._pid_exists(pid):
            self._kill_process_tree(pid)
        if profile_dir:
            self._kill_profile_processes(profile_dir)
            if cleanup_profile:
                self._remove_profile(profile_dir)

    def close_all(self):
        with self._lock:
            drivers = [record["driver"] for record in self._drivers.values()]
        for driver in drivers:
            self.close(driver)

    def cleanup_stale_resources(self):
        """Clean orphan app browsers and disposable browser profiles."""
        self.close_all()
        self._kill_profile_processes(self.profile_root)
        try:
            entries = list(os.scandir(self.profile_root))
        except FileNotFoundError:
            entries = []
        for entry in entries:
            path = entry.path
            try:
                if entry.is_dir(follow_symlinks=False):
                    self._remove_profile(path)
                else:
                    os.remove(path)
            except Exception:
                pass
        os.makedirs(self.profile_root, exist_ok=True)


BROWSER_MANAGER = BrowserManager(CHROME_DATA_DIR)


def close_chrome_driver(driver, cleanup_profile=True):
    BROWSER_MANAGER.close(driver, cleanup_profile=cleanup_profile)


def active_browser_count(profile_prefix=None):
    return BROWSER_MANAGER.active_count(profile_prefix)


def cleanup_browser_resources():
    BROWSER_MANAGER.cleanup_stale_resources()


def _install_selenium_hls_adapter(driver):
    """Attach the local HLS player to a regular Selenium browser on demand."""
    source_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web",
        "static",
        "hls.min.js",
    )
    source = None

    def attach_hls(playback_url):
        nonlocal source
        if not playback_url:
            return False
        if source is None:
            with open(source_path, "r", encoding="utf-8") as source_file:
                source = source_file.read()
        script = (
            source
            + """
            return (() => {
              let video = document.querySelector('video');
              if (!video) {
                video = document.createElement('video');
                video.id = 'kdm-stream-player';
                video.muted = true;
                video.autoplay = true;
                video.playsInline = true;
                document.body.appendChild(video);
              }
              if (typeof Hls === 'undefined' || !Hls.isSupported()) return false;
              try {
                if (window.__kdmHls) window.__kdmHls.destroy();
              } catch (error) {}
              video.removeAttribute('src');
              video.load();
              const hls = new Hls({
                enableWorker: false,
                lowLatencyMode: true,
                startLevel: 0,
                capLevelToPlayerSize: true,
                maxBufferLength: 6,
                maxMaxBufferLength: 10,
                backBufferLength: 0,
                maxBufferSize: 12582912
              });
              hls.loadSource(arguments[0]);
              hls.attachMedia(video);
              hls.on(Hls.Events.MANIFEST_PARSED, () => {
                video.muted = true;
                video.play().catch(() => {});
              });
              hls.on(Hls.Events.ERROR, (_event, data) => {
                if (!data.fatal) return;
                if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
                else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                  hls.recoverMediaError();
                }
              });
              window.__kdmHls = hls;
              return true;
            })();
            """
        )
        return bool(driver.execute_script(script, playback_url))

    def start_viewer_tracking(token, channel_id, livestream_id, vod_id=None):
        if not token or not channel_id or not livestream_id:
            return False
        return bool(
            driver.execute_script(
                """
                try {
                  const previous = window.__kdmViewer;
                  if (previous) {
                    clearInterval(previous.handshakeTimer);
                    clearInterval(previous.eventTimer);
                    clearInterval(previous.pingTimer);
                    try { previous.socket.close(); } catch (error) {}
                  }
                  const token = arguments[0];
                  const channelId = String(arguments[1]);
                  const livestreamId = String(arguments[2]);
                  const vodId = arguments[3] == null ? null : String(arguments[3]);
                  const socket = new WebSocket(
                    `wss://websockets.kick.com/viewer/v1/connect?token=${token}`
                  );
                  const state = {
                    socket,
                    open: false,
                    error: null,
                    lastMessage: null,
                    closeCode: null,
                    handshakeTimer: null,
                    eventTimer: null,
                    pingTimer: null
                  };
                  const send = (type, data) => {
                    if (socket.readyState !== WebSocket.OPEN) return false;
                    socket.send(JSON.stringify({type, data}));
                    return true;
                  };
                  const handshake = () => send(
                    'channel_handshake',
                    {message: {channelId}}
                  );
                  const watchEvent = () => send('user_event', {message: {
                    name: 'tracking.user.watch.livestream',
                    channel_id: Number(channelId),
                    livestream_id: Number(livestreamId),
                    vod_id: vodId ? Number(vodId) : null
                  }});
                  socket.addEventListener('open', () => {
                    state.open = true;
                    handshake();
                    watchEvent();
                    state.handshakeTimer = setInterval(handshake, 15000);
                    state.eventTimer = setInterval(watchEvent, 120000);
                    state.pingTimer = setInterval(() => {
                      if (socket.readyState === WebSocket.OPEN) {
                        socket.send(JSON.stringify({type: 'ping'}));
                      }
                    }, 30000);
                  });
                  socket.addEventListener('error', () => {
                    state.error = 'WebSocket hatası';
                  });
                  socket.addEventListener('message', event => {
                    state.lastMessage = String(event.data || '').slice(0, 160);
                  });
                  socket.addEventListener('close', event => {
                    state.open = false;
                    state.closeCode = event.code;
                    if (event.code !== 1000) {
                      state.error = `Bağlantı kapandı (${event.code})`;
                    }
                  });
                  window.__kdmViewer = state;
                  return true;
                } catch (error) {
                  window.__kdmViewer = {open: false, error: String(error)};
                  return false;
                }
                """,
                token,
                str(channel_id),
                str(livestream_id),
                str(vod_id) if vod_id else None,
            )
        )

    def viewer_tracking_status():
        return driver.execute_script(
            """
            const state = window.__kdmViewer;
            if (!state) return {
              exists: false,
              open: false,
              readyState: null,
              error: 'İzleyici bağlantısı bulunamadı.'
            };
            return {
              exists: true,
              open: Boolean(state.open),
              readyState: Number(state.socket?.readyState ?? -1),
              closeCode: state.closeCode,
              lastMessage: state.lastMessage,
              error: state.error
            };
            """
        )

    driver.attach_hls = attach_hls
    driver.start_viewer_tracking = start_viewer_tracking
    driver.viewer_tracking_status = viewer_tracking_status


def _chrome_executable_candidates():
    """Yield likely Chrome executables in preference order."""
    seen = set()
    candidates = []
    configured_binary = os.environ.get("CHROME_BINARY")
    if configured_binary:
        candidates.append(configured_binary)

    if os.name == "nt":
        for env_name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"))
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))

    for command in ("brave", "brave-browser", "chrome", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(command)
        if path:
            candidates.append(path)

    for path in candidates:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.exists(path):
            yield path


def _parse_major_version(version_text):
    match = re.search(r"(\d+)\.", version_text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _chrome_version_from_registry():
    if os.name != "nt":
        return None

    try:
        import winreg
    except Exception:
        return None

    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon"),
    )
    for root, key_name in keys:
        try:
            with winreg.OpenKey(root, key_name) as key:
                version, _ = winreg.QueryValueEx(key, "version")
                major = _parse_major_version(str(version))
                if major:
                    return major
        except Exception:
            continue
    return None


def _chrome_version_from_executable(path):
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
    except Exception:
        return None

    return _parse_major_version((proc.stdout or "") + " " + (proc.stderr or ""))


def _detect_chrome():
    """Return (major_version, executable_path) for installed Chrome when possible."""
    executable = next(_chrome_executable_candidates(), None)

    if os.name == "nt":
        major = _chrome_version_from_registry()
        if major:
            return major, executable

    for path in ([executable] if executable else []):
        major = _chrome_version_from_executable(path)
        if major:
            return major, path

    return None, executable


class FirefoxBiDiDriver:
    """Small Firefox adapter that lets Kick finish bot checks before BiDi."""

    def __init__(self, profile_dir, visible_width=1280, visible_height=800):
        self.profile_dir = profile_dir
        self.visible_width = visible_width
        self.visible_height = visible_height
        self.browser_pid = None
        self.service = None
        self._process = None
        self._socket = None
        self._request_id = 0
        self._context = None
        self._lock = threading.RLock()
        self._origin_prepared = False
        self._challenge_complete = False
        self._pending_cookies = []
        self._network_urls = set()
        self._stderr = None
        self._hls_source = None
        self._write_profile_preferences()
        try:
            self._launch("about:blank")
            self._connect()
        except Exception:
            self._disconnect()
            self._stop_process()
            raise

    def _write_profile_preferences(self):
        preferences = {
            "media.autoplay.default": 0,
            "media.autoplay.blocking_policy": 0,
            "media.autoplay.block-webaudio": False,
            "media.autoplay.allow-muted": True,
            "media.autoplay.enabled.user-gestures-needed": False,
            "media.block-autoplay-until-in-foreground": False,
            "media.ffmpeg.enabled": True,
            "media.hls.enabled": True,
            "media.rdd-ffmpeg.enabled": True,
            "media.hardware-video-decoding.enabled": False,
            "dom.ipc.processCount": 2,
            "fission.autostart": False,
            "webgl.disabled": False,
            "webgl.force-enabled": True,
            "gfx.webrender.software": True,
            "browser.cache.memory.capacity": 32768,
            "image.mem.max_decoded_image_kb": 32768,
            "browser.shell.checkDefaultBrowser": False,
            "browser.tabs.warnOnClose": False,
        }
        path = os.path.join(self.profile_dir, "user.js")
        with open(path, "w", encoding="utf-8") as preference_file:
            for name, value in preferences.items():
                preference_file.write(
                    f"user_pref({json.dumps(name)}, {json.dumps(value)});\n"
                )

    @staticmethod
    def _free_port():
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        return port

    def _launch(self, url):
        self._port = self._free_port()
        binary = (
            os.environ.get("FIREFOX_BINARY")
            or shutil.which("firefox-esr")
            or shutil.which("firefox")
        )
        if not binary:
            raise RuntimeError("Firefox çalıştırılabilir dosyası bulunamadı.")
        stderr_target = subprocess.DEVNULL
        if os.environ.get("KDM_FIREFOX_LOG") == "1":
            log_path = os.path.join(CHROME_DATA_DIR, "firefox-bidi.log")
            self._stderr = open(log_path, "a", encoding="utf-8")
            stderr_target = self._stderr
        command = [
            binary,
            f"--remote-debugging-port={self._port}",
            "--remote-allow-hosts",
            "localhost",
            "--profile",
            self.profile_dir,
            "--new-instance",
            "--width",
            str(self.visible_width),
            "--height",
            str(self.visible_height),
            url,
        ]
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=stderr_target,
        )
        self.browser_pid = self._process.pid
        for _ in range(150):
            connection = socket.socket()
            connection.settimeout(0.3)
            try:
                connection.connect(("127.0.0.1", self._port))
                return
            except OSError:
                if self._process.poll() is not None:
                    raise RuntimeError("Firefox beklenmedik biçimde kapandı.")
                time.sleep(0.2)
            finally:
                connection.close()
        raise RuntimeError("Firefox BiDi bağlantısı açılamadı.")

    def _connect(self):
        self._socket = websocket.create_connection(
            f"ws://localhost:{self._port}/session",
            timeout=15,
            suppress_origin=True,
        )
        self._request_id = 0
        self._call(
            "session.new",
            {
                "capabilities": {
                    "alwaysMatch": {"acceptInsecureCerts": True}
                }
            },
        )
        tree = self._call("browsingContext.getTree")
        contexts = tree.get("contexts") or []
        if not contexts:
            raise RuntimeError("Firefox tarayıcı bağlamı bulunamadı.")
        self._context = contexts[0]["context"]
        if os.environ.get("KDM_NETWORK_LOG") == "1":
            self._call(
                "session.subscribe",
                {
                    "events": [
                        "network.beforeRequestSent",
                        "network.responseCompleted",
                    ]
                },
            )

    def _handle_event(self, message):
        if os.environ.get("KDM_NETWORK_LOG") != "1":
            return
        method = message.get("method")
        if method not in (
            "network.beforeRequestSent",
            "network.responseCompleted",
        ):
            return
        params = message.get("params") or {}
        request = params.get("request") or {}
        url = request.get("url")
        event_key = (method, url)
        if not url or event_key in self._network_urls:
            return
        self._network_urls.add(event_key)
        response = params.get("response") or {}
        detail = (
            request.get("method")
            or response.get("status")
            or ""
        )
        path = os.path.join(CHROME_DATA_DIR, "firefox-network.log")
        try:
            with open(path, "a", encoding="utf-8") as log_file:
                log_file.write(f"{method}\t{detail}\t{url}\n")
        except Exception:
            pass

    def _call(self, method, params=None):
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._socket.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
            )
            while True:
                message = json.loads(self._socket.recv())
                if message.get("id") != request_id:
                    self._handle_event(message)
                    continue
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result", {})

    def _disconnect(self):
        if self._socket is None:
            return
        try:
            self._call("session.end")
        except Exception:
            pass
        try:
            self._socket.close()
        except Exception:
            pass
        self._socket = None
        self._context = None

    def _stop_process(self):
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        if self._stderr is not None:
            try:
                self._stderr.close()
            except Exception:
                pass
            self._stderr = None

    def _relaunch_after_challenge(self, url):
        self._disconnect()
        self._stop_process()
        time.sleep(0.5)
        self._launch(url)
        challenge_wait = max(
            5.0, float(os.environ.get("KDM_FIREFOX_CHALLENGE_WAIT", "30"))
        )
        deadline = time.monotonic() + challenge_wait
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("Firefox Kick sayfasını açarken kapandı.")
            time.sleep(0.25)
        self._connect()
        self._challenge_complete = True
        for cookie in self._pending_cookies:
            self._set_cookie(cookie)

    def get(self, url):
        if not self._origin_prepared:
            # Cookies are written into the blank profile before the real page opens.
            self._origin_prepared = True
            return
        self._relaunch_after_challenge(url)

    def _set_cookie(self, cookie):
        payload = {
            "name": str(cookie.get("name") or ""),
            "value": {
                "type": "string",
                "value": str(cookie.get("value") or ""),
            },
            "domain": str(cookie.get("domain") or "kick.com"),
            "path": str(cookie.get("path") or "/"),
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": bool(cookie.get("secure", False)),
        }
        same_site = str(cookie.get("sameSite") or "").lower()
        if same_site in ("strict", "lax", "none"):
            payload["sameSite"] = same_site
        if cookie.get("expiry"):
            payload["expiry"] = int(cookie["expiry"])
        try:
            self._call("storage.setCookie", {"cookie": payload})
        except Exception:
            payload["domain"] = payload["domain"].lstrip(".")
            self._call("storage.setCookie", {"cookie": payload})

    def add_cookie(self, cookie):
        if not self._challenge_complete:
            self._pending_cookies.append(dict(cookie))
            return
        self._set_cookie(cookie)

    def execute_script(self, script, *args):
        argument_json = json.dumps(args, ensure_ascii=False)
        expression = (
            "JSON.stringify((function(){"
            + script
            + "}).apply(null,"
            + argument_json
            + "))"
        )
        result = self._call(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": self._context},
                "awaitPromise": True,
            },
        ).get("result", {})
        if result.get("type") in ("null", "undefined"):
            return None
        raw = result.get("value")
        if raw is None:
            return None
        return json.loads(raw)

    def execute_async_script(self, _script, *_args):
        raise RuntimeError("BiDi worker eşzamansız tarayıcı API çağrısı kullanmıyor.")

    def start_viewer_tracking(
        self,
        token,
        channel_id,
        livestream_id,
        vod_id=None,
    ):
        if not token or not channel_id or not livestream_id:
            return False
        expression = (
            "(() => {"
            + "try{"
            + "const previous=window.__kdmViewer;"
            + "if(previous){"
            + "clearInterval(previous.handshakeTimer);"
            + "clearInterval(previous.eventTimer);"
            + "clearInterval(previous.pingTimer);"
            + "try{previous.socket.close();}catch(e){}}"
            + f"const channelId={json.dumps(str(channel_id))};"
            + f"const livestreamId={json.dumps(str(livestream_id))};"
            + f"const vodId={json.dumps(str(vod_id) if vod_id else None)};"
            + "const socket=new WebSocket("
            + json.dumps("wss://websockets.kick.com/viewer/v1/connect?token=")
            + "+"
            + json.dumps(str(token))
            + ");"
            + "const state={socket,open:false,error:null,"
            + "lastMessage:null,closeCode:null,"
            + "handshakeTimer:null,eventTimer:null,pingTimer:null};"
            + "const send=(type,data)=>{"
            + "if(socket.readyState!==WebSocket.OPEN)return false;"
            + "socket.send(JSON.stringify({type,data}));return true;};"
            + "const handshake=()=>send('channel_handshake',"
            + "{message:{channelId}});"
            + "const watchEvent=()=>send('user_event',{message:{"
            + "name:'tracking.user.watch.livestream',"
            + "channel_id:Number(channelId),"
            + "livestream_id:Number(livestreamId),"
            + "vod_id:vodId?Number(vodId):null}});"
            + "socket.addEventListener('open',()=>{"
            + "state.open=true;handshake();watchEvent();"
            + "state.handshakeTimer=setInterval(handshake,15000);"
            + "state.eventTimer=setInterval(watchEvent,120000);"
            + "state.pingTimer=setInterval(()=>{"
            + "if(socket.readyState===WebSocket.OPEN)"
            + "socket.send(JSON.stringify({type:'ping'}));},30000);"
            + "});"
            + "socket.addEventListener('error',()=>{state.error='WebSocket hatası';});"
            + "socket.addEventListener('message',event=>{"
            + "state.lastMessage=String(event.data||'').slice(0,160);});"
            + "socket.addEventListener('close',event=>{"
            + "state.open=false;state.closeCode=event.code;"
            + "if(event.code!==1000)state.error=`Bağlantı kapandı (${event.code})`;});"
            + "window.__kdmViewer=state;return true;"
            + "}catch(error){window.__kdmViewer={open:false,error:String(error)};return false;}"
            + "})()"
        )
        result = self._call(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": self._context},
                "awaitPromise": True,
            },
        ).get("result", {})
        return bool(result.get("value")) if result.get("type") == "boolean" else False

    def viewer_tracking_status(self):
        return self.execute_script(
            """
            const state = window.__kdmViewer;
            if (!state) return {
              exists: false,
              open: false,
              readyState: null,
              error: 'İzleyici bağlantısı bulunamadı.'
            };
            return {
              exists: true,
              open: Boolean(state.open),
              readyState: Number(state.socket?.readyState ?? -1),
              closeCode: state.closeCode,
              lastMessage: state.lastMessage,
              error: state.error
            };
            """
        )

    def attach_hls(self, playback_url):
        if not playback_url or not self._context:
            return False
        if self._hls_source is None:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(root, "web", "static", "hls.min.js")
            with open(path, "r", encoding="utf-8") as source_file:
                self._hls_source = source_file.read()
        expression = (
            self._hls_source
            + "\n;(() => {"
            + "const video=document.querySelector('video');"
            + "if(!video||typeof Hls==='undefined'||!Hls.isSupported())return false;"
            + "try{if(window.__kdmHls)window.__kdmHls.destroy();}catch(e){}"
            + "video.removeAttribute('src');video.load();"
            + "const hls=new Hls({enableWorker:false,lowLatencyMode:true,"
            + "startLevel:0,capLevelToPlayerSize:true,"
            + "maxBufferLength:6,maxMaxBufferLength:10,backBufferLength:0,"
            + "maxBufferSize:12582912});"
            + f"hls.loadSource({json.dumps(playback_url)});"
            + "hls.attachMedia(video);"
            + "hls.on(Hls.Events.MANIFEST_PARSED,()=>{"
            + "video.muted=true;video.play().catch(()=>{});});"
            + "hls.on(Hls.Events.ERROR,(_event,data)=>{"
            + "if(!data.fatal)return;"
            + "if(data.type===Hls.ErrorTypes.NETWORK_ERROR)hls.startLoad();"
            + "else if(data.type===Hls.ErrorTypes.MEDIA_ERROR)hls.recoverMediaError();"
            + "});"
            + "window.__kdmHls=hls;return true;"
            + "})()"
        )
        result = self._call(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": self._context},
                "awaitPromise": True,
            },
        ).get("result", {})
        return bool(result.get("value")) if result.get("type") == "boolean" else False

    def set_script_timeout(self, _seconds):
        return None

    def set_window_size(self, _width, _height):
        return None

    def set_window_position(self, _x, _y):
        return None

    def find_element(self, by, value):
        if str(by).lower().endswith("tag name") and str(value).lower() == "body":
            text = self.execute_script("return document.body?.innerText || '';")
            return SimpleNamespace(text=text or "")
        raise RuntimeError("BiDi worker yalnız body metnini sorgulayabilir.")

    def get_cookies(self):
        return []

    def quit(self):
        self._disconnect()
        self._stop_process()


def make_chrome_driver(
    headless=True,
    visible_width=1280,
    visible_height=800,
    driver_path=None,
    extension_path=None,
    profile_dir_name="default",
    role="browser",
):
    """Create and configure a Chrome driver instance"""
    if (
        role == "worker"
        and os.environ.get("KDM_STREAM_BROWSER", "").lower() == "firefox_bidi"
    ):
        profile_dir = os.path.join(CHROME_DATA_DIR, profile_dir_name)
        os.makedirs(profile_dir, exist_ok=True)
        try:
            driver = FirefoxBiDiDriver(
                profile_dir,
                visible_width=visible_width,
                visible_height=visible_height,
            )
        except Exception:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise
        return BROWSER_MANAGER.register(driver, profile_dir, role)

    if (
        role == "worker"
        and os.environ.get("KDM_STREAM_BROWSER", "").lower() == "firefox"
    ):
        return make_firefox_driver(
            headless=headless,
            visible_width=visible_width,
            visible_height=visible_height,
            profile_dir_name=profile_dir_name,
            role=role,
        )

    use_system_chromium = (
        os.name != "nt"
        or os.environ.get("KDM_SYSTEM_CHROMIUM") == "1"
        or uc is None
    )
    opts = Options() if use_system_chromium else uc.ChromeOptions()
    chrome_major, chrome_executable = _detect_chrome()
    platform_token = (
        "Windows NT 10.0; Win64; x64"
        if os.name == "nt"
        else "X11; Linux x86_64"
    )
    browser_major = chrome_major or 120
    user_agent = os.environ.get("KDM_USER_AGENT") or (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{browser_major}.0.0.0 Safari/537.36"
    )

    # Xvfb deployments use the same browser identity as headless deployments.
    opts.add_argument(f"--window-size={visible_width},{visible_height}")
    opts.add_argument(f"--user-agent={user_agent}")
    if headless:
        opts.add_argument("--headless=new")
    opts.page_load_strategy = "eager"

    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # Remove redundant experimental options to avoid parsing error
    # (undetected-chromedriver already handles this natively)
    opts.add_argument("--log-level=3")
    opts.add_argument("--silent")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-component-update")
    opts.add_argument("--disable-sync")
    opts.add_argument("--autoplay-policy=no-user-gesture-required")
    opts.add_argument("--disable-features=Translate,MediaRouter")
    if use_system_chromium and role == "worker":
        opts.add_experimental_option(
            "excludeSwitches",
            ["enable-automation"],
        )
        opts.add_experimental_option("useAutomationExtension", False)
    if os.name != "nt":
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--disable-site-isolation-trials")
        opts.add_argument("--renderer-process-limit=1")
        opts.add_argument("--process-per-site")
        opts.add_argument("--disk-cache-size=33554432")
        opts.add_argument("--media-cache-size=16777216")
        opts.add_argument("--js-flags=--max-old-space-size=256")
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-renderer-backgrounding")
    if role == "api":
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-extensions")

    user_data_dir = os.path.join(CHROME_DATA_DIR, profile_dir_name)
    os.makedirs(user_data_dir, exist_ok=True)
    opts.add_argument(f"--user-data-dir={user_data_dir}")

    # Extension loading (compatible with uc)
    if extension_path:
        try:
            if extension_path.lower().endswith(".crx"):
                opts.add_extension(extension_path)
            else:
                opts.add_argument(f"--load-extension={extension_path}")
        except Exception:
            pass

    if chrome_executable:
        opts.binary_location = chrome_executable

    # Ghost Mode: Natively hide Chrome via patched subprocess.Popen
    if headless:
        setattr(subprocess, "_kdm_hide_chrome", True)
        
    try:
        if use_system_chromium:
            executable = (
                driver_path
                or os.environ.get("CHROMEDRIVER_PATH")
                or shutil.which("chromedriver")
            )
            service_kwargs = {}
            if os.environ.get("KDM_CHROMEDRIVER_LOG") == "1":
                service_kwargs = {
                    "service_args": ["--verbose"],
                    "log_output": os.path.join(
                        CHROME_DATA_DIR, "chromedriver.log"
                    ),
                }
            service = (
                Service(executable_path=executable, **service_kwargs)
                if executable
                else Service(**service_kwargs)
            )
            driver = webdriver.Chrome(service=service, options=opts)
            try:
                driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {
                        "source": (
                            "Object.defineProperty(navigator, 'webdriver', "
                            "{get: () => undefined});"
                        )
                    },
                )
            except Exception:
                pass
        else:
            driver_kwargs = {
                "options": opts,
                "version_main": chrome_major,
            }
            if chrome_executable:
                driver_kwargs["browser_executable_path"] = chrome_executable
            if driver_path and os.path.isfile(driver_path):
                driver_kwargs["driver_executable_path"] = driver_path
            driver = uc.Chrome(**driver_kwargs)
    except Exception:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        raise
    finally:
        if headless:
            setattr(subprocess, "_kdm_hide_chrome", False)

    if role == "worker":
        _install_selenium_hls_adapter(driver)
    return BROWSER_MANAGER.register(driver, user_data_dir, role)


def make_firefox_driver(
    headless=True,
    visible_width=1280,
    visible_height=800,
    profile_dir_name="worker",
    role="worker",
):
    """Create a managed Firefox worker for Linux servers blocked on Chromium."""
    profile_dir = os.path.join(CHROME_DATA_DIR, profile_dir_name)
    os.makedirs(profile_dir, exist_ok=True)

    options = FirefoxOptions()
    options.binary_location = (
        os.environ.get("FIREFOX_BINARY")
        or shutil.which("firefox-esr")
        or shutil.which("firefox")
    )
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("-headless")
    options.add_argument("-profile")
    options.add_argument(profile_dir)
    options.set_preference("media.autoplay.default", 0)
    options.set_preference("media.autoplay.blocking_policy", 0)
    options.set_preference("media.autoplay.block-webaudio", False)
    options.set_preference("media.ffmpeg.enabled", True)
    options.set_preference("media.hls.enabled", True)
    options.set_preference("media.rdd-ffmpeg.enabled", True)
    options.set_preference("media.hardware-video-decoding.enabled", False)
    options.set_preference("dom.ipc.processCount", 2)
    options.set_preference("fission.autostart", False)
    options.set_preference("webgl.disabled", False)
    options.set_preference("webgl.force-enabled", True)
    options.set_preference("gfx.webrender.software", True)
    options.set_preference("browser.shell.checkDefaultBrowser", False)
    options.set_preference("browser.tabs.warnOnClose", False)
    options.set_capability("webSocketUrl", False)

    executable = (
        os.environ.get("GECKODRIVER_PATH")
        or shutil.which("geckodriver")
    )
    service_kwargs = {}
    if os.environ.get("KDM_GECKODRIVER_LOG") == "1":
        service_kwargs["log_output"] = os.path.join(
            CHROME_DATA_DIR, "geckodriver.log"
        )
    service = (
        FirefoxService(executable_path=executable, **service_kwargs)
        if executable
        else FirefoxService(**service_kwargs)
    )
    try:
        driver = webdriver.Firefox(service=service, options=options)
        driver.set_window_size(visible_width, visible_height)
    except Exception:
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise
    return BROWSER_MANAGER.register(driver, profile_dir, role)
