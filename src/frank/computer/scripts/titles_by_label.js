// Every tooltip on the page, keyed by the visible text of the element that carries it. Called once
// per read by the browser tool, so an element's `title` can enrich the words it is searched by.
//
// The join is by visible text rather than by aria-ref, because refs do not exist in the DOM and
// resolving them one at a time would be a round trip per element. A label claimed by two different
// titles is dropped rather than guessed at: a wrong tooltip is worse than none, since it would put
// words into the key of an element that never said them.
//
// Both sides of the join normalise through one Python function, so nothing here needs to guess at
// a length: what counts as the same label is decided in one place rather than agreed in two.
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
