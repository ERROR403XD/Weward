"use strict";

const concepts = JSON.parse(document.getElementById("concept-data").textContent);
const storageKey = "weward-icon-candidates-round-01";
const validIds = new Set(concepts.map(concept => concept.id));
let saved = new Set();
try {
  const stored = JSON.parse(localStorage.getItem(storageKey) || "[]");
  if (Array.isArray(stored)) saved = new Set(stored.filter(id => validIds.has(id)));
} catch {
  // The gallery also works with storage disabled or when opened as a local file.
}
let filter = "all";
let currentIndex = 0;
const cards = [...document.querySelectorAll(".candidate")];
const detail = document.getElementById("detail");

function renderGallery() {
  let count = 0;
  for (const card of cards) {
    const isSaved = saved.has(card.dataset.id);
    card.classList.toggle("saved", isSaved);
    card.hidden = filter === "saved" ? !isSaved : filter !== "all" && card.dataset.category !== filter;
    if (!card.hidden) count++;
  }
  document.getElementById("saved-count").textContent = saved.size;
  document.getElementById("empty-state").hidden = count !== 0;
  document.getElementById("selection-note").textContent = saved.size
    ? `当前候选：${[...saved].sort().join("、")}。选好后，在对话中回复编号即可。`
    : "选好后，回复编号即可，例如「选 01」。";
}

function renderDetail() {
  const concept = concepts[currentIndex];
  document.getElementById("detail-number").textContent = concept.id;
  document.getElementById("detail-title").textContent = concept.name;
  document.getElementById("detail-description").textContent = concept.description;
  for (const image of detail.querySelectorAll("img")) {
    image.src = concept.file;
    image.alt = `${concept.id} ${concept.name}`;
  }
  document.getElementById("download-svg").href = concept.file;
  document.getElementById("download-svg").download = `weward-${concept.id}-${concept.slug}.svg`;
  document.getElementById("detail-position").textContent = `${concept.id} / 20`;
  document.getElementById("previous").disabled = currentIndex === 0;
  document.getElementById("next").disabled = currentIndex === concepts.length - 1;
  const button = document.getElementById("save-candidate");
  const isSaved = saved.has(concept.id);
  button.textContent = isSaved ? "已加入候选 ✓" : "加入候选";
  button.setAttribute("aria-pressed", String(isSaved));
  document.getElementById("page-favicon").href = concept.file;
}

function openDetail(id) {
  const index = concepts.findIndex(concept => concept.id === id);
  if (index < 0) return;
  currentIndex = index;
  renderDetail();
  if (!detail.open) detail.showModal();
  detail.scrollTop = 0;
}

for (const card of cards) {
  card.addEventListener("click", () => {
    history.replaceState(null, "", `#${card.dataset.id}`);
    openDetail(card.dataset.id);
  });
}
for (const button of document.querySelectorAll("[data-filter]")) {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    for (const other of document.querySelectorAll("[data-filter]")) {
      const active = other === button;
      other.classList.toggle("active", active);
      other.setAttribute("aria-pressed", String(active));
    }
    renderGallery();
  });
}
for (const button of document.querySelectorAll("[data-mode]")) {
  button.addEventListener("click", () => {
    document.body.dataset.mode = button.dataset.mode;
    for (const other of document.querySelectorAll("[data-mode]")) {
      const active = other === button;
      other.classList.toggle("active", active);
      other.setAttribute("aria-pressed", String(active));
    }
  });
}
function closeDetail() {
  history.replaceState(null, "", location.pathname + location.search);
  detail.close();
}
document.getElementById("close-detail").addEventListener("click", closeDetail);
detail.addEventListener("cancel", event => {
  event.preventDefault();
  closeDetail();
});
detail.addEventListener("click", event => {
  if (event.target !== detail) return;
  const box = detail.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeDetail();
});
function step(direction) {
  const next = currentIndex + direction;
  if (next < 0 || next >= concepts.length) return;
  currentIndex = next;
  renderDetail();
  history.replaceState(null, "", `#${concepts[currentIndex].id}`);
}
document.getElementById("previous").addEventListener("click", () => step(-1));
document.getElementById("next").addEventListener("click", () => step(1));
detail.addEventListener("keydown", event => {
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    step(event.key === "ArrowLeft" ? -1 : 1);
  }
});
document.getElementById("save-candidate").addEventListener("click", () => {
  const id = concepts[currentIndex].id;
  if (saved.has(id)) saved.delete(id);
  else saved.add(id);
  try {
    localStorage.setItem(storageKey, JSON.stringify([...saved]));
  } catch {
    document.querySelector(".detail-note").textContent = "浏览器未允许保存候选，请记下编号并在对话中回复。";
  }
  renderGallery();
  renderDetail();
});
window.addEventListener("hashchange", () => {
  if (validIds.has(location.hash.slice(1))) openDetail(location.hash.slice(1));
});
renderGallery();
if (validIds.has(location.hash.slice(1))) openDetail(location.hash.slice(1));
