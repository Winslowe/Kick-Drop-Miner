const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

let appState = null;
let csrf = "";
let polling = null;
let currentView = "dashboard";
let inventoryAnimation = null;
let confirmAction = null;
let selectedGame = null;
let adminLoaded = false;
const failedImageUrls = new Set();

const viewMeta = {
  dashboard: ["MADENCİ KONTROLÜ", "Yayın Merkezi", "Drop sürecini doğrulanmış verilerle takip et."],
  inventory: ["KICK DROP MERKEZİ", "Drop Envanteri", "Aktif kampanyaları ve hesap ilerlemesini görüntüle."],
  settings: ["GÜVENLİK VE BAĞLANTI", "Ayarlar", "Kick oturumunu ve sunucu sağlığını yönet."],
  admin: ["YÖNETİCİ MERKEZİ", "Admin Paneli", "Kullanıcıları ve çalışan madencileri yönet."],
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
}

function formatDuration(seconds) {
  seconds = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  if (hours) return `${hours}sa ${minutes}dk`;
  if (minutes) return `${minutes}dk ${secs}sn`;
  return `${secs}sn`;
}

function channelName(url) {
  try {
    return new URL(url).pathname.split("/").filter(Boolean)[0] || "Kick Kanalı";
  } catch {
    return "Kick Kanalı";
  }
}

