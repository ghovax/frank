"use client";

import { Box, Dialog, Flex, Image, Link, Portal, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useState, type ReactNode } from "react";
import { LuExternalLink, LuMousePointerClick, LuX } from "react-icons/lu";
import { artifactPageUrl } from "@/lib/api";
import { iconForFilePath } from "@/lib/file-icons";
import { imageIdentityForArtifact, type ArtifactAnnotationRecord, type ArtifactImageAnnotation } from "@/lib/artifact-annotations";
import type { MessageAttachment } from "@/lib/use-chat";
import { PdfDocumentView, PdfThumbnail } from "./pdf-view";
import { ArtifactView } from "./tool-views";
import { InlineField } from "./tool-views/primitives";
import { Tooltip } from "./ui/tooltip";

// Whether an attachment is a raster/vector image we can render inline (a thumbnail
// on the chip, the full picture in the hover thumbnail and the lightbox). Prefers the
// mime type the upload reported, falling back to the filename extension.
function isImageAttachment(attachment: MessageAttachment): boolean {
  if (attachment.mimeType.startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico|tiff?)$/i.test(attachment.filename);
}

// Whether an attachment is a PDF — rendered inline via the browser's built-in
// viewer (an iframe), both in the hover thumbnail and the lightbox.
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

// The user-facing label for an annotated artifact image: its real file name when known
// (e.g. "plot.png"), falling back to the artifact title.
function annotationRecordLabel(record: ArtifactAnnotationRecord): string {
  return record.image.name || record.image.title;
}

// A numbered circle marking an image's version — the single, consistent way a version is
// shown across the app. Just the number, no "v" prefix; filled when it marks the
// active/selected version. `size="md"` is the prominent form used next to an artifact title.
export function VersionBadge({ number, active = false, size = "sm" }: { number: number; active?: boolean; size?: "sm" | "md" }) {
  // A circle for single digits, growing into a stadium/pill for 2+ digits so the number
  // never looks cramped: minW keeps the round shape, px lets it widen, borderRadius="full"
  // rounds either shape. Centering is content-flow (align/justify + cap-height trim) instead
  // of an absolute layer, so the box actually tracks the glyph width.
  const dimension = size === "md" ? "23px" : "19px";
  return (
    <Box
      as="span"
      display="inline-flex"
      alignItems="center"
      justifyContent="center"
      flexShrink={0}
      h={dimension}
      minW={dimension}
      px="5px"
      borderRadius="full"
      bg={active ? "blue.solid" : "bg.muted"}
      color={active ? "white" : "fg.muted"}
      border="1px solid"
      borderColor={active ? "blue.solid" : "border"}
      fontSize={size === "md" ? "13px" : "11px"}
      fontWeight={600}
      lineHeight="1"
      style={{ fontVariantNumeric: "tabular-nums", textBox: "trim-both cap alphabetic" }}
    >
      {number}
    </Box>
  );
}

// A media chip rendered as a vertical card: an enlarged thumbnail fills the top, the
// file name sits below on its own — no subtitle. Shared by uploaded-file chips and
// annotated artifact-image chips so both read identically. The name keeps its tail
// (extension) visible while the middle truncates. `onRemove` adds a corner remove control.
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
  const t = useTranslations("AttachmentChips");
  const tail = filename.slice(-7);
  const head = filename.slice(0, Math.max(0, filename.length - 7));
  return (
    <Flex
      direction="column"
      flexShrink={0}
      w={48}
      h="136px"
      overflow="hidden"
      borderRadius="md"
      border="1px solid"
      borderColor="border"
      bg="bg"
      cursor="pointer"
      textAlign="left"
      role="button"
      tabIndex={0}
      _hover={{ borderColor: "border.emphasized" }}
      onClick={onClick}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onClick(); } }}
    >
      <Flex flex={1} minH={0} w="100%" overflow="hidden" bg="bg.subtle" borderBottom="1px solid" borderColor="border" align="center" justify="center">
        {thumbnail}
      </Flex>
      <Flex flexShrink={0} w="100%" px={2.5} py={2} minW={0} align="center" gap={1} fontSize="xs" fontWeight="medium" color="fg" title={filename}>
        <Flex minW={0} flex={1}>
          <Text as="span" truncate>{head}</Text>
          <Text as="span" flexShrink={0}>{tail}</Text>
        </Flex>
        {badge}
        {onRemove && (
          <Box
            as="button"
            aria-label={t("removeAttachment")}
            flexShrink={0}
            w={4}
            h={4}
            borderRadius="sm"
            display="flex"
            alignItems="center"
            justifyContent="center"
            color="fg.muted"
            _hover={{ color: "fg", bg: "bg.muted" }}
            onClick={(event) => { event.stopPropagation(); onRemove(); }}
          >
            <LuX size={12} />
          </Box>
        )}
      </Flex>
    </Flex>
  );
}

