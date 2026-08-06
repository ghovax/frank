// What the page currently has focus on, described in whatever words it publishes.
() => {
  const focusedElement = document.activeElement;
  if (!focusedElement) return null;
  return (
    focusedElement.getAttribute("aria-label") ||
    focusedElement.innerText ||
    focusedElement.tagName
  );
}
