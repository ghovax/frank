"use client";

import { Button, Flex, Text } from "@chakra-ui/react";
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
        px={2}
        justifyContent="flex-start"
        // A single-line title, so the project name sits on the same horizontal line as the
        // chat title and the side-panel titles (their strips all share TOP_BAR_HEIGHT). px + a
        // 14px leading slot match the New-session row, keeping the folder icon on the sidebar's
        // left grid. The description rides as a hover tooltip rather than a second line, which
        // would push the name off that shared line.
        gap={1.5}
        title={current?.description || undefined}
        onClick={() => setManageOpen(true)}
        _focusVisible={{ outline: "none", boxShadow: "none", bg: "bg.subtle" }}
      >
        <Flex w="14px" flexShrink={0} align="center" justify="center" color="fg.muted">
          <LuFolderOpen size={14} />
        </Flex>
        <Text flex={1} minW={0} truncate textAlign="left" fontSize="sm" fontWeight="medium">
          {current?.name ?? t("selectProject")}
        </Text>
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
