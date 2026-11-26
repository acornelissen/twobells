// The footer's link to the settings (index.html, performance.html). They are
// on a port of their own, another origin, which the server writes into the
// link's data-port as it starts; this points the link there, at the name
// this page was opened at. Without a port, or with anything but one, the
// link stays /settings, which on this port is a page saying where they are.

const PORT = /^[0-9]{1,5}$/;

export function pointSettingsLink() {
  const link = document.getElementById("settings-link");
  const port = link?.dataset.port ?? "";
  if (!PORT.test(port) || Number(port) < 1 || Number(port) > 65535) return;
  link.href = `${location.protocol}//${location.hostname}:${Number(port)}/settings`;
}
