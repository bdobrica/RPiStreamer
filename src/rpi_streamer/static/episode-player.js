(() => {
  "use strict";

  const player = document.querySelector("[data-episode-player]");
  const selector = document.querySelector("[data-episode-select]");
  const previous = document.querySelector("[data-episode-previous]");
  const next = document.querySelector("[data-episode-next]");
  const heading = document.querySelector("[data-episode-heading]");
  const status = document.querySelector("[data-episode-status]");
  if (!player || !selector || !previous || !next || !heading || !status) return;

  const options = Array.from(selector.options);

  function select(index, updateFragment = true) {
    if (index < 0 || index >= options.length) return;
    const option = options[index];
    player.pause();
    player.src = option.value;
    player.load();
    selector.selectedIndex = index;
    heading.textContent = option.textContent;
    status.textContent = `Selected ${option.textContent}`;
    previous.disabled = index === 0;
    next.disabled = index === options.length - 1;
    if (updateFragment) history.replaceState(null, "", `#episode-${index + 1}`);
  }

  selector.addEventListener("change", () => select(selector.selectedIndex));
  previous.addEventListener("click", () => select(selector.selectedIndex - 1));
  next.addEventListener("click", () => select(selector.selectedIndex + 1));

  const fragment = /^#episode-(\d+)$/.exec(window.location.hash);
  const initial = fragment ? Number(fragment[1]) - 1 : 0;
  select(initial >= 0 && initial < options.length ? initial : 0, false);
})();
