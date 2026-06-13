const form = document.querySelector("#loginForm");
const username = document.querySelector("#username");
const email = document.querySelector("#email");
const password = document.querySelector("#password");
const emailField = document.querySelector("#emailField");
const errorBox = document.querySelector("#loginError");
const button = document.querySelector("#loginButton");
const buttonText = document.querySelector("#authButtonText");
const toggle = document.querySelector("#togglePassword");
let mode = "login";

document.querySelectorAll("[data-auth-mode]").forEach(tab => {
  tab.addEventListener("click", () => {
    mode = tab.dataset.authMode;
    document.querySelectorAll("[data-auth-mode]").forEach(item => {
      item.classList.toggle("active", item === tab);
    });
    const registering = mode === "register";
    if (registering && username.value.trim().toLowerCase() === "admin") {
      username.value = "";
    } else if (!registering && !username.value.trim()) {
      username.value = "admin";
    }
    emailField.classList.toggle("hidden", !registering);
    password.minLength = registering ? 8 : 1;
    document.querySelector("#authTitle").textContent = registering
      ? "Kendi alanını oluştur."
      : "Tekrar hoş geldin.";
    document.querySelector("#authDescription").textContent = registering
      ? "Drop sıran, Kick çerezin ve ilerlemen yalnız sana ait olur."
      : "Kendi Kick hesabınla güvenli madencilik alanına giriş yap.";
    buttonText.textContent = registering ? "Hesap Oluştur" : "Oturum Aç";
    errorBox.textContent = "";
  });
});

toggle.addEventListener("click", () => {
  const visible = password.type === "text";
  password.type = visible ? "password" : "text";
  toggle.classList.toggle("visible", !visible);
  password.focus();
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  errorBox.textContent = "";
  button.disabled = true;
  button.classList.add("loading");
  try {
    const payload = {
      username: username.value.trim(),
      password: password.value,
    };
    if (mode === "register") payload.email = email.value.trim();
    const response = await fetch(
      mode === "register" ? "/api/register" : "/api/login",
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "İşlem tamamlanamadı.");
    window.location.replace("/");
  } catch (error) {
    errorBox.textContent = error.message;
    password.select();
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
  }
});
