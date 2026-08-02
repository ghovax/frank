"use client";

/**
 * The other Franks this one knows how to reach.
 *
 * The desktop's half of what a phone shows as its list of machines, so that "which Frank" is a
 * question with the same answer and the same shape on both. It is the same set, described by the
 * same `frank://pair#…` link, and kept in the daemon's own database rather than in this browser —
 * because it is a fact about the machine, not about the window looking at it.
 *
 * Opening one is a *navigation*, not a change of API base, and that is not a shortcut. A page
 * cannot script another origin: `frank reach` answers no `access-control-allow-origin`, on
 * purpose, because letting arbitrary pages talk to a listener holding full control of a machine
 * is the thing it exists to prevent. So this hands the browser an address and that machine serves
 * its own interface, first party to itself, exactly as it does for a phone.
 */

import { Box, Button, Flex, IconButton, Input, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import { LuArrowUpRight, LuTrash2 } from "react-icons/lu";

import { toaster } from "@/components/ui/toaster";
import {
  addMachine, forgetMachine, listMachines, machineAddress, subscribeEvents, type Machine,
} from "@/lib/api";
import { errorMessage } from "@/lib/errors";
import { useOrigin } from "@/lib/pointer";
import { swallowed } from "@/lib/swallowed";

export function MachinesPanel() {
  const translation = useTranslations("ConnectionSettings");
  const [machines, setMachines] = useState<Machine[]>([]);
  const [link, setLink] = useState("");
  const [saving, setSaving] = useState(false);
  /**
   * Which of these is serving this page, if any.
   *
   * The list knew every machine except the one you were looking at, and offered to connect you
   * to it — a button whose whole effect is to reload the page you are already on. The answer was
   * in the address bar all along: this page is served *by* a machine, so its origin is that
   * machine's endpoint, and the row that matches is the one you are in.
   */
  const origin = useOrigin();

  const refresh = useCallback(() => {
    listMachines()
      .then(setMachines)
      .catch((caught) => swallowed({ component: "machines-panel", operation: "list the machines" }, caught));
  }, []);

  useEffect(() => {
    refresh();
    // Another window adding a machine is a change to the same set, and this panel is exactly
    // where somebody would be looking when it happens.
    return subscribeEvents((event) => {
      if (event.type === "machines_changed") refresh();
    });
  }, [refresh]);

  async function save() {
    const trimmed = link.trim();
    if (!trimmed || saving) return;
    setSaving(true);
    try {
      await addMachine(trimmed);
      setLink("");
      refresh();
    } catch (caught) {
      toaster.create({ type: "error", title: translation("couldNotConnect"), description: errorMessage(caught), closable: true });
    } finally {
      setSaving(false);
    }
  }

  async function open(machine: Machine) {
    try {
      // The address is fetched now rather than held since the list rendered: it is the one thing
      // here that carries a token, and it should exist for as long as it takes to follow it.
      window.location.assign(await machineAddress(machine.id));
    } catch (caught) {
      toaster.create({ type: "error", title: translation("couldNotReach", { name: machine.name }), description: errorMessage(caught), closable: true });
    }
  }

  return (
    <Flex direction="column" gap={4} w="100%">
      <Flex direction="column" gap={2}>
        <Text textStyle="sectionLabel" color="fg.muted">{translation("savedConnections")}</Text>
        {machines.length === 0 ? (
          <Box borderWidth="1px" borderColor="border" borderRadius="md" px={3} py={4}>
            <Text fontSize="sm" color="fg.muted">{translation("noSavedConnections")}</Text>
            <Text fontSize="xs" color="fg.subtle" mt={1}>{translation("noSavedConnectionsHint")}</Text>
          </Box>
        ) : (
          <Flex direction="column" gap={2}>
            {machines.map((machine) => (
              <Flex
                key={machine.id}
                align="center"
                gap={3}
                px={3}
                py={2}
                borderWidth="1px"
                borderColor="border"
                borderRadius="md"
                bg="bg.panel"
              >
                <Box flex={1} minW={0}>
                  <Text fontSize="sm" fontWeight="medium" truncate>{machine.name}</Text>
                  {/* The address, because it is what tells two similarly named machines apart. */}
                  <Text fontSize="xs" color="fg.subtle" truncate>{machine.endpoint.replace(/^https:\/\//, "")}</Text>
                </Box>
                {machine.endpoint === origin ? (
                  // Not a disabled button. There is nothing to do here, and a greyed-out control
                  // invites somebody to work out why it will not work; a state does not.
                  <Flex align="center" gap={1.5} color="green.fg" flexShrink={0}>
                    <Box boxSize="1.5" borderRadius="full" bg="green.solid" />
                    <Text fontSize="xs">{translation("connected")}</Text>
                  </Flex>
                ) : (
                  <Button size="xs" variant="outline" onClick={() => void open(machine)}>
                    <LuArrowUpRight />
                    {translation("connect")}
                  </Button>
                )}
                <IconButton
                  size="xs"
                  variant="ghost"
                  colorPalette="red"
                  // Forgetting the machine you are currently using would throw away the token for
                  // the page you are reading, which is a thing to do deliberately from somewhere
                  // else rather than by accident from here.
                  disabled={machine.endpoint === origin}
                  aria-label={translation("deleteConnection", { url: machine.endpoint })}
                  onClick={() => {
                    void forgetMachine(machine.id).then(refresh).catch((caught) =>
                      swallowed({ component: "machines-panel", operation: "forget a machine" }, caught)
                    );
                  }}
                >
                  <LuTrash2 />
                </IconButton>
              </Flex>
            ))}
          </Flex>
        )}
      </Flex>

      <Flex direction="column" gap={2}>
        <Text textStyle="sectionLabel" color="fg.muted">{translation("pairingLink")}</Text>
        <Flex gap={2} align="center">
          <Input
            size="xs"
            flex={1}
            value={link}
            placeholder={translation("pairingLinkPlaceholder")}
            onChange={(event) => setLink(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void save();
            }}
          />
          <Button size="xs" variant="solid" colorPalette="blue" loading={saving} disabled={!link.trim()} onClick={() => void save()}>
            {translation("saveConnection")}
          </Button>
        </Flex>
        <Text fontSize="xs" color="fg.subtle">{translation("pairingLinkHelper")}</Text>
      </Flex>
    </Flex>
  );
}
