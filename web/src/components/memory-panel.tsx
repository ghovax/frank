"use client";

// What a session remembers: the findings its work established, and the instructions it was given.

import { Badge, Box, Button, Flex, IconButton, Text, VStack } from "@chakra-ui/react";
import { Fragment, memo, useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  LuArchive, LuBookMarked, LuCircleDashed, LuCrosshair, LuDot, LuFile, LuGitBranch, LuGitMerge,
  LuCompass, LuFlag, LuHistory, LuInfo, LuLock, LuPencil, LuRefreshCw, LuTriangleAlert,
} from "react-icons/lu";
import { PanelBody, PanelCard, PanelEmptyState, PanelHeader } from "@/components/ui/panel";
import { RelativeTime } from "@/components/ui/relative-time";
import { fetchSessionRecord, type RecordEntry } from "@/lib/api";
import { swallowed } from "@/lib/swallowed";
import { InlineMarkdown } from "./markdown-content";

// A live entry and the versions it grew out of, newest first, so the reader sees one item rather than a pile.
interface Revised {
  entry: RecordEntry;
  earlier: RecordEntry[];
}

/** The record is append-only, so a revision is a new entry naming the old one; only the unreplaced ones are current. */
function current(all: RecordEntry[]): Revised[] {
  const byId = new Map(all.map((entry) => [entry.id, entry]));
  const replaced = new Set(all.flatMap((entry) => entry.supersedes ?? []));
  // Walk each chain back through what it replaced, guarding against a cycle the store cannot rule out.
  const history = (entry: RecordEntry): RecordEntry[] => {
    const seen = new Set<string>([entry.id]);
    const earlier: RecordEntry[] = [];
    let pending = [...(entry.supersedes ?? [])];
    while (pending.length > 0) {
      const id = pending.shift() as string;
      if (seen.has(id)) continue;
      seen.add(id);
      const older = byId.get(id);
      if (!older) continue;
      earlier.push(older);
      pending = pending.concat(older.supersedes ?? []);
    }
    return earlier.sort((one, other) =>
      (other.written_at ?? "").localeCompare(one.written_at ?? ""),
    );
  };
  return (
    all
      .filter((entry) => !replaced.has(entry.id))
      .map((entry) => ({ entry, earlier: history(entry) }))
      // Most recent first: a record spanning months is read from what it has just learned, not from its beginning.
      .sort((one, other) =>
        (other.entry.written_at ?? "").localeCompare(one.entry.written_at ?? ""),
      )
  );
}

/** Whether a poll brought anything new: the record is append-only, so identity and count settle it. */
function sameEntries(held: Revised[], found: Revised[]): boolean {
  return (
    held.length === found.length &&
    held.every((one, index) => one.entry.id === found[index]?.entry.id)
  );
}

// What a revision did to what it replaced, in its own colour and mark, since an entry whose ancestor was wrong reads differently from one merely sharpened.
const REVISION_MARK: Record<string, { icon: typeof LuPencil; tone: string }> = {
  correction: { icon: LuPencil, tone: "orange" },
  refinement: { icon: LuCrosshair, tone: "blue" },
  merge: { icon: LuGitMerge, tone: "purple" },
  retraction: { icon: LuArchive, tone: "gray" },
};

// What kind of thing an entry is, marked so a list is read by shape before it is read word by word.
const ENTRY_MARK: Record<string, { icon: typeof LuInfo; tone: string }> = {
  fact: { icon: LuInfo, tone: "blue" },
  decision: { icon: LuGitBranch, tone: "purple" },
  constraint: { icon: LuLock, tone: "orange" },
  failure: { icon: LuTriangleAlert, tone: "red" },
  artifact: { icon: LuFile, tone: "gray" },
  open: { icon: LuCircleDashed, tone: "yellow" },
  requirement: { icon: LuFlag, tone: "blue" },
  preference: { icon: LuCompass, tone: "cyan" },
};

// An entry that no longer governs the work, which is the one thing that moves it down the list.
function retired(entry: RecordEntry): boolean {
  return entry.revision === "retraction" || entry.still_binding === false;
}

// How sure the record is of an entry, which the reader needs before acting on it.
const STANDING_TONE: Record<string, string> = {
  verified: "green",
  reported: "gray",
  inferred: "orange",
};

