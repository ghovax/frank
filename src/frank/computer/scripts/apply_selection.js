// Select the character range [start, end) of an editable element in the page, or place the caret
// when start === end. Called by the browser tool's `select` and `caret` actions through
// Playwright's locator.evaluate, with the range computed on the Python side from the element's
// text (read natively with input_value()/text_content(), which this indexing matches).
//
// This is the one thing Playwright has no native API for: it exposes select_text() (whole field),
// double/triple-click (word/line), fill(), and keyboard selection, but not selecting an arbitrary
// substring or placing the caret at an offset. An <input>/<textarea> uses the native
// setSelectionRange; any other editable element (a contenteditable region) is mapped by walking
// its text nodes — offsets are over textContent, matching what the caller reads with
// text_content(). Returns the selected substring (empty string for a collapsed caret), or null
// when the range could not be placed so the caller can report it honestly.
(element, [start, end]) => {
  if ((element.tagName === "INPUT" || element.tagName === "TEXTAREA") && element.setSelectionRange) {
    element.focus();
    element.setSelectionRange(start, end);
    return element.value.substring(start, end);
  }

  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  let node = null;
  let position = 0;
  let startNode = null;
  let startOffset = 0;
  let endNode = null;
  let endOffset = 0;
  while ((node = walker.nextNode())) {
    const length = node.nodeValue.length;
    if (startNode === null && position + length >= start) {
      startNode = node;
      startOffset = start - position;
    }
    if (position + length >= end) {
      endNode = node;
      endOffset = end - position;
      break;
    }
    position += length;
  }
  if (startNode === null) {
    return null;
  }
  if (endNode === null) {
    endNode = startNode;
    endOffset = startNode.nodeValue.length;
  }

  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  if (element.focus) {
    element.focus();
  }
  return range.toString();
}
