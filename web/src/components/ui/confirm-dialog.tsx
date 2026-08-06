"use client";

// The one confirmation dialog: a title, a body, and a Cancel plus a confirm that reddens when destructive.

import { Button, Dialog, Portal, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  children,
  confirmLabel,
  cancelLabel = "Cancel",
  confirmIcon,
  danger,
  maxW,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  // The body message.
  children: ReactNode;
  confirmLabel: ReactNode;
  cancelLabel?: ReactNode;
  confirmIcon?: ReactNode;
  // Destructive action — the confirm button is red instead of blue.
  danger?: boolean;
  maxW?: string;
  onConfirm: () => void;
}) {
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(event) => onOpenChange(event.open)}
      placement="center"
      role="alertdialog"
    >
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content maxW={maxW}>
            <Dialog.Header>
              <Dialog.Title textStyle="panelTitle">{title}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Text fontSize="xs" color="fg.muted">
                {children}
              </Text>
            </Dialog.Body>
            <Dialog.Footer gap={2}>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                {cancelLabel}
              </Button>
              <Button
                colorPalette={danger ? "red" : "blue"}
                variant="solid"
                onClick={() => {
                  onConfirm();
                  onOpenChange(false);
                }}
              >
                {confirmIcon}
                {confirmLabel}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
