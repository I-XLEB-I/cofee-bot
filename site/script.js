const currentYear = document.getElementById("current-year");

if (currentYear) {
  currentYear.textContent = new Date().getFullYear().toString();
}

const revealItems = document.querySelectorAll("[data-reveal]");

if ("IntersectionObserver" in window && revealItems.length > 0) {
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    },
    {
      threshold: 0.18,
      rootMargin: "0px 0px -40px 0px",
    },
  );

  revealItems.forEach((item) => observer.observe(item));
} else {
  revealItems.forEach((item) => item.classList.add("is-visible"));
}

const partnerForm = document.getElementById("partner-form");
const formStatus = document.getElementById("form-status");

if (partnerForm && formStatus) {
  partnerForm.addEventListener("submit", (event) => {
    event.preventDefault();

    const formData = new FormData(partnerForm);
    const name = (formData.get("name") || "").toString().trim();
    const company = (formData.get("company") || "").toString().trim();
    const contact = (formData.get("contact") || "").toString().trim();
    const message = (formData.get("message") || "").toString().trim();

    const email = "partners@example.com";
    const subject = encodeURIComponent("Заявка на размещение кофейной точки");
    const body = encodeURIComponent(
      [
        "Новая заявка с сайта",
        "",
        `Имя: ${name}`,
        `Компания / площадка: ${company}`,
        `Контакт: ${contact}`,
        `Комментарий: ${message || "Не указан"}`,
      ].join("\n"),
    );

    window.location.href = `mailto:${email}?subject=${subject}&body=${body}`;

    formStatus.textContent = email.includes("example.com")
      ? "Почтовый клиент открыт. Перед публикацией сайта замени адрес partners@example.com на рабочий."
      : "Почтовый клиент открыт. Проверьте письмо и отправьте заявку.";
  });
}
