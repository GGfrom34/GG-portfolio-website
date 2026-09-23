/*
 * Ask the Tariff Advisor -- a small vanilla-JS chat widget over web_server.py's /chat endpoint
 * (tariff-advisor/web_server.py). No build step, no dependencies, matching the rest of this site.
 *
 * Point BACKEND_URL at the deployed backend before publishing.
 *
 * There is no build step to fingerprint this file, and GitHub Pages serves it with a browser
 * cache lifetime long enough that a visitor's browser (or this project's own dev tooling) can
 * keep running a stale copy after a change. Bump the `?v=` query string on the <script> tag in
 * octopus-tariff-advisor.html every time this file changes, so browsers treat it as a new URL.
 */
(function () {
  "use strict";

  const BACKEND_URL = "https://octopus-tariff-advisor.onrender.com";
  const SESSION_STORAGE_KEY = "tariff-advisor-session-id";

  const chatEl = document.getElementById("advisor-chat");
  const formEl = document.getElementById("advisor-form");
  const inputEl = document.getElementById("advisor-input");
  const resetEl = document.getElementById("advisor-reset");
  if (!chatEl || !formEl || !inputEl) return; // widget markup not on this page

  let sending = false;

  function addMessage(role, text) {
    const wrapper = document.createElement("div");
    wrapper.className = "advisor-message advisor-message--" + role;
    wrapper.textContent = text;
    chatEl.appendChild(wrapper);
    chatEl.scrollTop = chatEl.scrollHeight;
    return wrapper;
  }

  function setStatus(text) {
    let statusEl = chatEl.querySelector(".advisor-status");
    if (!text) {
      if (statusEl) statusEl.remove();
      return;
    }
    if (!statusEl) {
      statusEl = document.createElement("div");
      statusEl.className = "advisor-status";
      chatEl.appendChild(statusEl);
    }
    statusEl.textContent = text;
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function getSessionId() {
    try {
      return sessionStorage.getItem(SESSION_STORAGE_KEY);
    } catch (err) {
      return null; // private browsing / blocked storage: conversation just won't persist across reloads
    }
  }

  function setSessionId(id) {
    try {
      sessionStorage.setItem(SESSION_STORAGE_KEY, id);
    } catch (err) {
      /* ignore */
    }
  }

  function clearSessionId() {
    try {
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } catch (err) {
      /* ignore */
    }
  }

  // Parses one or more "event: X\ndata: Y\n\n" blocks out of an SSE byte stream, tolerating a
  // block being split across two chunks (fetch's reader has no framing guarantees).
  function makeSseParser(onEvent) {
    let buffer = "";
    return function feed(chunk) {
      buffer += chunk;
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop(); // last piece may be incomplete; keep it for the next chunk
      for (const block of blocks) {
        if (!block.trim()) continue;
        const lines = block.split("\n");
        const eventLine = lines.find((l) => l.startsWith("event: "));
        const dataLine = lines.find((l) => l.startsWith("data: "));
        if (!eventLine || !dataLine) continue;
        const event = eventLine.slice("event: ".length);
        let data;
        try {
          data = JSON.parse(dataLine.slice("data: ".length));
        } catch (err) {
          continue;
        }
        onEvent(event, data);
      }
    };
  }

  async function sendMessage(message) {
    sending = true;
    inputEl.disabled = true;
    addMessage("user", message);
    const assistantEl = addMessage("assistant", "");
    setStatus("Thinking…");

    let response;
    try {
      response = await fetch(BACKEND_URL + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: getSessionId(), message: message }),
      });
    } catch (err) {
      setStatus(null);
      assistantEl.textContent = "Sorry, I couldn't reach the advisor service. Please try again shortly.";
      sending = false;
      inputEl.disabled = false;
      return;
    }

    if (!response.ok) {
      setStatus(null);
      let errorMessage = "Something went wrong. Please try again.";
      try {
        const body = await response.json();
        if (body && body.error) errorMessage = body.error;
      } catch (err) {
        /* keep the default message */
      }
      assistantEl.textContent = errorMessage;
      sending = false;
      inputEl.disabled = false;
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let text = "";
    const parse = makeSseParser((event, data) => {
      if (event === "status") {
        setStatus(data);
      } else if (event === "text") {
        setStatus(null);
        text += data;
        assistantEl.textContent = text;
        chatEl.scrollTop = chatEl.scrollHeight;
      } else if (event === "done") {
        setStatus(null);
        if (data && data.session_id) setSessionId(data.session_id);
      }
    });

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      parse(decoder.decode(value, { stream: true }));
    }

    sending = false;
    inputEl.disabled = false;
    inputEl.focus();
  }

  formEl.addEventListener("submit", function (event) {
    event.preventDefault();
    if (sending) return;
    const message = inputEl.value.trim();
    if (!message) return;
    inputEl.value = "";
    sendMessage(message);
  });

  if (resetEl) {
    resetEl.addEventListener("click", function () {
      clearSessionId();
      chatEl.innerHTML = "";
      addMessage("assistant", "New conversation started. What would you like to know about Octopus tariffs?");
    });
  }
})();