interface EntryLabels {
  category: Record<string, string>;
  kind: Record<string, string>;
  standing: Record<string, string>;
  lifted: string;
  revisions: (count: number) => string;
  revision: Record<string, string>;
}

// The separator between the qualifiers under an entry, drawn rather than typed so it sits on the line.
function Dot() {
  return <LuDot size={16} style={{ flexShrink: 0, opacity: 0.7 }} />;
}

// A qualifier reads as a word on the line, not as a chip: the colour carries the meaning, so a background only adds noise.
function Qualifier({ children, tone, mark: Mark }: { children: string; tone?: string; mark?: typeof LuInfo }) {
  return (
    <Badge
      size="sm"
      variant="plain"
      px={0}
      minH={0}
      gap={1}
      colorPalette={tone}
      css={{ "& svg": { width: "13px", height: "13px" } }}
    >
      {Mark ? <Mark /> : null}
      {children}
    </Badge>
  );
}

// Every field is markdown the model may have marked up, truncated to one line because a list is scanned rather than read, with the whole text on the title.
function Body({ entry, muted }: { entry: RecordEntry; muted?: boolean }) {
  const claim = entry.claim ?? entry.summary ?? "";
  const cited = entry.evidence || entry.occasion || "";
  return (
    <>
      <Text
        textStyle="xs"
        fontWeight="medium"
        color={muted ? "fg.muted" : undefined}
        truncate
        title={claim}
      >
        <InlineMarkdown content={claim} />
      </Text>
      {entry.detail ? (
        <Text textStyle="2xs" color="fg.muted" mt={1} truncate title={entry.detail}>
          <InlineMarkdown content={entry.detail} />
        </Text>
      ) : null}
      {cited ? (
        // Prose as often as a path, so it is set as prose and the model's own backticks mark what is literal.
        <Text textStyle="2xs" color="fg.subtle" mt={1} truncate title={cited}>
          <InlineMarkdown content={cited} />
        </Text>
      ) : null}
    </>
  );
}

const Entry = memo(function Entry({ revised, labels }: { revised: Revised; labels: EntryLabels }) {
  const { entry, earlier } = revised;
  const [showHistory, setShowHistory] = useState(false);
  const label = entry.category
    ? labels.category[entry.category]
    : entry.kind
      ? labels.kind[entry.kind]
      : "";
  const standing = entry.standing ? labels.standing[entry.standing] : "";
  const qualifiers = [
    label ? (
      <Qualifier
        key="label"
        mark={ENTRY_MARK[entry.category ?? entry.kind ?? ""]?.icon}
        tone={ENTRY_MARK[entry.category ?? entry.kind ?? ""]?.tone}
      >
        {label}
      </Qualifier>
    ) : null,
    standing ? (
      <Qualifier key="standing" tone={STANDING_TONE[entry.standing ?? ""] ?? "gray"}>
        {standing}
      </Qualifier>
    ) : null,
    entry.still_binding === false ? <Qualifier key="lifted">{labels.lifted}</Qualifier> : null,
    entry.written_at ? (
      <RelativeTime key="learned" date={entry.written_at} textStyle="xs" color="fg.subtle" />
    ) : null,
    // Only the current version is shown; how it got here is one click away rather than another entry in the list.
    earlier.length > 0 ? (
      <Button
        key="revisions"
        variant="plain"
        px={0}
        h="auto"
        minW={0}
        gap={1}
        textStyle="xs"
        fontWeight="medium"
        css={{ "& svg": { width: "13px", height: "13px" } }}
        colorPalette={REVISION_MARK[entry.revision ?? ""]?.tone ?? "blue"}
        aria-expanded={showHistory}
        onClick={() => setShowHistory((shown) => !shown)}
      >
        {(() => {
          const Mark = REVISION_MARK[entry.revision ?? ""]?.icon ?? LuHistory;
          return <Mark />;
        })()}
        {entry.revision ? labels.revision[entry.revision] : labels.revisions(earlier.length)}
      </Button>
    ) : null,
  ].filter(Boolean);
  return (
    <Box borderWidth="1px" borderColor="border" borderRadius="md" px={2} py={1} bg="bg.subtle" opacity={retired(entry) ? 0.55 : 1}>
      <Body entry={entry} />
      <Flex align="center" gap={0.5} mt={0.5} wrap="wrap" color="fg.muted">
        {qualifiers.map((qualifier, index) => (
          <Fragment key={index}>
            {index > 0 ? <Dot /> : null}
            {qualifier}
          </Fragment>
        ))}
      </Flex>
      {showHistory ? (
        <VStack
          align="stretch"
          gap={1.5}
          mt={2}
          mb={2}
          ps={2}
          borderLeftWidth="2px"
          borderColor="border.emphasized"
        >
          {earlier.map((older) => (
            <Box key={older.id} opacity={0.65}>
              <Body entry={older} muted />
              {older.written_at ? (
                <RelativeTime date={older.written_at} display="block" mt={1} textStyle="2xs" color="fg.subtle" />
              ) : null}
            </Box>
          ))}
        </VStack>
      ) : null}
    </Box>
  );
});

