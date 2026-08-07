"use client";

// What a session remembers: the findings its work established, and the instructions it was given.

import { Box, Flex, IconButton, Text, VStack } from "@chakra-ui/react";
import { memo, useEffect, useMemo, useState } from "react";
import { useFormatter, useTranslations } from "next-intl";
import { LuBookMarked, LuRefreshCw } from "react-icons/lu";
import { PanelBody, PanelCard, PanelEmptyState, PanelHeader } from "@/components/ui/panel";
import { fetchSessionRecord, type RecordEntry } from "@/lib/api";
import { swallowed } from "@/lib/swallowed";
import { InlineMarkdown } from "./markdown-content";

/** Whether a poll brought anything new: the record is append-only, so identity and count settle it. */
function sameEntries(current: RecordEntry[], found: RecordEntry[]): boolean {
  return current.length === found.length && current.every((entry, index) => entry.id === found[index]?.id);
}

// How sure the record is of an entry, which the reader needs before acting on it.
const STANDING_TONE: Record<string, string> = { verified: "green", reported: "gray", inferred: "orange" };

interface EntryLabels {
  category: Record<string, string>;
  kind: Record<string, string>;
  standing: Record<string, string>;
  lifted: string;
  revises: string;
}

// The separator between the qualifiers under an entry.
function Dot() {
  return <Dot />;
}

const Entry = memo(function Entry({ entry, labels, format }: {
  entry: RecordEntry;
  labels: EntryLabels;
  format: ReturnType<typeof useFormatter>;
}) {
  // Named by the catalogue rather than by the wire, which spells its values for a machine.
  const label = entry.category ? labels.category[entry.category] : entry.kind ? labels.kind[entry.kind] : "";
  const standing = entry.standing ? labels.standing[entry.standing] : "";
  // Absolute rather than relative: a record can span months, and "3 months ago" locates nothing.
  const written = entry.written_at ? new Date(entry.written_at) : null;
  const learned = written && !Number.isNaN(written.getTime()) ? written : null;
  return (
    <Box borderWidth="1px" borderColor="border" borderRadius="md" px={2} py={1.5} bg="bg.subtle">
      {/* The finding leads. What kind it is and how sure we are of it qualify it, so they sit under it. */}
      <Text textStyle="xs" fontWeight="medium">
        <InlineMarkdown content={entry.claim ?? entry.summary ?? ""} />
      </Text>
      {entry.detail ? (
        <Text textStyle="2xs" color="fg.muted" mt={1}>
          <InlineMarkdown content={entry.detail} />
        </Text>
      ) : null}
      {entry.evidence ? (
        <Text textStyle="2xs" color="fg.subtle" mt={1} fontFamily="var(--app-font-mono)" truncate>
          {entry.evidence}
        </Text>
      ) : null}
      <Flex align="center" gap={1.5} mt={1.5} color="fg.subtle" wrap="wrap">
        {label ? <Text textStyle="2xs" fontWeight="medium">{label}</Text> : null}
        {standing ? (
          <>
            <Dot />
            <Text textStyle="2xs" color={`${STANDING_TONE[entry.standing ?? ""] ?? "gray"}.fg`}>{standing}</Text>
          </>
        ) : null}
        {learned ? (
          <>
            <Dot />
            <Text textStyle="2xs">
              {format.dateTime(learned, { year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" })}
            </Text>
          </>
        ) : null}
        {entry.still_binding === false ? (
          <>
            <Dot />
            <Text textStyle="2xs">{labels.lifted}</Text>
          </>
        ) : null}
        {entry.supersedes?.length ? (
          <>
            <Dot />
            <Text textStyle="2xs" color="blue.fg">{labels.revises}</Text>
          </>
        ) : null}
      </Flex>
    </Box>
  );
});

export function MemoryPanel({ sessionId, onClose }: { sessionId: string; onClose?: () => void }) {
  const translation = useTranslations("MemoryPanel");
  const format = useFormatter();
  const [findings, setFindings] = useState<RecordEntry[]>([]);
  const [instructions, setInstructions] = useState<RecordEntry[]>([]);
  // Set only once a read has come back, so an empty panel says "nothing yet" rather than "nothing".
  const [read, setRead] = useState(false);
  // Bumped by the refresh control, which is how the record is re-read now that nothing polls.
  const [refreshCount, setRefreshCount] = useState(0);

  // Declared inside the effect, as the other panels do, so nothing is called synchronously from its body.
  useEffect(() => {
    let cancelled = false;
    async function readRecord() {
      if (!sessionId) return;
      try {
        const [established, asked] = await Promise.all([
          fetchSessionRecord(sessionId, "observations"),
          fetchSessionRecord(sessionId, "directives"),
        ]);
        if (cancelled) return;
        // Same entries, new objects: replacing them would re-render and re-parse every one of them.
        setFindings((current) => sameEntries(current, established) ? current : established);
        setInstructions((current) => sameEntries(current, asked) ? current : asked);
        setRead(true);
      } catch (caught) {
        if (!cancelled) swallowed({ component: "memory-panel", operation: "read the session's record" }, caught);
      }
    }
    void readRecord();
    return () => {
      cancelled = true;
    };
    // Read when the panel opens and when the conversation changes, and not on a timer: the record only
    // grows when a turn settles, and a heartbeat that re-reads it is work nobody asked for.
  }, [sessionId, refreshCount]);

  const labels: EntryLabels = useMemo(() => ({
    category: {
      fact: translation("category.fact"), decision: translation("category.decision"),
      constraint: translation("category.constraint"), failure: translation("category.failure"),
      artifact: translation("category.artifact"), open: translation("category.open"),
    },
    kind: {
      requirement: translation("kind.requirement"), correction: translation("kind.correction"),
      preference: translation("kind.preference"),
    },
    standing: {
      verified: translation("standing.verified"), reported: translation("standing.reported"),
      inferred: translation("standing.inferred"),
    },
    lifted: translation("lifted"),
    revises: translation("revises"),
  }), [translation]);
  // What was asked for comes first: an instruction governs the findings under it.
  const sections: Array<[string, RecordEntry[]]> = [
    [translation("instructions"), instructions],
    [translation("findings"), findings],
  ];

  return (
    <PanelCard>
      <PanelHeader
        icon={<LuBookMarked size={14} />}
        title={translation("title")}
        onClose={onClose}
        closeLabel={translation("collapsePanel")}
      >
        <IconButton
          aria-label={translation("refresh")}
          title={translation("refresh")}
          variant="ghost"
          onClick={() => setRefreshCount((count) => count + 1)}
        >
          <LuRefreshCw size={13} />
        </IconButton>
      </PanelHeader>
      <PanelBody pt={1}>
        {findings.length === 0 && instructions.length === 0 ? (
          <PanelEmptyState
            icon={<LuBookMarked />}
            title={translation(read ? "emptyTitle" : "loading")}
            description={translation("emptyDescription")}
          />
        ) : (
          <VStack align="stretch" gap={2.5}>
            {sections.map(([heading, entries]) => entries.length === 0 ? null : (
              <VStack key={heading} align="stretch" gap={1}>
                <Text textStyle="fieldLabel" color="fg.subtle">{heading}</Text>
                {entries.map((entry) => (
                  <Entry key={entry.id} entry={entry} labels={labels} format={format} />
                ))}
              </VStack>
            ))}
          </VStack>
        )}
      </PanelBody>
    </PanelCard>
  );
}
