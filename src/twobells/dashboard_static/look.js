// The Look picker in a page's header (index.html, performance.html): one
// of its options, "modern" (the Standard look) by default, kept per browser
// under "dashboard.theme". theme.js applied the saved look before paint;
// this keeps the picker and the page in step as it changes.

import { $, store } from "./dom.js";

export function setUpLook() {
  const look = $("look");
  const looks = [...look.options].map((option) => option.value);
  const apply = (theme) => {
    document.documentElement.dataset.theme = theme;
    look.value = theme;
  };
  look.addEventListener("change", () => {
    if (!looks.includes(look.value)) return;
    store.set("dashboard.theme", look.value);
    apply(look.value);
  });
  const shown = document.documentElement.dataset.theme;
  apply(looks.includes(shown) ? shown : "modern");
}