function kickMediaUrl(value) {
  const source = String(value || "").trim();
  if (!source) return "";
  if (/^https?:\/\//i.test(source)) return source;
  if (/^drops\/reward-image\//i.test(source)) return `/${source.replace(/^\/+/, "")}`;
  return `https://files.kick.com/${source.replace(/^\/+/, "")}`;
}

function imageSource(primary, fallback = "") {
  const first = kickMediaUrl(primary);
  const second = kickMediaUrl(fallback);
  if (first && !failedImageUrls.has(first)) return first;
  if (second && !failedImageUrls.has(second)) return second;
  return "";
}

async function request(url, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrf;
  const response = await fetch(url, {...options, headers});
  if (response.status === 401) {
    window.location.replace("/");
    throw new Error("Oturum sona erdi.");
  }
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.detail || "İşlem tamamlanamadı.");
  return data;
}

function toast(title, message = "", type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.innerHTML = `<i></i><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div>`;
  $("#toastStack").append(node);
  setTimeout(() => node.remove(), 4200);
}

function showConfirm(title, text, action) {
  $("#confirmTitle").textContent = title;
  $("#confirmText").textContent = text;
  confirmAction = action;
  $("#confirmModal").classList.remove("hidden");
}

function setView(view) {
  currentView = view;
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  $$(".view").forEach(item => item.classList.toggle("active", item.id === `${view}View`));
  const meta = viewMeta[view];
  $("#pageEyebrow").textContent = meta[0];
  $("#pageTitle").textContent = meta[1];
  $("#pageDescription").textContent = meta[2];
  $(".sidebar").classList.remove("open");
  if (view === "admin" && appState?.user?.role === "admin") refreshAdmin();
}

function stateClass(state) {
  return String(state || "waiting").replace(/[^a-z_]/g, "");
}

function transitionForItem(item) {
  if (item.transition?.message) return item.transition;
  const statuses = Object.values(item.channel_statuses || {});
  const latest = statuses.sort(
    (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")),
  )[0];
  if (!latest) return null;
  const messages = {
    offline: "Kanal iki kez kontrol edildi ve çevrimdışı olduğu için sıradaki göreve geçildi.",
    wrong_category: "Kanal gerekli oyunu yayınlamadığı için sıradaki uygun kanala geçildi.",
    no_progress: "Video oynasa da Kick ilerlemesi değişmediği için alternatif kanal denendi.",
    verification_failed: "Canlı yayın ve video akışı doğrulanamadığı için görev geçici olarak atlandı.",
    browser_error: "Tarayıcı yeniden denemelerden sonra açılamadığı için sıradaki göreve geçildi.",
  };
  return {
    ...latest,
    message: messages[latest.state] || "Görev tamamlanamadığı için sıradaki yayına geçildi.",
  };
}

function mergeInventoryProgress(state) {
  const progressList = state.inventory?.progress || [];
  state.items = (state.items || []).map(item => {
    if (!item.campaign_id) return item;
    const inventoryProgress = progressForCampaign(
      {id: item.campaign_id},
      progressList,
    );
    const workerPercent = Number(item.progress_percent);
    const effectivePercent = Math.max(
      Number.isFinite(workerPercent) ? workerPercent : 0,
      Number(inventoryProgress.percent) || 0,
    );
    return {
      ...item,
      progress_percent: effectivePercent,
      drop_progress: effectivePercent > 0 ? effectivePercent : item.drop_progress,
      inventory_progress: inventoryProgress.percent,
      inventory_claimed: inventoryProgress.claimed,
    };
  });
  return state;
}

function renderState(state) {
  state = mergeInventoryProgress(state);
  appState = state;
  csrf = state.csrf || csrf;
  const active = state.items.find(item => item.active);
  $("#statTotal").textContent = state.stats.total;
  $("#statCompleted").textContent = state.stats.completed;
  $("#statBrowsers").textContent = state.browser_count;
  $("#statUptime").textContent = formatDuration(state.stats.uptime_seconds);
  $("#statTotalHint").textContent = `${state.stats.pending} görev sırada`;
  $("#statCompletedHint").textContent = state.stats.total
    ? `%${Math.round((state.stats.completed / state.stats.total) * 100)} tamamlandı`
    : "henüz görev yok";
  $("#statBrowserHint").textContent = state.browser_count
    ? "madenci tarayıcısı çalışıyor"
    : "boştayken kaynak kullanmaz";
  $("#statUptimeHint").textContent = state.queue_running
    ? "madencilik hizmeti çevrimiçi"
    : "sunucu oturumu";
  $$(".stat-card").forEach((card, index) => {
    card.classList.toggle("is-live", index === 2 && state.browser_count > 0);
    card.classList.toggle("has-value", Number(card.querySelector("strong")?.textContent || 0) > 0);
  });
  $("#browserCount").textContent = `${state.browser_count} aktif`;
  $("#inventoryCount").textContent = state.inventory.campaigns.length;
  const cookieReady = state.cookie.available && !state.cookie.expired;
  $("#cookieMini").textContent = cookieReady ? "Bağlı" : "Bağlı Değil";
  $("#cookieMini").className = `state-badge ${cookieReady ? "completed" : "error"}`;
  $("#cookieMini").style.cursor = "pointer";
  $("#currentUsername").textContent = state.user.username;
  $("#currentRole").textContent = state.user.role === "admin" ? "Yönetici" : "Üye";
  $("#adminNav").classList.toggle("hidden", state.user.role !== "admin");
  $("#startQueueButton").disabled = state.queue_running || !state.items.length;
  $("#startQueueButton").textContent = state.queue_running
    ? active && ["starting", "verifying"].includes(active.status)
      ? "Başlatılıyor..."
      : "Sıra Çalışıyor"
    : "Sırayı Başlat";
  $("#stopQueueButton").disabled = !state.queue_running;
  $("#clearQueueButton").disabled = !state.items.length;
  $("#refreshInventoryButton").disabled = state.queue_running || state.inventory.loading;

  renderHero(active, state.queue_running);
  renderQueue(state.items);
  renderInventory(state.inventory, state.items);
  renderCookie(state.cookie);
  renderLogs(state.logs);
  if (state.user.role === "admin" && !adminLoaded) {
    adminLoaded = true;
    refreshAdmin();
  }
}

function renderHero(active, running) {
  const hero = $("#activeHero");
  const badge = $("#heroState");
  const miningStatus = $("#heroMiningStatus");
  const heroProgress = $("#heroProgress");
  badge.className = "state-badge";
  hero.classList.remove("is-starting", "is-verifying", "is-running");
  if (!active) {
    badge.classList.add("waiting");
    badge.textContent = running ? "SIRADAKİ YAYIN BEKLENİYOR" : "BEKLİYOR";
    $("#heroTitle").textContent = running ? "Yeni kanal hazırlanıyor" : "Madenci çalışmaya hazır";
    $("#heroDescription").textContent = "Listeden bir yayın seç veya sırayı başlat. Süre yalnız canlı yayın ve ilerleyen video doğrulandığında artar.";
    $("#heroReward").classList.add("hidden");
    miningStatus.classList.add("hidden");
    heroProgress.classList.add("hidden");
    ["#verifyLive", "#verifyVideo", "#verifyDrop"].forEach(id => $(id).classList.remove("ok"));
    return;
  }
  const preparing = ["starting", "verifying"].includes(active.status);
  hero.classList.add(
    active.status === "starting"
      ? "is-starting"
      : active.status === "verifying"
        ? "is-verifying"
        : "is-running",
  );
  badge.classList.add(stateClass(active.status));
  badge.innerHTML = preparing
    ? `<i class="state-spinner"></i>${escapeHtml(active.status_label.toUpperCase())}`
    : escapeHtml(active.status_label.toUpperCase());
  $("#heroTitle").textContent = active.campaign_name || channelName(active.url);
  const progressText = active.campaign_id && active.drop_progress != null
    ? `Kick drop ilerlemesi %${Number(active.progress_percent).toFixed(1)} olarak doğrulanıyor.`
    : `${formatDuration(active.elapsed_seconds)} doğrulanmış izleme kaydedildi.`;
  const activeName = active.campaign_channels?.find(
    channel => channel.url === active.url,
  )?.username || channelName(active.url);
  $("#heroDescription").textContent = active.status === "starting"
    ? `${activeName} için güvenli tarayıcı hazırlanıyor. Kanal henüz izleniyor sayılmıyor.`
    : active.status === "verifying"
      ? `${activeName} kanalında canlılık ve video akışı doğrulanıyor. Sayaç doğrulamadan sonra başlayacak.`
      : `${activeName} yayını izleniyor. ${progressText}`;
  const mining = (
    active.campaign_id
    && !preparing
    && active.live === true
    && active.video_ok
    && active.video_advanced
  );
  const heroPercent = Math.min(100, Math.max(0, Number(active.progress_percent) || 0));
  miningStatus.classList.toggle("hidden", !mining);
  heroProgress.classList.toggle("hidden", !mining);
  if (mining) {
    $("#heroMiningDetail").textContent = `${activeName} izleniyor • ${formatDuration(active.elapsed_seconds)} doğrulanmış süre`;
    $("#heroMiningPercent").textContent = `%${heroPercent.toFixed(1)}`;
    $("#heroProgressFill").style.width = `${heroPercent}%`;
    $("#heroProgressFill").classList.toggle("is-mining-progress", heroPercent > 0);
  }
  const reward = $("#heroReward");
  const rewardImage = imageSource(active.reward_image);
  reward.classList.remove("hidden", "image-failed");
  reward.innerHTML = rewardImage
    ? `<img src="${escapeHtml(rewardImage)}" data-kdm-image data-fallback="" alt=""><span>${escapeHtml(active.reward_name || "DROP")}</span>`
    : `<span>${escapeHtml(String(active.reward_name || "DROP").slice(0, 5).toUpperCase())}</span>`;
  $("#verifyLive").classList.toggle("ok", !preparing && active.live === true);
  $("#verifyVideo").classList.toggle("ok", !preparing && active.video_ok && active.video_advanced);
  $("#verifyDrop").classList.toggle("ok", !preparing && active.drop_verified);
}

function renderQueue(items) {
  const container = $("#queueList");
  if (!items.length) {
    container.innerHTML = `<div class="empty-state"><div><strong>Yayın listesi boş</strong><span>İlk Kick kanalını ekleyerek madencilik planını oluştur.</span></div></div>`;
    return;
  }
  container.innerHTML = items.map(item => {
    const name = item.campaign_name || channelName(item.url);
    const target = item.minutes ? `${item.minutes} dk hedef` : "Kampanya tamamlanana kadar";
    const action = item.active
      ? `<button class="button button-danger compact-button" data-stop>Durdur</button>`
      : `<button class="button button-secondary compact-button" data-start="${escapeHtml(item.id)}" ${appState.queue_running || item.finished ? "disabled" : ""}>Başlat</button>`;
    const rewardImage = imageSource(item.reward_image);
    const preparing = item.active && ["starting", "verifying"].includes(item.status);
    const transition = transitionForItem(item);
    const percent = Math.min(100, Math.max(0, Number(item.progress_percent) || 0));
    const mining = (
      item.active
      && !preparing
      && item.live === true
      && item.video_ok
      && item.video_advanced
    );
    const progressLabel = item.finished || item.inventory_claimed
      ? "Drop tamamlandı"
      : mining
        ? item.drop_verified ? "Drop kasılıyor" : "İlerleme doğrulaması bekleniyor"
        : percent > 0
          ? `Kaldığı yerden devam edecek`
          : "Henüz ilerleme yok";
    const channels = item.campaign_channels || [];
    const channelNodes = channels.map(channel => {
      const url = channel.url || `https://kick.com/${channel.slug || ""}`;
      const status = item.channel_statuses?.[url]?.state;
      const active = url === item.url;
      const avatar = imageSource(channel.profile_picture);
      const label = active && item.active && item.live === true
        ? "Yayında"
        : status ? item.channel_statuses[url].label : active ? "Sıradaki" : "Hazır";
      return `<a class="plan-channel ${active ? "selected" : ""} ${status || ""}" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(channel.username || channel.slug || "Kick kanalını")} Kick'te aç">
        <span class="plan-channel-avatar">
          ${avatar ? `<img src="${escapeHtml(avatar)}" data-kdm-image data-fallback="" alt="">` : ""}
          <b>${escapeHtml(String(channel.username || "?").slice(0, 1).toUpperCase())}</b>
          ${active && item.active && item.live === true ? `<i class="live-pulse"></i>` : ""}
        </span>
        <span><strong>${escapeHtml(channel.username || channel.slug || "Kick kanalı")}</strong><small>${escapeHtml(label)}</small></span>
      </a>`;
    }).join("");
    return `
      <article class="queue-card ${item.active ? "active" : ""} ${preparing ? "preparing" : ""}" data-item="${escapeHtml(item.id)}">
        <div class="plan-primary">
          <span class="drop-art ${rewardImage ? "" : "image-failed"}">
            ${rewardImage ? `<img src="${escapeHtml(rewardImage)}" data-kdm-image data-fallback="" alt="">` : ""}
            <b>${escapeHtml(String(item.reward_name || name).slice(0, 3).toUpperCase())}</b>
          </span>
          <div class="plan-title">
            <span class="plan-game">${escapeHtml(item.game || "Özel yayın")}</span>
            <strong>${escapeHtml(name)}</strong>
            <small>${escapeHtml(item.reward_name || target)}</small>
          </div>
        </div>
        <div class="plan-channels">
          ${channelNodes || `<div class="plan-channel selected"><span class="plan-channel-avatar"><b>${escapeHtml(channelName(item.url).slice(0, 1))}</b></span><span><strong>${escapeHtml(channelName(item.url))}</strong><small>${item.live === true ? "Yayında" : "Kanal"}</small></span></div>`}
        </div>
        <div class="queue-progress plan-progress">
          <div class="progress-meta">
            <span class="state-badge ${stateClass(item.status)}">${escapeHtml(item.status_label.toUpperCase())}</span>
            <span>${formatDuration(item.elapsed_seconds)} • ${target}</span>
          </div>
          <div class="plan-progress-status ${mining ? "mining" : ""}">
            <span><i></i>${escapeHtml(progressLabel)}</span>
            <strong>%${percent.toFixed(1)}</strong>
          </div>
          <div class="progress-track"><span class="${mining && percent > 0 ? "is-mining-progress" : ""}" style="width:${percent}%"></span></div>
          <div class="plan-verification">
            <span class="${!preparing && item.live === true ? "ok" : ""}"><i></i> Canlı</span>
            <span class="${!preparing && item.video_ok && item.video_advanced ? "ok" : ""}"><i></i> Video</span>
            <span class="${!preparing && item.drop_verified ? "ok" : ""}"><i></i> Drop</span>
          </div>
          ${preparing ? `<div class="queue-status-note preparing-note"><i></i><span><strong>${item.status === "starting" ? "Yayın hazırlanıyor" : "Yayın doğrulanıyor"}</strong><small>${item.status === "starting" ? "Tarayıcı ve güvenli oturum açılıyor." : "Canlılık ile video akışı kontrol ediliyor."}</small></span></div>` : ""}
          ${!item.active && transition ? `<div class="queue-status-note skipped-note"><i></i><span><strong>Neden geçildi?</strong><small>${escapeHtml(transition.message)} Sıra yeniden başlatıldığında tekrar kontrol edilir.</small></span></div>` : ""}
        </div>
        <div class="queue-actions">
          ${action}
          <button class="remove-button" data-remove="${escapeHtml(item.id)}" ${item.active ? "disabled" : ""} aria-label="Yayını kaldır"><svg class="button-icon" viewBox="0 0 24 24"><use href="/static/icons.svg?v=20260612-2#trash"></use></svg></button>
        </div>
      </article>`;
  }).join("");
}

function progressForCampaign(campaign, progressList) {
  const record = progressList.find(item => String(item.id || item.campaign_id) === String(campaign.id));
  if (!record) return {percent: 0, claimed: false};
  const rewards = Array.isArray(record.rewards) ? record.rewards : [];
  if (rewards.length) {
    const values = rewards.map(item => {
      let value = Number(item.progress || 0);
      return value <= 1 && value > 0 ? value * 100 : value;
    });
    return {
      percent: values.reduce((a,b) => a + b, 0) / values.length,
      claimed: rewards.every(item => Boolean(item.claimed)),
    };
  }
  let percent = Number(record.percentage || 0);
  if (percent <= 1 && percent > 0) percent *= 100;
  return {percent, claimed: Boolean(record.is_claimed || record.is_fully_watched)};
}

function renderInventory(inventory, queueItems) {
  if (inventory.loading) startInventoryAnimation();
  else stopInventoryAnimation();
  const gameGrid = $("#gameGrid");
  const grid = $("#campaignGrid");
  if (!inventory.campaigns.length) {
    gameGrid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div><strong>Envanter henüz yüklenmedi</strong><span>Güncel Kick kampanyalarını görmek için Envanteri Yenile düğmesini kullan.</span></div></div>`;
    grid.innerHTML = "";
    $("#inventorySubtitle").textContent = inventory.error || "Kampanyaları yenileyerek güncel ödülleri ve ilerlemeyi getir.";
    return;
  }
  $("#inventorySubtitle").textContent = `${inventory.campaigns.length} kampanya • Son doğrulama ${inventory.updated_at ? new Date(inventory.updated_at).toLocaleString("tr-TR") : "-"}`;
  const groups = new Map();
  inventory.campaigns.forEach(campaign => {
    const game = campaign.game || "Genel Kampanyalar";
    if (!groups.has(game)) groups.set(game, []);
    groups.get(game).push(campaign);
  });
  if (selectedGame && !groups.has(selectedGame)) selectedGame = null;
  $("#backToGamesButton").classList.toggle("hidden", !selectedGame);
  gameGrid.classList.toggle("hidden", Boolean(selectedGame));
  grid.classList.toggle("hidden", !selectedGame);
  gameGrid.innerHTML = [...groups.entries()].map(([game, campaigns]) => {
    const rewards = campaigns.flatMap(campaign =>
      (campaign.rewards || []).map(reward =>
        imageSource(reward.image_url || reward.image || reward.icon_url),
      ),
    ).filter(Boolean).slice(0, 4);
    const banner = rewards[0] || imageSource(campaigns.find(item => item.game_image)?.game_image);
    const activeCount = campaigns.filter(item => item.status === "active").length;
    return `<button class="game-card" data-game="${escapeHtml(game)}">
      <span class="game-card-cover">
        ${banner ? `<span class="cover-art"><img src="${escapeHtml(banner)}" data-kdm-image data-fallback="" alt=""></span>` : ""}
        <span class="game-reward-stack">${rewards.map(image => `<i><img src="${escapeHtml(image)}" data-kdm-image data-fallback="" alt=""></i>`).join("")}</span>
      </span>
      <span class="game-card-copy">
        <span><small>OYUN KOLEKSİYONU</small><strong>${escapeHtml(game)}</strong><em>${campaigns.length} kampanya • ${activeCount} aktif</em></span>
        <svg class="ui-icon" viewBox="0 0 24 24"><use href="/static/icons.svg?v=20260612-2#chevron-right"></use></svg>
      </span>
    </button>`;
  }).join("");

  grid.innerHTML = (selectedGame ? groups.get(selectedGame) || [] : []).map(campaign => {
    const progress = progressForCampaign(campaign, inventory.progress);
    const channels = campaign.channels || [];
    const queueCount = queueItems.filter(item => String(item.campaign_id) === String(campaign.id)).length;
    const allAdded = queueCount > 0;
    const firstChannelImage = channels.find(channel => channel.profile_picture)?.profile_picture || "";
    const progressRecord = (inventory.progress || []).find(
      item => String(item.id || item.campaign_id) === String(campaign.id),
    );
    const rewardRecords = (campaign.rewards || []).filter(
      item => item && typeof item === "object",
    );
    const progressRewards = progressRecord?.rewards || [];
    const focusReward = rewardRecords.find((reward, index) => {
      const rewardId = reward.id || reward.reward_id;
      const rewardProgress = progressRewards.find(
        item => rewardId && String(item.id || item.reward_id) === String(rewardId),
      ) || progressRewards[index];
      return !rewardProgress?.claimed;
    }) || rewardRecords.at(-1) || {};
    const focusImage = imageSource(
      focusReward.image_url || focusReward.image || focusReward.icon_url,
      campaign.game_image || firstChannelImage,
    );
    const coverImage = focusImage || imageSource(campaign.game_image, firstChannelImage);
    const coverFallback = imageSource(campaign.game_image || firstChannelImage);
    const channelAvatars = channels.slice(0, 5).map(channel => {
      const avatar = imageSource(channel.profile_picture);
      const initial = String(channel.username || channel.slug || "?").slice(0, 1).toUpperCase();
      return `<span class="campaign-channel-avatar" title="${escapeHtml(channel.username || channel.slug || "Kick kanalı")}">
        ${avatar ? `<img src="${escapeHtml(avatar)}" data-kdm-image data-fallback="" alt="">` : ""}
        <b>${escapeHtml(initial)}</b>
      </span>`;
    }).join("");
    const moreChannels = channels.length > 5
      ? `<span class="campaign-channel-more">+${channels.length - 5}</span>`
      : "";
    const rewards = rewardRecords.slice(0,5).map(reward => {
      const image = imageSource(
        reward.image_url || reward.image || reward.icon_url,
        campaign.game_image || firstChannelImage,
      );
      const fallback = imageSource(campaign.game_image || firstChannelImage);
      return `<span class="reward-chip" title="${escapeHtml(reward.name || "Ödül")}">
        ${image ? `<img src="${escapeHtml(image)}" data-kdm-image data-fallback="${escapeHtml(fallback)}" alt="">` : ""}
        <span class="reward-fallback">${escapeHtml(String(reward.name || "DROP").slice(0, 3).toUpperCase())}</span>
      </span>`;
    }).join("");
    return `
      <article class="campaign-card">
        <div class="campaign-cover ${coverImage ? "" : "no-image"}">
          ${coverImage ? `<span class="cover-art"><img src="${escapeHtml(coverImage)}" data-kdm-image data-fallback="${escapeHtml(coverFallback)}" alt=""></span>` : ""}
          <span class="campaign-cover-mark">${escapeHtml(String(campaign.game || campaign.name || "K").slice(0, 2).toUpperCase())}</span>
          <span class="campaign-game">${escapeHtml(campaign.game || "Genel Kampanya")}</span>
        </div>
        <div class="campaign-body">
          <div class="campaign-head">
            <div><h3>${escapeHtml(campaign.name)}</h3><p>${channels.length ? `${channels.length} uygun kanal` : "Canlı yayıncı otomatik bulunacak"}</p></div>
            <span class="state-badge ${progress.claimed ? "completed" : campaign.status === "active" ? "watch_verified" : "waiting"}">${progress.claimed ? "TAMAMLANDI" : campaign.status === "active" ? "AKTİF" : "BEKLİYOR"}</span>
          </div>
          <div class="campaign-focus">
            <span class="campaign-focus-art">
              ${focusImage ? `<img src="${escapeHtml(focusImage)}" data-kdm-image data-fallback="${escapeHtml(coverImage)}" alt="">` : ""}
              <b>${escapeHtml(String(focusReward.name || "DROP").slice(0, 4).toUpperCase())}</b>
            </span>
            <span><small>SIRADAKİ ÖDÜL</small><strong>${escapeHtml(focusReward.name || campaign.name)}</strong></span>
          </div>
          ${channels.length ? `<div class="campaign-channels">${channelAvatars}${moreChannels}</div>` : ""}
          <div class="campaign-rewards">${rewards || `<span class="reward-chip"><span class="reward-fallback">DROP</span></span>`}</div>
          <div class="campaign-progress">
            <div class="progress-meta"><span>Kick ilerlemesi</span><strong>%${progress.percent.toFixed(1)}</strong></div>
            <div class="progress-track"><span style="width:${Math.min(100,progress.percent)}%"></span></div>
          </div>
          <div class="campaign-footer">
            <span>${queueCount ? "Drop madencilik planında" : "Henüz plana eklenmedi"}</span>
            <button class="button ${allAdded ? "button-ghost" : "button-secondary"} compact-button" data-campaign="${escapeHtml(campaign.id)}" ${allAdded || progress.claimed ? "disabled" : ""}>
              ${progress.claimed ? "Tamamlandı" : allAdded ? "Plana Eklendi" : "Dropu Plana Ekle"}
            </button>
          </div>
        </div>
      </article>`;
  }).join("");
}

function renderCookie(cookie) {
  const badge = $("#cookieBadge");
  badge.className = `state-badge ${cookie.available && !cookie.expired ? "completed" : "error"}`;
  badge.textContent = cookie.available && !cookie.expired ? `${cookie.count} ÇEREZ HAZIR` : cookie.expired ? "SÜRESİ DOLMUŞ" : "ÇEREZ GEREKLİ";
}

function renderLogs(logs) {
  const levelNames = {info: "BİLGİ", success: "BAŞARILI", warning: "UYARI", error: "HATA"};
  const rows = logs.length ? logs.map(log => `
    <div class="log-row">
      <span class="log-time">${escapeHtml(log.time)}</span>
      <span class="log-level ${escapeHtml(log.level)}">${escapeHtml(levelNames[log.level] || log.level)}</span>
      <span class="log-message">${escapeHtml(log.message)}</span>
    </div>`).join("") : `<div class="console-empty">Henüz olay kaydı yok. Madenci işlemleri burada görünecek.</div>`;
  $("#consoleLogList").innerHTML = rows;
  const latest = logs[0];
  $("#lastLogMini").textContent = latest
    ? String(latest.message).slice(0, 24)
    : "Hazır";
}

function formatDate(value) {
  if (!value) return "Henüz giriş yok";
  return new Date(value).toLocaleString("tr-TR");
}

async function refreshAdmin() {
  try {
    const data = await request("/api/admin/users");
    $("#adminUserCount").textContent = data.users.length;
    const activeMiners = data.users.filter(user => user.runtime.queue_running).length;
    const readyCookies = data.users.filter(user => user.runtime.cookie_ready).length;
    $("#adminStats").innerHTML = `
      <article><small>Toplam kullanıcı</small><strong>${data.users.length}</strong><span>Kayıtlı hesap</span></article>
      <article><small>Aktif hesap</small><strong>${data.users.filter(user => user.active).length}</strong><span>Giriş yapabilir</span></article>
      <article><small>Çalışan madenci</small><strong>${activeMiners}/${data.max_active_miners}</strong><span>Kaynak sınırı</span></article>
      <article><small>Kick bağlantısı</small><strong>${readyCookies}</strong><span>Hazır hesap</span></article>`;
    $("#adminUserList").innerHTML = data.users.map(user => {
      const initials = user.username.slice(0, 2).toUpperCase();
      return `<article class="admin-user-row ${user.active ? "" : "disabled"}">
        <div class="admin-user-cell">
          <span class="admin-avatar">${escapeHtml(initials)}</span>
          <span><strong>${escapeHtml(user.username)}</strong><small>${escapeHtml(user.email || "E-posta belirtilmedi")}</small><em>${user.role === "admin" ? "Yönetici" : "Üye"} • ${user.login_count} giriş</em></span>
        </div>
        <div><strong>${escapeHtml(formatDate(user.last_seen_at || user.last_login_at))}</strong><small>${escapeHtml(user.last_ip || "IP kaydı yok")}</small></div>
        <div><span class="state-badge ${user.runtime.queue_running ? "drop_verified" : "waiting"}">${user.runtime.queue_running ? "ÇALIŞIYOR" : "BOŞTA"}</span><small>${user.runtime.active_status || `${user.runtime.queue_count} görev`}</small></div>
        <div><span class="state-badge ${user.runtime.cookie_ready ? "completed" : "error"}">${user.runtime.cookie_ready ? "HAZIR" : "GEREKLİ"}</span><small>${user.runtime.browser_count} tarayıcı</small></div>
        <div class="admin-actions">
          <button class="button button-ghost compact-button" data-admin-stop="${escapeHtml(user.id)}" ${user.runtime.queue_running ? "" : "disabled"}>Durdur</button>
          <button class="button button-ghost compact-button" data-admin-sessions="${escapeHtml(user.id)}" ${user.role === "admin" ? "disabled" : ""}>Çıkış yaptır</button>
          <button class="button ${user.active ? "button-danger" : "button-secondary"} compact-button" data-admin-active="${escapeHtml(user.id)}" data-next-active="${user.active ? "0" : "1"}" ${user.role === "admin" ? "disabled" : ""}>${user.active ? "Devre dışı" : "Etkinleştir"}</button>
        </div>
      </article>`;
    }).join("");
  } catch (error) {
    adminLoaded = false;
    toast("Admin verileri alınamadı", error.message, "error");
  }
}

function startInventoryAnimation() {
  $("#inventoryLoading").classList.remove("hidden");
  $("#campaignGrid").classList.add("hidden");
  $("#gameGrid").classList.add("hidden");
  if (inventoryAnimation) return;
  let value = 5;
  inventoryAnimation = setInterval(() => {
    if (value < 90) value += value < 55 ? 3 : 1;
    $("#inventoryLoadingBar").style.width = `${value}%`;
    $("#inventoryLoadingPercent").textContent = `%${value}`;
    $("#inventoryLoadingText").textContent = value > 65 ? "Kampanyalar ve ilerlemeler eşleştiriliyor..." : value > 30 ? "Aktif kampanyalar alınıyor..." : "Güvenli oturum kontrol ediliyor...";
  }, 160);
}

function stopInventoryAnimation() {
  if (inventoryAnimation) clearInterval(inventoryAnimation);
  inventoryAnimation = null;
  $("#inventoryLoadingBar").style.width = "100%";
  $("#inventoryLoadingPercent").textContent = "%100";
  setTimeout(() => {
    $("#inventoryLoading").classList.add("hidden");
    $("#campaignGrid").classList.toggle("hidden", !selectedGame);
    $("#gameGrid").classList.toggle("hidden", Boolean(selectedGame));
    $("#inventoryLoadingBar").style.width = "5%";
  }, 250);
}

async function refreshState(showConnectionError = false) {
  try {
    const state = await request("/api/state");
    renderState(state);
    $("#connectionPill").innerHTML = `<i class="status-dot online"></i> Canlı bağlantı`;
    $("#serviceStatus").textContent = "Çevrimiçi";
    $("#serviceDot").classList.add("online");
    return true;
  } catch (error) {
    $("#connectionPill").innerHTML = `<i class="status-dot"></i> Bağlantı kesildi`;
    $("#serviceStatus").textContent = "Bağlantı yok";
    $("#serviceDot").classList.remove("online");
    if (showConnectionError) toast("Sunucuya ulaşılamadı", error.message, "error");
    return false;
  }
}

async function boot() {
  $("#bootOverlay").classList.remove("done");
  $("#bootRetryButton").classList.add("hidden");
  let progress = 8;
  const timer = setInterval(() => {
    if (progress < 86) progress += progress < 55 ? 6 : 2;
    $("#bootBar").style.width = `${progress}%`;
    $("#bootPercent").textContent = `%${progress}`;
    if (progress > 62) $("#bootText").textContent = "Yayın sırası ve envanter hazırlanıyor...";
  }, 90);
  try {
    let ready = false;
    for (let attempt = 1; attempt <= 3 && !ready; attempt += 1) {
      $("#bootText").textContent = attempt === 1
        ? "Güvenli oturum doğrulanıyor..."
        : `Sunucu bağlantısı yeniden deneniyor (${attempt}/3)...`;
      ready = await refreshState(attempt === 3);
      if (!ready && attempt < 3) await new Promise(resolve => setTimeout(resolve, 900));
    }
    if (!ready) {
      $("#bootBar").style.width = "100%";
      $("#bootBar").classList.add("error");
      $("#bootPercent").textContent = "Bağlantı kurulamadı";
      $("#bootText").textContent = "Sunucu geçici olarak yanıt vermiyor. Bağlantını kontrol edip yeniden dene.";
      $("#bootRetryButton").classList.remove("hidden");
      return;
    }
    $("#bootBar").classList.remove("error");
    progress = 100;
    $("#bootBar").style.width = "100%";
    $("#bootPercent").textContent = "%100";
    $("#bootText").textContent = "Kontrol merkezi hazır";
    setTimeout(() => $("#bootOverlay").classList.add("done"), 280);
    if (polling) clearInterval(polling);
    polling = setInterval(refreshState, 2000);
  } finally {
    clearInterval(timer);
  }
}

$$(".nav-item").forEach(item => item.addEventListener("click", () => setView(item.dataset.view)));
$("#bootRetryButton").addEventListener("click", boot);
$("#mobileMenuButton").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
$("#openAddModal").addEventListener("click", () => $("#addModal").classList.remove("hidden"));
$("#closeAddModal").addEventListener("click", () => $("#addModal").classList.add("hidden"));
$("#confirmCancel").addEventListener("click", () => $("#confirmModal").classList.add("hidden"));
$("#confirmAccept").addEventListener("click", async () => {
  $("#confirmModal").classList.add("hidden");
  if (confirmAction) await confirmAction();
  confirmAction = null;
});

$("#addStreamForm").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await request("/api/queue", {
      method: "POST",
      body: JSON.stringify({url: $("#streamUrl").value, minutes: Number($("#streamMinutes").value)}),
    });
    $("#addModal").classList.add("hidden");
    event.target.reset();
    $("#streamMinutes").value = 120;
    toast("Yayın eklendi", "Kanal madencilik sırasına alındı.");
    await refreshState();
  } catch (error) { toast("Yayın eklenemedi", error.message, "error"); }
});

$("#startQueueButton").addEventListener("click", async () => {
  try {
    await request("/api/miner/start", {method: "POST", body: JSON.stringify({item_id: null})});
    toast("Madenci başlatıldı", "İlk kanal doğrulanıyor.");
    await refreshState();
  } catch (error) { toast("Başlatılamadı", error.message, "error"); }
});

$("#stopQueueButton").addEventListener("click", () => showConfirm(
  "Madenci durdurulsun mu?",
  "Aktif tarayıcı güvenli biçimde kapatılacak ve doğrulanmış süre korunacak.",
  async () => {
    try {
      await request("/api/miner/stop", {method: "POST"});
      toast("Madenci durduruldu", "Tarayıcı kaynakları serbest bırakıldı.");
      await refreshState();
    } catch (error) { toast("Durdurulamadı", error.message, "error"); }
  },
));

$("#clearQueueButton").addEventListener("click", () => showConfirm(
  "Yayın listesi sıfırlansın mı?",
  "Listedeki tüm kanallar ve yerel süre bilgileri kaldırılacak.",
  async () => {
    try {
      await request("/api/queue/clear", {method: "POST"});
      toast("Liste sıfırlandı");
      await refreshState();
    } catch (error) { toast("Liste temizlenemedi", error.message, "error"); }
  },
));

$("#queueList").addEventListener("click", async event => {
  const start = event.target.closest("[data-start]");
  const remove = event.target.closest("[data-remove]");
  const stop = event.target.closest("[data-stop]");
  try {
    if (start) {
      await request("/api/miner/start", {method: "POST", body: JSON.stringify({item_id: start.dataset.start})});
      toast("Yayın başlatıldı", "Canlılık ve video akışı doğrulanıyor.");
    } else if (remove) {
      await request(`/api/queue/${remove.dataset.remove}`, {method: "DELETE"});
      toast("Yayın kaldırıldı");
    } else if (stop) {
      await request("/api/miner/stop", {method: "POST"});
      toast("Madenci durduruldu");
    }
    await refreshState();
  } catch (error) { toast("İşlem tamamlanamadı", error.message, "error"); }
});

$("#refreshInventoryButton").addEventListener("click", async () => {
  startInventoryAnimation();
  try {
    await request("/api/inventory/refresh", {method: "POST"});
    toast("Envanter güncellendi", "Kick kampanyaları ve hesap ilerlemesi doğrulandı.");
  } catch (error) {
    toast("Envanter alınamadı", error.message, "error");
  } finally {
    await refreshState();
    stopInventoryAnimation();
  }
});

$("#gameGrid").addEventListener("click", event => {
  const card = event.target.closest("[data-game]");
  if (!card) return;
  selectedGame = card.dataset.game;
  renderInventory(appState.inventory, appState.items);
  $("#inventorySubtitle").textContent = `${selectedGame} drop kampanyaları`;
});

$("#backToGamesButton").addEventListener("click", () => {
  selectedGame = null;
  renderInventory(appState.inventory, appState.items);
});

$("#campaignGrid").addEventListener("click", async event => {
  const button = event.target.closest("[data-campaign]");
  if (!button) return;
  button.disabled = true;
  try {
    const result = await request(`/api/inventory/${button.dataset.campaign}/add`, {method: "POST"});
    toast(
      result.added ? "Drop plana eklendi" : "Drop zaten planda",
      result.added
        ? "Uygun yayıncılar tek görev altında alternatifli olarak kaydedildi."
        : "Bu kampanya için ikinci bir görev oluşturulmadı.",
    );
    await refreshState();
  } catch (error) {
    toast("Kampanya eklenemedi", error.message, "error");
    button.disabled = false;
  }
});

/* Connect Modal Logic */
function openConnectModal() {
  $("#connectModal").classList.remove("hidden");
}
$("#openConnectModalButton")?.addEventListener("click", openConnectModal);
$("#openConnectModalFromSettings")?.addEventListener("click", openConnectModal);
$("#closeConnectModal")?.addEventListener("click", () => $("#connectModal").classList.add("hidden"));

/* Tabs Switching */
$$("#connectModal .tab-button").forEach(btn => {
  btn.addEventListener("click", () => {
    $$("#connectModal .tab-button").forEach(b => b.classList.remove("active"));
    $$("#connectModal .tab-content").forEach(c => c.classList.remove("active", "hidden"));
    $$("#connectModal .tab-content").forEach(c => c.classList.add("hidden"));
    
    btn.classList.add("active");
    const target = $(`#${btn.dataset.tab}`);
    target.classList.remove("hidden");
    target.classList.add("active");
  });
});

