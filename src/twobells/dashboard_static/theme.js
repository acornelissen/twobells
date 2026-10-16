// Applies the saved look, and kiosk mode ("/?kiosk", for a wall tablet),
// before the first paint. It is a classic script in <head> because module
// scripts run after parsing (and the CSP allows no inline script), so the
// page would otherwise flash the other look or its controls. Kiosk mode
// takes the Dot matrix look ("board") unless this browser has a saved choice.
{
  // The looks: the values of the Look picker's options in index.html, each
  // with its palette in app.css. "modern" is the Standard look.
  const LOOKS = ["modern", "board", "tft", "flap"];
  const root = document.documentElement;
  const kiosk = new URLSearchParams(location.search).has("kiosk");
  if (kiosk) root.dataset.kiosk = "on";
  let theme = null;
  try {
    theme = localStorage.getItem("dashboard.theme");
  } catch { /* storage blocked: keep the default */ }
  if (LOOKS.includes(theme)) root.dataset.theme = theme;
  else if (kiosk) root.dataset.theme = "board";
}
