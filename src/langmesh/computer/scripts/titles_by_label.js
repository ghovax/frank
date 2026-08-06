// Every tooltip on the page, keyed by the visible text of the element that carries it.
() => {
  const titlesByLabel = new Map();
  const ambiguousLabels = new Set();
  for (const node of document.querySelectorAll("[title]")) {
    const title = (node.getAttribute("title") || "").trim();
    if (!title) continue;
    const label = (node.innerText || node.getAttribute("aria-label") || node.getAttribute("alt") || "")
      .trim()
      .replace(/\s+/g, " ");
    if (!label) continue;
    if (titlesByLabel.has(label) && titlesByLabel.get(label) !== title) {
      ambiguousLabels.add(label);
      continue;
    }
    titlesByLabel.set(label, title);
  }
  for (const label of ambiguousLabels) titlesByLabel.delete(label);
  return Object.fromEntries(titlesByLabel);
}
