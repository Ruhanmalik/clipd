// The only client-side code in Step 3a. Everything else is a link.
(function () {
  "use strict";

  var video = document.querySelector("video.player");

  document.addEventListener("keydown", function (event) {
    // Never steal a key from a field, and never from a browser shortcut.
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;

    if (event.key === "Escape") {
      var back = document.querySelector(".crumb a");
      if (back) window.location.href = back.href;
      return;
    }

    if (!video) return;

    if (event.key === " ") {
      event.preventDefault();
      video.paused ? video.play() : video.pause();
    } else if (event.key === "ArrowLeft") {
      video.currentTime -= 5;
    } else if (event.key === "ArrowRight") {
      video.currentTime += 5;
    }
  });
})();
