document.getElementById('copyBtn').addEventListener('click', () => {
  chrome.cookies.get({url: 'https://kick.com', name: 'session_token'}, (cookie) => {
    if (cookie) {
      navigator.clipboard.writeText(cookie.value).then(() => {
        document.getElementById('status').innerText = "✅ Kopyalandı! Şimdi panele yapıştırın.";
        document.getElementById('status').style.color = "#53fc18";
      });
    } else {
      document.getElementById('status').innerText = "❌ Hata: Önce Kick.com'a giriş yapmalısınız!";
      document.getElementById('status').style.color = "#ff5571";
    }
  });
});