export function MemoryPanel({
  sessionId,
  onClose,
}: {
  sessionId: string | null;
  onClose?: () => void;
}) {
  const translation = useTranslations("MemoryPanel");
  const [findings, setFindings] = useState<Revised[]>([]);
  const [instructions, setInstructions] = useState<Revised[]>([]);
  // Set only once a read has come back, so an empty panel says "nothing yet" rather than "nothing".
  const [read, setRead] = useState(false);
  // Bumped by the refresh control, which is how the record is re-read now that nothing polls.
  const [refreshCount, setRefreshCount] = useState(0);

  // Declared inside the effect, as the other panels do, so nothing is called synchronously from its body.
  useEffect(() => {
    let cancelled = false;
    async function readRecord() {
      // A conversation with no session yet has an empty record rather than an unfinished read.
      if (!sessionId) {
        setRead(true);
        return;
      }
      try {
        // The whole chain, not the live view: superseded versions are what the history under an entry is made of.
        const [established, asked] = await Promise.all([
          fetchSessionRecord(sessionId, "observations", undefined, false),
          fetchSessionRecord(sessionId, "directives", undefined, false),
        ]);
        if (cancelled) return;
        const [live, given] = [current(established), current(asked)];
        // Same entries, new objects: replacing them would re-render and re-parse every one of them.
        setFindings((held) => (sameEntries(held, live) ? held : live));
        setInstructions((held) => (sameEntries(held, given) ? held : given));
        setRead(true);
      } catch (caught) {
        if (!cancelled)
          swallowed({ component: "memory-panel", operation: "read the session's record" }, caught);
      }
    }
    void readRecord();
    return () => {
      cancelled = true;
    };
    // Read on open and on request, not on a timer: the record only grows when a turn settles.
  }, [sessionId, refreshCount]);

  const countRevisions = useCallback(
    (count: number) => translation("revisions", { count }),
    [translation],
  );
  const labels: EntryLabels = useMemo(
    () => ({
      category: {
        fact: translation("category.fact"),
        decision: translation("category.decision"),
        constraint: translation("category.constraint"),
        failure: translation("category.failure"),
        artifact: translation("category.artifact"),
        open: translation("category.open"),
      },
      kind: {
        requirement: translation("kind.requirement"),
        preference: translation("kind.preference"),
      },
      standing: {
        verified: translation("standing.verified"),
        reported: translation("standing.reported"),
        inferred: translation("standing.inferred"),
      },
      lifted: translation("lifted"),
      revisions: countRevisions,
      revision: {
        correction: translation("revision.correction"),
        refinement: translation("revision.refinement"),
        merge: translation("revision.merge"),
        retraction: translation("revision.retraction"),
      },
    }),
    [translation, countRevisions],
  );
  // What was asked for comes first: an instruction governs the findings under it.
  const sections: Array<[string, Revised[]]> = [
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
            {sections.map(([heading, entries]) =>
              entries.length === 0 ? null : (
                <VStack key={heading} align="stretch" gap={2}>
                  <Text textStyle="sectionLabel">{heading}</Text>
                  {entries.map((revised) => (
                    <Entry key={revised.entry.id} revised={revised} labels={labels} />
                  ))}
                </VStack>
              ),
            )}
          </VStack>
        )}
      </PanelBody>
    </PanelCard>
  );
}
