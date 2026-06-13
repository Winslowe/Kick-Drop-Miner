<div align="center">
  <img src="assets/screenshot.png" alt="Kick Drop Miner Dashboard" width="850" />
  
  <br/>

  # ⛏️ Kick Drop Miner
  
  **Kick drop kampanyalarını doğrulanmış video oynatımı ve Kick sunucu ilerlemesiyle otomatik olarak takip eden akıllı ve yenilmez madenci.**

  <p align="center">
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%2B-blue.svg?style=for-the-badge&logo=python" alt="Python"></a>
    <a href="#"><img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Ubuntu-lightgrey.svg?style=for-the-badge&logo=linux" alt="Platform"></a>
    <a href="#"><img src="https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge" alt="License"></a>
  </p>

  <p align="center">
    <a href="#-proje-hakkında">Proje Hakkında</a> •
    <a href="#-öne-çıkan-özellikler">Özellikler</a> •
    <a href="#-kurulum-ve-çalıştırma">Kurulum</a> •
    <a href="#-kick-çerezi-session-token-nasıl-alınır">Kullanım Rehberi</a>
  </p>
</div>

---

## 🌟 Proje Hakkında

**Kick Drop Miner**, arka planda Kick yayınlarını izleyerek ödüllerinizi (droplarınızı) otomatik olarak toplayan son teknoloji bir otomasyon aracıdır. Gelişmiş sunucu tabanlı doğrulama mimarisi sayesinde sadece yayın gerçekten canlıyken ve video akışı varken ilerleme kaydeder, böylece gereksiz kaynak tüketiminin önüne geçer.

Sistem ihtiyaçlarınıza göre iki farklı kullanım sunar:
- 🌐 **Web Paneli (`webapp.py`)**: 7/24 Pterodactyl veya Ubuntu sunucularında çalışması için tasarlanmış modern bir arayüz.
- 🖥️ **Masaüstü Arayüzü (`main.py`)**: Windows bilgisayarlarında kullanmak isteyenler için lokal uygulama.

---

## 🚀 Öne Çıkan Özellikler

### 🛡️ Gelişmiş Madenci Motoru (Sunucu Sürümü)
> Madenci altyapısı tamamen performans ve gizlilik odaklı tasarlanmıştır.

- **⚡ Sıfır Kaynak Tüketimi**: Boştayken sistemde `0` tarayıcı barındırır. Sadece madencilik anında `1` adet süreç çalıştırır.
- **🧠 Akıllı Doğrulama**: Video süresi sadece kanal canlıysa ve süre (`currentTime`) gerçekten ilerliyorsa artar.
- **🔐 Tam Güvenlik**: İzleyici tokenı, WebSocket bağlantıları ve handshake işlemleri sıkı denetimden geçer.
- **🔄 Gerçek Zamanlı Senkronizasyon**: Drop ilerlemeleri her 60 saniyede bir Kick'in kendi sunucusundan çekilir.
- **🎯 Tamamlanma Teyidi**: Bir kampanya ancak Kick API'si `%100 / Claimed` yanıtını verdiğinde tamamlanmış sayılır.
- **🧹 Otomatik Temizlik**: Uygulama durdurulduğunda arkada bırakılan hiçbir "Zombi Tarayıcı" kalmaz, her şey temizlenir.

### 💻 Modern Web Arayüzü
> Tüm kontrolü elinize alan, şık ve tepkisel (responsive) kontrol paneli.

| Özellik | Açıklama |
| :--- | :--- |
| **Animasyonlu Panel** | Türkçe dil destekli, göz yormayan karanlık tema ve pürüzsüz animasyonlar. |
| **Oyun Bazlı Envanter** | Rust, CS2 gibi oyunların droplarını kendi alt kategorilerinde akıllıca gruplar. |
| **Çoklu Kullanıcı** | Her kullanıcı için tamamen izole çerezler, sıralar ve ayrı worker görevleri. |
| **Gelişmiş Yönetim** | Admin paneli üzerinden üyelerin durumunu, aktif madencileri ve IP geçmişini izleme. |
| **Canlı Hata Ayıklama** | Renk kodlarıyla zenginleştirilmiş canlı konsol. |
| **Sıkı Güvenlik** | CSRF korumaları, Scrypt tabanlı şifrelemeler ve Brute-force önlemleri. |

---

## ⚙️ Sunucu Gereksinimleri (Pterodactyl)

Bu proje düşük kaynaklı ARM64/AMD64 sistemler için optimize edilmiştir.
* **OS:** Ubuntu 20.04+ (ARM64 / AMD64)
* **Yazılım:** Python 3.11+, Firefox ESR, FFmpeg, Geckodriver, Xvfb

<details>
<summary><b>🛠️ Kurulum Adımlarını Görmek İçin Tıklayın</b></summary>

### 1. Ortam Ayarları (.env)
Projeyi indirdikten sonra `.env.example` dosyasının adını `.env` olarak değiştirin ve içini doldurun:

```env
KDM_PASSWORD_HASH=scrypt$...
KDM_SESSION_SECRET=uzun-rastgele-bir-deger
KDM_SECURE_COOKIES=1
KDM_ADMIN_USERNAME=admin
KDM_REGISTRATION_ENABLED=1
KDM_MAX_ACTIVE_MINERS=3
KDM_DATA_DIR=/home/container/data
KDM_STREAM_BROWSER=firefox_bidi
FIREFOX_BINARY=/usr/bin/firefox-esr
```

### 2. Yerel Sunucuyu Başlatma
Gerekli Python kütüphanelerini kurun ve sunucuyu ayaklandırın:
```bash
pip install -r requirements-server.txt
python -m uvicorn webapp:app --host 0.0.0.0 --port 8000
```
*(Güvenliğiniz için production ortamında Cloudflare Tunnel veya Ters Proxy kullanmanız önerilir.)*
</details>

---

## 🍪 Kick Çerezi (Session Token) Nasıl Alınır?

Sistemin sizin hesabınızla izleme yapabilmesi için tek bir şeye ihtiyacı var: `session_token`.
Bunu bulmak çok basittir:

1. Tarayıcınızdan **Kick.com** adresine girip hesabınıza normal şekilde giriş yapın.
2. Klavyenizden `F12` tuşuna basarak **Geliştirici Araçları**nı (Developer Tools) açın.
3. Üst sekmelerden **Application** (veya Uygulama) kısmına gelin.
4. Sol menüden **Storage > Cookies > `https://kick.com`** yolunu izleyin.
5. Sağdaki listeden **`session_token`** yazan satırı bulun ve karşısındaki uzun değeri kopyalayın.
6. Bu değeri kopyalayıp Kick Drop Miner web panelindeki çerez alanına yapıştırıp kaydedin.

> **⚠️ ÖNEMLİ:** `session_token` sizin dijital anahtarınızdır. **Kesinlikle** hiç kimseyle paylaşmayın! Bu sistem çerezlerinizi şifrelenmiş biçimde özel bir klasörde izole ederek saklar.

---

## 📂 Veri Yapısı ve Güvenlik

Sistem tüm kişisel verilerinizi `KDM_DATA_DIR` altında güvenle muhafaza eder:
* 📝 `config.json` — Kayıtlı genel yayın sıraları.
* 🍪 `cookies/` — Şifrelenmiş doğrulanmış oturum çerezleri.
* 👥 `accounts.sqlite3` — Kullanıcı veritabanı.
* 🔒 `users/` — Üyelerin birbirinden izole edilmiş verileri.

*(Tüm bu klasörler güvenlik kalkanı olarak `.gitignore` üzerinden engellenmiştir, GitHub'a asla yüklenmez.)*
