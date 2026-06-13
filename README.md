# Kick Drop Miner

Kick drop kampanyalarını doğrulanmış video oynatımı ve Kick sunucu ilerlemesiyle takip eden madenci. Proje iki arayüz içerir:

- `webapp.py`: Ubuntu/Pterodactyl için modern web paneli
- `main.py`: Windows için eski masaüstü arayüzü

## Sunucu Sürümü

Web paneli Raspberry Pi ve düşük kaynaklı Ubuntu sunucular için tasarlanmıştır.

- Boştayken uygulamaya ait tarayıcı sayısı `0`
- Madencilik sırasında normalde `1` Firefox
- Envanter yenilemesi doğrudan API ile yapılabildiğinde ek tarayıcı açılmaz
- Video süresi yalnız kanal canlı ve `currentTime` gerçekten ilerliyorsa artar
- Kick izleyici tokenı, WebSocket bağlantısı ve kanal handshake mesajları doğrulanır
- Drop ilerlemesi her 60 saniyede Kick sunucusundan okunur
- İlerleme 8 dakika değişmezse kanal kapatılır ve sıradaki uygun kanala geçilir
- Kampanya yalnız Kick `%100/claimed` döndürdüğünde tamamlanır
- Durdurma, sıra temizleme ve uygulama kapanışı tarayıcı süreçlerini merkezi olarak kapatır

## Web Arayüzü

- Türkçe yönetim paneli ve animasyonlu başlangıç ekranı
- Kullanıcı adı/şifre ile üyelik ve oturum açma
- Kullanıcı başına tamamen ayrı Kick çerezi, sıra, envanter ve worker
- Yöneticiye özel kullanıcı, son erişim, IP, Kick ve madenci durum paneli
- Kampanya bannerları, ödül görselleri ve kanal avatarları
- Oyun bazında gruplanmış drop envanteri
- Aynı drop için uygun yayıncıları tek görev ve tek ilerleme altında toplama
- Yayın kapanınca doğrulanmış süreyi koruyarak alternatif yayıncıya geçme
- Aynı kampanyanın iki kez eklenmesini engelleme
- Aktif tarayıcı ve doğrulanmış izleme durumu
- Renk kodlu canlı madenci konsolu
- Konsol indirme ve temizleme
- Adım adım `session_token` kurulum rehberi
- CSRF koruması, imzalı oturum, scrypt parola doğrulaması ve giriş hız sınırı

## Pterodactyl Gereksinimleri

- Ubuntu ARM64 veya AMD64
- Python 3.11+
- Firefox ESR
- Xvfb
- FFmpeg ve Firefox medya kitaplıkları
- Geckodriver

Bu depo için hazırlanan `Dockerfile.arm64` ve `pterodactyl-start.sh`, ARM64 Pterodactyl kurulumunu destekler.

## Ortam Ayarları

`.env.example` dosyasını `.env` olarak kopyalayın ve en az şu iki değeri değiştirin:

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

Parola özeti oluşturma örneği:

```powershell
@'
import hashlib, secrets
password = input("Parola: ").encode()
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(password, salt=salt, n=16384, r=8, p=1, dklen=32)
print(f"scrypt$16384$8$1${salt.hex()}${digest.hex()}")
'@ | python -
```

## Yerel Sunucu Başlatma

```powershell
pip install -r requirements-server.txt
python -m uvicorn webapp:app --host 0.0.0.0 --port 8000
```

Üretimde HTTPS arkasında çalıştırın. Cloudflare Tunnel veya ters proxy kullanılabilir.

## Kick Çerezi

Web panelindeki **Ayarlar** sayfasında ayrıntılı rehber bulunur. Kısa sürüm:

1. Kendi bilgisayarınızda Kick hesabına giriş yapın.
2. `F12` ile geliştirici araçlarını açın.
3. `Application/Uygulama > Storage > Cookies > https://kick.com` yoluna gidin.
4. `session_token` satırının değerini kopyalayın.
5. Web panelindeki çerez alanına yapıştırıp kaydedin.

`session_token` hesap oturum anahtarıdır. Paylaşmayın ve kaynak koduna eklemeyin. Çok kullanıcılı sürümde her üyenin çerezi kendi kullanıcı veri klasöründe saklanır.

## Testler

```powershell
python -m unittest discover -s tests -v
python -m compileall -q core webapp.py
node --check web/static/app.js
```

Test kapsamı; tarayıcı sahipliği, kapanış, video ilerlemesi, bilinmeyen canlılık, sunucu doğrulamalı kampanya tamamlanması, dönen HLS tokenları ve bozuk yapılandırma dosyalarını içerir.

## Veri Klasörü

Sunucu verileri `KDM_DATA_DIR` altında tutulur:

- `config.json`: yayın sırası ve doğrulanmış süre
- `cookies/kick.com.json`: Kick oturum çerezleri
- `accounts.sqlite3`: kullanıcı hesapları ve yönetim bilgileri
- `users/<id>/`: üyelerin birbirinden ayrılmış sıra ve çerez verileri
- `chrome_data/`: yalnız çalışan tarayıcıların geçici profilleri ve tanılama dosyaları

`.env`, `data/`, çerezler ve geçici tarayıcı profilleri Git tarafından izlenmez.
