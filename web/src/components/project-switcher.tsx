"use client";

import { Button, Menu, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { LuChevronDown, LuFolderOpen, LuPlus, LuSettings } from "react-icons/lu";
import { listProjects, listSshHosts, subscribeEvents, type Project, type SshHost } from "@/lib/api";
import { DropdownMenu, MenuOption, MenuSeparator } from "@/components/ui/menu";
import { NewProjectDialog } from "./new-project-dialog";
import { ManageProjectsDialog } from "./manage-projects-dialog";

// The compact project switcher that anchors the sessions sidebar: the current project's name
// with a chevron, opening a dropdown of every project (switch on click), plus "New project…"
// and "Manage projects…". Replaces the old landing grid — picking/managing projects now
// happens here, in place, without leaving the chat.
export function ProjectSwitcher({
  currentProjectId,
  onSwitchProject,
}: {
  currentProjectId: string;
  onSwitchProject: (projectId: string) => void;
}) {
  const t = useTranslations("ProjectSwitcher");
  const [projects, setProjects] = useState<Project[]>([]);
  const [hosts, setHosts] = useState<SshHost[]>([]);
  const [newOpen, setNewOpen] = useState(false);
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
      <DropdownMenu
        minW="220px"
        positioning={{ placement: "bottom-start" }}
        trigger={
          <Button
            variant="ghost"
            w="full"
            minW={0}
            justifyContent="flex-start"
            gap={2}
            fontWeight="medium"
            _focusVisible={{ outline: "none", boxShadow: "none", bg: "bg.subtle" }}
          >
            <LuFolderOpen size={14} />
            <Text flex={1} minW={0} truncate textAlign="left" fontSize="sm">
              {current?.name ?? t("selectProject")}
            </Text>
            <LuChevronDown size={13} />
          </Button>
        }
      >
        <Menu.ItemGroup>
          <Menu.ItemGroupLabel fontSize="xs" color="fg.muted">{t("projects")}</Menu.ItemGroupLabel>
          {projects.map((project) => (
            <MenuOption
              key={project.id}
              value={project.id}
              icon={<LuFolderOpen size={13} />}
              selected={project.id === currentProjectId}
              onClick={() => onSwitchProject(project.id)}
            >
              <Text fontWeight="medium" truncate>{project.name}</Text>
            </MenuOption>
          ))}
        </Menu.ItemGroup>
        <MenuSeparator />
        <MenuOption value="__new__" icon={<LuPlus size={13} />} onClick={() => setNewOpen(true)}>
          <Text fontWeight="medium">{t("newProject")}</Text>
        </MenuOption>
        <MenuOption value="__manage__" icon={<LuSettings size={13} />} onClick={() => setManageOpen(true)}>
          <Text fontWeight="medium">{t("manageProjects")}</Text>
        </MenuOption>
      </DropdownMenu>

      {newOpen && (
        <NewProjectDialog
          hosts={hosts}
          open
          onOpenChange={setNewOpen}
          onCreated={(project) => { setNewOpen(false); refresh(); onSwitchProject(project.id); }}
        />
      )}
      <ManageProjectsDialog
        open={manageOpen}
        onOpenChange={setManageOpen}
        projects={projects}
        hosts={hosts}
        currentProjectId={currentProjectId}
        onSwitchProject={onSwitchProject}
        onProjectsChanged={refresh}
      />
    </>
  );
}