function imageArtifactForAnnotationRecord(record: ArtifactAnnotationRecord): Record<string, unknown> {
  const artifact: Record<string, unknown> = {
    type: "image",
    title: record.image.title,
    artifact_id: record.image.artifactId,
    version_id: record.image.versionId,
    name: record.image.name,
    version_seq: record.image.versionSeq,
  };
  if (record.image.source.startsWith("data:image/")) {
    artifact.data = record.image.source;
  } else if (/^https?:\/\//i.test(record.image.source)) {
    artifact.src = record.image.source;
  } else {
    artifact.file = record.image.source;
  }
  return artifact;
}

function imageSourceForAnnotationRecord(record: ArtifactAnnotationRecord): string {
  if (record.image.source.startsWith("data:image/") || /^https?:\/\//i.test(record.image.source)) return record.image.source;
  return artifactPageUrl(record.image.source);
}

// The full-size view opened on click. Images render as a contained picture;
// everything else (PDF, HTML, text) is shown in an iframe pointed at the backend's
// /artifact-page route — the same route open_artifact artifacts use. PDFs render in the
// browser's native viewer; the /artifact-page route serves them inline (no attachment
// disposition), so the iframe displays rather than downloads them.
function AttachmentLightbox({
  attachment,
  annotations,
  onAnnotationsChange,
  onClose,
}: {
  attachment: MessageAttachment;
  annotations?: ArtifactImageAnnotation[];
  onAnnotationsChange?: (annotations: ArtifactImageAnnotation[]) => void;
  onClose: () => void;
}) {
  const t = useTranslations("AttachmentChips");
  const url = artifactPageUrl(attachment.path);
  const image = isImageAttachment(attachment);
  const pdf = isPdfAttachment(attachment);
  // Reuse the exact artifact renderer used for tool image artifacts, so an
  // uploaded image is annotated with the same UI. `file` routes through the same
  // /artifact-page source as tool artifacts; the derived identity keys the control.
  const imageArtifact = { type: "image", file: attachment.path, title: attachment.filename, artifact_id: attachment.path };
  const imageIdentity = image ? imageIdentityForArtifact(imageArtifact) : null;
  const annotationCount = annotations?.length ?? 0;
  const annotationsControl = imageIdentity
    ? {
        image: imageIdentity,
        annotations: annotations ?? [],
        onChange: onAnnotationsChange ?? (() => {}),
        readOnly: !onAnnotationsChange,
      }
    : undefined;
  return (
    <Dialog.Root open onOpenChange={(event) => { if (!event.open) onClose(); }} placement="center" size="cover">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content maxW="min(1100px, 92vw)" maxH="90vh" overflow="hidden">
            <Dialog.Header py={2} px={2} display="flex" alignItems="center" gap={2} position="relative">
              <Dialog.Title fontSize="sm" truncate>{attachment.filename}</Dialog.Title>
              {image && !onAnnotationsChange && annotationCount > 0 ? (
                <Flex
                  align="center"
                  gap={1.5}
                  px={2}
                  py={1}
                  borderRadius="full"
                  color="fg.muted"
                  fontSize="sm"
                  fontWeight="medium"
                  flexShrink={0}
                  pointerEvents="none"
                >
                  <LuMousePointerClick size={13} />
                  {t("annotationStatus", { count: annotationCount })}
                </Flex>
              ) : null}
              {image && onAnnotationsChange ? (
                <Flex
                  align="center"
                  gap={1.5}
                  px={2}
                  py={1}
                  borderRadius="full"
                  color="fg.muted"
                  fontSize="sm"
                  fontWeight="medium"
                  flexShrink={0}
                  pointerEvents="none"
                >
                  <LuMousePointerClick size={13} />
                  {t("clickToAnnotate")}
                </Flex>
              ) : null}
              <Flex align="center" gap={2} ml="auto">
                <Link href={url} target="_blank" rel="noreferrer" color="fg.muted" _hover={{ color: "fg" }} title={t("openInNewTab")}>
                  <LuExternalLink size={14} />
                </Link>
                <Dialog.CloseTrigger position="static" />
              </Flex>
            </Dialog.Header>
            <Dialog.Body p={0} display="flex" alignItems="center" justifyContent="center" bg="bg.subtle" minH="60vh">
              {image ? (
                <Box w="100%" h="100%" display="flex" flexDirection="column">
                  <ArtifactView artifact={imageArtifact} showHeader={false} fillContainer annotationsControl={annotationsControl} />
                </Box>
              ) : pdf ? (
                <Box w="100%" h="100%">
                  <PdfDocumentView url={url} />
                </Box>
              ) : (
                <iframe
                  src={url}
                  title={attachment.filename}
                  style={{ width: "100%", height: "100%", border: "none", background: "white" }}
                />
              )}
            </Dialog.Body>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

function ArtifactAnnotationLightbox({ record, onClose }: { record: ArtifactAnnotationRecord; onClose: () => void }) {
  const t = useTranslations("AttachmentChips");
  const imageArtifact = imageArtifactForAnnotationRecord(record);
  const imageIdentity = imageIdentityForArtifact(imageArtifact) ?? record.image;
  const link = record.image.source.startsWith("data:image/") ? "" : imageSourceForAnnotationRecord(record);
  return (
    <Dialog.Root open onOpenChange={(event) => { if (!event.open) onClose(); }} placement="center" size="cover">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content maxW="min(1100px, 92vw)" maxH="90vh" overflow="hidden">
            <Dialog.Header py={2} px={2} display="flex" alignItems="center" gap={2} position="relative">
              <Dialog.Title fontSize="sm" truncate>{annotationRecordLabel(record)}</Dialog.Title>
              <Flex align="center" gap={1.5} px={2} py={1} borderRadius="full" color="fg.muted" fontSize="sm" fontWeight="medium" flexShrink={0} pointerEvents="none">
                <LuMousePointerClick size={13} />
                {t("annotationStatus", { count: record.annotations.length })}
              </Flex>
              <Flex align="center" gap={2} ml="auto">
                {link ? (
                  <Link href={link} target="_blank" rel="noreferrer" color="fg.muted" _hover={{ color: "fg" }} title={t("openInNewTab")}>
                    <LuExternalLink size={14} />
                  </Link>
                ) : null}
                <Dialog.CloseTrigger position="static" />
              </Flex>
            </Dialog.Header>
            <Dialog.Body p={0} display="flex" alignItems="center" justifyContent="center" bg="bg.subtle" minH="60vh">
              <Box w="100%" h="100%" display="flex" flexDirection="column">
                <ArtifactView
                  artifact={imageArtifact}
                  showHeader={false}
                  fillContainer
                  annotationsControl={{
                    image: imageIdentity,
                    annotations: record.annotations,
                    onChange: () => {},
                    readOnly: true,
                  }}
                />
              </Box>
            </Dialog.Body>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

function ArtifactAnnotationChip({ record }: { record: ArtifactAnnotationRecord }) {
  const t = useTranslations("AttachmentChips");
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const versionNumber = record.image.versionSeq;
  // The same key/value hover card the artifacts-panel version node uses, so an annotated
  // image in the composer/transcript reads exactly like a version there.
  const tooltip = (
    <Box whiteSpace="nowrap">
      <Text fontWeight="semibold" mb={1} color="fg" maxW="280px" truncate>{annotationRecordLabel(record)}</Text>
      <Flex direction="column" gap={0.5}>
        {versionNumber > 0 && <InlineField label={t("fieldVersion")}><Text>{versionNumber}</Text></InlineField>}
        <InlineField label={t("fieldAnnotations")}><Text>{record.annotations.length}</Text></InlineField>
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
            filename={annotationRecordLabel(record)}
            onClick={() => setLightboxOpen(true)}
            // The version this feedback was drawn on, badged next to the file name — the
            // same numbered badge (and placement) the artifacts panel uses by its title.
            badge={versionNumber > 0 ? <VersionBadge number={versionNumber} active /> : undefined}
            thumbnail={
              <Image
                src={imageSourceForAnnotationRecord(record)}
                alt={annotationRecordLabel(record)}
                w="100%"
                h="100%"
                objectFit="cover"
                objectPosition="top"
              />
            }
          />
        </Box>
      </Tooltip>
      {lightboxOpen ? <ArtifactAnnotationLightbox record={record} onClose={() => setLightboxOpen(false)} /> : null}
    </>
  );
}

export function ArtifactAnnotationChips({ records }: { records: ArtifactAnnotationRecord[] }) {
  const visibleRecords = records.filter((record) => record.annotations.length > 0);
  if (visibleRecords.length === 0) return null;
  return (
    <Flex gap={2} flexWrap="wrap" justify="flex-end">
      {visibleRecords.map((record) => (
        <ArtifactAnnotationChip key={record.image.key} record={record} />
      ))}
    </Flex>
  );
}

// One attachment chip — the single shared unit rendered BOTH above a user message
// (read-only) and in the composer's pending strip (removable). A thumbnail (images)
// or file icon plus the name/size; hover pops the mini thumbnail, click opens the
// lightbox. Passing `onRemove` adds the composer's remove button (its click never
// opens the lightbox).
export function AttachmentChip({
  attachment,
  onRemove,
  annotations,
  onAnnotationsChange,
}: {
  attachment: MessageAttachment;
  onRemove?: () => void;
  // Image annotations to display; passing `onAnnotationsChange` makes them editable in
  // the lightbox (composer), omitting it shows them read-only (sent messages).
  annotations?: ArtifactImageAnnotation[];
  onAnnotationsChange?: (annotations: ArtifactImageAnnotation[]) => void;
}) {
  const t = useTranslations("AttachmentChips");
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const image = isImageAttachment(attachment);
  const pdf = isPdfAttachment(attachment);
  const { icon: Icon, iconColor } = iconForFilePath(attachment.filename);
  const thumbnail = image ? (
    <Image src={artifactPageUrl(attachment.path)} alt={attachment.filename} w="100%" h="100%" objectFit="cover" objectPosition="top" />
  ) : pdf ? (
    <PdfThumbnail url={artifactPageUrl(attachment.path)} width={192} />
  ) : (
    <Box color={iconColor} display="flex" alignItems="center" justifyContent="center">
      <Icon size={40} />
    </Box>
  );
  const annotationCount = (annotations ?? attachment.annotations)?.length ?? 0;
  // The same key/value hover card the annotated-image chips (and the git-status indicator)
  // use — carrying whatever metadata the attachment has.
  const tooltip = (
    <Box whiteSpace="nowrap">
      <Text fontWeight="semibold" mb={1} color="fg" maxW={80} truncate>{attachment.filename}</Text>
      <Flex direction="column" gap={0.5}>
        {attachment.mimeType && <InlineField label={t("fieldType")}><Text>{attachment.mimeType}</Text></InlineField>}
        {attachment.size > 0 && <InlineField label={t("fieldSize")}><Text>{formatFileSize(attachment.size)}</Text></InlineField>}
        {annotationCount > 0 && <InlineField label={t("fieldAnnotations")}><Text>{annotationCount}</Text></InlineField>}
        {attachment.path && <InlineField label={t("fieldPath")}><Text truncate maxW={80}>{attachment.path}</Text></InlineField>}
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
        <AttachmentLightbox
          attachment={attachment}
          annotations={annotations ?? attachment.annotations}
          onAnnotationsChange={onAnnotationsChange}
          onClose={() => setLightboxOpen(false)}
        />
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
