// Select a character range of an editable element, or place the caret when the range is empty.
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
