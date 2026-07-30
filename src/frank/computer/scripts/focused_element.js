// What the page currently has focus on, described in whatever words it publishes. Called once on
// each side of an action by the browser tool's `glance`, so a diff can say that focus moved.
//
// The three sources are tried in the order a person would name the thing: its accessible label
// first, because that is what a page author wrote for exactly this purpose; its visible text next,
// which is what somebody reading the screen would call it; and its tag name last, which says
// almost nothing but distinguishes a BUTTON from a DIV when a page offers neither of the others.
//
// Returned whole rather than clipped. The caller compares these for equality to decide whether
// anything moved, and two rows of one table agree for their first eighty characters — so a
// truncated answer makes different things look identical and a real change read as nothing.
() => {
  const focusedElement = document.activeElement;
  if (!focusedElement) return null;
  return (
    focusedElement.getAttribute("aria-label") ||
    focusedElement.innerText ||
    focusedElement.tagName
  );
}
