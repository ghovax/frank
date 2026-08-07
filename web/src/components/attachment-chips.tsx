"use client";

import {
  Box,
  Button,
  Dialog,
  Flex,
  IconButton,
  Image,
  Link,
  Portal,
  Span,
  Text,
} from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useState, type ReactNode } from "react";
import { LuExternalLink, LuX } from "react-icons/lu";
import { localFileUrl } from "@/lib/api";
import { iconForFilePath } from "@/lib/file-icons";
import type { MessageAttachment } from "@/lib/use-chat";
import { PdfDocumentView, PdfThumbnail } from "./pdf-view";
import { InlineField } from "./ui/display";
import { Tooltip } from "./ui/tooltip";
import { Frame } from "./ui/semantic";

// Whether an attachment is an image we can render inline, preferring its mime type.
function isImageAttachment(attachment: MessageAttachment): boolean {
  if (attachment.mimeType.startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico|tiff?)$/i.test(attachment.filename);
}

// Whether an attachment is a PDF, rendered inline in both the hover thumbnail and the lightbox.
function isPdfAttachment(attachment: MessageAttachment): boolean {
  if (attachment.mimeType === "application/pdf") return true;
  return /\.pdf$/i.test(attachment.filename);
}

// A human-readable file size (e.g. "1.4 MB"), for the attachment hover card.
function formatFileSize(bytes: number): string {
  if (!bytes || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unitIndex]}`;
}

// One attachment as a chip, shared by the composer and the transcript so both look identical.
function MediaChipCard({
  thumbnail,
  filename,
  onClick,
  onRemove,
  badge,
}: {
  thumbnail: ReactNode;
  filename: string;
  onClick: () => void;
  onRemove?: () => void;
  badge?: ReactNode;
}) {
  const translation = useTranslations("AttachmentChips");
  const tail = filename.slice(-7);
  const head = filename.slice(0, Math.max(0, filename.length - 7));
  return (
    <Box
      position="relative"
      flexShrink={0}
      w={48}
      h="136px"
      overflow="hidden"
      borderRadius="md"
      border="1px solid"
      borderColor="border"
      bg="bg"
      _hover={{ borderColor: "border.emphasized" }}
      _focusWithin={{ borderColor: "border.emphasized" }}
    >
      <Button
        variant="plain"
        display="flex"
        flexDirection="column"
        w="full"
        h="full"
        p={0}
        textAlign="left"
        fontWeight="normal"
        overflow="hidden"
        onClick={onClick}
      >
        <Flex
          flex={1}
          minH={0}
          w="100%"
          overflow="hidden"
          bg="bg.subtle"
          borderBottom="1px solid"
          borderColor="border"
          align="center"
          justify="center"
        >
          {thumbnail}
        </Flex>
        <Flex
          flexShrink={0}
          w="100%"
          px={2.5}
          py={2}
          pe={onRemove ? 8 : 2.5}
          minW={0}
          align="center"
          gap={1}
          textStyle="fieldLabel"
          color="fg"
          title={filename}
        >
          <Flex minW={0} flex={1}>
            <Span truncate>{head}</Span>
            <Span flexShrink={0}>{tail}</Span>
          </Flex>
          {badge}
        </Flex>
      </Button>
      {onRemove && (
        <IconButton
          aria-label={translation("removeAttachment")}
          variant="ghost"
          size="2xs"
          position="absolute"
          right={2}
          bottom={1.5}
          color="fg.muted"
          _hover={{ color: "fg", bg: "bg.muted" }}
          onClick={onRemove}
        >
          <LuX />
        </IconButton>
      )}
    </Box>
  );
}

function AttachmentLightbox({
  attachment,
  onClose,
}: {
  attachment: MessageAttachment;
  onClose: () => void;
}) {
  const translation = useTranslations("AttachmentChips");
  const url = localFileUrl(attachment.path);
  const image = isImageAttachment(attachment);
  const pdf = isPdfAttachment(attachment);
  return (
    <Dialog.Root
      open
      onOpenChange={(event) => {
        if (!event.open) onClose();
      }}
      placement="center"
      size="cover"
    >
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content
            maxW={{ base: "100%", sm: "min(1100px, 92vw)" }}
            maxH={{ base: "100dvh", sm: "90vh" }}
            overflow="hidden"
          >
            <Dialog.Header display="flex" alignItems="center" gap={2} position="relative">
              <Dialog.Title textStyle="panelTitle" truncate>
                {attachment.filename}
              </Dialog.Title>
              <Flex align="center" gap={2} ml="auto">
                <Link
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  color="fg.muted"
                  _hover={{ color: "fg" }}
                  title={translation("openInNewTab")}
                >
                  <LuExternalLink size={14} />
                </Link>
                <Dialog.CloseTrigger position="static" />
              </Flex>
            </Dialog.Header>
            <Dialog.Body
              p={0}
              display="flex"
              alignItems="center"
              justifyContent="center"
              bg="bg.subtle"
              minH="60vh"
            >
              {image ? (
                <Image
                  src={url}
                  alt={attachment.filename}
                  maxW="100%"
                  maxH="90vh"
                  objectFit="contain"
                />
              ) : pdf ? (
                <Box w="100%" h="100%">
                  <PdfDocumentView url={url} />
                </Box>
              ) : (
                <Frame
                  src={url}
                  title={attachment.filename}
                  w="full"
                  h="full"
                  border="none"
                  bg="white"
                />
              )}
            </Dialog.Body>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

export function AttachmentChip({
  attachment,
  onRemove,
}: {
  attachment: MessageAttachment;
  onRemove?: () => void;
}) {
  const translation = useTranslations("AttachmentChips");
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const image = isImageAttachment(attachment);
  const pdf = isPdfAttachment(attachment);
  const { icon: Icon, iconColor } = iconForFilePath(attachment.filename);
  const thumbnail = image ? (
    <Image
      src={localFileUrl(attachment.path)}
      alt={attachment.filename}
      w="100%"
      h="100%"
      objectFit="cover"
      objectPosition="top"
    />
  ) : pdf ? (
    <PdfThumbnail url={localFileUrl(attachment.path)} width={192} />
  ) : (
    <Box color={iconColor} display="flex" alignItems="center" justifyContent="center">
      <Icon size={40} />
    </Box>
  );
  // The same hover card the git indicator uses, carrying whatever metadata the attachment has.
  const tooltip = (
    <Box whiteSpace="nowrap">
      <Text fontWeight="semibold" mb={1} color="fg" maxW={80} truncate>
        {attachment.filename}
      </Text>
      <Flex direction="column" gap={1}>
        {attachment.mimeType && (
          <InlineField label={translation("fieldType")}>
            <Text truncate maxW={80}>
              {attachment.mimeType}
            </Text>
          </InlineField>
        )}
        {attachment.size > 0 && (
          <InlineField label={translation("fieldSize")}>
            <Text>{formatFileSize(attachment.size)}</Text>
          </InlineField>
        )}
        {attachment.path && (
          <InlineField label={translation("fieldPath")}>
            <Text truncate maxW={80}>
              {attachment.path}
            </Text>
          </InlineField>
        )}
      </Flex>
    </Box>
  );
  return (
    <>
      <Tooltip
        content={tooltip}
        rich
        openDelay={300}
        closeDelay={60}
        positioning={{ placement: "top" }}
      >
        <Box flexShrink={0}>
          <MediaChipCard
            thumbnail={thumbnail}
            filename={attachment.filename}
            onClick={() => setLightboxOpen(true)}
            onRemove={onRemove}
          />
        </Box>
      </Tooltip>
      {lightboxOpen && (
        <AttachmentLightbox attachment={attachment} onClose={() => setLightboxOpen(false)} />
      )}
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
