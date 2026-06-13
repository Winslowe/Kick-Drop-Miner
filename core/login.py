import logging
import time
import os
import secrets
import threading
from typing import Dict, Any

logger = logging.getLogger("kick_login")

# Store active login sessions for verification code flow
_login_sessions: Dict[str, Any] = {}
_sessions_lock = threading.Lock()


def _cleanup_old_sessions():
    """Remove sessions older than 10 minutes."""
    now = time.time()
    with _sessions_lock:
        expired = [k for k, v in _login_sessions.items() if now - v.get("created", 0) > 600]
        for k in expired:
            session = _login_sessions.pop(k, None)
            if session and session.get("driver"):
                try:
                    session["driver"].quit()
                except Exception:
                    pass


def perform_kick_login(username: str, password: str) -> Dict[str, Any]:
    """
    Attempt to login to kick.com via headless browser.
    Returns:
      {"success": True, "cookies": [...]}
      {"needs_verification": True, "session_id": "..."}
      {"success": False, "error": "Message"}
    """
    _cleanup_old_sessions()

    try:
        from core.browser import make_chrome_driver, make_firefox_driver
    except ImportError:
        return {"success": False, "error": "Tarayıcı modülü bulunamadı."}

    driver = None
    try:
        browser_type = os.environ.get("KDM_STREAM_BROWSER", "firefox_bidi").lower()
        profile_dir = os.path.join(
            os.environ.get("KDM_DATA_DIR", "data"),
            "chrome_data",
            f"login_{secrets.token_hex(4)}",
        )

        try:
            if "chrome" in browser_type:
                driver = make_chrome_driver(
                    headless=True, mute=True, is_worker=False, profile_dir=profile_dir
                )
            else:
                driver = make_firefox_driver(
                    headless=True, mute=True, is_worker=False, profile_dir=profile_dir
                )
        except Exception as e:
            logger.error(f"Browser launch failed: {e}")
            return {"success": False, "error": f"Tarayıcı başlatılamadı: {e}"}

        # Navigate to login
        driver.get("https://kick.com/login")
        time.sleep(5)

        # Check for Cloudflare
        page_title = ""
        try:
            page_title = driver.title.lower()
        except Exception:
            pass

        if "just a moment" in page_title or "cloudflare" in page_title:
            driver.quit()
            return {
                "success": False,
                "error": "Cloudflare bot korumasına takıldı. Lütfen Yer İmi yöntemini kullan.",
            }

        # Try finding login form
        try:
            email_input = driver.find_element(
                "xpath",
                "//input[@type='email' or @name='email' or @name='username' "
                "or @id='login-username' or @placeholder='Email']",
            )
            password_input = driver.find_element(
                "xpath",
                "//input[@type='password' or @name='password' or @id='login-password']",
            )

            email_input.send_keys(username)
            time.sleep(0.3)
            password_input.send_keys(password)
            time.sleep(0.3)

            submit = driver.find_element("xpath", "//button[@type='submit']")
            submit.click()
        except Exception:
            driver.quit()
            return {
                "success": False,
                "error": "Giriş formu bulunamadı. Kick arayüzü değişmiş olabilir. Yer İmi yöntemini dene.",
            }

        # Wait for login processing
        time.sleep(6)

        # Check if we landed on a verification code page
        page_source = ""
        try:
            page_source = driver.page_source.lower()
        except Exception:
            pass

        is_verification = any(
            kw in page_source
            for kw in [
                "verification",
                "doğrulama",
                "verify",
                "enter the code",
                "one-time",
                "otp",
                "kod",
            ]
        )

        if is_verification:
            # Store session for later verification
            session_id = secrets.token_hex(16)
            with _sessions_lock:
                _login_sessions[session_id] = {
                    "driver": driver,
                    "created": time.time(),
                    "profile_dir": profile_dir,
                }
            return {"needs_verification": True, "session_id": session_id}

        # Check if login succeeded
        cookies = driver.get_cookies()
        has_session = any(c.get("name") == "session_token" for c in cookies)

        if has_session:
            result = {"success": True, "cookies": cookies}
            driver.quit()
            return result
        else:
            driver.quit()
            return {
                "success": False,
                "error": "Giriş başarısız. Şifre yanlış olabilir veya Captcha çıktı. Yer İmi yöntemini dene.",
            }

    except Exception as e:
        logger.error(f"Login failed: {e}")
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        return {"success": False, "error": f"Bilinmeyen bir hata: {e}"}


def submit_verification_code(session_id: str, code: str) -> Dict[str, Any]:
    """
    Submit the email verification code in the stored browser session.
    """
    with _sessions_lock:
        session = _login_sessions.get(session_id)

    if not session or not session.get("driver"):
        return {"success": False, "error": "Giriş oturumu bulunamadı veya süresi dolmuş. Tekrar dene."}

    driver = session["driver"]

    try:
        # Try to find verification code input
        code_input = None
        for selector in [
            "//input[@type='text' and (@name='code' or @name='otp' or @id='otp' or @autocomplete='one-time-code')]",
            "//input[@maxlength='6' or @maxlength='4']",
            "//input[contains(@class, 'otp') or contains(@class, 'code') or contains(@class, 'verify')]",
            "//input[@type='number']",
        ]:
            try:
                code_input = driver.find_element("xpath", selector)
                if code_input:
                    break
            except Exception:
                continue

        if not code_input:
            return {
                "success": False,
                "error": "Doğrulama kodu alanı bulunamadı. Yer İmi yöntemini dene.",
            }

        code_input.clear()
        code_input.send_keys(code)
        time.sleep(0.5)

        # Try to submit
        try:
            submit_btn = driver.find_element("xpath", "//button[@type='submit']")
            submit_btn.click()
        except Exception:
            # Try pressing Enter
            from selenium.webdriver.common.keys import Keys
            code_input.send_keys(Keys.RETURN)

        time.sleep(5)

        # Check if login succeeded after verification
        cookies = driver.get_cookies()
        has_session = any(c.get("name") == "session_token" for c in cookies)

        if has_session:
            result = {"success": True, "cookies": cookies}
        else:
            result = {
                "success": False,
                "error": "Kod doğrulanamadı. Geçersiz veya süresi dolmuş olabilir.",
            }

        # Cleanup
        try:
            driver.quit()
        except Exception:
            pass
        with _sessions_lock:
            _login_sessions.pop(session_id, None)

        return result

    except Exception as e:
        logger.error(f"Verification failed: {e}")
        try:
            driver.quit()
        except Exception:
            pass
        with _sessions_lock:
            _login_sessions.pop(session_id, None)
        return {"success": False, "error": f"Doğrulama hatası: {e}"}
