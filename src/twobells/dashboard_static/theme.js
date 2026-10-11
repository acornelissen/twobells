// Applies the saved theme, and kiosk mode ("/?kiosk", for a wall tablet),
// before the first paint. It is a classic script in <head> because module
// scripts run after parsing (and the CSP allows no inline script), so the
// page would otherwise flash the other theme or its controls. Kiosk mode
// takes the Board look unless this browser has a saved choice.
{
  const root = document.documentElement;
  const kiosk = new URLSearchParams(location.search).has("kiosk");
  if (kiosk) root.dataset.kiosk = "on";
  let theme = null;
  try {
    theme = localStorage.getItem("dashboard.theme");
  } catch { /* storage blocked: keep the default */ }
  if (theme === "board" || theme === "modern") root.dataset.theme = theme;
  else if (kiosk) root.dataset.theme = "board";
}