/* Manual Cookie Submit */
$("#manualCookieForm")?.addEventListener("submit", async e => {
  e.preventDefault();
  const btn = $("#saveCookiesModalButton");
  btn.disabled = true;
  try {
    const raw = $("#cookieJsonModal").value.trim();
    if (!raw) throw new Error("Lütfen session_token girin.");
    let cookies;
    try {
      const parsed = JSON.parse(raw);
      cookies = Array.isArray(parsed) ? parsed : parsed.cookies;
    } catch {
      cookies = [{ name: "session_token", value: raw, domain: ".kick.com", path: "/", secure: true, httpOnly: true }];
    }
    await request("/api/cookies", {method: "POST", body: JSON.stringify({cookies})});
    $("#cookieJsonModal").value = "";
    $("#connectModal").classList.add("hidden");
    toast("Kick oturumu bağlandı", "Hesabınız başarıyla entegre edildi.");
    await refreshState();
  } catch (error) { toast("Bağlantı başarısız", error.message, "error"); }
  btn.disabled = false;
});

/* Direct Login Submit */
$("#directLoginForm")?.addEventListener("submit", async e => {
  e.preventDefault();
  const btn = $("#directLoginButton");
  btn.disabled = true;
  btn.textContent = "Giriş yapılıyor, lütfen bekleyin...";
  try {
    const username = $("#kickUsername").value.trim();
    const password = $("#kickPassword").value.trim();
    if (!username || !password) throw new Error("Kullanıcı adı ve şifre zorunludur.");
    
    const res = await request("/api/kick-login", {
      method: "POST", 
      body: JSON.stringify({ username, password })
    });
    
    if (res.success) {
      $("#kickUsername").value = "";
      $("#kickPassword").value = "";
      $("#connectModal").classList.add("hidden");
      toast("Giriş Başarılı", "Kick hesabınız başarıyla bağlandı.");
      await refreshState();
    } else {
      throw new Error(res.error || "Giriş yapılamadı.");
    }
  } catch (error) { 
    toast("Giriş Başarısız", error.message, "error"); 
  }
  btn.disabled = false;
  btn.textContent = "Otomatik Giriş Yap";
});

