// What the participant sees on top of the website they test: the message screens and the X button.
//
// hardware_tobii.py gives this file to Chrome ("run it on every new page"), so it is there on every page,
// also after the participant clicked a link and went to another page of the site.
//
// It makes a message screen (dark background, text, one green button) and a round X button in the top right corner.
// Python controls it with:   window.__et.showMessage(text, buttonLabel)   window.__et.hideMessage()
//                            window.__et.showX(true or false)
// and asks what the participant did with:   window.__et.takeClick()  (pressed the green button?)
//                                           window.__et.takeX()      (pressed X?)
// Each "take" answers true once, then false again until the next press.
//
// Everything is drawn inside a shadow root, so the CSS of the tested website cannot change how it looks.

(() => {
  if (window.top !== window || window.__et) return; // only the main page, not frames inside it; only once

  const state = { clicked: false, xClicked: false };
  let host = null;
  let messageBox = null;
  let messageText = null;
  let messageButton = null;
  let xButton = null;

  const CSS = `
    * { box-sizing: border-box; }
    .message {
      position: fixed; inset: 0; display: none; flex-direction: column; align-items: center; justify-content: center;
      gap: 24px; padding: 48px; background: rgba(0, 0, 0, 0.6);
    }
    .text {
      max-width: 1100px; margin: 0; color: #fff; text-align: center;
      font: 700 28px/1.3 Rajdhani, "Segoe UI", Arial, sans-serif;
    }
    .button {
      padding: 12px 24px; background: #275d38; color: #fff; border: 0; border-radius: 8px; cursor: pointer;
      font: 600 16px Lato, "Segoe UI", Arial, sans-serif;
    }
    .button:hover { background: #1a4127; }
    .x {
      position: fixed; top: 24px; right: 24px; display: none; align-items: center; justify-content: center;
      width: 48px; height: 48px; padding: 0; background: #fff; border: 0; border-radius: 50%; cursor: pointer;
      box-shadow: 0 1px 6px rgba(0, 0, 0, 0.4);
    }
    .x:hover { background: #e8e8e8; }
  `;

  // Build everything once; put it into the page. Returns false while the page has no <html> yet.
  function mount() {
    if (host && host.isConnected) return true;
    const parent = document.documentElement;
    if (!parent) return false;

    if (!host) {
      host = document.createElement("div");
      host.style.cssText = "all: initial; position: fixed; inset: 0; z-index: 2147483647; pointer-events: none;";
      const shadow = host.attachShadow({ mode: "closed" });

      const style = document.createElement("style");
      style.textContent = CSS;

      messageBox = document.createElement("div");
      messageBox.className = "message";
      messageBox.style.pointerEvents = "auto"; // the host lets clicks through, the message screen catches them
      messageText = document.createElement("p");
      messageText.className = "text";
      messageButton = document.createElement("button");
      messageButton.type = "button";
      messageButton.className = "button";
      messageButton.addEventListener("click", () => {
        state.clicked = true;
        messageBox.style.display = "none";
      });
      messageBox.append(messageText, messageButton);

      xButton = document.createElement("button");
      xButton.type = "button";
      xButton.className = "x";
      xButton.setAttribute("aria-label", "I have finished this task");
      xButton.style.pointerEvents = "auto";
      xButton.innerHTML =
        '<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">' +
        '<path d="M5 5L19 19M19 5L5 19" stroke="#000" stroke-width="3" stroke-linecap="round" /></svg>';
      xButton.addEventListener("click", () => {
        state.xClicked = true;
        xButton.style.display = "none";
      });

      shadow.append(style, messageBox, xButton);
    }
    parent.appendChild(host);
    return true;
  }

  window.__et = {
    showMessage(text, buttonLabel) {
      if (!mount()) return false;
      messageText.textContent = text; // text, not HTML
      messageButton.textContent = buttonLabel;
      state.clicked = false;
      messageBox.style.display = "flex";
      return true;
    },
    hideMessage() {
      if (messageBox) messageBox.style.display = "none";
    },
    showX(visible) {
      if (!mount()) return false;
      state.xClicked = false;
      xButton.style.display = visible ? "flex" : "none";
      return true;
    },
    takeClick() {
      const answer = state.clicked;
      state.clicked = false;
      return answer;
    },
    takeX() {
      const answer = state.xClicked;
      state.xClicked = false;
      return answer;
    },
  };

  // A link that wants a NEW tab (target="_blank") opens in the same tab instead: the participant stays in the tab that is recorded
  document.addEventListener(
    "click",
    (event) => {
      const link = event.target && event.target.closest ? event.target.closest("a[target]") : null;
      if (link && link.target === "_blank") link.target = "_self";
    },
    true,
  );
  const openHere = window.open;
  window.open = (url, ...rest) => {
    if (url) {
      window.location.href = url;
      return window;
    }
    return openHere.call(window, url, ...rest);
  };

  // During a task Python sets window.__etWantX = true before this file runs: every new page then shows X by itself.
  if (window.__etWantX) {
    const showWhenReady = () => window.__et.showX(true);
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", showWhenReady);
    } else {
      showWhenReady();
    }
  }

  // Some sites rebuild the whole page: put our layer back if it was removed
  setInterval(() => {
    if (host && !host.isConnected) mount();
  }, 500);
})();
