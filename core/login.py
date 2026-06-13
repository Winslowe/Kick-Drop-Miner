import logging
import time
import os
import secrets
from typing import Dict, Any

from core.browser import make_firefox_driver, make_chrome_driver, close_chrome_driver
from utils.helpers import DATA_DIR

logger = logging.getLogger("kick_login")

def perform_kick_login(username: str, password: str) -> Dict[str, Any]:
    """
    Attempt to login to kick.com via headless browser.
    Returns {"success": True, "cookies": [...]} or {"success": False, "error": "Message"}
    """
    driver = None
    profile_dir = DATA_DIR / "chrome_data" / f"login_{secrets.token_hex(4)}"
    
    try:
        # Determine which browser to use based on env, similar to worker
        browser_type = os.environ.get("KDM_STREAM_BROWSER", "firefox_bidi").lower()
        
        try:
            if "chrome" in browser_type:
                driver = make_chrome_driver(headless=True, mute=True, is_worker=False, profile_dir=str(profile_dir))
            else:
                driver = make_firefox_driver(headless=True, mute=True, is_worker=False, profile_dir=str(profile_dir))
        except Exception as e:
            return {"success": False, "error": f"Tarayıcı başlatılamadı: {e}"}
            
        driver.get("https://kick.com/login")
        
        # Wait for page load or cloudflare check
        time.sleep(5)
        
        page_source = ""
        try:
            page_source = driver.page_source.lower()
        except:
            pass
            
        if "cloudflare" in page_source or "just a moment" in driver.title.lower():
            return {"success": False, "error": "Cloudflare bot korumasına takıldı. Lütfen 'Yer İmi' (Bookmarklet) yöntemini kullanın."}
            
        try:
            email_input = driver.find_element("xpath", "//input[@type='email' or @name='email' or @name='username' or @id='login-username']")
            password_input = driver.find_element("xpath", "//input[@type='password' or @name='password' or @id='login-password']")
            
            email_input.send_keys(username)
            password_input.send_keys(password)
            
            submit = driver.find_element("xpath", "//button[@type='submit']")
            submit.click()
        except Exception as e:
            return {"success": False, "error": "Giriş formu bulunamadı veya değişmiş. Lütfen Yer İmi yöntemini kullanın."}
        
        # Wait for login processing
        time.sleep(6)
        
        cookies = driver.get_cookies()
        has_session = any(c.get("name") == "session_token" for c in cookies)
        
        if has_session:
            return {"success": True, "cookies": cookies}
        else:
            return {"success": False, "error": "Giriş başarısız. Şifre yanlış olabilir veya Captcha çıktı."}
            
    except Exception as e:
        logger.error(f"Login failed: {e}")
        return {"success": False, "error": "Bilinmeyen bir hata oluştu."}
    finally:
        if driver:
            try:
                close_chrome_driver(driver, cleanup_profile=True)
            except:
                try:
                    driver.quit()
                except:
                    pass