document.addEventListener("error", event => {
  const image = event.target;
  if (!(image instanceof HTMLImageElement) || !image.matches("[data-kdm-image]")) return;
  failedImageUrls.add(image.currentSrc || image.src);
  const fallback = image.dataset.fallback || "";
  if (fallback && image.src !== fallback && !failedImageUrls.has(fallback)) {
    image.src = fallback;
    image.dataset.fallback = "";
    return;
  }
  image.closest(".reward-chip, .campaign-channel-avatar, .campaign-cover, .campaign-focus-art, .drop-art, .hero-reward, .game-card-cover, .plan-channel-avatar")?.classList.add("image-failed");
  image.remove();
}, true);

$("#openConsoleButton").addEventListener("click", () => {
  $("#consoleModal").classList.remove("hidden");
});
$("#closeConsoleButton").addEventListener("click", () => {
  $("#consoleModal").classList.add("hidden");
});

$("#refreshAdminButton").addEventListener("click", refreshAdmin);
$("#adminUserList").addEventListener("click", async event => {
  const stop = event.target.closest("[data-admin-stop]");
  const sessions = event.target.closest("[data-admin-sessions]");
  const active = event.target.closest("[data-admin-active]");
  try {
    if (stop) {
      await request(`/api/admin/users/${stop.dataset.adminStop}/stop`, {method: "POST"});
      toast("Madenci durduruldu", "Kullanıcının tarayıcı kaynakları kapatılıyor.");
    } else if (sessions) {
      await request(`/api/admin/users/${sessions.dataset.adminSessions}/sessions/reset`, {method: "POST"});
      toast("Oturumlar kapatıldı");
    } else if (active) {
      await request(`/api/admin/users/${active.dataset.adminActive}/active`, {
        method: "POST",
        body: JSON.stringify({active: active.dataset.nextActive === "1"}),
      });
      toast("Hesap durumu güncellendi");
    } else {
      return;
    }
    await refreshAdmin();
  } catch (error) {
    toast("Yönetici işlemi başarısız", error.message, "error");
  }
});

$("#downloadLogsButton").addEventListener("click", () => {
  const logs = appState?.logs || [];
  const text = logs.slice().reverse()
    .map(log => `[${log.time}] ${String(log.level).toUpperCase()} ${log.message}`)
    .join("\n");
  const url = URL.createObjectURL(new Blob(
    [text || "Henüz olay kaydı yok."],
    {type: "text/plain;charset=utf-8"},
  ));
  const link = document.createElement("a");
  link.href = url;
  link.download = `kick-drop-miner-${new Date().toISOString().slice(0, 10)}.log`;
  link.click();
  URL.revokeObjectURL(url);
});

$("#clearLogsButton").addEventListener("click", async () => {
  try {
    await request("/api/logs/clear", {method: "POST"});
    await refreshState();
    toast("Konsol temizlendi", "Yeni olaylar kaydedilmeye devam edecek.");
  } catch (error) {
    toast("Konsol temizlenemedi", error.message, "error");
  }
});

$("#logoutButton").addEventListener("click", async () => {
  try { await request("/api/logout", {method: "POST"}); } finally { window.location.replace("/"); }
});

boot();
