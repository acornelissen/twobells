// Applies the saved theme before the first paint. It is a classic script in
// <head> because module scripts run after parsing (and the CSP allows no
// inline script), so the page would otherwise flash the other theme.
try {
  const theme = localStorage.getItem("dashboard.theme");
  if (theme === "board" || theme === "modern") document.documentElement.dataset.theme = theme;
} catch { /* storage blocked: keep the default */ }
