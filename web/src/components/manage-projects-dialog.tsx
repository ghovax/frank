"use client";

import { Box, Button, Dialog, Flex, IconButton, Portal, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { LuPlus, LuSettings, LuTrash2 } from "react-icons/lu";
import { deleteProject, type Project, type SshHost } from "@/lib/api";
import { LocationStatusDot } from "./location-status";
import { NewProjectDialog } from "./new-project-dialog";
import { toaster } from "./ui/toaster";

// The project manager: switch, create, delete, and open each project's settings — the
// operations that used to live on the (now-removed) Projects landing grid, folded into a
// modal opened from the sidebar's project button. A project's own settings (locations and
// the rest) live in the real Settings dialog, which the gear opens for that project.
export function ManageProjectsDialog({
  open,
  onOpenChange,
  projects,
  hosts,
  currentProjectId,
  onSwitchProject,
  onOpenProjectSettings,
  onProjectsChanged,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projects: Project[];
  hosts: SshHost[];
  currentProjectId: string;
  onSwitchProject: (projectId: string) => void;
  onOpenProjectSettings: (projectId: string) => void;
  onProjectsChanged: () => void;
}) {
  const t = useTranslations("ProjectsHome");
  const tc = useTranslations("Common");
  const [newOpen, setNewOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);

  async function confirmDelete() {
    if (!deleteTarget) return;
    try {
      await deleteProject(deleteTarget.id);
      onProjectsChanged();
    } catch (error) {
      toaster.create({ type: "error", title: t("deleteProject"), description: error instanceof Error ? error.message : "", closable: true });
    } finally {
      setDeleteTarget(null);
    }
  }

  return (
    <>
      <Dialog.Root open={open} onOpenChange={(event) => { if (!event.open) onOpenChange(false); }} placement="center">
        <Portal>
          <Dialog.Backdrop />
          <Dialog.Positioner>
            <Dialog.Content maxW="min(680px, 94vw)">
              <Dialog.Header>
                <Flex align="center" gap={2} w="full">
                  <Dialog.Title textStyle="panelTitle" flex={1} truncate>
                    {t("manageProjects")}
                  </Dialog.Title>
                  <Button colorPalette="blue" onClick={() => setNewOpen(true)}>
                    <LuPlus size={13} /> {t("newProject")}
                  </Button>
                </Flex>
              </Dialog.Header>
              <Dialog.Body maxH="70vh" overflowY="auto">
                  <Flex direction="column" gap={2}>
                    {projects.map((project) => (
                      <Flex
                        key={project.id}
                        align="center"
                        gap={3}
                        px={3}
                        py={3}
                        bg={project.id === currentProjectId ? "bg.subtle" : "bg.panel"}
                        borderWidth="1px"
                        borderColor={project.id === currentProjectId ? "border.emphasized" : "border"}
                        borderRadius="md"
                        cursor="pointer"
                        transition="background 0.12s, border-color 0.12s"
                        _hover={{ borderColor: "border.emphasized", bg: "bg.subtle" }}
                        // The whole row is the switch affordance — click it to make this the active
                        // project and close the manager; the gear/delete controls stop propagation.
                        onClick={() => { onSwitchProject(project.id); onOpenChange(false); }}
                      >
                        <Box flex={1} minW={0}>
                          <Text fontSize="sm" fontWeight="medium" truncate>{project.name}</Text>
                          {project.description && <Text fontSize="xs" color="fg.muted" truncate mt={0.5}>{project.description}</Text>}
                        </Box>
                        {(project.locations ?? []).length > 0 && (
                          <Flex align="center" gap={2.5} flexShrink={0} display={{ base: "none", sm: "flex" }}>
                            {(project.locations ?? []).map((location) => (
                              <Flex key={location.id} align="center" gap={1.5}>
                                <LocationStatusDot location={location} />
                                <Text fontSize="xs" color="fg.muted">{location.name}</Text>
                              </Flex>
                            ))}
                          </Flex>
                        )}
                        <Text fontSize="xs" color="fg.subtle" flexShrink={0} whiteSpace="nowrap">
                          {t("sessionCount", { count: project.session_count })}
                        </Text>
                        <IconButton aria-label={t("projectSettings")} variant="ghost" flexShrink={0} onClick={(event) => { event.stopPropagation(); onOpenChange(false); onOpenProjectSettings(project.id); }}>
                          <LuSettings size={13} />
                        </IconButton>
                        <IconButton
                          aria-label={t("deleteProject")}
                          variant="ghost"
                          colorPalette="red"
                          flexShrink={0}
                          disabled={projects.length <= 1}
                          onClick={(event) => { event.stopPropagation(); setDeleteTarget(project); }}
                        >
                          <LuTrash2 size={13} />
                        </IconButton>
                      </Flex>
                    ))}
                  </Flex>
              </Dialog.Body>
            </Dialog.Content>
          </Dialog.Positioner>
        </Portal>
      </Dialog.Root>

      {newOpen && (
        <NewProjectDialog hosts={hosts} open onOpenChange={setNewOpen} onCreated={() => { setNewOpen(false); onProjectsChanged(); }} />
      )}

      <Dialog.Root open={deleteTarget !== null} onOpenChange={(event) => { if (!event.open) setDeleteTarget(null); }} placement="center" role="alertdialog">
        <Portal>
          <Dialog.Backdrop />
          <Dialog.Positioner>
            <Dialog.Content maxW="min(440px, 92vw)">
              <Dialog.Header><Dialog.Title textStyle="panelTitle">{t("deleteProject")}</Dialog.Title></Dialog.Header>
              <Dialog.Body>
                <Text fontSize="sm" color="fg.muted">
                  {t.rich("deleteConfirm", {
                    name: deleteTarget?.name ?? "",
                    count: deleteTarget?.session_count ?? 0,
                    b: (chunks) => <Text as="span" fontWeight="semibold" color="fg">{chunks}</Text>,
                  })}
                </Text>
              </Dialog.Body>
              <Dialog.Footer>
                <Button variant="outline" onClick={() => setDeleteTarget(null)}>{tc("cancel")}</Button>
                <Button colorPalette="red" onClick={confirmDelete}>{tc("delete")}</Button>
              </Dialog.Footer>
            </Dialog.Content>
          </Dialog.Positioner>
        </Portal>
      </Dialog.Root>
    </>
  );
}
