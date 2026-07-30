/* SHOLA evaluation flow.
   Two jobs: keep the word queue moving without page loads, and let a volunteer
   type characters their keyboard does not have. Everything degrades to plain
   form posts if this file fails to load. */

(function () {
  "use strict";

  var root = document.getElementById("eval-root");
  if (!root) return;

  var cfg = JSON.parse(root.getAttribute("data-config"));
  var queue = JSON.parse(document.getElementById("queue-data").textContent);
  var remaining = cfg.remaining;
  var doneTotal = cfg.doneTotal;
  var atStart = remaining;
  var busy = false;

  var els = {
    card: document.getElementById("word-card"),
    phrase: document.getElementById("the-word"),
    options: document.getElementById("options"),
    left: document.getElementById("count-left"),
    bar: document.getElementById("bar"),
    sheet: document.getElementById("sheet"),
    sheetBack: document.getElementById("sheet-back"),
    sheetWord: document.getElementById("sheet-word"),
    input: document.getElementById("own-text"),
    keystrip: document.getElementById("keystrip"),
    popkeys: document.getElementById("popkeys"),
    skip: document.getElementById("skip-btn")
  };

  /* ------------------------------------------------------------ rendering */

  function render() {
    var item = queue[0];
    if (!item) {
      window.location.href = cfg.doneUrl;
      return;
    }
    els.phrase.textContent = item.phrase;
    els.options.innerHTML = "";

    item.options.forEach(function (opt) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "option";
      b.setAttribute("data-choice", String(opt.id));
      b.innerHTML = '<span class="mark" aria-hidden="true">✓</span>' +
        '<span class="txt"></span>';
      b.querySelector(".txt").textContent = opt.text;
      b.addEventListener("click", function () { choose(b, String(opt.id)); });
      els.options.appendChild(b);
    });

    var own = document.createElement("button");
    own.type = "button";
    own.className = "option own";
    own.innerHTML = '<span class="mark" aria-hidden="true">✎</span>' +
      "<span>Type your own translation</span>";
    own.addEventListener("click", openSheet);
    els.options.appendChild(own);

    var pos = atStart - remaining + 1;
    els.left.textContent = remaining === 1 ? "1 word left"
      : remaining + " words left";
    els.bar.style.width = Math.min(100, ((pos - 1) / Math.max(atStart, 1)) * 100) + "%";
  }

  function choose(button, choice) {
    if (busy) return;
    button.classList.add("chosen");
    submit(choice, "");
  }

  /* -------------------------------------------------------------- posting */

  function submit(choice, customText) {
    if (busy) return;
    busy = true;
    var item = queue[0];
    var body = new FormData();
    body.append("choice", choice);
    if (customText) body.append("custom_text", customText);

    fetch(cfg.submitUrl.replace("0", String(item.word_id)), {
      method: "POST",
      body: body,
      headers: { "X-Requested-With": "shola" },
      credentials: "same-origin"
    })
      .then(function (r) {
        if (r.status === 401) { window.location.href = cfg.resendUrl; return null; }
        if (!r.ok) throw new Error("submit failed");
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        remaining = data.remaining;
        doneTotal = data.done_total;
        queue.shift();
        // Top up from the server so the queue never runs dry mid-session.
        if (queue.length < 2 && data.next && data.next.length) {
          var have = {};
          queue.forEach(function (q) { have[q.word_id] = true; });
          data.next.forEach(function (n) {
            if (!have[n.word_id]) queue.push(n);
          });
        }
        busy = false;
        if (remaining <= 0 && !queue.length) {
          window.location.href = cfg.doneUrl;
          return;
        }
        render();
      })
      .catch(function () {
        busy = false;
        // Fall back to a full page post rather than losing the verdict.
        var f = document.getElementById("fallback-form");
        f.action = cfg.submitUrl.replace("0", String(item.word_id));
        f.querySelector('[name=choice]').value = choice;
        f.querySelector('[name=custom_text]').value = customText || "";
        f.submit();
      });
  }

  if (els.skip) {
    els.skip.addEventListener("click", function () { submit("skip", ""); });
  }

  /* ---------------------------------------------------------- own-text sheet */

  function openSheet() {
    els.sheetWord.textContent = queue[0].phrase;
    els.input.value = "";
    els.sheetBack.classList.add("open");
    els.sheet.classList.add("open");
    setTimeout(function () { els.input.focus(); }, 80);
  }

  function closeSheet() {
    els.sheetBack.classList.remove("open");
    els.sheet.classList.remove("open");
    hidePop();
  }

  els.sheetBack.addEventListener("click", closeSheet);
  document.getElementById("sheet-cancel").addEventListener("click", closeSheet);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && els.sheet.classList.contains("open")) closeSheet();
  });

  document.getElementById("sheet-save").addEventListener("click", saveOwn);
  els.input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); saveOwn(); }
  });

  function saveOwn() {
    var text = els.input.value.trim();
    if (!text) { els.input.focus(); return; }
    closeSheet();
    submit("custom", text);
  }

  /* --------------------------------------------------- special characters */

  function insert(ch) {
    var el = els.input;
    var s = el.selectionStart === null ? el.value.length : el.selectionStart;
    var e = el.selectionEnd === null ? el.value.length : el.selectionEnd;
    el.value = el.value.slice(0, s) + ch + el.value.slice(e);
    var at = s + ch.length;
    el.setSelectionRange(at, at);
    el.focus();
  }

  function hidePop() {
    els.popkeys.classList.remove("open");
    els.popkeys.innerHTML = "";
  }

  function showPop(anchor, variants) {
    els.popkeys.innerHTML = "";
    variants.forEach(function (v) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "key";
      b.textContent = v;
      b.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        insert(v);
        hidePop();
      });
      els.popkeys.appendChild(b);
    });
    var r = anchor.getBoundingClientRect();
    els.popkeys.classList.add("open");
    var top = r.top + window.scrollY - els.popkeys.offsetHeight - 8;
    var left = r.left + window.scrollX;
    var maxLeft = window.innerWidth - els.popkeys.offsetWidth - 10;
    els.popkeys.style.top = Math.max(8, top) + "px";
    els.popkeys.style.left = Math.min(Math.max(8, left), maxLeft) + "px";
  }

  /* Build the strip: every special character as a direct key, plus base
     letters that reveal their variants on a long press. */
  function buildKeys() {
    els.keystrip.innerHTML = "";

    cfg.special.forEach(function (ch) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "key";
      b.textContent = ch;
      b.addEventListener("click", function (e) { e.preventDefault(); insert(ch); });
      els.keystrip.appendChild(b);
    });

    Object.keys(cfg.longpress).forEach(function (base) {
      var variants = cfg.longpress[base];
      var b = document.createElement("button");
      b.type = "button";
      b.className = "key";
      b.textContent = base;
      b.title = "Hold for " + variants.join(" ");
      var timer = null;
      var fired = false;

      function down(e) {
        fired = false;
        b.classList.add("holding");
        timer = setTimeout(function () {
          fired = true;
          showPop(b, variants);
        }, 420);
      }
      function up(e) {
        clearTimeout(timer);
        b.classList.remove("holding");
        if (!fired) { insert(base); }
        else if (e) { e.preventDefault(); }
      }
      function cancel() { clearTimeout(timer); b.classList.remove("holding"); }

      b.addEventListener("touchstart", function (e) { e.preventDefault(); down(e); }, { passive: false });
      b.addEventListener("touchend", function (e) { e.preventDefault(); up(e); });
      b.addEventListener("touchcancel", cancel);
      b.addEventListener("mousedown", down);
      b.addEventListener("mouseup", up);
      b.addEventListener("mouseleave", cancel);
      b.addEventListener("contextmenu", function (e) { e.preventDefault(); });
      els.keystrip.appendChild(b);
    });
  }

  document.addEventListener("click", function (e) {
    if (els.popkeys.classList.contains("open") &&
        !els.popkeys.contains(e.target) &&
        !e.target.classList.contains("key")) {
      hidePop();
    }
  });

  buildKeys();
  render();
})();
