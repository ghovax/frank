"use client";

import { Box, Button, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { LuFolderOpen } from "react-icons/lu";
import { listProjects, listSshHosts, subscribeEvents, type Project, type SshHost } from "@/lib/api";
import { ManageProjectsDialog } from "./manage-projects-dialog";

// The project anchor at the top of the sessions sidebar: the current project's name with its
// description beneath it. Clicking it opens the Manage Projects dialog directly — switching,
// creating, and editing projects all happen there, so there is no intermediate dropdown.
export function ProjectSwitcher({
  currentProjectId,
  onSwitchProject,
  onOpenProjectSettings,
}: {
  currentProjectId: string;
  onSwitchProject: (projectId: string) => void;
  onOpenProjectSettings: (projectId: string) => void;
}) {
  const t = useTranslations("ProjectSwitcher");
  const [projects, setProjects] = useState<Project[]>([]);
  const [hosts, setHosts] = useState<SshHost[]>([]);
  const [manageOpen, setManageOpen] = useState(false);

  const refresh = useCallback(() => {
    listProjects().then(setProjects).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    listSshHosts().then(setHosts).catch(() => {});
    return subscribeEvents((event) => {
      if (event.type === "projects_changed") refresh();
      if (event.type === "hosts_changed") listSshHosts().then(setHosts).catch(() => {});
    });
  }, [refresh]);

  const current = projects.find((project) => project.id === currentProjectId);

  return (
    <>
      <Button
        variant="ghost"
        w="full"
        minW={0}
        h="auto"
        minH={9}
        py={1}
        justifyContent="flex-start"
        gap={2}
        onClick={() => setManageOpen(true)}
        _focusVisible={{ outline: "none", boxShadow: "none", bg: "bg.subtle" }}
      >
        <Box color="fg.muted" display="flex" alignItems="center" flexShrink={0}>
          <LuFolderOpen size={14} />
        </Box>
        <Box flex={1} minW={0} textAlign="left">
          <Text truncate fontSize="sm" fontWeight="medium" lineHeight="1.3">
            {current?.name ?? t("selectProject")}
          </Text>
          {current?.description ? (
            <Text truncate fontSize="xs" fontWeight="normal" color="fg.muted" lineHeight="1.3">
              {current.description}
            </Text>
          ) : null}
        </Box>
      </Button>

      <ManageProjectsDialog
        open={manageOpen}
        onOpenChange={setManageOpen}
        projects={projects}
        hosts={hosts}
        currentProjectId={currentProjectId}
        onSwitchProject={onSwitchProject}
        onOpenProjectSettings={onOpenProjectSettings}
        onProjectsChanged={refresh}
      />
    </>
  );
}
