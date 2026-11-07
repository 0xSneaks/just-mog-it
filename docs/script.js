// JUST MOG IT v2 — tiny extras. No dependencies, no build step.

// Duplicate every ticker's content so the CSS marquee loop is seamless.
document.querySelectorAll("[data-ticker]").forEach(function (track) {
  track.innerHTML += track.innerHTML;
});

// The cat points at you. Click it and it jabs. As it should.
(function () {
  var wrap = document.getElementById("mascotWrap");
  if (!wrap) return;

  wrap.addEventListener("mouseenter", function () {
    wrap.style.transform = "scale(1.04) rotate(1.2deg)";
  });
  wrap.addEventListener("mouseleave", function () {
    wrap.style.transform = "";
  });
  wrap.addEventListener("click", function () {
    wrap.classList.remove("jab");
    // force reflow so the animation can restart
    void wrap.offsetWidth;
    wrap.classList.add("jab");
  });
})();

// CTA click = tiny burst of mog confetti.
(function () {
  var bits = ["★", "🐱", "💥", "👉", "✦"];
  document.querySelectorAll(".cta-main").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var rect = btn.getBoundingClientRect();
      var parent = btn.offsetParent || document.body;
      var prect = parent.getBoundingClientRect();
      for (var i = 0; i < 10; i++) {
        var s = document.createElement("span");
        s.className = "burst-bit";
        s.textContent = bits[Math.floor(Math.random() * bits.length)];
        s.style.left = (rect.left - prect.left + rect.width / 2) + "px";
        s.style.top = (rect.top - prect.top) + "px";
        var ang = Math.random() * Math.PI * 2;
        var dist = 60 + Math.random() * 90;
        s.style.setProperty("--dx", Math.cos(ang) * dist + "px");
        s.style.setProperty("--dy", (Math.sin(ang) * dist - 50) + "px");
        s.style.setProperty("--rot", (Math.random() * 260 - 130) + "deg");
        parent.appendChild(s);
        (function (el) { setTimeout(function () { el.remove(); }, 850); })(s);
      }
    });
  });
})();
