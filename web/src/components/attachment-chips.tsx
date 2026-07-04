"use client";

import { Box, Dialog, Flex, Image, Link, Portal, Text } from "@chakra-ui/react";
import { useState, type MouseEvent } from "react";
import { LuExternalLink, LuX } from "react-icons/lu";
import { filePreviewUrl } from "@/lib/api";
import { iconForFilePath } from "@/lib/file-icons";
import type { MessageAttachment } from "@/lib/use-chat";
import { PdfDocumentView, PdfThumbnail } from "./pdf-view";
import { Tooltip } from "./ui/tooltip";

// Whether an attachment is a raster/vector image we can render inline (a thumbnail
// on the chip, the full picture in the hover preview and the lightbox). Prefers the
// mime type the upload reported, falling back to the filename extension.
function isImageAttachment(attachment: MessageAttachment): boolean {
  if (attachment.mimeType.startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico|tiff?)$/i.test(attachment.filename);
}

// Whether an attachment is a PDF — rendered inline via the browser's built-in
// viewer (an iframe), both in the hover preview and the lightbox.
function isPdfAttachment(attachment: MessageAttachment): boolean {
  if (attachment.mimeType === "application/pdf") return true;
  return /\.pdf$/i.test(attachment.filename);
}

function formatSize(bytes: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// The hover preview shown in a tooltip: the image itself for pictures, an inline
// first-page render for PDFs, otherwise a compact metadata card (icon + full name +
// type + size) so any file still gets a legible peek.
function AttachmentHoverPreview({ attachment }: { attachment: MessageAttachment }) {
  if (isImageAttachment(attachment)) {
    return (
      <Box p={0.5}>
        <Image
          src={filePreviewUrl(attachment.path)}
          alt={attachment.filename}
          maxW="260px"
          maxH="260px"
          borderRadius="sm"
          display="block"
        />
        <Text mt={1} fontSize="xs" fontWeight="medium" color="fg.muted" truncate maxW="260px">
          {attachment.filename}
        </Text>
      </Box>
    );
  }
  if (isPdfAttachment(attachment)) {
    return (
      <Box p={0.5}>
        <Box borderRadius="sm" overflow="hidden" border="1px solid" borderColor="border" bg="white">
          <PdfThumbnail url={filePreviewUrl(attachment.path)} width={240} />
        </Box>
        <Text mt={1} fontSize="xs" fontWeight="medium" color="fg.muted" truncate maxW="260px">
          {attachment.filename}
        </Text>
      </Box>
    );
  }
  const { icon: Icon, iconColor } = iconForFilePath(attachment.filename);
  return (
    <Flex align="center" gap={2} maxW="260px" p={0.5}>
      <Box color={iconColor} flexShrink={0}>
        <Icon size={20} />
      </Box>
      <Box minW={0}>
        <Text fontSize="sm" fontWeight="medium" truncate>{attachment.filename}</Text>
        <Text fontSize="xs" color="fg.subtle" truncate>
          {[attachment.mimeType, formatSize(attachment.size)].filter(Boolean).join(" — ")}
        </Text>
      </Box>
    </Flex>
  );
}

// The full-size preview opened on click. Images render as a contained picture;
// everything else (PDF, HTML, text) is shown in an iframe pointed at the backend's
// /preview route — the same route open_preview artifacts use. PDFs render in the
// browser's native viewer; the /preview route serves them inline (no attachment
// disposition), so the iframe displays rather than downloads them.
function AttachmentLightbox({
  attachment,
  onClose,
}: {
  attachment: MessageAttachment;
  onClose: () => void;
}) {
  const url = filePreviewUrl(attachment.path);
  const image = isImageAttachment(attachment);
  const pdf = isPdfAttachment(attachment);
  return (
    <Dialog.Root open onOpenChange={(event) => { if (!event.open) onClose(); }} placement="center" size="cover">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content borderRadius="md" maxW="min(1100px, 92vw)" maxH="90vh" overflow="hidden">
            <Dialog.Header py={2} px={3} display="flex" alignItems="center" gap={2}>
              <Dialog.Title fontSize="sm" truncate>{attachment.filename}</Dialog.Title>
              <Flex align="center" gap={2} ml="auto">
                <Link href={url} target="_blank" rel="noreferrer" color="fg.muted" _hover={{ color: "fg" }} title="Open in a new tab">
                  <LuExternalLink size={14} />
                </Link>
                <Dialog.CloseTrigger position="static" />
              </Flex>
            </Dialog.Header>
            <Dialog.Body p={0} display="flex" alignItems="center" justifyContent="center" bg="bg.subtle" minH="60vh">
              {image ? (
                <Image src={url} alt={attachment.filename} maxW="100%" maxH="82vh" objectFit="contain" display="block" />
              ) : pdf ? (
                <Box w="100%" h="82vh">
                  <PdfDocumentView url={url} />
                </Box>
              ) : (
                <iframe
                  src={url}
                  title={attachment.filename}
                  style={{ width: "100%", height: "82vh", border: "none", background: "white" }}
                />
              )}
            </Dialog.Body>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

// One attachment chip — the single shared unit rendered BOTH above a user message
// (read-only) and in the composer's pending strip (removable). A thumbnail (images)
// or file icon plus the name/size; hover pops the mini preview, click opens the
// lightbox. Passing `onRemove` adds the composer's remove button (its click never
// opens the lightbox).
export function AttachmentChip({
  attachment,
  onRemove,
}: {
  attachment: MessageAttachment;
  onRemove?: () => void;
}) {
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const image = isImageAttachment(attachment);
  const { icon: Icon, iconColor } = iconForFilePath(attachment.filename);
  const handleRemove = (event: MouseEvent) => {
    event.stopPropagation();
    onRemove?.();
  };
  return (
    <>
      <Tooltip
        content={<AttachmentHoverPreview attachment={attachment} />}
        contentProps={{ p: 1, bg: "bg", color: "fg", borderRadius: "sm", boxShadow: "lg", border: "1px solid", borderColor: "border" }}
        openDelay={120}
        closeDelay={60}
        positioning={{ placement: "top" }}
      >
        <Flex
          as="button"
          align="center"
          gap={1.5}
          maxW="240px"
          px={2.5}
          py={1}
          border="1px solid"
          borderColor="border"
          borderRadius="sm"
          bg="bg"
          cursor="pointer"
          _hover={{ borderColor: "border.emphasized", bg: "bg.muted" }}
          onClick={() => setLightboxOpen(true)}
        >
          {image ? (
            <Image
              src={filePreviewUrl(attachment.path)}
              alt={attachment.filename}
              w="24px"
              h="24px"
              borderRadius="xs"
              objectFit="cover"
              flexShrink={0}
            />
          ) : (
            <Box color={iconColor} flexShrink={0} display="flex" alignItems="center">
              <Icon size={16} />
            </Box>
          )}
          <Box flex={1} minW={0} textAlign="left">
            <Text fontSize="xs" fontWeight="medium" truncate>{attachment.filename}</Text>
            {attachment.size > 0 && (
              <Text fontSize="2xs" color="fg.subtle" truncate>{formatSize(attachment.size)}</Text>
            )}
          </Box>
          {onRemove && (
            <Box
              as="span"
              role="button"
              aria-label="Remove attachment"
              onClick={handleRemove}
              color="fg.subtle"
              _hover={{ color: "fg" }}
              display="flex"
              alignItems="center"
              flexShrink={0}
              ml={0.5}
            >
              <LuX size={12} />
            </Box>
          )}
        </Flex>
      </Tooltip>
      {lightboxOpen && <AttachmentLightbox attachment={attachment} onClose={() => setLightboxOpen(false)} />}
    </>
  );
}

// The row of attachment chips shown above a user message in the transcript.
export function AttachmentChips({ attachments }: { attachments: MessageAttachment[] }) {
  if (attachments.length === 0) return null;
  return (
    <Flex gap={2} flexWrap="wrap" justify="flex-end">
      {attachments.map((attachment, index) => (
        <AttachmentChip key={`${attachment.path}-${index}`} attachment={attachment} />
      ))}
    </Flex>
  );
}
