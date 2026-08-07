"use client";

import { Box, Flex, Spinner, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { LuTriangleAlert } from "react-icons/lu";
import * as pdfjsLib from "pdfjs-dist";
import type { PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import { Canvas } from "./ui/semantic";
import { expected, swallowed } from "@/lib/swallowed";

// Render PDFs to a canvas rather than relying on the browser's own plugin, which is inconsistent.

// The worker must be configured before a document is opened, and the bundler emits it as an asset.
pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

// Cache the fetched bytes per URL, since opening a document transfers the buffer it is given.
const pdfBytesCache = new Map<string, Promise<ArrayBuffer>>();

function loadPdfBytes(url: string): Promise<ArrayBuffer> {
  let pending = pdfBytesCache.get(url);
  if (!pending) {
    pending = fetch(url).then((response) => {
      if (!response.ok) throw new Error(`Failed to load PDF (${response.status})`);
      return response.arrayBuffer();
    });
    pending.catch(() => pdfBytesCache.delete(url)); // never cache a failed fetch
    pdfBytesCache.set(url, pending);
  }
  return pending;
}

async function openDocument(url: string): Promise<PDFDocumentProxy> {
  const bytes = await loadPdfBytes(url);
  return pdfjsLib.getDocument({ data: new Uint8Array(bytes.slice(0)) }).promise;
}

// Render one page fitted to a width and sharp on HiDPI, returning the task so the caller can cancel it.
function renderPageToCanvas(
  page: Awaited<ReturnType<PDFDocumentProxy["getPage"]>>,
  canvas: HTMLCanvasElement,
  cssWidth: number,
): RenderTask | null {
  const baseViewport = page.getViewport({ scale: 1 });
  const scale = cssWidth / baseViewport.width;
  const viewport = page.getViewport({ scale });
  const outputScale = window.devicePixelRatio || 1;
  const context = canvas.getContext("2d");
  if (!context) return null;
  canvas.width = Math.floor(viewport.width * outputScale);
  canvas.height = Math.floor(viewport.height * outputScale);
  canvas.style.width = `${Math.floor(viewport.width)}px`;
  canvas.style.height = `${Math.floor(viewport.height)}px`;
  const transform = outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : undefined;
  return page.render({ canvasContext: context, viewport, transform });
}

function PdfStatus({ kind }: { kind: "loading" | "error" }) {
  return (
    <Flex
      position="absolute"
      inset={0}
      align="center"
      justify="center"
      direction="column"
      gap={2}
      color="fg.subtle"
    >
      {kind === "loading" ? (
        <>
          <Spinner size="sm" borderWidth="2px" />
          <Text fontSize="xs">Rendering PDF…</Text>
        </>
      ) : (
        <>
          <LuTriangleAlert size={18} />
          <Text fontSize="xs">Could not render this PDF</Text>
        </>
      )}
    </Flex>
  );
}

// The first page of a PDF, rendered small — used as the hover thumbnail.
export function PdfThumbnail({ url, width = 240 }: { url: string; width?: number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [renderState, setRenderState] = useState<{
    url: string;
    status: "loading" | "ready" | "error";
  }>(() => ({
    url,
    status: "loading",
  }));
  const status = renderState.url === url ? renderState.status : "loading";

  useEffect(() => {
    let cancelled = false;
    let document: PDFDocumentProxy | null = null;
    (async () => {
      document = await openDocument(url);
      const page = await document.getPage(1);
      if (cancelled || !canvasRef.current) return;
      await renderPageToCanvas(page, canvasRef.current, width)?.promise;
      if (!cancelled) setRenderState({ url, status: "ready" });
    })().catch(() => {
      if (!cancelled) setRenderState({ url, status: "error" });
    });
    return () => {
      cancelled = true;
      document
        ?.destroy()
        .catch((caught) => expected("a document being torn down cannot fail usefully", caught));
    };
  }, [url, width]);

  return (
    <Box
      position="relative"
      minH="120px"
      minW={`${width}px`}
      display="flex"
      justifyContent="center"
    >
      <Canvas ref={canvasRef} display={status === "ready" ? "block" : "none"} borderRadius="md" />
      {status !== "ready" && <PdfStatus kind={status === "error" ? "error" : "loading"} />}
    </Box>
  );
}

// One page inside the full document view, rendered lazily so a long PDF does not rasterize up front.
function PdfPageView({
  document,
  pageNumber,
  width,
  eager,
}: {
  document: PDFDocumentProxy;
  pageNumber: number;
  width: number;
  eager: boolean;
}) {
  const holderRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(eager);
  // A portrait ratio keeps the placeholder page-shaped, so lazy pages do not jump as they appear.
  const [aspect, setAspect] = useState(1.414);

  useEffect(() => {
    if (visible) return;
    const element = holderRef.current;
    if (!element) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "800px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    let renderTask: RenderTask | null = null;
    (async () => {
      const page = await document.getPage(pageNumber);
      if (cancelled || !canvasRef.current) return;
      const baseViewport = page.getViewport({ scale: 1 });
      setAspect(baseViewport.height / baseViewport.width);
      renderTask = renderPageToCanvas(page, canvasRef.current, width);
      await renderTask?.promise;
    })().catch((caught) =>
      swallowed({ component: "pdf-view", operation: "render a page" }, caught),
    );
    // Cancel an in-flight render before the next width re-runs this, so two never touch one canvas.
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [visible, document, pageNumber, width]);

  return (
    <Box
      ref={holderRef}
      mx="auto"
      bg="bg"
      boxShadow="sm"
      w={`${width}px`}
      minH={visible ? undefined : `${Math.round(width * aspect)}px`}
      borderRadius="md"
      overflow="hidden"
    >
      {visible && <Canvas ref={canvasRef} display="block" w={`${width}px`} borderRadius="md" />}
    </Box>
  );
}

// The whole document, one page under the next, fitted to the container and re-fitted on resize.
export function PdfDocumentView({ url }: { url: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [documentState, setDocumentState] = useState<{
    url: string;
    document: PDFDocumentProxy | null;
    status: "loading" | "ready" | "error";
  }>(() => ({ url, document: null, status: "loading" }));
  const document = documentState.url === url ? documentState.document : null;
  const status = documentState.url === url ? documentState.status : "loading";
  const [pageWidth, setPageWidth] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let opened: PDFDocumentProxy | null = null;
    (async () => {
      opened = await openDocument(url);
      if (cancelled) {
        opened
          .destroy()
          .catch((caught) =>
            expected("a document abandoned mid-open cannot fail usefully", caught),
          );
        return;
      }
      setDocumentState({ url, document: opened, status: "ready" });
    })().catch(() => {
      if (!cancelled) setDocumentState({ url, document: null, status: "error" });
    });
    return () => {
      cancelled = true;
      opened
        ?.destroy()
        .catch((caught) => expected("a document being torn down cannot fail usefully", caught));
    };
  }, [url]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    let frame = 0;
    const apply = () => {
      // Leave a little breathing room; cap so pages stay readable on wide screens.
      const available = element.clientWidth - 24;
      setPageWidth(Math.max(240, Math.min(900, available)));
    };
    // Coalesce the burst of resize callbacks to one width update per frame.
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(apply);
    };
    apply();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [document]);

  return (
    <Box
      ref={containerRef}
      position="relative"
      w="100%"
      h="100%"
      overflowY="auto"
      bg="bg.subtle"
      py={3}
    >
      {status === "ready" && document && pageWidth > 0 && (
        <Flex direction="column" gap={3} align="center">
          {Array.from({ length: document.numPages }, (_, index) => (
            <PdfPageView
              key={index}
              document={document}
              pageNumber={index + 1}
              width={pageWidth}
              eager={index < 2}
            />
          ))}
        </Flex>
      )}
      {status !== "ready" && <PdfStatus kind={status === "error" ? "error" : "loading"} />}
    </Box>
  );
}
