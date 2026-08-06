"use client";

import { Box, Flex } from "@chakra-ui/react";
import { useCallback, useEffect, useRef, useState, type PointerEvent, type ReactNode } from "react";

// One tileable panel: a stable key (drives layout identity) and its rendered content.
export interface TilePanel {
  key: string;
  content: ReactNode;
  onActivate?: () => void;
}

// Balance panels into columns, filling vertically first and spreading only once a column is full.
function columnCounts(count: number): number[] {
  if (count <= 0) return [];
  const maxColumnHeight = Math.ceil(Math.sqrt(count));
  const cols = Math.ceil(count / maxColumnHeight);
  const base = Math.floor(count / cols);
  const extra = count % cols;
  return Array.from({ length: cols }, (_, index) => base + (index < extra ? 1 : 0)).filter((columnCount) => columnCount > 0);
}

const RESIZE_THICKNESS = 8; // px hit area for a handle (its visible line is 1px, centered)

// A tiling container for the side panels, every boundary carrying a drag handle that repartitions.
export function PanelTiles({ panels, gap = 8 }: { panels: TilePanel[]; gap?: number }) {
  const columns: TilePanel[][] = [];
  {
    let index = 0;
    for (const columnCount of columnCounts(panels.length)) {
      columns.push(panels.slice(index, index + columnCount));
      index += columnCount;
    }
  }

  // A signature of the arrangement; when it changes the stored weights reset to an even split.
  const signature = columns.map((column) => column.map((panel) => panel.key).join(",")).join("|");

  const containerRef = useRef<HTMLDivElement>(null);
  const columnRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [colFlex, setColFlex] = useState<number[]>(() => columns.map(() => 1));
  const [rowFlex, setRowFlex] = useState<number[][]>(() => columns.map((column) => column.map(() => 1)));

  // Reset in render rather than in an effect, so panel contents are not remounted and terminal state survives.
  const [layoutSignature, setLayoutSignature] = useState(signature);
  if (layoutSignature !== signature) {
    setLayoutSignature(signature);
    setColFlex(columns.map(() => 1));
    setRowFlex(columns.map((column) => column.map(() => 1)));
  }

  // Mirror the weights into refs, so a resize starting later reads current values.
  const colFlexRef = useRef(colFlex);
  const rowFlexRef = useRef(rowFlex);
  useEffect(() => { colFlexRef.current = colFlex; }, [colFlex]);
  useEffect(() => { rowFlexRef.current = rowFlex; }, [rowFlex]);

  // Repartition weight between two adjacent siblings as the pointer drags.
  const startResize = useCallback(
    (
      event: PointerEvent<HTMLDivElement>,
      axis: "x" | "y",
      element: HTMLElement | null,
      read: () => number[],
      write: (next: number[]) => void,
      first: number,
    ) => {
      event.preventDefault();
      const measured = element ?? containerRef.current;
      if (!measured) return;
      const rect = measured.getBoundingClientRect();
      const total = axis === "x" ? rect.width : rect.height;
      if (total <= 0) return;
      const start = axis === "x" ? event.clientX : event.clientY;
      const base = [...read()];
      const sum = base[first] + base[first + 1];
      const target = event.currentTarget;
      target.setPointerCapture(event.pointerId);

      const onMove = (moveEvent: globalThis.PointerEvent) => {
        const position = axis === "x" ? moveEvent.clientX : moveEvent.clientY;
        const deltaFraction = ((position - start) / total) * (base.reduce((a, b) => a + b, 0));
        // Keep each side at least 12% of the pair so a panel never collapses to nothing.
        const minWeight = sum * 0.12;
        let firstWeight = base[first] + deltaFraction;
        firstWeight = Math.max(minWeight, Math.min(sum - minWeight, firstWeight));
        const next = [...base];
        next[first] = firstWeight;
        next[first + 1] = sum - firstWeight;
        write(next);
      };
      const onUp = (upEvent: globalThis.PointerEvent) => {
        target.releasePointerCapture(upEvent.pointerId);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [],
  );

  if (panels.length === 0) return null;

  return (
    <Flex ref={containerRef} h="full" minW={0} minH={0} align="stretch" style={{ gap }}>
      {columns.map((column, colIndex) => (
        <Flex
          key={`col-${colIndex}-${column.map((panel) => panel.key).join(",")}`}
          ref={(node) => {
            columnRefs.current[colIndex] = node;
          }}
          direction="column"
          minW={0}
          minH={0}
          position="relative"
          style={{ flex: `${colFlex[colIndex] ?? 1} 1 0`, gap }}
        >
          {column.map((panel, rowIndex) => (
            <Box
              key={panel.key}
              // A joining panel fades in; the split itself stays instant so drag-resizing never fights it.
              className="reveal-enter"
              minW={0}
              minH={0}
              position="relative"
              onPointerDownCapture={panel.onActivate}
              onFocusCapture={panel.onActivate}
              // No overflow clip: each panel clips its own content, so this would only cut the card's shadow.
              style={{ flex: `${rowFlex[colIndex]?.[rowIndex] ?? 1} 1 0` }}
            >
              {panel.content}
              {/* Handle between this row and the next one in the same column. */}
              {rowIndex < column.length - 1 && (
                <Box
                  role="separator"
                  aria-orientation="horizontal"
                  position="absolute"
                  left={0}
                  right={0}
                  bottom={`-${gap / 2 + RESIZE_THICKNESS / 2}px`}
                  h={`${RESIZE_THICKNESS}px`}
                  cursor="row-resize"
                  zIndex={2}
                  css={{ touchAction: "none", "&:hover > div, &[data-dragging] > div": { background: "var(--chakra-colors-border-emphasized)" } }}
                  onPointerDown={(event) =>
                    startResize(
                      event,
                      "y",
                      columnRefs.current[colIndex],
                      () => rowFlexRef.current[colIndex] ?? [],
                      (nextColumn) => {
                        const next = rowFlexRef.current.map((entry, index) => (index === colIndex ? nextColumn : entry));
                        setRowFlex(next);
                      },
                      rowIndex,
                    )
                  }
                >
                  <Box position="absolute" left={0} right={0} top="50%" h="1px" transform="translateY(-50%)" background="transparent" />
                </Box>
              )}
            </Box>
          ))}
          {/* Handle between this column and the next one. */}
          {colIndex < columns.length - 1 && (
            <Box
              role="separator"
              aria-orientation="vertical"
              position="absolute"
              top={0}
              bottom={0}
              right={`-${gap / 2 + RESIZE_THICKNESS / 2}px`}
              w={`${RESIZE_THICKNESS}px`}
              cursor="col-resize"
              zIndex={2}
              css={{ touchAction: "none", "&:hover > div, &[data-dragging] > div": { background: "var(--chakra-colors-border-emphasized)" } }}
              onPointerDown={(event) =>
                startResize(event, "x", containerRef.current, () => colFlexRef.current, setColFlex, colIndex)
              }
            >
              <Box position="absolute" top={0} bottom={0} left="50%" w="1px" transform="translateX(-50%)" background="transparent" />
            </Box>
          )}
        </Flex>
      ))}
    </Flex>
  );
}
